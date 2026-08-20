"""Keeping the local store in step with the Google Sheet.

Tickets are also typed directly into the sheet, and Google cannot push to this
server, so changes made there are discovered by polling. Two complementary
operations, because one alone is not enough:

**Poll** — ask for tickets numbered above the local maximum and append them.
Cheap, runs often, but strictly additive: it can never see an edit or a deletion.

**Reconcile** — re-fetch the last few days and make the local recent window match
the sheet exactly. More expensive, runs less often, and it is what makes a
deletion or an edit performed in the sheet visible within minutes rather than at
the nightly rebuild.

The reconcile is the delicate one. It *replaces* a window of local data with
whatever the sheet returns, so it has to be conservative: an empty or failed
response is treated as "no information" and changes nothing, rather than as "the
sheet has no tickets" and wiping the window.
"""

from __future__ import annotations

from typing import Any

from zz2.core.errors import UpstreamError
from zz2.core.logging import logger
from zz2.core.text import as_text, digits_only
from zz2.gateways.appscript import appscript
from zz2.repositories.tickets import TicketStoreRepository
from zz2.repositories.write_queue import PendingWriteRepository
from zz2.services.runtime_config import runtime_config


class SheetSyncService:
    """Polls and reconciles against the Apps Script sheet."""

    def __init__(
        self,
        store: TicketStoreRepository | None = None,
        queue: PendingWriteRepository | None = None,
    ) -> None:
        self._store = store or TicketStoreRepository()
        self._queue = queue or PendingWriteRepository()

    def poll(self) -> int:
        """Append tickets created directly in the sheet. Returns how many.

        Returns ``0`` when the store has not been seeded: there is no meaningful
        "highest number we have" to ask about, and the first full rebuild will
        collect everything anyway.
        """
        if not self._store.exists():
            return 0

        after = self._store.max_number()
        parsed = appscript.call("since", {"after": after})
        rows = self._extract_rows(parsed)
        if not rows:
            return 0

        added = self._store.merge_new(rows)
        if added:
            logger.info("poll added sheet-direct tickets", extra={"added": added})
        return added

    def reconcile(self, days: int | None = None) -> int:
        """Make the recent window match the sheet. Returns net rows removed.

        Two in-flight states are respected, and both matter to the user:

        * Tickets created here but not yet written to the sheet are *protected* —
          they are legitimately absent from the sheet's answer, and dropping them
          would make a just-saved ticket vanish from its author's pane.
        * Tickets whose delete has not yet reached the sheet are *suppressed* —
          the sheet still has them, and re-adding them would undo a delete the
          user already saw succeed.
        """
        if not self._store.exists():
            return 0

        window_days = days if days is not None else runtime_config.reconcile_days
        parsed = appscript.call("recent", {"days": int(window_days)})
        rows = self._extract_rows(parsed)
        if not rows:
            # Nothing authoritative came back. Leave the store alone.
            return 0

        numbers = [int(digits) for row in rows
                   if (digits := digits_only(row.get("ticket") or row.get("ticketNum")))]
        if not numbers:
            return 0

        normalised = self._forward_fill_dates(rows)

        removed = self._store.replace_window(
            normalised,
            floor=min(numbers),
            protect=self._queue.predicted_numbers(),
            suppress=self._queue.pending_deletes(),
        )
        if removed:
            logger.info("reconcile removed stale rows", extra={"removed": removed})
        return removed

    # -- Helpers ---------------------------------------------------------------
    @staticmethod
    def _extract_rows(parsed: Any) -> list[dict[str, Any]]:
        """Pull the ticket list out of whichever shape the script returned.

        The script answers ``{"tickets": [...]}`` for some actions and
        ``{"results": [...]}`` for others, and a bare list for older deployments.
        """
        if isinstance(parsed, list):
            rows = parsed
        elif isinstance(parsed, dict):
            rows = parsed.get("results") or parsed.get("tickets") or []
        else:
            raise UpstreamError(log_detail="sheet returned an unexpected shape")
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _forward_fill_dates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fill in dates the sheet omits, ordered by ticket number.

        The sheet dates only the first ticket of each day — a visual
        carry-forward a human reading the spreadsheet completes by eye — so every
        other row comes back with a blank date. Ticket numbers increase
        chronologically, so sorting by number and carrying the last seen date
        forward reconstructs the intended value.

        Without this, yesterday's rows land in the store dateless. Today's are
        re-dated client-side and older days are fixed at the nightly rebuild, so
        the gap is exactly the recent window this reconcile is responsible for.
        """
        normalised: list[dict[str, Any]] = []
        for row in rows:
            copy = dict(row)
            if not copy.get("ticket") and copy.get("ticketNum"):
                copy["ticket"] = copy["ticketNum"]
            normalised.append(copy)

        def sort_key(row: dict[str, Any]) -> tuple[bool, int]:
            digits = digits_only(row.get("ticket"))
            return (not digits, int(digits) if digits else 0)

        normalised.sort(key=sort_key)

        last_date = ""
        for row in normalised:
            value = as_text(row.get("date") or row.get("Date")).strip()
            if value:
                last_date = value
            elif last_date:
                row["date"] = last_date
        return normalised


sheet_sync_service = SheetSyncService()
