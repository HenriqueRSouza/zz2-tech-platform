"""Write-behind worker: drains the queued sheet writes.

Strict FIFO, retrying the head item rather than skipping it. Order is not a
nicety here — the sheet assigns ticket numbers by append order, so reordering the
queue reorders the numbering and two tickets end up swapped.

A head item that keeps failing therefore blocks everything behind it, which is
correct up to a point and unacceptable past it. After
:data:`~zz2.repositories.write_queue.MAX_ATTEMPTS` the operation is dead-lettered
and the queue moves on: the local store already reflects the change, and a later
reconcile will resync that row from the sheet's actual state.
"""

from __future__ import annotations

from zz2.core.errors import UpstreamError
from zz2.core.logging import audit, logger
from zz2.core.text import as_text
from zz2.domain.models import WriteOperation
from zz2.gateways.appscript import appscript
from zz2.repositories.tickets import TicketStoreRepository
from zz2.repositories.write_queue import (
    MAX_ATTEMPTS,
    DeadLetterRepository,
    PendingWriteRepository,
    retry_delay,
)
from zz2.workers.base import BackgroundWorker

IDLE_INTERVAL_SECONDS = 2.0
"""Poll interval for an empty queue. Short: a queued ticket save should reach the
sheet within seconds, and checking a small JSON file is nearly free."""


class WriteBehindWorker(BackgroundWorker):
    """Pushes queued operations to the Apps Script sheet."""

    name = "zz2-write-behind"

    def __init__(
        self,
        queue: PendingWriteRepository | None = None,
        dead_letters: DeadLetterRepository | None = None,
        store: TicketStoreRepository | None = None,
    ) -> None:
        super().__init__()
        self._queue = queue or PendingWriteRepository()
        self._dead = dead_letters or DeadLetterRepository()
        self._store = store or TicketStoreRepository()
        self._next_delay = IDLE_INTERVAL_SECONDS

    @property
    def interval_seconds(self) -> float:
        return self._next_delay

    def run_once(self) -> None:
        operation = self._queue.head()
        if operation is None:
            self._next_delay = IDLE_INTERVAL_SECONDS
            return

        try:
            result = appscript.write(operation.params)
        except UpstreamError:
            self._handle_failure(operation)
            return

        self._reconcile_number(operation, result)
        self._queue.remove(operation.id)
        # Drain aggressively: another item may be waiting and there is no reason
        # to sleep between two successful writes.
        self._next_delay = 0.0

    def _handle_failure(self, operation: WriteOperation) -> None:
        attempts = operation.attempts + 1

        if attempts >= MAX_ATTEMPTS:
            logger.error(
                "write-behind gave up",
                extra={"action": operation.action, "op_id": operation.id,
                       "attempts": attempts,
                       "ticket": operation.predicted_number or ""},
            )
            self._dead.add(operation, f"gave up after {attempts} attempts")
            self._queue.remove(operation.id)
            self._next_delay = IDLE_INTERVAL_SECONDS
            return

        self._queue.record_attempt(operation.id, attempts)
        self._next_delay = retry_delay(attempts)
        logger.info("write-behind retrying",
                    extra={"action": operation.action, "attempts": attempts,
                           "retry_in": self._next_delay})

    def _reconcile_number(self, operation: WriteOperation, result: dict) -> None:
        """Fix up local state when the sheet assigned a different number.

        Two things must move together, and the monolith is where the need became
        clear: the store row is renumbered *and* any still-queued update or delete
        for that ticket is repointed. Repointing only the store would leave a
        queued delete aimed at the predicted number — which by then belongs to
        somebody else's ticket.
        """
        if operation.action != "newticket":
            return

        actual = as_text(result.get("ticketNum")).strip()
        predicted = operation.predicted_number
        if not actual or predicted is None or actual == str(predicted):
            return

        self._store.renumber(predicted, actual)
        self._queue.remap_ticket_number(predicted, actual)
        audit("ticket.renumbered", predicted=predicted, actual=actual)


write_behind_worker = WriteBehindWorker()
