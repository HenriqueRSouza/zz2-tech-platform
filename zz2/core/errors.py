"""Application exception hierarchy and the HTTP mapping for it.

The monolith answered every unexpected failure with
``{"error": "Server error: %s" % e}``. That echoed exception text — absolute
filesystem paths, ``KeyError`` field names, database messages — straight to the
client, which on a LAN is untidy and on a public VPS is reconnaissance.

Here the two audiences are separated. The client gets a stable ``code`` and a
message that was *written to be read by a user*; the log gets the traceback. A
handler raises a domain error and never formats an HTTP response itself.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every failure this application raises deliberately.

    Attributes:
        status_code: HTTP status to answer with.
        code: Stable machine-readable identifier for the client.
        message: Human-readable text that is *safe to disclose*.
        detail: Structured extras for the client (field names, limits).
        log_detail: Context for the log only. Never serialised to a response.
    """

    status_code: int = 500
    code: str = "internal_error"
    message: str = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        log_detail: str | None = None,
    ) -> None:
        self.message = message or self.message
        self.detail = detail or {}
        self.log_detail = log_detail
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.detail:
            payload["detail"] = self.detail
        return payload


# -- 4xx: the caller can fix it ------------------------------------------------
class ValidationError(AppError):
    status_code = 400
    code = "invalid_request"
    message = "The request was not valid."


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthenticated"
    message = "Sign in to continue."


class CredentialExpired(AuthenticationError):
    """A genuine credential that has run out.

    Separate from a forged or garbled one because the brute-force guard must not
    count it: an expired token proves the caller once held a valid session, and
    an office full of people whose sessions lapsed at lunch is not an attack.
    """


class PermissionDenied(AppError):
    status_code = 403
    code = "forbidden"
    message = "You do not have permission to do that."


class NotFound(AppError):
    status_code = 404
    code = "not_found"
    message = "Not found."


class Conflict(AppError):
    status_code = 409
    code = "conflict"
    message = "That change conflicts with the current state."


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "The request body is too large."


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests. Slow down and try again shortly."

    def __init__(self, retry_after: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.retry_after = retry_after


class LockedOut(RateLimited):
    code = "locked_out"
    message = "Too many failed sign-in attempts from this address. Try again later."


# -- 5xx: the caller cannot fix it ---------------------------------------------
class StorageError(AppError):
    status_code = 500
    code = "storage_error"
    message = "Could not read or write server data."


class UpstreamError(AppError):
    """A dependency (the database, Blob Storage, Apps Script) failed or timed out.

    ``502`` rather than ``500`` on purpose: it tells an operator reading the
    access log that the fault is outside this process, which is the single most
    useful bit of triage information for an integration-heavy service.
    """

    status_code = 502
    code = "upstream_error"
    message = "An upstream service is unavailable. Try again shortly."


class NotConfigured(AppError):
    status_code = 503
    code = "not_configured"
    message = "This feature is not configured on the server."
