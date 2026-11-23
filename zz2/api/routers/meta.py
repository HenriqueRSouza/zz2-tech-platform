"""Public configuration, session verification and health probes.

``/api/config`` is the one endpoint that must stay unauthenticated: the sign-in
page needs to know which sign-in methods are offered *before* anyone has a session.
Its payload comes from an explicit allowlist in
:meth:`zz2.settings.Settings.public_config`, so adding a secret to ``Settings``
cannot leak it here by accident.

The health probes are separate on purpose. ``/healthz`` answers "is this process
alive" and must never touch disk or the network — a liveness probe that fails
because the database is slow gets the container restarted for no reason. ``/readyz``
answers "should traffic be routed here" and does check dependencies.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Header, Response

from zz2.api.deps import CurrentPrincipal
from zz2.core.logging import audit
from zz2.domain.permissions import PERMISSION_CATALOG, Principal
from zz2.gateways.database import db
from zz2.paths import ANALYTICS_FILE, DATA_DIR
from zz2.services.auth import auth_service
from zz2.services.runtime_config import runtime_config
from zz2.services.telemetry import PROCESS_STARTED_AT
from zz2.settings import get_settings
from zz2.workers.manager import worker_manager

router = APIRouter(tags=["meta"])


@router.get("/api/config")
def public_config() -> dict[str, Any]:
    """Browser-safe configuration for the sign-in page.

    Unauthenticated by necessity. The response also carries
    ``pane_refresh_interval`` so pages do not need a second, authenticated call
    just to learn their poll cadence.
    """
    settings = get_settings()
    return {
        **settings.public_config(),
        "server_url": settings.public_url,
        "pane_refresh_interval": runtime_config.pane_refresh_interval,
    }


@router.post("/api/session/verify")
def verify_session(actor: Annotated[Principal, CurrentPrincipal]) -> dict[str, Any]:
    """Confirm the caller's token and return their resolved identity.

    Every page calls this on load to decide what to render. It replaces the
    monolith's ``POST /verify``, which took the token *in the request body* — so
    it appeared in any proxy that logged bodies, and could be triggered
    cross-site by a form post. The token now travels only in the ``Authorization``
    header.

    The identity fields appear twice: nested under ``user`` and flattened at the
    top level. The flattening is not decoration — the pages read ``d.permissions``,
    ``d.display`` and ``d.role`` directly, and this is the shape the monolith
    returned. New code should read ``user``; the flat keys keep the eight existing
    pages working and can be dropped once none of them reads them.
    """
    identity = actor.to_public_dict()
    return {"ok": True, "user": identity, **identity}


@router.post("/api/session/logout")
def logout(actor: Annotated[Principal, CurrentPrincipal],
           response: Response,
           authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """End the session server-side.

    The session this server issued is revoked and the server-side principal
    cache is evicted, which otherwise would keep honouring the token for up to
    :data:`~zz2.services.auth.PRINCIPAL_CACHE_TTL` seconds after sign-out.
    """
    token = auth_service.token_from_header(authorization) or ""
    revoked = auth_service.sign_out(token)
    audit("session.logout", actor=actor.username,
          provider=actor.metadata.get("provider", "-"), revoked=revoked)
    response.headers["Clear-Site-Data"] = '"cache", "storage"'
    return {"ok": True}


@router.get("/api/permissions")
def permission_catalog(actor: Annotated[Principal, CurrentPrincipal]) -> dict[str, Any]:
    """The permission catalogue, for the Users and Roles pages."""
    return {
        "ok": True,
        "permissions": [
            {"key": info.key.value, "label": info.label, "desc": info.description}
            for info in PERMISSION_CATALOG
        ],
    }


# HEAD as well as GET: external uptime monitors and some load balancers probe
# with HEAD, and a 405 from a health endpoint reads as an outage.
@router.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
def liveness() -> dict[str, Any]:
    """Liveness: is this process running?

    No disk, no network, no locks. Anything that can block belongs in
    ``/readyz``.
    """
    return {"ok": True, "build": get_settings().build,
            "uptime_sec": round(time.time() - PROCESS_STARTED_AT, 1)}


@router.api_route("/readyz", methods=["GET", "HEAD"], include_in_schema=False)
def readiness(response: Response) -> dict[str, Any]:
    """Readiness: can this process actually serve requests?

    Returns 503 when a hard dependency is missing, so a load balancer or
    ``systemd`` health check stops sending traffic instead of letting every
    request fail individually.

    The ticket store being unseeded is reported but does *not* fail the probe: the
    site is fully usable without it, since reads fall through to the database.

    ``database_configured`` checks configuration only, not connectivity: a
    database blip must not pull every instance out of rotation at once, and
    every data path already degrades gracefully when the database is down.
    """
    checks = {
        "data_dir_writable": _writable(),
        "database_configured": db.configured,
        "workers": worker_manager.status(),
        "ticket_store_seeded": ANALYTICS_FILE.exists(),
    }
    ready = checks["data_dir_writable"] and checks["database_configured"]
    if not ready:
        response.status_code = 503
    return {"ok": ready, "checks": checks}


def _writable() -> bool:
    """Whether the data directory accepts a write.

    Actually attempts one. A full disk or a read-only remount presents as a
    perfectly normal-looking directory until something tries to write to it, and
    every state-mutating request would fail one at a time instead of the process
    reporting itself unready.
    """
    probe = DATA_DIR / ".readyz"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False
