"""Request rate limiting.

The monolith had one throttle: a failed-login lockout keyed on
``username|ip``. Everything else was unlimited. On a LAN that is fine — the
attacker population is the staff. Exposed on a VPS, an unauthenticated
``/api/config`` and an authenticated but unbounded ``/api/analytics`` (which
rebuilds a multi-megabyte view) are both trivially abusable.

A fixed-window counter, held in memory. Deliberately not Redis: this deployment is
a single VPS running one service, so a shared store would add an operational
dependency and a new failure mode to protect against a distributed attack that
this app's threat model does not include. The docstring on
:class:`RateLimitMiddleware` records what to change if that stops being true.

Two buckets, because reads and writes deserve different budgets. A dashboard
polling every 30 seconds across eight open tabs is legitimate and read-heavy; sixty
ticket writes a minute from one client is not.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from zz2.core.errors import RateLimited
from zz2.core.logging import audit
from zz2.settings import get_settings

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Never rate limited. A health check that can be throttled is a health check
#: that reports the service down under load.
_EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})

MAX_TRACKED_CLIENTS = 20_000
"""Cap on distinct keys held. Prevents the limiter itself from becoming the
memory-exhaustion vector — an attacker rotating source addresses would otherwise
grow the table without bound."""


class _FixedWindowCounter:
    """Counts events per key within a wall-clock window."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._counts: OrderedDict[str, tuple[int, float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Record a hit. Returns ``(allowed, seconds_until_reset)``."""
        now = time.monotonic()
        with self._lock:
            entry = self._counts.get(key)
            if entry is None or entry[1] <= now:
                self._counts[key] = (1, now + self._window)
                self._counts.move_to_end(key)
                self._evict_locked()
                return True, self._window

            count, expires_at = entry
            retry_after = max(1, int(expires_at - now))
            if count >= self._limit:
                return False, retry_after

            self._counts[key] = (count + 1, expires_at)
            self._counts.move_to_end(key)
            return True, retry_after

    def _evict_locked(self) -> None:
        while len(self._counts) > MAX_TRACKED_CLIENTS:
            self._counts.popitem(last=False)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client request caps.

    Keyed on the authenticated subject when available, falling back to client IP.
    Keying on identity first is what keeps a whole office behind one NAT address
    from sharing a single budget.

    Single-process only. Behind more than one worker each process enforces its own
    counters, so the effective limit is ``workers x limit``. If that becomes a
    problem the counter needs to move to Redis; the interface here would not
    change.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        super().__init__(app)
        settings = get_settings()
        self._enabled = settings.rate_limit_enabled
        self._reads = _FixedWindowCounter(
            settings.rate_limit_requests, settings.rate_limit_window_seconds
        )
        self._writes = _FixedWindowCounter(
            settings.rate_limit_write_requests, settings.rate_limit_window_seconds
        )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._enabled or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        counter = self._writes if request.method in _WRITE_METHODS else self._reads
        key = self._client_key(request)
        allowed, retry_after = counter.check(key)

        if not allowed:
            audit("ratelimit.blocked", client=key, method=request.method,
                  path=request.url.path)
            error = RateLimited(retry_after)
            return JSONResponse(
                error.to_payload(),
                status_code=error.status_code,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    @staticmethod
    def _client_key(request: Request) -> str:
        """Identify the caller.

        The bearer token's *hash prefix* stands in for the identity: the middleware
        runs before authentication, so the token has not been verified and must not
        be stored or logged. Hashing gives a stable key without holding the
        credential.
        """
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
            if token:
                return "t:" + str(hash(token) & 0xFFFFFFFF)
        return "ip:" + (getattr(request.state, "client_ip", None)
                        or (request.client.host if request.client else "-"))
