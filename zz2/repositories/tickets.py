"""The local ticket store.

A mirror of every ticket, seeded once from the Google Sheet and kept current as
tickets are created and edited. The Analytics page reads a view derived from it,
so a page load never triggers a full sheet export.

Ticket numbers are matched on **digits only**, everywhere. The same ticket is
written as ``"7388"`` by the sheet, ``"TKT-7388"`` by the database, ``7388`` (int) by
one older export path and ``" 7388 "`` by hand-edited rows. The monolith's
delete used a strict string comparison and carried a four-line comment about the
resulting bug: a discarded ticket kept reappearing in another technician's Today
pane until the next reconcile, because the delete had silently matched nothing.

Every method here takes the document lock for the whole read-modify-write. The
monolith interleaved ``_load_json`` and ``_save_json`` calls outside its lock in
several paths, which is how two concurrent saves could each append to a store
they had read before the other's write.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from zz2.core.events import TicketStoreChanged, bus
from zz2.core.jsonstore import mutate, read_json
from zz2.core.logging import logger
from zz2.core.text import digits_only
from zz2.domain.models import Ticket
from zz2.paths import ANALYTICS_FILE
from zz2.repositories.base import Repository


def _empty_store() -> dict[str, Any]:
    return {"generated": "", "count": 0, "tickets": []}


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TicketStoreRepository(Repository):
    """Append-oriented repository over ``analytics.json``."""

    def __init__(self) -> None:
        self._path = ANALYTICS_FILE

    # -- Reads -----------------------------------------------------------------
    def exists(self) -> bool:
        return self._path.exists()

    def raw(self) -> dict[str, Any]:
        """The whole store document."""
        document = read_json(self._path, _empty_store())
        return document if isinstance(document, dict) else _empty_store()

    def rows(self) -> list[dict[str, Any]]:
        """Raw ticket rows, in stored order."""
        rows = self.raw().get("tickets")
        return rows if isinstance(rows, list) else []

    def tickets(self) -> list[Ticket]:
        """Rows parsed into domain objects."""
        return [Ticket.from_store(row) for row in self.rows()]

    def generated_at(self) -> str:
        return str(self.raw().get("generated") or "")

    def mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def max_number(self) -> int:
        """Highest ticket number present, or ``0`` for an empty store."""
        highest = 0
        for row in self.rows():
            digits = digits_only(row.get("ticket") or row.get("ticketNum"))
            if digits:
                highest = max(highest, int(digits))
        return highest

    def find(self, number: object) -> Ticket | None:
        """Look a ticket up by number, comparing digits only."""
        target = digits_only(number)
        if not target:
            return None
        for row in self.rows():
            if digits_only(row.get("ticket") or row.get("ticketNum")) == target:
                return Ticket.from_store(row)
        return None

    # -- Writes ----------------------------------------------------------------
    def append(self, ticket: Ticket) -> None:
        """Add one ticket.

        A store that has not been seeded is left alone: appending to a
        ``{"tickets": []}`` placeholder would make the next full rebuild look
        like it lost rows, and the sheet remains the source of truth until the
        seed completes.
        """
        if not self.exists():
            logger.debug("ticket store not seeded; skipping append")
            return
        with mutate(self._path, _empty_store()) as store:
            rows = store.get("tickets")
            if not isinstance(rows, list):
                return
            rows.append(ticket.to_store())
            store["count"] = len(rows)
            store["updated"] = _utc_now_iso()
        bus.publish(TicketStoreChanged("append", ticket.number))

    def append_and_number(self, ticket: Ticket, floor: int = 0,
                          *, seed: bool = False) -> int | None:
        """Assign the next number and append, atomically.

        Returns the assigned number, or ``None`` when the store is not seeded so
        the caller can fall back to the synchronous Apps Script path.

        Numbering and appending must happen in one critical section. Split apart
        — as ``_reserve_next_num`` and ``_append_ticket_to_store`` were — two
        concurrent saves can read the same maximum and both claim it, and the
        collision only surfaces later as a sheet row that overwrote another.

        Args:
            floor: Externally observed high-water mark, e.g. the highest number
                already in the database. Tickets can also be created directly from
                the Tickets page, so the local maximum alone is not enough.
            seed: Start an empty store when none exists. For installations
                without the sheet, where there is no export to seed from and
                ``floor`` alone is the authoritative high-water mark.
        """
        if not self.exists() and not seed:
            return None
        assigned: int | None = None
        with mutate(self._path, _empty_store()) as store:
            rows = store.get("tickets")
            if not isinstance(rows, list):
                return None
            local_max = 0
            for row in rows:
                digits = digits_only(row.get("ticket") or row.get("ticketNum"))
                if digits:
                    local_max = max(local_max, int(digits))
            assigned = max(local_max, floor) + 1
            record = ticket.to_store()
            record["ticket"] = str(assigned)
            rows.append(record)
            store["count"] = len(rows)
            store["updated"] = _utc_now_iso()
        bus.publish(TicketStoreChanged("create", str(assigned)))
        return assigned

    def update(self, number: object, ticket: Ticket) -> bool:
        """Merge an edited ticket over the stored row. Returns whether it matched."""
        target = digits_only(number)
        if not target or not self.exists():
            return False
        matched = False
        with mutate(self._path, _empty_store()) as store:
            rows = store.get("tickets")
            if not isinstance(rows, list):
                return False
            for row in rows:
                if digits_only(row.get("ticket") or row.get("ticketNum")) == target:
                    row.update(ticket.to_store())
                    matched = True
                    break
            store["updated"] = _utc_now_iso()
        if matched:
            bus.publish(TicketStoreChanged("update", str(number)))
        return matched

    def delete(self, number: object) -> int:
        """Remove every row matching ``number``. Returns how many were removed."""
        target = digits_only(number)
        if not target or not self.exists():
            return 0
        removed = 0
        with mutate(self._path, _empty_store()) as store:
            rows = store.get("tickets")
            if not isinstance(rows, list):
                return 0
            kept = [
                row for row in rows
                if digits_only(row.get("ticket") or row.get("ticketNum")) != target
            ]
            removed = len(rows) - len(kept)
            store["tickets"] = kept
            store["count"] = len(kept)
            store["updated"] = _utc_now_iso()
        if removed:
            bus.publish(TicketStoreChanged("delete", str(number)))
        return removed

    def renumber(self, old_number: object, new_number: object) -> bool:
        """Repoint a row after the sheet assigned a number we did not predict."""
        target = digits_only(old_number)
        if not target:
            return False
        changed = False
        with mutate(self._path, _empty_store()) as store:
            for row in store.get("tickets") or []:
                if digits_only(row.get("ticket") or row.get("ticketNum")) == target:
                    row["ticket"] = str(new_number)
                    changed = True
                    break
            store["updated"] = _utc_now_iso()
        if changed:
            logger.info("renumbered ticket", extra={"from": str(old_number),
                                                    "to": str(new_number)})
        return changed

    def replace_all(self, rows: list[dict[str, Any]], *, source: str) -> None:
        """Overwrite the store with a full snapshot (nightly rebuild)."""
        document = {
            "generated": _utc_now_iso(),
            "count": len(rows),
            "source": source,
            "tickets": rows,
        }
        with mutate(self._path, _empty_store()) as store:
            store.clear()
            store.update(document)
        bus.publish(TicketStoreChanged("rebuild"))

    def merge_new(self, incoming: list[dict[str, Any]]) -> int:
        """Add rows whose ticket number is not already present.

        Used by the poller to pick up tickets typed straight into the sheet.
        Returns how many were added.
        """
        if not incoming or not self.exists():
            return 0
        added = 0
        with mutate(self._path, _empty_store()) as store:
            rows = store.get("tickets")
            if not isinstance(rows, list):
                return 0
            present = {digits_only(row.get("ticket")) for row in rows}
            for row in incoming:
                digits = digits_only(row.get("ticket"))
                if digits and digits not in present:
                    rows.append(row)
                    present.add(digits)
                    added += 1
            if added:
                store["count"] = len(rows)
                store["updated"] = _utc_now_iso()
        if added:
            bus.publish(TicketStoreChanged("poll"))
        return added

    def replace_window(
        self,
        authoritative: list[dict[str, Any]],
        *,
        floor: int,
        protect: set[int],
        suppress: set[int],
    ) -> int:
        """Make the recent window match the sheet exactly.

        Rows numbered at or above ``floor`` are dropped and rebuilt from
        ``authoritative``; older rows are untouched and wait for the nightly
        rebuild. This is what makes a deletion or edit performed directly in the
        sheet visible within minutes instead of at midnight.

        Args:
            authoritative: The sheet's version of the recent window.
            floor: Lowest ticket number the window covers.
            protect: Numbers created here but not yet written to the sheet. They
                are absent from ``authoritative`` through no fault of their own,
                so dropping them would make a just-saved ticket blink out of the
                user's view.
            suppress: Numbers whose delete is still queued. The sheet still has
                them; re-adding them would undo a delete the user has already
                seen succeed.

        Returns:
            Net rows removed, never negative.
        """
        if not authoritative or not self.exists():
            return 0
        recent_numbers = {
            int(digits) for row in authoritative
            if (digits := digits_only(row.get("ticket") or row.get("ticketNum")))
        }
        dropped = 0
        with mutate(self._path, _empty_store()) as store:
            rows = store.get("tickets")
            if not isinstance(rows, list):
                return 0
            kept: list[dict[str, Any]] = []
            for row in rows:
                digits = digits_only(row.get("ticket") or row.get("ticketNum"))
                number = int(digits) if digits else None
                if number is not None and number >= floor:
                    if number in protect and number not in recent_numbers:
                        kept.append(row)
                    else:
                        dropped += 1
                else:
                    kept.append(row)
            kept.extend(
                row for row in authoritative
                if not (
                    (digits := digits_only(row.get("ticket") or row.get("ticketNum")))
                    and int(digits) in suppress
                )
            )
            store["tickets"] = kept
            store["count"] = len(kept)
            store["updated"] = _utc_now_iso()
        bus.publish(TicketStoreChanged("reconcile"))
        net = dropped - len(authoritative)
        return max(0, net)
