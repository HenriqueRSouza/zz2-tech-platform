"""Exception handlers — the single place an error becomes a response.

Four handlers, ordered from most to least specific, and one rule they all share:
the client learns *what* went wrong, never *where*. Paths, exception types,
upstream bodies and tracebacks go to the log with the request id attached, so a
user reporting "request 8f2c1a failed" is enough to find the exact traceback.

The monolith's ``except Exception as e: self._json(500, {'error': 'Server error:
%s' % e})`` did the opposite: a ``KeyError`` disclosed an internal field name, an
``IOError`` disclosed the NAS path, and a Supabase failure disclosed the table and
constraint that rejected the write.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from zz2.core.errors import AppError, RateLimited
from zz2.core.logging import logger


def register_error_handlers(app: FastAPI) -> None:
    """Install every handler on ``app``."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """Deliberate domain failures.

        5xx are logged at error level with the stack; 4xx at info without one — a
        403 is the system working correctly and does not need a traceback.
        """
        context = {
            "code": exc.code,
            "path": request.url.path,
            "status": exc.status_code,
            "detail": exc.log_detail or "",
        }
        if exc.status_code >= 500:
            logger.error("request failed", extra=context, exc_info=exc)
        else:
            logger.info("request rejected", extra=context)

        headers: dict[str, str] = {}
        if isinstance(exc, RateLimited):
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(exc.to_payload(), status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Malformed request bodies and parameters.

        Pydantic's report is summarised to field locations and messages. The raw
        report echoes the submitted ``input`` back, which would put a rejected
        password or ticket body into the response of a request that failed.
        """
        fields = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
                "problem": error.get("msg", "invalid"),
            }
            for error in exc.errors()[:10]
        ]
        logger.info("request validation failed",
                    extra={"path": request.url.path, "fields": fields})
        return JSONResponse(
            {"error": "Some fields were not valid.", "code": "invalid_request",
             "detail": {"fields": fields}},
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Framework-raised errors: unmatched routes, wrong method."""
        return JSONResponse(
            {"error": str(exc.detail), "code": f"http_{exc.status_code}"},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Anything unanticipated.

        A bug, by definition. Logged with the full stack; answered with a fixed
        string and the request id, which is the only thing the client needs in
        order to let someone find the real cause.
        """
        logger.exception("unhandled exception",
                         extra={"path": request.url.path, "method": request.method})
        return JSONResponse(
            {
                "error": "The request could not be completed.",
                "code": "internal_error",
                "request_id": getattr(request.state, "request_id", "-"),
            },
            status_code=500,
        )
