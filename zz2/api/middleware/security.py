"""Security response headers.

The monolith sent four headers on every response: CORS wildcard, allowed methods,
allowed headers and a build stamp. No CSP, no frame protection, no MIME-sniffing
protection, no referrer policy. Acceptable on an isolated LAN; not on a public
VPS, where these headers are the difference between an XSS bug being contained
and being an account takeover.

The Content-Security-Policy is the interesting one, because it has to be
*honest* — a policy that forbids what the pages actually do is either silently
unenforced or breaks the app, and both outcomes are worse than a narrower policy
that is true.

Two things it now does that it could not before the frontend pass:

* **No external script hosts.** The Supabase client and pdf.js were loaded from
  jsDelivr and cdnjs; both are vendored under ``web/static/vendor/``, so
  ``script-src`` is ``'self'`` and a compromised CDN can no longer execute here.
* **No external connect target beyond Supabase**, which the app genuinely needs
  for authentication and realtime.

One thing it still cannot do: drop ``'unsafe-inline'`` from ``script-src``. The
pages carry 209 inline event handlers (``onclick="..."``), most of them generated
inside ``innerHTML`` template strings, and every one requires it. Removing them
means rewriting those call sites as delegated listeners — real work, tracked in
``SECURITY.md`` rather than papered over with a header that looks strict and is
not. ``style-src`` needs it for the same reason: roughly 300 inline ``style=``
attributes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from zz2.settings import get_settings


def _content_security_policy(supabase_origin: str) -> str:
    """Build the policy.

    ``connect-src`` is the directive that limits exfiltration: it is what turns a
    successful script injection into a script that cannot send anything anywhere.
    Supabase is listed over both https and wss — the Tickets page uses Postgres
    Changes and Presence over a websocket.
    """
    connect = ["'self'"]
    if supabase_origin:
        connect.extend([supabase_origin, supabase_origin.replace("https://", "wss://")])
    return "; ".join([
        "default-src 'self'",
        # 'unsafe-inline' is required by the pages' inline event handlers. See the
        # module docstring; removing it is tracked work, not an oversight.
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        # pdf.js runs its parser in a worker loaded from this origin.
        "worker-src 'self' blob:",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        f"connect-src {' '.join(connect)}",
        # Official legacy manuals are embedded from the ZZ2-owned Drive folder.
        # No other external frame origin is allowed.
        # Private manuals are fetched with Authorization and displayed through
        # a browser-local blob URL; plain iframes cannot attach bearer headers.
        "frame-src 'self' blob: https://drive.google.com",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "upgrade-insecure-requests",
    ])


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach hardening headers to every response."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        super().__init__(app)
        settings = get_settings()
        supabase_origin = settings.supabase_url.rstrip("/") if settings.supabase_url else ""

        self._headers: dict[str, str] = {
            # Stops the browser second-guessing Content-Type. Without it an
            # uploaded "image" that is really HTML can be rendered as a page on
            # this origin, which is stored XSS.
            "X-Content-Type-Options": "nosniff",

            # frame-ancestors above supersedes this for modern browsers; kept for
            # older ones that do not implement it.
            "X-Frame-Options": "DENY",

            # Send only the origin cross-site. Ticket URLs carry identifiers that
            # should not leak in a Referer to jsDelivr or Supabase.
            "Referrer-Policy": "strict-origin-when-cross-origin",

            # Nothing here needs a camera, microphone or geolocation.
            "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",

            "Content-Security-Policy": _content_security_policy(supabase_origin),
            "X-ZZ2-Build": settings.build,

            # Isolate this origin's browsing context group from cross-origin
            # windows, closing the door on cross-window reference attacks.
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
        }

        if settings.is_production:
            # HSTS only in production: sent over plain HTTP in development it
            # would pin localhost to https in the developer's browser and be
            # tedious to undo.
            self._headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in self._headers.items():
            response.headers.setdefault(name, value)

        # Never let an intermediary cache an authenticated API response. The
        # monolith set no-store on some JSON paths and nothing at all on others.
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store, private")
            response.headers.setdefault("Vary", "Authorization")
        return response
