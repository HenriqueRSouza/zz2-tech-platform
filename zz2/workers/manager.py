"""Worker lifecycle.

One place that knows which background workers exist and in what order they start
and stop. Wired into the application's lifespan, so ``SIGTERM`` from systemd
stops them cleanly instead of the process being killed mid-write.

Stop order is the reverse of start order, and the write-behind worker stops last
on purpose: it is the one holding unflushed user work, and giving it the longest
window before shutdown maximises the chance the queue drains.
"""

from __future__ import annotations

from zz2.core.logging import logger
from zz2.settings import get_settings
from zz2.workers.base import BackgroundWorker
from zz2.workers.poller import poll_worker
from zz2.workers.scheduler import scheduler_worker
from zz2.workers.write_behind import write_behind_worker


class WorkerManager:
    """Starts and stops every background worker."""

    def __init__(self) -> None:
        # Start order: write-behind first so any operations left queued by a
        # previous run begin draining before new traffic arrives.
        self._workers: tuple[BackgroundWorker, ...] = (
            write_behind_worker,
            poll_worker,
            scheduler_worker,
        )

    def start_all(self) -> None:
        if not get_settings().enable_background_workers:
            logger.info("background workers disabled by configuration")
            return
        for worker in self._workers:
            worker.start()

    def stop_all(self, timeout: float = 10.0) -> None:
        for worker in reversed(self._workers):
            worker.stop(timeout=timeout)

    def status(self) -> dict[str, bool]:
        return {worker.name: worker.running for worker in self._workers}

    def wake(self, name: str) -> bool:
        """Interrupt one worker's sleep. Returns whether it was found."""
        for worker in self._workers:
            if worker.name == name:
                worker.wake()
                return True
        return False


worker_manager = WorkerManager()
