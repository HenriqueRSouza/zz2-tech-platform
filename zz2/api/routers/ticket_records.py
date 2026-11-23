"""Raw ``tickets`` table access and ticket presence for the browser.

The Tickets grid, the ticket-history panel and the dashboard counters used to talk
to Supabase straight from the page (supabase-js / PostgREST), and presence ran over
Supabase Realtime. With the database now reachable only from this server, these
endpoints are the replacement — same row shape, same ordering, same permissions
the row-level policies used to express. See ``docs/AZURE_MIGRATION.md``.

Rows are returned exactly as the table holds them, with dates as ISO strings.
Writes accept only the named columns: ``id`` and ``created_at`` are never
client-writable, and anything else in the body is rejected rather than ignored.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from zz2.api.deps import CurrentPrincipal, RequireCreateTickets, RequireTickets
from zz2.core.errors import (
    Conflict,
    NotFound,
    PermissionDenied,
    UpstreamError,
    ValidationError,
)
from zz2.core.logging import audit
from zz2.domain.permissions import Permission, Principal
from zz2.gateways.database import (
    assignment_list,
    column_list,
    db,
    escape_like,
    json_row,
    json_rows,
    placeholder_list,
    sql,
    sqlstate,
)
from zz2.services.auth import record_denial
from zz2.services.presence import presence_hub

router = APIRouter(tags=["ticket-records"])

COLUMNS = ("id", "ticket_number", "ticket_date", "tech", "client", "product", "vehicle",
           "issue", "resolution", "status", "serial", "notes", "created_at")

MAX_PAGE = 5_000
MAX_TEXT = 10_000
"""Per-field cap. Generous for an issue description; small enough that one request
cannot store megabytes in a single cell."""

TextField = Annotated[str | None, Field(default=None, max_length=MAX_TEXT)]


# -- Authorization ---------------------------------------------------------------
class RequireAnyPermission:
    """Like :class:`zz2.api.deps.RequirePermission`, satisfied by any one of several."""

    def __init__(self, *permissions: Permission) -> None:
        self.permissions = permissions

    def __call__(self, principal: Annotated[Principal, CurrentPrincipal],
                 request: Request) -> Principal:
        if not principal.has_any(*self.permissions):
            record_denial(principal, "|".join(p.value for p in self.permissions),
                          request.url.path)
            raise PermissionDenied()
        return principal


RequireCountAccess = Depends(RequireAnyPermission(Permission.DASHBOARD, Permission.TICKETS))
RequirePresenceAccess = Depends(
    RequireAnyPermission(Permission.TICKETS, Permission.CREATE_TICKETS)
)


# -- Schemas ---------------------------------------------------------------------
class _TicketColumns(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tech: TextField
    client: TextField
    product: TextField
    vehicle: TextField
    issue: TextField
    resolution: TextField
    status: TextField
    serial: TextField
    notes: TextField


class TicketRecordCreate(_TicketColumns):
    """A new row. ``ticket_date`` defaults to today in the database."""

    ticket_number: str = Field(min_length=1, max_length=64)
    ticket_date: dt.date | None = None


class TicketRecordPatch(_TicketColumns):
    """A partial update: only the fields present in the body are written."""

    ticket_number: str | None = Field(default=None, min_length=1, max_length=64)
    ticket_date: dt.date | None = None

    @field_validator("ticket_number", "ticket_date")
    @classmethod
    def _not_null(cls, value: Any) -> Any:
        # Only reached when the key is present: a JSON null for a NOT NULL column.
        if value is None:
            raise ValueError("cannot be null")
        return value


def _write_error(exc: UpstreamError) -> Exception:
    """Map a constraint failure to a client error; anything else stays a 502."""
    state = sqlstate(exc)
    if state == "23505":
        return Conflict("A ticket with that number already exists.")
    if state.startswith("22") or state in ("23502", "23514"):
        return ValidationError("The ticket was rejected by the database.")
    return exc


def _select(where: Any, tail: str) -> Any:
    """``SELECT <every column> FROM tickets <where> <tail>``."""
    return sql.SQL("SELECT {} FROM tickets {} {}").format(
        column_list(COLUMNS), where, sql.SQL(tail))


def _returning() -> Any:
    return sql.SQL("RETURNING {}").format(column_list(COLUMNS))


def _history_pattern(pattern: str) -> str:
    """Translate the client's pattern to a safe ``ILIKE`` pattern.

    ``%`` (SQL) and ``*`` (the PostgREST convention the pages used) are both
    wildcards. ``_`` and the backslash are matched literally — a product code like
    ``ITZ_100`` must not match ``ITZ-100`` or ``ITZX100``.
    """
    return "%".join(escape_like(part) for part in pattern.replace("*", "%").split("%"))


# -- Tickets ---------------------------------------------------------------------
@router.get("/api/ticket-records")
def list_ticket_records(
    actor: Annotated[Principal, RequireTickets],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1000,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Rows newest number first, one page at a time."""
    rows = db.fetch_all(
        _select(sql.SQL(""), "ORDER BY ticket_number DESC LIMIT %s OFFSET %s"),
        [limit, offset],
    )
    return {"ok": True, "rows": json_rows(rows)}


