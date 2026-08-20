"""Application factory.

Replaces ``if __name__ == '__main__':`` at the bottom of a 5,475-line module.
Everything the process needs in order to serve is assembled here, in one readable
sequence, and nothing is created at import time — so a test can build an app with
different settings, and importing any module has no side effects.

Middleware order is significant and runs outermost first:

1. ``TrustedHostMiddleware`` — reject a bad ``Host`` header before anything else
   reads it.
2. ``CORSMiddleware`` — answer preflights without waking the rest of the stack.
3. ``RequestContextMiddleware`` — assign the request id, enforce the body cap,
   log the outcome. Third so the log covers everything below it.
4. ``RateLimitMiddleware`` — after the request id exists, so a block is
   traceable, and before any handler work is done.
5. ``SecurityHeadersMiddleware`` — innermost, so it stamps every response
   including ones the layers above generate.
"""

from __future__ import annotations

import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from zz2.api.error_handlers import register_error_handlers
from zz2.api.middleware.rate_limit import RateLimitMiddleware
from zz2.api.middleware.request_context import RequestContextMiddleware
from zz2.api.middleware.security import SecurityHeadersMiddleware
from zz2.api.routers import (
    analytics,
    compat,
    customers,
    kits,
    meta,
    pages,
    roles,
    system,
    tickets,
    users,
    workspace,
)
from zz2.core.errors import AppError
from zz2.core.logging import audit, configure_logging, logger
from zz2.gateways.http import close_client
from zz2.paths import ensure_runtime_dirs
from zz2.services.analytics import analytics_service
from zz2.services.catalog import catalog_service
from zz2.services.mirror import mirror_service
from zz2.settings import Settings, get_settings
from zz2.workers.manager import worker_manager

API_ROUTERS = (
    meta.router,
    tickets.router,
    analytics.router,
    kits.router,
    customers.router,
    workspace.router,
    users.router,
    roles.router,
    system.router,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop everything with a lifetime.

    Startup is ordered so a failure happens as early as possible: directories
    before anything writes, the catalogue before a request can ask for it, workers
    last so they never observe a half-initialised process.

    Each optional step is guarded. A catalogue that cannot be seeded or a mirror
    that will not subscribe is worth logging loudly, but neither is a reason to
    refuse to serve — the site is still useful without them, and a process that
    will not start is harder to diagnose than one that starts and complains.
    """
    settings: Settings = app.state.settings

    ensure_runtime_dirs()
    logger.info("starting", extra={"build": settings.build,
                                   "environment": settings.environment,
                                   "public_url": settings.public_url})
    audit("server.started", build=settings.build, environment=settings.environment)

    try:
        summary = catalog_service.ensure_initialised()
        logger.info("kit catalogue ready", extra=summary)
    except AppError as exc:
        logger.error("kit catalogue unavailable",
                     extra={"code": exc.code, "detail": exc.log_detail or ""})

    with suppress(AppError):
        analytics_service.ensure_view()

    mirror_service.register()
    worker_manager.start_all()

    try:
        yield
    finally:
        logger.info("shutting down")
        # Reverse order: stop producing work, cancel pending work, then release
        # the connections that work would have used.
        worker_manager.stop_all()
        mirror_service.cancel()
        close_client()
        audit("server.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Override for tests. Defaults to the process singleton.
    """
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, json_output=resolved.log_json)

    app = FastAPI(
        title="ZZ2 Tech Support Platform",
        version=resolved.build,
        description="Internal CarPlay/Android-Auto kit diagnostics and support ticketing.",
        lifespan=lifespan,
        # Interactive docs off in production. They are a complete, accurate map of
        # every endpoint and its schema — exactly what an attacker would otherwise
        # have to infer.
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_production else "/openapi.json",
    )
    app.state.settings = resolved

    _register_middleware(app, resolved)
    register_error_handlers(app)

    for router in API_ROUTERS:
        app.include_router(router)
    app.include_router(compat.router)

    # Last: its catch-all page and media routes must not shadow an API path.
    app.include_router(pages.router)

    return app


def _register_middleware(app: FastAPI, settings: Settings) -> None:
    """Install the middleware stack. See the module docstring for the ordering."""
    # Added innermost-first — Starlette wraps each new layer around the previous,
    # so the last one added runs first.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    if settings.cors_origins:
        # Only when explicitly configured. The default is same-origin, which is
        # correct here: the pages are served by this very process. The monolith
        # sent ``Access-Control-Allow-Origin: *`` on every response, authenticated
        # ones included.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            max_age=600,
        )

    if settings.trusted_hosts and "*" not in settings.trusted_hosts:
        app.add_middleware(TrustedHostMiddleware,
                           allowed_hosts=list(settings.trusted_hosts))


app = create_app()
"""Module-level instance for ``uvicorn zz2.main:app``."""


def _install_signal_handlers() -> None:
    """Log SIGTERM and SIGINT before the lifespan teardown runs.

    Purely diagnostic, and it answers the question that matters after an
    unexplained restart: did the process exit because it was asked to, or because
    it crashed? Without this, a clean systemd stop and an OOM kill look the same
    in the log.
    """
    def handler(signum: int, _frame: object) -> None:
        logger.info("signal received", extra={"signal": signal.Signals(signum).name})
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(ValueError, OSError):
            signal.signal(sig, handler)


def main() -> None:
    """Development entry point: ``python -m zz2.main``.

    Production runs gunicorn with uvicorn workers under systemd — see
    ``deploy/systemd/zz2.service``. This exists so a developer can start the app
    without remembering the uvicorn invocation.
    """
    import uvicorn

    settings = get_settings()
    _install_signal_handlers()
    uvicorn.run(
        "zz2.main:app",
        host=settings.host,
        port=settings.port,
        reload=not settings.is_production,
        access_log=False,  # RequestContextMiddleware logs requests with context.
        log_config=None,   # configure_logging owns logging.
    )


if __name__ == "__main__":
    main()
