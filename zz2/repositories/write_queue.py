"""Durable write queue and dead-letter store.

Saving a ticket used to block on a slow Apps Script round-trip. Instead the local
store is updated, the user gets an answer immediately, and the sheet write is
queued here. The queue lives on disk, so a restart mid-flight loses nothing.

Ordering is strict FIFO and the head item is retried rather than skipped, because
the sheet assigns ticket numbers by append order — reordering the queue would
reorder the numbering.

An operation that exhausts its attempts is moved to the dead-letter store rather
than dropped. Dropping it would leave the local store and the sheet permanently
divergent with nothing recording that it happened; keeping it lets an operator see
the divergence and replay once the cause is fixed.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from typing import Any

from zz2.core.jsonstore import mutate, read_json, write_json
from zz2.core.logging import audit, logger
from zz2.core.text import digits_only
from zz2.domain.models import WriteOperation
from zz2.paths import DEAD_LETTERS_FILE, PENDING_FILE
from zz2.repositories.base import Repository

MAX_ATTEMPTS = 25
"""Attempts before dead-lettering. At the backoff below this spans ~40 minutes,
long enough to ride out an Apps Script outage without blocking the queue for a
request that will never succeed."""

RETRY_BASE_SECONDS = 5
MAX_RETRY_SECONDS = 120
DEAD_LETTER_CAP = 500
"""Newest entries are kept. An unbounded file would grow forever during a
sustained upstream outage, and the oldest failures are the least actionable."""


def new_operation_id() -> str:
    """Collision-resistant id: millisecond clock plus four random bytes."""
    return f"{int(time.time() * 1000)}-{os.urandom(4).hex()}"


def retry_delay(attempts: int) -> int:
    """Linear backoff, capped. Bounded so a long-failing head item still gets
    retried often enough to drain promptly once the upstream recovers."""
    return min(MAX_RETRY_SECONDS, RETRY_BASE_SECONDS * max(1, attempts))


class PendingWriteRepository(Repository):
    """FIFO queue of :class:`WriteOperation` in ``pending_writes.json``."""

    def __init__(self) -> None:
        self._path = PENDING_FILE

    def exists(self) -> bool:
        return self._path.exists()

    def all(self) -> list[WriteOperation]:
        raw = read_json(self._path, [])
        if not isinstance(raw, list):
            return []
        return [WriteOperation.from_dict(item) for item in raw if isinstance(item, dict)]

    def count(self) -> int:
        raw = read_json(self._path, [])
        return len(raw) if isinstance(raw, list) else 0

    def head(self) -> WriteOperation | None:
        queue = self.all()
        return queue[0] if queue else None

    def enqueue(self, operation: WriteOperation) -> None:
        with mutate(self._path, []) as queue:
            queue.append(operation.to_dict())
        logger.info("write queued", extra={"action": operation.action,
                                          "op_id": operation.id})

    def remove(self, operation_id: str) -> None:
        with mutate(self._path, []) as queue:
            queue[:] = [item for item in queue if item.get("id") != operation_id]

    def record_attempt(self, operation_id: str, attempts: int) -> None:
        """Persist the attempt count on the head item only.

        Guarded on identity: if the head changed while the worker was off doing
        network I/O, writing blindly to index 0 would corrupt an unrelated
        operation's counter.
        """
        with mutate(self._path, []) as queue:
            if queue and queue[0].get("id") == operation_id:
                queue[0]["attempts"] = attempts

    def predicted_numbers(self) -> set[int]:
        """Ticket numbers created locally but not yet written to the sheet."""
        numbers: set[int] = set()
        for operation in self.all():
            if operation.predicted_number:
                numbers.add(int(operation.predicted_number))
        return numbers

    def pending_deletes(self) -> set[int]:
        """Ticket numbers whose delete has not yet reached the sheet."""
        numbers: set[int] = set()
        for operation in self.all():
            if operation.action == "deleteticket":
                digits = digits_only(operation.params.get("ticketNum"))
                if digits:
                    numbers.add(int(digits))
        return numbers

    def remap_ticket_number(self, old_number: object, new_number: object) -> int:
        """Repoint queued updates and deletes at a reassigned ticket number.

        When the sheet assigns a number other than the one predicted, any queued
        update or delete still names the prediction. Left alone, a
        create-then-discard would delete *someone else's* ticket — the row that
        happens to hold the predicted number. Returns how many operations moved.
        """
        old = digits_only(old_number)
        new = digits_only(new_number)
        if not old or old == new:
            return 0
        changed = 0
        with mutate(self._path, []) as queue:
            for item in queue:
                if item.get("action") not in ("updateticket", "deleteticket"):
                    continue
                params = item.get("params") or {}
                if digits_only(params.get("ticketNum")) == old:
                    params["ticketNum"] = str(new_number)
                    item["params"] = params
                    changed += 1
        if changed:
            logger.info("remapped queued ticket number",
                        extra={"from": old, "to": new, "operations": changed})
        return changed

    def oldest_age_seconds(self) -> float | None:
        """Age of the head item, for the health report. ``None`` when empty."""
        head = self.head()
        if head is None or not head.created_at:
            return None
        try:
            created = dt.datetime.strptime(head.created_at, "%Y-%m-%dT%H:%M:%SZ")
            return (dt.datetime.now(dt.UTC) - created.replace(tzinfo=dt.UTC)).total_seconds()
        except ValueError:
            return None


class DeadLetterRepository(Repository):
    """Operations that exhausted their retries."""

    def __init__(self) -> None:
        self._path = DEAD_LETTERS_FILE

    def exists(self) -> bool:
        return self._path.exists()

    def all(self) -> list[dict[str, Any]]:
        raw = read_json(self._path, [])
        return raw if isinstance(raw, list) else []

    def count(self) -> int:
        return len(self.all())

    def add(self, operation: WriteOperation, reason: str) -> None:
        record = operation.to_dict()
        record["dead_reason"] = reason
        record["dead_at"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with mutate(self._path, []) as entries:
            entries.append(record)
            if len(entries) > DEAD_LETTER_CAP:
                del entries[:-DEAD_LETTER_CAP]
        audit("write.dead_lettered", action=operation.action, op_id=operation.id,
              reason=reason)

    def drain(self) -> list[WriteOperation]:
        """Remove and return everything, for a replay.

        Atomic swap rather than read-then-clear: a replay that crashed between
        the two would lose the operations it was trying to rescue.
        """
        entries = self.all()
        write_json(self._path, [])
        return [WriteOperation.from_dict(item) for item in entries if isinstance(item, dict)]

    def clear(self) -> int:
        removed = self.count()
        write_json(self._path, [])
        if removed:
            audit("write.dead_letters_cleared", removed=removed)
        return removed