@router.get("/api/ticket-records/history")
def ticket_record_history(
    actor: Annotated[Principal, RequireTickets],
    field: Literal["client", "product"],
    pattern: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1000,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Every row whose client or product matches ``pattern`` (case-insensitive)."""
    rows = db.fetch_all(
        _select(sql.SQL("WHERE {} ILIKE %s").format(sql.Identifier(field)),
                "ORDER BY ticket_date DESC NULLS LAST, ticket_number DESC "
                "LIMIT %s OFFSET %s"),
        [_history_pattern(pattern), limit, offset],
    )
    return {"ok": True, "rows": json_rows(rows)}


@router.get("/api/ticket-records/count")
def count_ticket_records(
    actor: Annotated[Principal, RequireCountAccess],
    since: dt.date | None = None,
    before: dt.date | None = None,
) -> dict[str, Any]:
    """Rows with ``since <= ticket_date < before``; either bound may be omitted."""
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("ticket_date >= %s")
        params.append(since)
    if before is not None:
        clauses.append("ticket_date < %s")
        params.append(before)
    where = (sql.SQL(" WHERE {}").format(sql.SQL(" AND ").join(map(sql.SQL, clauses)))
             if clauses else sql.SQL(""))
    row = db.fetch_one(sql.SQL("SELECT count(*) AS count FROM tickets{}").format(where),
                       params)
    return {"ok": True, "count": int((row or {}).get("count") or 0)}


@router.post("/api/ticket-records", status_code=201)
def create_ticket_record(
    payload: TicketRecordCreate,
    actor: Annotated[Principal, RequireCreateTickets],
) -> dict[str, Any]:
    """Insert one row and return it as stored."""
    values = payload.model_dump(exclude_unset=True)
    if values.get("ticket_date") is None:
        values.pop("ticket_date", None)  # let the column default (today) apply
    # Every key is a declared model field — extra="forbid" makes it a whitelist.
    try:
        row = db.fetch_one(
            sql.SQL("INSERT INTO tickets ({}) VALUES ({}) {}").format(
                column_list(values), placeholder_list(len(values)), _returning()),
            list(values.values()),
        )
    except UpstreamError as exc:
        raise _write_error(exc) from exc
    audit("ticket_record.created", actor=actor.username, ticket=payload.ticket_number)
    return {"ok": True, "row": json_row(row)}


@router.patch("/api/ticket-records/{record_id}")
def update_ticket_record(
    record_id: int,
    payload: TicketRecordPatch,
    actor: Annotated[Principal, RequireTickets],
) -> dict[str, Any]:
    """Change the given columns of one row and return the row as stored."""
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise ValidationError("Nothing to update.")
    try:
        row = db.fetch_one(
            sql.SQL("UPDATE tickets SET {} WHERE id = %s {}").format(
                assignment_list(values), _returning()),
            [*values.values(), record_id],
        )
    except UpstreamError as exc:
        raise _write_error(exc) from exc
    if row is None:
        raise NotFound("Ticket not found.")
    audit("ticket_record.updated", actor=actor.username, record=str(record_id),
          fields=",".join(values))
    return {"ok": True, "row": json_row(row)}


# -- Presence --------------------------------------------------------------------
_CLIENT_ID = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
MAX_EVENT_PAYLOAD_BYTES = 8 * 1024


class PresenceHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_id: Annotated[str, _CLIENT_ID]
    name: str = Field(default="", max_length=120)


class PresenceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_id: Annotated[str, _CLIENT_ID]
    type: str = Field(min_length=1, max_length=64)
    payload: Any = None

    @field_validator("payload")
    @classmethod
    def _bounded(cls, value: Any) -> Any:
        if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError(f"must be at most {MAX_EVENT_PAYLOAD_BYTES} bytes as JSON")
        return value


TicketNumber = Annotated[str, Path(min_length=1, max_length=64)]


@router.post("/api/presence/tickets/{ticket_number}")
def presence_heartbeat(
    ticket_number: TicketNumber,
    payload: PresenceHeartbeat,
    actor: Annotated[Principal, RequirePresenceAccess],
) -> dict[str, Any]:
    """Announce (or keep alive) this tab on a ticket; get the others' state."""
    state = presence_hub.heartbeat(ticket_number, user=actor.username,
                                   client_id=payload.client_id,
                                   name=payload.name or actor.display or actor.username)
    return {"ok": True, **state}


@router.delete("/api/presence/tickets/{ticket_number}")
def presence_leave(
    ticket_number: TicketNumber,
    client_id: Annotated[str, Query(min_length=1, max_length=64)],
    actor: Annotated[Principal, RequirePresenceAccess],
) -> dict[str, Any]:
    """Withdraw this tab from a ticket immediately instead of waiting for expiry."""
    presence_hub.leave(ticket_number, user=actor.username, client_id=client_id)
    return {"ok": True}


@router.post("/api/presence/tickets/{ticket_number}/events")
def presence_event(
    ticket_number: TicketNumber,
    payload: PresenceEvent,
    actor: Annotated[Principal, RequirePresenceAccess],
) -> dict[str, Any]:
    """Broadcast an ephemeral event to the ticket's other viewers."""
    presence_hub.publish(ticket_number, user=actor.username, client_id=payload.client_id,
                         event_type=payload.type, payload=payload.payload,
                         name=actor.display or actor.username)
    return {"ok": True}
