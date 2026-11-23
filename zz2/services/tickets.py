"""Ticket business logic.

Where the write-behind design lives. A ticket save touches three systems:

1. **Database** (PostgreSQL) — synchronous and authoritative. If this fails, the save fails.
2. **Local store** — updated immediately so the Today pane reflects the change.
3. **Google Sheet** — queued via :mod:`zz2.workers.write_behind`, retried until it
   lands.

Ordering is the point. The database first, because it is the copy other technicians
read in real time and the only one whose failure the user must hear about. The
sheet last, because it is slow (multi-second Apps Script round-trips) and its
delay must not be the user's delay.

Reads follow the same hierarchy: the database is authoritative, and the local store is
a fallback for when it is unreachable plus a holding area for writes still in
flight. A ticket created here but not yet in the database must still appear in its
author's own pane, or the save looks like it failed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Literal

from zz2.core.errors import UpstreamError, ValidationError
from zz2.core.logging import audit, logger
from zz2.core.text import as_text, digits_only
from zz2.domain.models import Ticket, WriteOperation
from zz2.gateways.database import (
    assignment_list,
    column_list,
    db,
    escape_like,
    json_rows,
    placeholder_list,
    sql,
)
from zz2.repositories.stats import StatsRepository
from zz2.repositories.tickets import TicketStoreRepository
from zz2.repositories.write_queue import PendingWriteRepository, new_operation_id
from zz2.services.normalization import norm_key
from zz2.services.runtime_config import runtime_config
from zz2.settings import get_settings

_SELECT_TICKETS = (
    "SELECT ticket_date, ticket_number, tech, client, product, vehicle, issue, "
    "resolution, status, serial, notes FROM tickets"
)

RECENT_ROW_LIMIT = 10_000
"""Safety bound on the recent-tickets query (the PostgREST path paged 10 × 1,000)."""

HISTORY_ROW_LIMIT = 50_000
"""Safety bound on a history query (the PostgREST path paged 50 × 1,000)."""

MAX_SEARCH_RESULTS = 500
"""Cap on returned matches, so a one-character query cannot ship the whole store
to a browser that will render 40,000 rows and lock the tab."""


@dataclass(frozen=True, slots=True)
class SaveResult:
    """Outcome of a create, update or delete."""

    ticket_number: str
    queued: bool
    deleted: bool = False

    def to_api(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "ticketNum": self.ticket_number,
            "queued": self.queued,
        }
        if self.deleted:
            payload["deleted"] = True
        return payload


class TicketService:
    """Reads and writes tickets across the database, the local store and the sheet."""

    def __init__(
        self,
        store: TicketStoreRepository | None = None,
        queue: PendingWriteRepository | None = None,
        stats: StatsRepository | None = None,
    ) -> None:
        self._store = store or TicketStoreRepository()
        self._queue = queue or PendingWriteRepository()
        self._stats = stats or StatsRepository()

    # -- Writes ----------------------------------------------------------------
    def create(self, payload: dict[str, Any], *, actor: str) -> SaveResult:
        """Create a ticket.

        Raises:
            ValidationError: Neither a client nor an item was supplied. A ticket
                identifying nothing is unusable, and rejecting it here is cheaper
                than someone deleting it later.
            UpstreamError: The database refused the insert. Nothing is queued, so the
                sheet and the local store stay consistent with the failure.
        """
        ticket = Ticket.from_store(payload)
        if not (ticket.client.strip() or ticket.product.strip()):
            raise ValidationError("Enter a client or an item before creating the ticket.")

        # Dated before numbering: numbering appends this ticket to the local store,
        # and an undated row there drops out of every analytics time series.
        ticket = ticket.model_copy(update={"date": dt.date.today().isoformat()})

        number = self._next_ticket_number(ticket)
        if number is None:
            # The store has never been seeded, so a number cannot be predicted
            # locally. The synchronous path lets the sheet assign one.
            return self._create_synchronously(payload, ticket, actor=actor)

        ticket = ticket.model_copy(update={"number": str(number)})

        # Authoritative write first. A failure here must abort: queueing the sheet
        # write anyway would leave a ticket that exists in the sheet and the local
        # store but not in the copy every other technician reads. The row numbering
        # reserved in the store goes with it, or it would linger as a phantom.
        try:
            _insert_ticket(ticket.to_record())
        except UpstreamError:
            self._store.delete(number)
            raise

        if ticket.tech:
            self._stats.increment(ticket.tech.lower())

        # No append here: numbering already wrote the row to the store.
        self._enqueue("newticket", payload, predicted_number=number)

        audit("ticket.created", actor=actor, ticket=str(number))
        return SaveResult(str(number), queued=True)

    def update(self, payload: dict[str, Any], *, actor: str) -> SaveResult:
        """Edit an existing ticket.

        ``ticket_date`` and ``ticket_number`` are excluded from the database
        update: the number identifies the row, and the date records when the
        ticket was created, not when it was last touched.
        """
        number = as_text(payload.get("ticketNum")).strip()
        if not number:
            raise ValidationError("A ticket number is required to save an edit.")

        ticket = Ticket.from_store(payload).model_copy(update={"number": number})
        record = ticket.to_record(include_identity=False)
        db.execute(
            sql.SQL("UPDATE tickets SET {} WHERE ticket_number = %s").format(
                assignment_list(record)),
            [*record.values(), ticket.canonical_number],
        )

        self._enqueue("updateticket", payload)

        # The stored row keeps its original date: to_store() would overwrite it
        # with whatever the form submitted.
        stored = self._store.find(number)
        if stored is not None:
            ticket = ticket.model_copy(update={"date": stored.date})
        self._store.update(number, ticket)

        audit("ticket.updated", actor=actor, ticket=number)
        return SaveResult(number, queued=True)

    def delete(self, payload: dict[str, Any], *, actor: str) -> SaveResult:
        """Discard a ticket."""
        number = as_text(payload.get("ticketNum")).strip()
        if not number:
            raise ValidationError("A ticket number is required to discard a ticket.")

        canonical = Ticket(number=number).canonical_number
        db.execute("DELETE FROM tickets WHERE ticket_number = %s", [canonical])

        self._enqueue("deleteticket", payload)
        self._store.delete(number)

        audit("ticket.deleted", actor=actor, ticket=number)
        return SaveResult(number, queued=True, deleted=True)

    def _create_synchronously(
        self, payload: dict[str, Any], ticket: Ticket, *, actor: str
    ) -> SaveResult:
        """Fallback when no local number can be predicted.

        Used on a fresh installation before the first sheet export. The Apps
        Script call blocks and assigns the number, which is slow but correct.
        """
        from zz2.gateways.appscript import appscript

        result = appscript.write({"action": "newticket", **payload})
        number = as_text(result.get("ticketNum")) or ticket.number
        if not number:
            raise UpstreamError(log_detail="sheet accepted newticket without a number")

        assigned = ticket.model_copy(update={"number": number,
                                             "date": dt.date.today().isoformat()})
        if assigned.tech:
            self._stats.increment(assigned.tech.lower())
        self._store.append(assigned)

        audit("ticket.created", actor=actor, ticket=number, path="synchronous")
        return SaveResult(number, queued=False)

    def _next_ticket_number(self, ticket: Ticket) -> int | None:
        """Reserve the next number, respecting the database's high-water mark.

        Tickets are also created straight from the Tickets page into the database, so
        the local maximum alone would reuse their numbers. The remote maximum is
        read *before* the store lock is taken — holding a file lock across a
        network call would block every unrelated store read for the duration.
        """
        remote_max = 0
        try:
            rows = db.fetch_all(
                "SELECT ticket_number FROM tickets ORDER BY ticket_number DESC LIMIT 100"
            )
            for row in rows:
                digits = digits_only(row.get("ticket_number"))
                if digits:
                    remote_max = max(remote_max, int(digits))
        except UpstreamError:
            # Degrade rather than fail: the local maximum still produces a usable
            # number, and the sheet reconciles any collision by renumbering.
            logger.warning("could not read remote ticket high-water mark")

        return self._store.append_and_number(
            ticket, floor=remote_max,
            seed=not get_settings().enable_appscript_sync,
        )

    def _enqueue(self, action: str, payload: dict[str, Any],
                 *, predicted_number: int | None = None) -> None:
        # Without the sheet, the database is the only copy that matters; queueing would
        # only retry against an unconfigured script until the write dead-letters.
        if not get_settings().enable_appscript_sync:
            return
        self._queue.enqueue(WriteOperation(
            id=new_operation_id(),
            action=action,
            params={**payload, "action": action},
            predicted_number=predicted_number,
            created_at=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ))

    # -- Reads -----------------------------------------------------------------
    def recent(self, days: int = 1) -> dict[str, Any]:
        """Tickets from the last ``days`` days, merged across both sources.

        Local rows are laid down first, then database rows overwrite them by
        number. That order is deliberate: a queued local write stays visible, and
        anywhere both sources have the ticket, the shared authoritative copy wins.

        One extra calendar day is fetched. The browser applies the exact local-day
        boundary, so the overlap absorbs the timezone difference without a ticket
        near midnight disappearing.
        """
        days = max(1, days)
        window = min(3000, max(300, days * 200))
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        suppressed = self._queue.pending_deletes()

        merged: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(self._store.rows()[-window:]):
            ticket = Ticket.from_store(row)
            if ticket.numeric in suppressed:
                continue
            key = str(ticket.numeric) if ticket.numeric is not None else f"local-{index}"
            merged[key] = ticket.to_api()

        source = "database"
        try:
            rows = json_rows(db.fetch_all(
                _SELECT_TICKETS + " WHERE ticket_date >= %s "
                "ORDER BY ticket_date ASC, ticket_number ASC LIMIT %s",
                [cutoff, RECENT_ROW_LIMIT],
            ))
            for index, row in enumerate(rows):
                ticket = Ticket.from_record(row)
                if ticket.numeric in suppressed:
                    continue
                key = str(ticket.numeric) if ticket.numeric is not None else f"remote-{index}"
                merged[key] = ticket.to_api()
        except UpstreamError:
            logger.warning("recent tickets: database unavailable, serving local store")
            source = "local_fallback"

        def sort_key(item: dict[str, Any]) -> tuple[str, int]:
            return (as_text(item.get("date")),
                    int(digits_only(item.get("ticketNum")) or 0))

        results = sorted(merged.values(), key=sort_key)
        return {
            "ok": True,
            "results": results,
            "pane_refresh": runtime_config.pane_refresh_interval,
            "source": source,
        }

    def get(self, ticket_number: str) -> dict[str, Any] | None:
        """One ticket from the local store, or ``None``.

        Used by the realtime layer to resync a single open ticket without waiting
        for the next pane poll. The store is scanned from the tail because the
        ticket being resynced is nearly always a recent one.
        """
        wanted = digits_only(ticket_number)
        if not wanted:
            raise ValidationError("A ticket number is required.")
        if int(wanted) in self._queue.pending_deletes():
            return None

        for row in reversed(self._store.rows()):
            if digits_only(row.get("ticket") or row.get("ticketNum")) == wanted:
                return Ticket.from_store(row).to_api()
        return None

    def search(self, query: str, scan_limit: int = 300) -> dict[str, Any]:
        """Substring search over the most recent ``scan_limit`` tickets.

        ``scan_limit`` is a *scan window*, not a result cap — the page's dropdown
        reads "Scan last N tickets". Every match inside the window is returned, up
        to :data:`MAX_SEARCH_RESULTS`.
        """
        needle = as_text(query).strip().lower()
        rows = self._store.rows()
        scan_limit = max(1, scan_limit)
        window = rows[-scan_limit:] if scan_limit < len(rows) else rows

        if not needle:
            return {"ok": True, "results": [], "scanned": len(window)}

        suppressed = self._queue.pending_deletes()
        searchable = ("ticket", "client", "product", "tech", "vehicle",
                      "issue", "resolution", "status", "serial")
        results: list[dict[str, Any]] = []

        for row in reversed(window):
            digits = digits_only(row.get("ticket") or row.get("ticketNum"))
            if digits and int(digits) in suppressed:
                continue
            haystack = " ".join(as_text(row.get(field)) for field in searchable).lower()
            if needle in haystack:
                results.append(Ticket.from_store(row).to_api())
                if len(results) >= MAX_SEARCH_RESULTS:
                    break

        return {"ok": True, "results": results, "scanned": len(window)}

    def history(
        self,
        *,
        client: str = "",
        product: str = "",
        fuzzy: bool = False,
    ) -> dict[str, Any]:
        """Every ticket for one client or one item.

        Product history is always global: when a product is supplied the client is
        ignored, because "how has this kit behaved everywhere" is the question
        being asked.

        Args:
            fuzzy: Client names only. Widens an exact match to a substring search
                on the most distinctive token, so ``"Auto Elétrica Silva Ltda"``
                still finds rows stored as ``"Auto Eletrica Silva"``.
        """
        client, product = as_text(client).strip(), as_text(product).strip()
        if not client and not product:
            raise ValidationError("Provide a client or a product to search history for.")

        field: Literal["client", "product"] = "product" if product else "client"
        value = product or client
        pattern = self._history_pattern(value, fuzzy=fuzzy and field == "client")

        try:
            rows = json_rows(db.fetch_all(
                sql.SQL(_SELECT_TICKETS + " WHERE {} ILIKE %s "
                        "ORDER BY ticket_date DESC NULLS LAST, ticket_number DESC "
                        "LIMIT %s").format(sql.Identifier(field)),
                [pattern, HISTORY_ROW_LIMIT],
            ))
            results = [Ticket.from_record(row).to_api() for row in rows]
            source = "database"
        except UpstreamError:
            logger.warning("ticket history: database unavailable, serving local store")
            results = self._local_history(field, value)
            source = "local_fallback"

        return {
            "ok": True,
            "results": results,
            "count": len(results),
            "field": field,
            "value": value,
            "scope": "all_tickets" if field == "product" else "client",
            "source": source,
        }

    @staticmethod
    def _history_pattern(value: str, *, fuzzy: bool) -> str:
        """Build the ``ILIKE`` pattern, with user text escaped to match literally.

        Fuzzy mode picks the single most distinctive token rather than searching
        the whole phrase: numbers first (a street number or branch code is highly
        selective), then words that are not generic trade terms, longest first.
        Searching the full phrase as a substring finds nothing whenever the stored
        name differs by one word.
        """
        if not fuzzy:
            return escape_like(value)

        import re

        tokens = re.findall(r"[a-z0-9]+", norm_key(value))
        numeric = [group for token in tokens for group in re.findall(r"\d+", token)]
        generic = {"car", "stereo", "audio", "alarm", "the", "and", "inc", "llc", "ltda"}
        distinctive = [token for token in tokens if token not in generic]
        choices = sorted(numeric or distinctive or tokens, key=len, reverse=True)
        return "%" + escape_like(choices[0] if choices else value) + "%"

    def _local_history(self, field: str, value: str) -> list[dict[str, Any]]:
        """Exact-key history from the local store, newest first."""
        wanted = norm_key(value)
        suppressed = self._queue.pending_deletes()
        results: list[dict[str, Any]] = []
        for row in reversed(self._store.rows()):
            digits = digits_only(row.get("ticket") or row.get("ticketNum"))
            if digits and int(digits) in suppressed:
                continue
            if norm_key(as_text(row.get(field)).strip()) == wanted:
                results.append(Ticket.from_store(row).to_api())
        return results


def _insert_ticket(record: dict[str, Any]) -> None:
    """``INSERT`` one ``tickets`` row; columns come from :meth:`Ticket.to_record`."""
    db.execute(
        sql.SQL("INSERT INTO tickets ({}) VALUES ({})").format(
            column_list(record), placeholder_list(len(record))),
        list(record.values()),
    )


ticket_service = TicketService()
