"""Serving pages and media.

The monolith's static handler was a *denylist* over the entire application
directory: resolve the path, confirm it stays inside ``BASE_DIR``, then refuse a
hard-coded list of filenames. Structurally wrong, and it failed in three ways at
once:

* ``GET /server.py`` returned the complete server source — 5,475 lines including
  the auth design, the Supabase table layout and the write-queue protocol.
* ``supabase_setup.sql``, ``config.example.json``, ``deploy.sh`` and ``gitignore``
  were all served; none were on the list.
* ``_save_json`` wrote ``config.json.tmp.<pid>`` beside the real file, and *that*
  name was not on the list either. A crash between write and rename left the
  Apps Script secret and the Supabase service key downloadable.

An allowlist inverts the failure mode. Only files under ``web/`` are reachable,
and ``web/`` contains nothing but pages and assets — no code, no state, no
secrets. Adding a secret to the deployment can no longer expose it here, because
the directory it would have to be in is not served.

Media is separate and narrower still: only ``images/<kit>/<file>`` and
``manuals/<kit>/<file>``, with both components validated, and only for
extensions this application actually stores.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, RedirectResponse

from zz2.api.deps import CurrentPrincipal
from zz2.core.errors import NotFound
from zz2.core.text import safe_filename, safe_key
from zz2.domain.permissions import Principal
from zz2.paths import IMAGES_DIR, MANUALS_DIR, PAGES_DIR, STATIC_DIR

router = APIRouter(tags=["pages"])

#: The eight pages. An explicit set, so a stray file dropped into ``web/pages``
#: is not silently published.
SERVED_PAGES: frozenset[str] = frozenset({
    "login.html", "dashboard.html", "index.html", "tickets.html",
    "analytics.html", "kit-editor.html", "users.html", "settings.html",
})

STATIC_EXTENSIONS: frozenset[str] = frozenset({".css", ".js", ".map", ".svg",
                                               ".png", ".jpg", ".jpeg", ".ico",
                                               ".woff", ".woff2"})

MEDIA_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp",
                                              ".gif", ".pdf"})

CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

STATIC_CACHE_CONTROL = "no-cache"
"""Cache, but revalidate on every load.

Not ``max-age=3600``. The pages are revalidated by ETag, but the scripts and
stylesheets they reference are addressed by a stable URL with no build hash — so a
one-hour ``max-age`` means a deploy's JavaScript can take up to an hour to reach a
browser that already has the old copy, while the *new* HTML arrives immediately.
The result is a page running a mix of old and new code, which is the worst possible
failure mode and exactly what this project's assets did during the frontend split.

``no-cache`` costs one conditional request per asset per load and answers it with a
~100-byte 304. On a LAN-or-VPS internal tool that is the right trade: correctness
after a deploy, with no build step and no cache-busting query strings to maintain.
"""

MEDIA_CACHE_CONTROL = "private, max-age=300"
"""Media is immutable in practice — an upload replaces the whole file — and large,
so a short real cache is worth having. ``private`` keeps it out of shared caches,
since these are authenticated responses."""


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Send the bare origin to the sign-in page."""
    return RedirectResponse("/login.html", status_code=302)


