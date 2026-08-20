"""Ticket endpoints.

The monolith routed every ticket write through one ``POST /api/proxy`` whose
behaviour depended on an ``action`` string in the body: ``newticket``,
``updateticket``, ``deleteticket``, or anything else fell through to a generic
pass-through to Apps Script. That is a single endpoint with four authorization
profiles and an open-ended fifth, which is why its permission check could only be
the coarsest of them — ``create_tickets`` gated deletes too.

Split into named routes with their own methods, schemas and permissions. The
legacy ``/api/proxy`` path is kept as a thin compatibility shim in
:mod:`zz2.api.routers.compat` while the pages migrate.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from zz2.api.deps import (
    CurrentPrincipal,
    RequireCreateTickets,
    RequireTickets,
)
from zz2.core.errors import NotFound
from zz2.domain.permissions import Principal
from zz2.services.sheet_sync import sheet_sync_service
from zz2.services.telemetry import telemetry_service
from zz2.services.tickets import ticket_service

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


class TicketPayload(BaseModel):
    """A ticket as submitted by the tool's form.

    ``extra="allow"`` because the payload is forwarded verbatim to the Apps
    Script sheet, which accepts fields this application does not model. Dropping
    unknown fields here would silently stop them reaching the sheet — and the
    Pydantic default would do exactly that.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    ticketNum: str = ""  # noqa: N815 - the existing wire format the pages send
    tech: str = ""
    client: str = ""
    product: str = ""
    vehicle: str = ""
    issue: str = ""
    resolution: str = ""
    status: str = ""
    serial: str = ""
    notes: str = ""

    def to_params(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class DeletePayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    ticketNum: str = Field(min_length=1)  # noqa: N815 - existing wire format


# -- Writes --------------------------------------------------------------------
@router.post("", status_code=201)
def create_ticket(
    payload: TicketPayload,
    actor: Annotated[Principal, RequireCreateTickets],
) -> dict[str, Any]:
    """Create a ticket."""
    return ticket_service.create(payload.to_params(), actor=actor.username).to_api()


@router.patch("/{ticket_number}")
def update_ticket(
    ticket_number: str,
    payload: TicketPayload,
    actor: Annotated[Principal, RequireCreateTickets],
) -> dict[str, Any]:
    """Edit an existing ticket.

    The number comes from the path, not the body. A body-supplied identifier is
    editable by the client independently of the URL it was sent to, which makes
    "am I allowed to change this ticket" impossible to reason about from the
    request line alone.
    """
    params = payload.to_params()
    params["ticketNum"] = ticket_number
    return ticket_service.update(params, actor=actor.username).to_api()


@router.delete("/{ticket_number}")
def delete_ticket(
    ticket_number: str,
    actor: Annotated[Principal, RequireCreateTickets],
) -> dict[str, Any]:
    """Discard a ticket."""
    return ticket_service.delete({"ticketNum": ticket_number},
                                 actor=actor.username).to_api()


# -- Reads ---------------------------------------------------------------------
@router.get("/recent")
def recent_tickets(
    actor: Annotated[Principal, CurrentPrincipal],
    days: Annotated[int, Query(ge=1, le=30)] = 1,
) -> dict[str, Any]:
    """Tickets from the last ``days`` days.

    Any signed-in principal, including technicians without the ``tickets``
    permission: this feeds the Today pane, which is how a technician sees their
    own work.
    """
    return ticket_service.recent(days)


@router.get("/search")
def search_tickets(
    actor: Annotated[Principal, RequireTickets],
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=50_000)] = 300,
) -> dict[str, Any]:
    """Substring search over the most recent ``limit`` tickets.

    ``limit`` is the scan window, not a result cap — see
    :meth:`zz2.services.tickets.TicketService.search`.
    """
    return ticket_service.search(q, limit)


@router.get("/history")
def ticket_history(
    actor: Annotated[Principal, RequireTickets],
    client: Annotated[str, Query(max_length=200)] = "",
    product: Annotated[str, Query(max_length=200)] = "",
    fuzzy: bool = False,
) -> dict[str, Any]:
    """Every ticket for one client or one item."""
    return ticket_service.history(client=client, product=product, fuzzy=fuzzy)


@router.get("/{ticket_number}")
def get_ticket(
    ticket_number: str,
    actor: Annotated[Principal, CurrentPrincipal],
) -> dict[str, Any]:
    """One ticket by number.

    Declared *after* ``/recent``, ``/search`` and ``/history``: FastAPI matches in
    declaration order, so a path parameter registered earlier would swallow all
    three.
    """
    result = ticket_service.get(ticket_number)
    if result is None:
        raise NotFound("Ticket not found.")
    return {"ok": True, "result": result}


@router.post("/poll")
def force_poll(actor: Annotated[Principal, RequireTickets]) -> dict[str, Any]:
    """Poll and reconcile the sheet now, and report what changed.

    Backs the page's manual Refresh. Reconcile failures are reported rather than
    raised: the poll result is still useful on its own, and the button should not
    look broken because the slower half of the work failed.
    """
    added = sheet_sync_service.poll()
    telemetry_service.record_poll(added)

    removed = 0
    reconcile_error: str | None = None
    try:
        removed = sheet_sync_service.reconcile()
        telemetry_service.record_reconcile()
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        reconcile_error = type(exc).__name__

    response: dict[str, Any] = {"ok": True, "added": added, "removed": removed}
    if reconcile_error:
        response["reconcile_error"] = reconcile_error
    return response
