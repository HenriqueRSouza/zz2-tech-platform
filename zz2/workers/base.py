"""Background worker foundation.

The monolith's three background threads were each ``while True:`` with a
``time.sleep`` and a bare ``except``. They could not be stopped: shutdown meant
``pkill -f server.py``, which killed the write-behind worker mid-request and
relied on the queue file being durable enough to recover from it.

Under systemd that is the wrong shape. ``SIGTERM`` should let a worker finish its
current iteration and stop; only if it overruns should it be killed. This base
class provides that, plus three behaviours every one of those loops needed and
none of them had:

* **Interruptible sleep.** A :class:`threading.Event` wait, so a stop request is
  honoured immediately instead of after up to a full poll interval.
* **Error backoff.** A loop whose body raises every iteration would otherwise spin
  at full speed, filling the log and the CPU. Consecutive failures widen the delay.
* **Named threads.** So the System page can report liveness, which it does by
  looking up thread names.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

from zz2.core.logging import logger

MAX_ERROR_BACKOFF_SECONDS = 300.0
ERROR_BACKOFF_BASE_SECONDS = 5.0


class BackgroundWorker(ABC):
    """A named daemon thread running a loop until asked to stop.

    Subclasses implement :meth:`run_once` and :attr:`interval_seconds`.
    """

    name: str = "zz2-worker"

    error_backoff_base: float = ERROR_BACKOFF_BASE_SECONDS
    """Seconds added to the delay per consecutive failure.

    Overridable per worker rather than a module constant: a worker that polls
    every two seconds should not back off for five minutes after one transient
    error, and a nightly scheduler should not retry a failing step every five
    seconds.
    """

    max_error_backoff: float = MAX_ERROR_BACKOFF_SECONDS

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_errors = 0

    # -- Subclass contract -----------------------------------------------------
    @property
    @abstractmethod
    def interval_seconds(self) -> float:
        """Delay between iterations.

        A property, not a constant: several of these read a live configuration
        value, so a changed poll interval takes effect on the next iteration
        without a restart.
        """

    @abstractmethod
    def run_once(self) -> None:
        """One iteration. May raise; the loop logs and backs off."""

    def on_start(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Optional hook, before the first iteration.

        Concrete on purpose: most workers do not need it, and making it
        abstract would force every subclass to write an empty override.
        """

    def on_stop(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Optional hook, after the loop exits."""

    # -- Lifecycle -------------------------------------------------------------
    def start(self) -> None:
        """Start the thread. A second call is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        logger.info("worker started", extra={"worker": self.name})

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the loop to exit and wait for it.

        A worker that does not stop in time is left behind rather than blocking
        shutdown further. It is a daemon thread, so the interpreter will not wait
        on it — but the overrun is logged, because it means an iteration is stuck
        and that is worth knowing.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("worker did not stop in time",
                               extra={"worker": self.name, "timeout": timeout})
        logger.info("worker stopped", extra={"worker": self.name})

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wake(self) -> None:
        """Interrupt the current sleep so the next iteration starts now.

        Used by request handlers that need work done promptly — the manual
        Refresh button, or a newly queued write.
        """
        self._stop.set()
        self._stop.clear()

    # -- The loop --------------------------------------------------------------
    def _loop(self) -> None:
        try:
            self.on_start()
        except Exception:  # noqa: BLE001 - a failed hook must not kill the worker
            logger.exception("worker start hook failed", extra={"worker": self.name})

        while not self._stop.is_set():
            delay = self.interval_seconds
            try:
                self.run_once()
                self._consecutive_errors = 0
            except Exception:  # noqa: BLE001 - the loop outlives any one failure
                self._consecutive_errors += 1
                delay = min(
                    self.max_error_backoff,
                    self.error_backoff_base * self._consecutive_errors,
                )
                logger.exception(
                    "worker iteration failed",
                    extra={"worker": self.name,
                           "consecutive_errors": self._consecutive_errors,
                           "backoff_seconds": delay},
                )
            # Event.wait returns early when stop is set, so shutdown does not
            # have to wait out a long interval.
            self._stop.wait(delay)

        try:
            self.on_stop()
        except Exception:  # noqa: BLE001
            logger.exception("worker stop hook failed", extra={"worker": self.name})
