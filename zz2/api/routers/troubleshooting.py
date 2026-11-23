"""Executed troubleshooting sessions.

=======  ==========================================  =====================
Method   Path                                        Requires
=======  ==========================================  =====================
POST     /api/troubleshooting/sessions               create_tickets
GET      /api/troubleshooting/sessions               tickets
GET      /api/troubleshooting/sessions/{id}          tickets
PATCH    /api/troubleshooting/sessions/{id}          create_tickets (+ owner or kit_editor)
GET      /api/troubleshooting/stats                  analytics
=======  ==========================================  =====================
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from zz2.api.deps import RequireAnalytics, RequireCreateTickets, RequireTickets
from zz2.domain.permissions import Principal
from zz2.domain.troubleshooting import (
    MAX_NOTES_LENGTH,
    MAX_PATH_STEPS,
    SessionOutcome,
)
from zz2.repositories.troubleshooting_sessions import SessionQuery
from zz2.services.troubleshooting_sessions import troubleshooting_session_service as service

router = APIRouter(prefix="/api/troubleshooting", tags=["troubleshooting"])

SessionId = Annotated[str, Path(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9]+$")]


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    kit_key: str = Field(min_length=1, max_length=64)
    path: list[str] = Field(min_length=1, max_length=MAX_PATH_STEPS)
    checked_steps: list[str] = Field(default_factory=list, max_length=MAX_PATH_STEPS)
    outcome: SessionOutcome = SessionOutcome.PENDING
    ticket_number: str = Field(default="", max_length=32)
    notes: str = Field(default="", max_length=MAX_NOTES_LENGTH)
    started_at: str | None = Field(default=None, max_length=40)


class SessionAmendRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    outcome: SessionOutcome | None = None
    ticket_number: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)


@router.post("/sessions", status_code=201)
def record_session(
    payload: SessionCreateRequest,
    actor: Annotated[Principal, RequireCreateTickets],
) -> dict[str, Any]:
    session = service.record(
        actor,
        kit_key=payload.kit_key,
        path=payload.path,
        checked_steps=payload.checked_steps,
        outcome=payload.outcome,
        ticket_number=payload.ticket_number,
        notes=payload.notes,
        started_at=payload.started_at,
    )
    return {"ok": True, "session": session.to_record()}


@router.get("/sessions")
def list_sessions(
    actor: Annotated[Principal, RequireTickets],
    kit: Annotated[str, Query(max_length=64)] = "",
    technician: Annotated[str, Query(max_length=254)] = "",
    ticket: Annotated[str, Query(max_length=32)] = "",
    outcome: SessionOutcome | None = None,
    since: Annotated[str, Query(max_length=40)] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict[str, Any]:
    sessions, has_more = service.list(SessionQuery(
        kit_key=kit, technician=technician, ticket_number=ticket, outcome=outcome,
        since=since, limit=limit, offset=offset,
    ))
    return {"ok": True, "sessions": [s.to_record() for s in sessions],
            "has_more": has_more, "limit": limit, "offset": offset}


@router.get("/sessions/{session_id}")
def get_session(session_id: SessionId,
                actor: Annotated[Principal, RequireTickets]) -> dict[str, Any]:
    return {"ok": True, "session": service.get(session_id).to_record()}


@router.patch("/sessions/{session_id}")
def amend_session(
    session_id: SessionId,
    payload: SessionAmendRequest,
    actor: Annotated[Principal, RequireCreateTickets],
) -> dict[str, Any]:
    session = service.amend(actor, session_id, outcome=payload.outcome,
                            ticket_number=payload.ticket_number, notes=payload.notes)
    return {"ok": True, "session": session.to_record()}


@router.get("/stats")
def session_stats(
    actor: Annotated[Principal, RequireAnalytics],
    kit: Annotated[str, Query(max_length=64)] = "",
) -> dict[str, Any]:
    return service.stats(kit)
