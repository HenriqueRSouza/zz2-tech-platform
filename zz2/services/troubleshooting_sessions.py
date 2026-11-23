"""Recording and reporting executed troubleshooting sessions.

Business rules live here, not in the router and not in the repository:

* the kit must exist in the catalogue — a session against a kit that was never
  defined cannot be attributed to any tree and would only pollute the statistics;
* the technician is the caller, always;
* only the technician who recorded a session, or a kit editor, may change it, and
  a final outcome is final — reopening a resolved session would let statistics be
  rewritten after the fact;
* the ticket number is stored in the canonical ``TKT-nnnn`` form the database uses,
  so a session can be joined to its ticket.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import replace
from typing import Any

from zz2.core.errors import Conflict, NotFound, PermissionDenied, ValidationError
from zz2.core.logging import audit
from zz2.core.text import safe_key
from zz2.domain.models import Ticket
from zz2.domain.permissions import Permission, Principal
from zz2.domain.troubleshooting import (
    MAX_NOTES_LENGTH,
    MAX_PATH_STEPS,
    MAX_STEP_LENGTH,
    SessionOutcome,
    TroubleshootingSession,
    utc_now_iso,
)
from zz2.repositories.kits import KitRepository
from zz2.repositories.troubleshooting_sessions import (
    SessionQuery,
    TroubleshootingSessionStore,
    session_store,
)

TOP_PATHS = 5


def _clean_steps(steps: list[str] | None, field: str) -> list[str]:
    cleaned = [str(step).strip() for step in (steps or []) if str(step).strip()]
    if len(cleaned) > MAX_PATH_STEPS:
        raise ValidationError(f"{field} has more than {MAX_PATH_STEPS} steps.")
    if any(len(step) > MAX_STEP_LENGTH for step in cleaned):
        raise ValidationError(f"A step in {field} is longer than {MAX_STEP_LENGTH} characters.")
    return cleaned


def _canonical_ticket(value: str | None) -> str:
    text = (value or "").strip()
    return Ticket(number=text).canonical_number if text else ""


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("started_at must be an ISO-8601 timestamp.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


class TroubleshootingSessionService:
    """Create, amend, list and aggregate sessions."""

    def __init__(self, store: TroubleshootingSessionStore | None = None,
                 kits: KitRepository | None = None) -> None:
        self._fixed_store = store
        self._kits = kits or KitRepository()

    @property
    def _store(self) -> TroubleshootingSessionStore:
        return self._fixed_store or session_store()

    # -- Commands --------------------------------------------------------------
    def record(
        self,
        actor: Principal,
        *,
        kit_key: str,
        path: list[str] | None,
        checked_steps: list[str] | None = None,
        outcome: SessionOutcome = SessionOutcome.PENDING,
        ticket_number: str | None = None,
        notes: str = "",
        started_at: str | None = None,
    ) -> TroubleshootingSession:
        key = safe_key(kit_key)
        if not key or self._kits.get(key) is None:
            raise NotFound("That kit does not exist.")

        cleaned_path = _clean_steps(path, "path")
        if not cleaned_path:
            raise ValidationError("A session needs at least one step in its path.")
        if len(notes) > MAX_NOTES_LENGTH:
            raise ValidationError(f"Notes are limited to {MAX_NOTES_LENGTH} characters.")

        now = dt.datetime.now(dt.UTC)
        started = _parse_iso(started_at)
        duration: int | None = None
        if started is not None:
            if started > now + dt.timedelta(minutes=5):
                raise ValidationError("started_at is in the future.")
            duration = int((now - started).total_seconds())

        session = TroubleshootingSession(
            kit_key=key,
            path=cleaned_path,
            checked_steps=_clean_steps(checked_steps, "checked_steps"),
            outcome=outcome,
            ticket_number=_canonical_ticket(ticket_number),
            notes=notes.strip(),
            technician=actor.username,
            technician_name=actor.display,
            started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ") if started else None,
            duration_seconds=duration,
        )
        self._store.add(session)
        audit("troubleshooting.session.recorded", actor=actor.username,
              session=session.id, kit=key, outcome=session.outcome.value)
        return session

    def amend(
        self,
        actor: Principal,
        session_id: str,
        *,
        outcome: SessionOutcome | None = None,
        ticket_number: str | None = None,
        notes: str | None = None,
    ) -> TroubleshootingSession:
        session = self.get(session_id)
        if session.technician != actor.username and not actor.has(Permission.KIT_EDITOR):
            raise PermissionDenied("Only the technician who recorded this session can change it.")

        changes: dict[str, Any] = {}
        if outcome is not None and outcome != session.outcome:
            if session.outcome.is_final:
                raise Conflict("This session already has a final outcome.")
            changes["outcome"] = outcome
        if ticket_number is not None:
            changes["ticket_number"] = _canonical_ticket(ticket_number)
        if notes is not None:
            if len(notes) > MAX_NOTES_LENGTH:
                raise ValidationError(f"Notes are limited to {MAX_NOTES_LENGTH} characters.")
            changes["notes"] = notes.strip()
        if not changes:
            return session

        updated = session.model_copy(update={**changes, "updated_at": utc_now_iso()})
        self._store.replace(updated)
        audit("troubleshooting.session.amended", actor=actor.username,
              session=session_id, fields=",".join(sorted(changes)))
        return updated

    # -- Queries ---------------------------------------------------------------
    def get(self, session_id: str) -> TroubleshootingSession:
        session = self._store.get(session_id) if session_id.isalnum() else None
        if session is None:
            raise NotFound("Troubleshooting session not found.")
        return session

    def list(self, query: SessionQuery) -> tuple[list[TroubleshootingSession], bool]:
        if query.ticket_number:
            query = replace(query, ticket_number=_canonical_ticket(query.ticket_number))
        return self._store.query(query)

    def stats(self, kit_key: str = "") -> dict[str, Any]:
        """Per-kit outcome counts and the paths that resolve most often.

        Pending and abandoned sessions are counted but excluded from the
        resolution rate: one the customer has not yet confirmed, or one a
        technician replaced by re-applying a different path, is neither a success
        nor a failure of the path it recorded.
        """
        key = safe_key(kit_key) if kit_key else ""
        if kit_key and not key:
            raise ValidationError("Invalid kit key.")

        per_kit: dict[str, Counter[str]] = defaultdict(Counter)
        resolving_paths: dict[str, Counter[str]] = defaultdict(Counter)
        failing_paths: dict[str, Counter[str]] = defaultdict(Counter)

        for session in self._store.for_stats(key):
            per_kit[session.kit_key][session.outcome.value] += 1
            if session.outcome is SessionOutcome.RESOLVED:
                resolving_paths[session.kit_key][session.path_signature] += 1
            elif session.outcome in (SessionOutcome.UNRESOLVED, SessionOutcome.ESCALATED):
                failing_paths[session.kit_key][session.path_signature] += 1

        kits = []
        for kit, counts in sorted(per_kit.items()):
            final = sum(counts.get(o, 0) for o in ("resolved", "unresolved", "escalated"))
            kits.append({
                "kit_key": kit,
                "total": sum(counts.values()),
                "outcomes": {o.value: counts.get(o.value, 0) for o in SessionOutcome},
                "resolution_rate": round(counts.get("resolved", 0) / final, 3) if final else None,
                "top_resolving_paths": [
                    {"path": path, "count": n}
                    for path, n in resolving_paths[kit].most_common(TOP_PATHS)
                ],
                "top_failing_paths": [
                    {"path": path, "count": n}
                    for path, n in failing_paths[kit].most_common(TOP_PATHS)
                ],
            })
        return {"ok": True, "kits": kits}


troubleshooting_session_service = TroubleshootingSessionService()
