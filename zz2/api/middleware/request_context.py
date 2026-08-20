"""Request identity, access logging and body-size enforcement.

Three concerns that all need to run before the route and see the raw request, kept
in one middleware so the ordering between them is explicit rather than emergent.

The access log is new. ``ZZ2Handler.log_message`` was::

    def log_message(self, fmt, *args):
        pass  # Suppress request logs to keep things clean

On a LAN that is a tidiness choice. On a VPS it means no record of who requested
what — no way to investigate an incident, spot scanning, or prove that a request
was never served.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from zz2.core.errors import PayloadTooLarge
from zz2.core.logging import actor_var, logger, request_id_var
from zz2.settings import get_settings

#: Paths excluded from the access log. Health checks run every few seconds and
#: would otherwise be the overwhelming majority of log volume.
_QUIET_PATHS = frozenset({"/healthz", "/readyz"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, enforce the body cap, and log the outcome."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        super().__init__(app)
        settings = get_settings()
        self._max_body = settings.max_body_bytes
        self._behind_proxy = settings.behind_proxy

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request_token = request_id_var.set(request_id)
        actor_token = actor_var.set("-")
        request.state.request_id = request_id
        request.state.client_ip = self._client_ip(request)

        # Reject an oversized body from its declared length, before reading it.
        # The point is to never buffer it: the monolith's 32 MB cap was checked
        # the same way, but the limit itself let a single request allocate 32 MB
        # and a handful of concurrent ones exhaust a small VPS.
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_body:
                    return self._too_large(request_id)
            except ValueError:
                return JSONResponse(
                    {"error": "Invalid Content-Length header.", "code": "invalid_request"},
                    status_code=400,
                )

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request failed",
                extra={"method": request.method, "path": request.url.path,
                       "ip": request.state.client_ip, "duration_ms": round(duration_ms, 1)},
            )
            raise
        finally:
            request_id_var.reset(request_token)
            actor_var.reset(actor_token)

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id

        if request.url.path not in _QUIET_PATHS:
            # Query strings are not logged. They can carry customer names and
            # ticket identifiers, and logging them would recreate the leak that
            # accepting ``?token=`` caused in the first place.
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(duration_ms, 1),
                    "ip": request.state.client_ip,
                },
            )
        return response

    def _client_ip(self, request: Request) -> str:
        """Best available client address.

        ``X-Forwarded-For`` is trusted only when ``behind_proxy`` is set, and only
        its first entry. Trusting it unconditionally would let any client set
        their own apparent IP and walk straight through the rate limiter.
        """
        if self._behind_proxy:
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "-"

    @staticmethod
    def _too_large(request_id: str) -> JSONResponse:
        error = PayloadTooLarge()
        return JSONResponse(
            error.to_payload(),
            status_code=error.status_code,
            headers={"X-Request-ID": request_id},
        )