@router.get("/{page}.html", include_in_schema=False)
def serve_page(page: str, request: Request) -> Response:
    """Serve one of the application pages.

    Pages are *not* authenticated here, and that is deliberate rather than an
    oversight. They contain no data — every page fetches its content from an
    authenticated API endpoint and redirects to sign-in when that returns 401.
    Gating the HTML as well would mean a signed-out visitor gets a bare 403
    instead of the login form.
    """
    filename = f"{page}.html"
    if filename not in SERVED_PAGES:
        raise NotFound()

    path = PAGES_DIR / filename
    if not path.is_file():
        raise NotFound()

    body = path.read_bytes()
    etag = f'W/"{hashlib.md5(body, usedforsecurity=False).hexdigest()}"'

    # A content-derived ETag, not mtime. A deploy that preserves timestamps —
    # ``cp -p``, a container layer, an rsync without ``--times`` — would leave an
    # mtime-based validator answering 304 for a page that really did change. The
    # monolith hit this and worked around it by never revalidating HTML at all,
    # which meant re-sending every page in full on every load.
    if etag in [tag.strip() for tag in (request.headers.get("if-none-match") or "").split(",")]:
        return Response(status_code=304, headers={"ETag": etag,
                                                  "Cache-Control": "no-cache"})

    return Response(
        content=body,
        media_type=CONTENT_TYPES[".html"],
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


@router.get("/static/{path:path}", include_in_schema=False)
def serve_static(path: str, request: Request) -> Response:
    """Serve a stylesheet, script or asset from ``web/static``.

    Conditional: an ``If-None-Match`` that matches gets a 304. Starlette's
    ``FileResponse`` sets an ``ETag`` but does not act on the request's
    validators, so the comparison is done here — without it, ``no-cache`` would
    mean re-downloading every asset on every page load.
    """
    target = _resolve_within(STATIC_DIR, path, STATIC_EXTENSIONS)
    etag = _asset_etag(target)

    if etag and etag in _request_etags(request):
        return Response(status_code=304,
                        headers={"ETag": etag, "Cache-Control": STATIC_CACHE_CONTROL})

    headers = {"Cache-Control": STATIC_CACHE_CONTROL}
    if etag:
        headers["ETag"] = etag
    return FileResponse(
        target,
        media_type=CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"),
        headers=headers,
    )


def _asset_etag(path: Path) -> str:
    """Weak validator from size and modification time.

    Cheap — one ``stat``, no read — which matters because it runs on every asset
    request. Size plus mtime is not a content hash, but for files that only change
    via a deploy it is equivalent in practice: a deploy that rewrites a file
    changes its mtime.
    """
    try:
        info = path.stat()
    except OSError:
        return ""
    return f'W/"{info.st_size:x}-{int(info.st_mtime):x}"'


def _request_etags(request: Request) -> set[str]:
    """Validators from ``If-None-Match``."""
    header = request.headers.get("if-none-match") or ""
    return {tag.strip() for tag in header.split(",") if tag.strip()}


@router.get("/images/{kit_key}/{filename}", include_in_schema=False)
def serve_kit_image(
    kit_key: str,
    filename: str,
    actor: Annotated[Principal, CurrentPrincipal],
) -> FileResponse:
    """Serve a kit's wiring or DIP diagram.

    Authenticated. Diagrams are internal technical documentation, and the
    monolith served them to anyone who could reach the port.
    """
    return _serve_media(IMAGES_DIR, kit_key, filename)


@router.get("/manuals/{kit_key}/{filename}", include_in_schema=False)
def serve_kit_manual(
    kit_key: str,
    filename: str,
    actor: Annotated[Principal, CurrentPrincipal],
) -> FileResponse:
    """Serve a kit's PDF manual. Authenticated, as above."""
    return _serve_media(MANUALS_DIR, kit_key, filename)


def _serve_media(root: Path, kit_key: str, filename: str) -> FileResponse:
    """Resolve and serve one media file under ``root/<kit>/<file>``.

    Both components are validated independently: the kit key through
    :func:`~zz2.core.text.safe_key` and the filename through
    :func:`~zz2.core.text.safe_filename`. Neither can contain a separator
    afterwards, so the join cannot escape ``root`` — checked again below anyway,
    because defence that depends on one function staying correct is not defence.
    """
    validated_key = safe_key(kit_key)
    if not validated_key:
        raise NotFound()

    validated_name = safe_filename(filename, fallback="")
    if not validated_name:
        raise NotFound()

    target = _resolve_within(root / validated_key, validated_name, MEDIA_EXTENSIONS)
    return FileResponse(
        target,
        media_type=CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"),
        headers={
            "Cache-Control": MEDIA_CACHE_CONTROL,
            # Media is user-uploaded. Even with magic-byte validation on the way
            # in, telling the browser never to sniff and never to treat this as a
            # document to render inline removes the last route from a stored file
            # to script execution on this origin.
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{validated_name}"',
        },
    )


def _resolve_within(root: Path, relative: str, allowed_extensions: frozenset[str]) -> Path:
    """Resolve ``relative`` under ``root``, or raise :class:`NotFound`.

    Four checks, and each catches something the others do not:

    1. **Extension allowlist** — a file type this application does not serve is
       refused whatever its location.
    2. **Symlink-resolved containment** — ``resolve()`` follows links, so a symlink
       inside the served tree pointing at ``/etc/passwd`` is caught. Checking the
       unresolved path would not catch it.
    3. **Is a regular file** — not a directory, socket or device.
    4. **No dotfiles** — nothing in a served tree needs a leading dot, and it
       excludes editor swap files and ``.git`` remnants in one rule.

    ``NotFound`` rather than ``Forbidden``: a 403 confirms the path exists, which
    is free reconnaissance.
    """
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in {"..", ""} for part in candidate.parts):
        raise NotFound()
    if any(part.startswith(".") for part in candidate.parts):
        raise NotFound()
    if candidate.suffix.lower() not in allowed_extensions:
        raise NotFound()

    root_resolved = root.resolve()
    target = (root_resolved / candidate).resolve()
    if root_resolved not in target.parents:
        raise NotFound()
    if not target.is_file():
        raise NotFound()
    return target
