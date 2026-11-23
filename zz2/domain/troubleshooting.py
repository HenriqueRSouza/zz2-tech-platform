"""Executed troubleshooting sessions.

A *tree* (``store/troubleshooting.json``) is the procedure. A *session* is one
technician walking that procedure for one customer: which branch they took, which
steps they actually carried out, and whether it fixed the problem.

Until now the only trace of a session was the free text "Apply to ticket" pasted
into a ticket's Issue and Resolution fields. That is enough for a human reading
one ticket, and useless for the question the kit editors actually ask — "for this
kit, which path resolves the problem, and which one never does?" Storing sessions
as structured records is what makes that answerable.

Pure domain: no I/O. The repositories decide where a session lives.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

MAX_PATH_STEPS = 60
MAX_STEP_LENGTH = 300
MAX_NOTES_LENGTH = 4000


class SessionOutcome(StrEnum):
    """How a session ended.

    ``pending`` is the state at "Apply to ticket": the technician has chosen a
    path but the customer has not yet confirmed the fix. Everything else is final
    and counts towards the per-kit statistics.
    """

    PENDING = "pending"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"

    @property
    def is_final(self) -> bool:
        return self is not SessionOutcome.PENDING


def utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TroubleshootingSession(BaseModel):
    """One executed troubleshooting session.

    ``technician`` is always the authenticated principal's username, set by the
    service — never accepted from the request body, or anyone could record work
    in a colleague's name and skew the per-technician numbers.
    """

    model_config = ConfigDict(extra="ignore", use_enum_values=False)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    kit_key: str
    path: list[str] = Field(default_factory=list)
    checked_steps: list[str] = Field(default_factory=list)
    outcome: SessionOutcome = SessionOutcome.PENDING
    ticket_number: str = ""
    notes: str = ""
    technician: str
    technician_name: str = ""
    started_at: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    duration_seconds: int | None = None

    @property
    def path_signature(self) -> str:
        """The branch taken, as one comparable string. Used to group sessions."""
        return " > ".join(step.strip() for step in self.path if step.strip())

    def to_record(self) -> dict[str, Any]:
        """Storage and API representation. Identical by design — one shape."""
        record = self.model_dump()
        record["outcome"] = self.outcome.value
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Self:
        return cls.model_validate(record)
