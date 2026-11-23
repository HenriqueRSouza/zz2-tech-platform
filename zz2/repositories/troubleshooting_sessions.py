"""Persistence for executed troubleshooting sessions.

Two implementations behind one interface, chosen by
``ZZ2_TROUBLESHOOTING_STORE``:

**Database** — the ``techSupport.troubleshooting_sessions`` table in PostgreSQL
(``sql/azure_schema.sql``). Authoritative in production: sessions are shared data
that analytics and reporting read, and they should survive the loss of the server
without depending on a backup.

**Local** — a JSON document under ``DATA_DIR/store``. For development, tests and
installations without ``ZZ2_DATABASE_URL``. Bounded, because it is rewritten
whole on every insert.

This is the Repository pattern doing the job ``repositories/base.py`` describes:
the service asks for sessions and never learns which of the two it is talking to.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from zz2.core.logging import logger
from zz2.domain.troubleshooting import SessionOutcome, TroubleshootingSession
from zz2.gateways.database import (
    as_json,
    assignment_list,
    column_list,
    db,
    json_row,
    placeholder_list,
    sql,
)
from zz2.paths import TS_SESSIONS_JSON
from zz2.repositories.base import JsonDocumentRepository, Repository
from zz2.settings import get_settings

TABLE = "troubleshooting_sessions"

MAX_LOCAL_SESSIONS = 20_000
"""Cap on the local document. Oldest sessions are dropped beyond it; the database
is the store for anything that needs to keep more history than this."""

STATS_ROW_LIMIT = 50_000


@dataclass(frozen=True, slots=True)
class SessionQuery:
    """Filters for a session listing. Empty fields do not filter."""

    kit_key: str = ""
    technician: str = ""
    ticket_number: str = ""
    outcome: SessionOutcome | None = None
    since: str = ""
    limit: int = 50
    offset: int = 0

    def matches(self, record: dict[str, Any]) -> bool:
        if self.kit_key and record.get("kit_key") != self.kit_key:
            return False
        if self.technician and record.get("technician") != self.technician:
            return False
        if self.ticket_number and record.get("ticket_number") != self.ticket_number:
            return False
        if self.outcome is not None and record.get("outcome") != self.outcome.value:
            return False
        return not (self.since and str(record.get("created_at") or "") < self.since)


class TroubleshootingSessionStore(Repository):
    """The interface every session store implements."""

    @abstractmethod
    def add(self, session: TroubleshootingSession) -> None: ...

    @abstractmethod
    def get(self, session_id: str) -> TroubleshootingSession | None: ...

    @abstractmethod
    def replace(self, session: TroubleshootingSession) -> None: ...

    @abstractmethod
    def query(self, query: SessionQuery) -> tuple[list[TroubleshootingSession], bool]:
        """Newest first. Returns ``(page, has_more)``."""

    @abstractmethod
    def for_stats(self, kit_key: str = "") -> list[TroubleshootingSession]:
        """Every session (optionally of one kit), for aggregation."""


# -- Local JSON ----------------------------------------------------------------
class LocalSessionRepository(JsonDocumentRepository[list], TroubleshootingSessionStore):
    """``store/troubleshooting_sessions.json`` — a list, oldest first."""

    def __init__(self) -> None:
        super().__init__(TS_SESSIONS_JSON, list)

    def _records(self) -> list[dict[str, Any]]:
        document = self.read()
        if not isinstance(document, list):
            return []
        return [row for row in document if isinstance(row, dict)]

    def add(self, session: TroubleshootingSession) -> None:
        with self.mutate() as document:
            document.append(session.to_record())
            overflow = len(document) - MAX_LOCAL_SESSIONS
            if overflow > 0:
                del document[:overflow]

    def get(self, session_id: str) -> TroubleshootingSession | None:
        for record in self._records():
            if record.get("id") == session_id:
                return TroubleshootingSession.from_record(record)
        return None

    def replace(self, session: TroubleshootingSession) -> None:
        with self.mutate() as document:
            for index, record in enumerate(document):
                if isinstance(record, dict) and record.get("id") == session.id:
                    document[index] = session.to_record()
                    return

    def query(self, query: SessionQuery) -> tuple[list[TroubleshootingSession], bool]:
        matched = [r for r in reversed(self._records()) if query.matches(r)]
        page = matched[query.offset:query.offset + query.limit]
        has_more = len(matched) > query.offset + query.limit
        return [TroubleshootingSession.from_record(r) for r in page], has_more

    def for_stats(self, kit_key: str = "") -> list[TroubleshootingSession]:
        return [TroubleshootingSession.from_record(r) for r in self._records()
                if not kit_key or r.get("kit_key") == kit_key]


# -- Database ------------------------------------------------------------------
_COLUMNS = (
    "id", "kit_key", "path", "checked_steps", "outcome", "ticket_number", "notes",
    "technician", "technician_name", "started_at", "created_at", "updated_at",
    "duration_seconds",
)
_JSON_COLUMNS = frozenset({"path", "checked_steps"})
_UPDATABLE = tuple(c for c in _COLUMNS if c != "id")


def _select(tail: str, clauses: list[str] | None = None) -> Any:
    """``SELECT <every column> FROM troubleshooting_sessions [WHERE …] <tail>``.

    ``clauses`` are fixed ``column = %s`` fragments from :meth:`_filters`; values
    are always bound parameters.
    """
    where = (sql.SQL(" WHERE {}").format(sql.SQL(" AND ").join(map(sql.SQL, clauses)))
             if clauses else sql.SQL(""))
    return sql.SQL("SELECT {} FROM {}{} {}").format(
        column_list(_COLUMNS), sql.Identifier(TABLE), where, sql.SQL(tail))


def _params(record: dict[str, Any], columns: tuple[str, ...]) -> list[Any]:
    return [as_json(record.get(c)) if c in _JSON_COLUMNS else record.get(c)
            for c in columns]


def _session(row: dict[str, Any]) -> TroubleshootingSession:
    return TroubleshootingSession.from_record(json_row(row) or {})


class DatabaseSessionRepository(TroubleshootingSessionStore):
    """``techSupport.troubleshooting_sessions`` in PostgreSQL."""

    def exists(self) -> bool:
        return db.configured

    def add(self, session: TroubleshootingSession) -> None:
        db.execute(
            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(TABLE), column_list(_COLUMNS),
                placeholder_list(len(_COLUMNS))),
            _params(session.to_record(), _COLUMNS),
        )

    def get(self, session_id: str) -> TroubleshootingSession | None:
        row = db.fetch_one(_select("LIMIT 1", ["id = %s"]), [session_id])
        return _session(row) if row else None

    def replace(self, session: TroubleshootingSession) -> None:
        db.execute(
            sql.SQL("UPDATE {} SET {} WHERE id = %s").format(
                sql.Identifier(TABLE), assignment_list(_UPDATABLE)),
            [*_params(session.to_record(), _UPDATABLE), session.id],
        )

    @staticmethod
    def _filters(query: SessionQuery) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.kit_key:
            clauses.append("kit_key = %s")
            params.append(query.kit_key)
        if query.technician:
            clauses.append("technician = %s")
            params.append(query.technician)
        if query.ticket_number:
            clauses.append("ticket_number = %s")
            params.append(query.ticket_number)
        if query.outcome is not None:
            clauses.append("outcome = %s")
            params.append(query.outcome.value)
        if query.since:
            clauses.append("created_at >= %s")
            params.append(query.since)
        return clauses, params

    def query(self, query: SessionQuery) -> tuple[list[TroubleshootingSession], bool]:
        # One extra row tells us whether another page exists without an exact
        # count, which is a full scan on a large table.
        clauses, params = self._filters(query)
        rows = db.fetch_all(
            _select("ORDER BY created_at DESC LIMIT %s OFFSET %s", clauses),
            [*params, query.limit + 1, query.offset],
        )
        sessions = [_session(r) for r in rows[:query.limit]]
        return sessions, len(rows) > query.limit

    def for_stats(self, kit_key: str = "") -> list[TroubleshootingSession]:
        clauses, params = (["kit_key = %s"], [kit_key]) if kit_key else ([], [])
        rows = db.fetch_all(
            _select("ORDER BY created_at ASC LIMIT %s", clauses),
            [*params, STATS_ROW_LIMIT],
        )
        return [_session(r) for r in rows]


def session_store() -> TroubleshootingSessionStore:
    """The configured store. Resolved per call so a test can switch it."""
    mode = get_settings().troubleshooting_store
    if mode == "database" or (mode == "auto" and db.configured):
        return DatabaseSessionRepository()
    if mode == "auto":
        logger.debug("troubleshooting sessions stored locally: no database configured")
    return LocalSessionRepository()
