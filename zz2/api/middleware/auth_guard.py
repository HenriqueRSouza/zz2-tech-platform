"""Applies :mod:`zz2.services.lockout` to every request.

Before the handler: a blocked address is answered ``429 locked_out`` without any
further work. After it: a ``401`` whose credential the authorization dependency
marked as *rejected* (``request.state.auth_failure``) is counted.

Runs inside :class:`RequestContextMiddleware` (it needs ``client_ip``) and
outside the rate limiter, so a blocked address does not also spend budget.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from zz2.core.errors import LockedOut
from zz2.services.lockout import fingerprint, lockout_guard
from zz2.settings import get_settings

_EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})


def client_address(request: Request) -> str:
    return getattr(request.state, "client_ip", None) or (
        request.client.host if request.client else "-")


class AuthGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not get_settings().auth_lockout_enabled or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        address = client_address(request)
        retry_after = lockout_guard.retry_after(address)
        if retry_after is not None:
            error = LockedOut(retry_after)
            return JSONResponse(error.to_payload(), status_code=error.status_code,
                                headers={"Retry-After": str(retry_after)})

        response = await call_next(request)

        if (response.status_code == 401
                and getattr(request.state, "auth_failure", None) == "rejected"):
            authorization = request.headers.get("Authorization", "")
            credential = authorization[7:].strip() if len(authorization) > 7 else authorization
            lockout_guard.record_failure(address, fingerprint(credential), source="bearer")
        return response
