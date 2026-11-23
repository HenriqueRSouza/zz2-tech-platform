"""Brute-force protection: block an address that keeps presenting bad credentials.

The gap this closes. :class:`~zz2.api.middleware.rate_limit.RateLimitMiddleware`
keys its budget on a hash of the bearer token, so an attacker who sends a
*different* random token with every request gets a fresh budget every time — the
limiter never sees the same caller twice. Token guessing, a stolen-signing-key
probe and SAML-response forging were all unbounded.

This guard keys on the client address instead, and counts **distinct rejected
credentials**:

* *Rejected*, not expired — an expired token was once genuine (see
  :class:`~zz2.core.errors.CredentialExpired`), and a missing one is just a
  signed-out browser. Neither is counted.
* *Distinct* — a page issuing twelve parallel calls with the same bad token
  counts once, so one broken tab cannot lock out an office behind one NAT
  address. Twenty different bad tokens from one address in ten minutes is not a
  broken tab.

Past the threshold the address is blocked for ``ZZ2_AUTH_LOCKOUT_SECONDS``: every
request from it gets ``429 locked_out``. Administrators can list and release
blocks from ``/api/system/security/lockouts``.

In memory, like the rate limiter, and for the same reason: one process.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict

from zz2.core.logging import audit
from zz2.settings import get_settings

MAX_TRACKED_ADDRESSES = 20_000


def fingerprint(credential: str | bytes) -> str:
    """A short, non-reversible identifier for a credential. Never store the raw value."""
    data = credential.encode("utf-8") if isinstance(credential, str) else credential
    return hashlib.blake2b(data, digest_size=8).hexdigest()


class AuthLockoutGuard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, dict[str, float]] = OrderedDict()
        self._blocked: dict[str, float] = {}

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._blocked.clear()

    def retry_after(self, address: str) -> int | None:
        """Seconds until ``address`` is unblocked, or ``None`` if it is not blocked."""
        now = time.monotonic()
        with self._lock:
            until = self._blocked.get(address)
            if until is None:
                return None
            if until <= now:
                del self._blocked[address]
                return None
            return max(1, int(until - now))

    def record_failure(self, address: str, credential_fingerprint: str, *,
                       source: str) -> bool:
        """Count one rejected credential. Returns ``True`` if this blocks ``address``."""
        settings = get_settings()
        if not settings.auth_lockout_enabled or not address or address == "-":
            return False
        now = time.monotonic()
        with self._lock:
            seen = self._failures.get(address) or {}
            cutoff = now - settings.auth_lockout_window_seconds
            seen = {fp: at for fp, at in seen.items() if at > cutoff}
            seen[credential_fingerprint] = now
            self._failures[address] = seen
            self._failures.move_to_end(address)
            while len(self._failures) > MAX_TRACKED_ADDRESSES:
                self._failures.popitem(last=False)

            if len(seen) < settings.auth_lockout_threshold:
                return False
            self._blocked[address] = now + settings.auth_lockout_seconds
            self._failures.pop(address, None)

        audit("auth.lockout", client=address, source=source,
              failures=settings.auth_lockout_threshold,
              seconds=settings.auth_lockout_seconds)
        return True

    def blocked(self) -> list[dict[str, object]]:
        now = time.monotonic()
        with self._lock:
            live = sorted(((until, address) for address, until in self._blocked.items()
                           if until > now), reverse=True)
        return [{"address": address, "retry_after": int(until - now)}
                for until, address in live]

    def release(self, address: str) -> bool:
        with self._lock:
            self._failures.pop(address, None)
            return self._blocked.pop(address, None) is not None


lockout_guard = AuthLockoutGuard()
