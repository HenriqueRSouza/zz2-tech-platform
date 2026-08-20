"""Background worker behaviour."""

from __future__ import annotations

import threading
import time

import pytest

from zz2.workers.base import BackgroundWorker


class CountingWorker(BackgroundWorker):
    name = "zz2-test-counting"

    # Tiny backoff so the failure-recovery test does not wait out the production
    # five-second-per-failure ramp.
    error_backoff_base = 0.01
    max_error_backoff = 0.05

    def __init__(self, *, fail_times: int = 0) -> None:
        super().__init__()
        self.iterations = 0
        self.started = False
        self.stopped = False
        self._fail_times = fail_times
        self._done = threading.Event()

    @property
    def interval_seconds(self) -> float:
        return 0.01

    def on_start(self) -> None:
        self.started = True

    def on_stop(self) -> None:
        self.stopped = True

    def run_once(self) -> None:
        self.iterations += 1
        if self.iterations <= self._fail_times:
            raise RuntimeError("simulated failure")
        self._done.set()

    def wait_for_success(self, timeout: float = 5.0) -> bool:
        return self._done.wait(timeout)


class TestWorkerLifecycle:
    def test_start_runs_the_loop_and_hooks(self) -> None:
        worker = CountingWorker()
        worker.start()
        try:
            assert worker.wait_for_success()
            assert worker.started is True
            assert worker.running is True
        finally:
            worker.stop(timeout=2.0)
        assert worker.stopped is True
        assert worker.running is False

    def test_stop_is_honoured_promptly(self) -> None:
        """Event.wait, not time.sleep: shutdown must not wait out a long interval.

        The monolith's loops were ``while True: time.sleep(interval)`` with no
        way to stop them, so shutdown meant killing the process mid-write.
        """
        class SlowWorker(CountingWorker):
            name = "zz2-test-slow"

            @property
            def interval_seconds(self) -> float:
                return 30.0

        worker = SlowWorker()
        worker.start()
        assert worker.wait_for_success()

        started = time.monotonic()
        worker.stop(timeout=5.0)
        assert time.monotonic() - started < 2.0, "stop waited out the interval"

    def test_a_failing_iteration_does_not_kill_the_worker(self) -> None:
        """The loop must outlive any one failure. Verified because the monolith's
        bare ``except`` was the only thing keeping its threads alive."""
        worker = CountingWorker(fail_times=2)
        worker.start()
        try:
            assert worker.wait_for_success(), "worker died on the first failure"
            assert worker.iterations >= 3
        finally:
            worker.stop(timeout=2.0)

    def test_start_is_idempotent(self) -> None:
        worker = CountingWorker()
        worker.start()
        worker.start()
        try:
            assert worker.wait_for_success()
        finally:
            worker.stop(timeout=2.0)


class TestWriteBehindWorker:
    @pytest.fixture
    def wiring(self, data_dir, monkeypatch):
        from zz2.domain.models import WriteOperation
        from zz2.repositories.tickets import TicketStoreRepository
        from zz2.repositories.write_queue import (
            DeadLetterRepository,
            PendingWriteRepository,
            new_operation_id,
        )
        from zz2.workers.write_behind import WriteBehindWorker

        queue = PendingWriteRepository()
        dead = DeadLetterRepository()
        store = TicketStoreRepository()
        store.replace_all([{"ticket": "100", "client": "Original"}], source="test")

        worker = WriteBehindWorker(queue=queue, dead_letters=dead, store=store)
        return worker, queue, dead, store, WriteOperation, new_operation_id

    def test_a_successful_write_is_dequeued(self, wiring, monkeypatch) -> None:
        worker, queue, dead, _store, Operation, new_id = wiring
        monkeypatch.setattr("zz2.workers.write_behind.appscript.write",
                            lambda params: {"ok": True})

        queue.enqueue(Operation(id=new_id(), action="updateticket",
                                params={"action": "updateticket", "ticketNum": "100"}))
        worker.run_once()

        assert queue.count() == 0
        assert dead.count() == 0

    def test_a_failed_write_is_retried_with_a_counter(self, wiring, monkeypatch) -> None:
        from zz2.core.errors import UpstreamError

        worker, queue, dead, _store, Operation, new_id = wiring

        def fail(_params):
            raise UpstreamError(log_detail="simulated")

        monkeypatch.setattr("zz2.workers.write_behind.appscript.write", fail)

        queue.enqueue(Operation(id=new_id(), action="updateticket",
                                params={"action": "updateticket"}))
        worker.run_once()

        assert queue.count() == 1
        assert queue.head().attempts == 1
        assert dead.count() == 0

    def test_exhausted_retries_dead_letter_and_unblock_the_queue(
        self, wiring, monkeypatch
    ) -> None:
        """One bad operation must not block the queue forever, and must not be
        dropped silently either."""
        from zz2.core.errors import UpstreamError
        from zz2.repositories.write_queue import MAX_ATTEMPTS

        worker, queue, dead, _store, Operation, new_id = wiring

        def fail(_params):
            raise UpstreamError(log_detail="simulated")

        monkeypatch.setattr("zz2.workers.write_behind.appscript.write", fail)

        queue.enqueue(Operation(id=new_id(), action="updateticket",
                                params={"action": "updateticket"},
                                attempts=MAX_ATTEMPTS - 1))
        worker.run_once()

        assert queue.count() == 0
        assert dead.count() == 1
        assert "gave up" in dead.all()[0]["dead_reason"]

    def test_a_reassigned_number_renumbers_the_store_and_the_queue(
        self, wiring, monkeypatch
    ) -> None:
        """Both must move together. Renumbering only the store leaves a queued
        delete aimed at a number that now belongs to somebody else's ticket."""
        worker, queue, _dead, store, Operation, new_id = wiring
        monkeypatch.setattr("zz2.workers.write_behind.appscript.write",
                            lambda params: {"ok": True, "ticketNum": "250"})

        queue.enqueue(Operation(id=new_id(), action="newticket",
                                params={"action": "newticket"},
                                predicted_number=100))
        queue.enqueue(Operation(id=new_id(), action="deleteticket",
                                params={"action": "deleteticket", "ticketNum": "100"}))
        worker.run_once()

        assert store.find("250") is not None
        assert store.find("100") is None
        assert queue.head().params["ticketNum"] == "250"


class TestWorkerManager:
    def test_status_reports_every_worker(self) -> None:
        from zz2.workers.manager import worker_manager

        status = worker_manager.status()
        assert set(status) == {"zz2-poller", "zz2-write-behind", "zz2-scheduler"}
