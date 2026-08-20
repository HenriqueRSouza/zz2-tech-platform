"""Analytics endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Response

from zz2.api.deps import RequireAnalytics, RequireTickets, TicketStore
from zz2.core.errors import NotFound
from zz2.domain.permissions import Principal
from zz2.services.analytics import analytics_service

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("")
def analytics_view(actor: Annotated[Principal, RequireAnalytics]) -> Response:
    """The precomputed analytics view.

    Returns a raw :class:`Response` rather than a dict. The payload is megabytes
    of already-serialised JSON cached by the service; handing FastAPI a dict would
    make it re-serialise the same bytes on every request, which on this endpoint
    is the dominant cost.
    """
    payload = analytics_service.payload()
    if payload is None:
        raise NotFound("The analytics view has not been built yet.")
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store, private"},
    )


@router.post("/rebuild")
def rebuild_view(actor: Annotated[Principal, RequireAnalytics]) -> dict[str, Any]:
    """Recompute the view from the local store.

    Consolidation only — it does not re-export from the sheet. That is
    ``POST /api/system/rebuild``, which is far more expensive and restricted to
    ``system_settings``.
    """
    view = analytics_service.build_view()
    if view is None:
        raise NotFound("The ticket store has not been built yet.")
    return {"ok": True, "count": view.get("count", 0)}


@router.get("/store")
def ticket_store(
    actor: Annotated[Principal, RequireTickets],
    store: TicketStore,
) -> dict[str, Any]:
    """The raw local ticket store.

    Served to any principal with ``tickets``, technicians included — it is the
    same data as the analytics view but without the admin-only consolidation
    report, and the Tickets page renders directly from it.
    """
    if not store.exists():
        raise NotFound("The ticket store has not been built yet.")
    return store.raw()
