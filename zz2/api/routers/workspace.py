"""Per-user workspace: ticket drafts and personal counters.

Both are scoped to the caller. Neither takes a username parameter, and that is
the security property: a draft can only be read or written for the principal that
authenticated. The monolith derived the path from ``session['username']`` too, but
did so in each handler; here it is structural — there is no parameter to get wrong.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from zz2.api.deps import CurrentPrincipal, Drafts, RequireDashboard, Stats
from zz2.domain.permissions import Principal

router = APIRouter(tags=["workspace"])


class DraftRequest(BaseModel):
    """A draft save, or a clear.

    ``draft`` is deliberately untyped: it is the tool's in-progress form state,
    whose shape belongs to the page and changes with it. The server stores and
    returns it without interpretation.
    """

    model_config = ConfigDict(extra="ignore")

    draft: Any = None
    clear: bool = False


@router.get("/api/draft")
def get_draft(
    actor: Annotated[Principal, CurrentPrincipal],
    drafts: Drafts,
) -> dict[str, Any]:
    """The caller's saved draft, or ``None``."""
    return {"ok": True, "draft": drafts.get(actor.username)}


@router.post("/api/draft")
def save_draft(
    payload: DraftRequest,
    actor: Annotated[Principal, CurrentPrincipal],
    drafts: Drafts,
) -> dict[str, Any]:
    """Save or clear the caller's draft."""
    if payload.clear:
        drafts.clear(actor.username)
        return {"ok": True, "cleared": True}
    drafts.save(actor.username, payload.draft)
    return {"ok": True}


@router.get("/api/stats")
def get_stats(
    actor: Annotated[Principal, RequireDashboard],
    stats: Stats,
) -> dict[str, Any]:
    """Per-technician ticket counters for the dashboard."""
    return {"ok": True, "stats": stats.all()}


@router.post("/api/stats/increment")
def increment_stats(
    actor: Annotated[Principal, CurrentPrincipal],
    stats: Stats,
) -> dict[str, Any]:
    """Add one to the caller's own counters.

    The username comes from the token, never the body. A body-supplied name would
    let any signed-in user inflate — or corrupt — a colleague's numbers, which the
    dashboard presents as performance data.
    """
    return {"ok": True, "stats": stats.increment(actor.username)}
