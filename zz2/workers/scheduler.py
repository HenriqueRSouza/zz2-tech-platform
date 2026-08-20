"""Scheduler worker: nightly maintenance and the periodic health line.

Nightly tasks run in a fixed order, and the order is the design:

1. **Backup** — first, so if anything later goes wrong there is a snapshot from
   *before* it.
2. **Auto-resolve** — mark stale tickets resolved in the sheet.
3. **Analytics rebuild** — re-export every ticket and rebuild the view, which
   picks up the resolutions from step 2 in the same pass.

Gated on a date rather than a timer. The monolith's ``_last_nightly != today``
check is kept because it is the property that matters: whatever happens — a
restart at 00:02, a clock adjustment, a long-running task — the nightly work runs
at most once per calendar day.
"""

from __future__ import annotations

import datetime as dt
import time

from zz2.core.errors import AppError
from zz2.core.logging import audit, logger
from zz2.services.analytics import analytics_service
from zz2.services.autoresolve import auto_resolve_service
from zz2.services.backup import backup_service
from zz2.services.runtime_config import runtime_config
from zz2.services.telemetry import telemetry_service
from zz2.workers.base import BackgroundWorker

TICK_SECONDS = 60.0
NIGHTLY_HOUR = 0
NIGHTLY_MINUTE_WINDOW = 5
"""Minutes after midnight during which the nightly run may start.

A window, not an instant: the loop ticks once a minute, so an exact comparison
would miss the run whenever a tick landed either side of 00:00.
"""

HEALTH_INTERVAL_SECONDS = 3600.0


class SchedulerWorker(BackgroundWorker):
    """Runs nightly maintenance and logs periodic health."""

    name = "zz2-scheduler"

    def __init__(self) -> None:
        super().__init__()
        self._last_nightly_date: str | None = None
        self._last_health = 0.0

    @property
    def interval_seconds(self) -> float:
        return TICK_SECONDS

    def run_once(self) -> None:
        self._maybe_run_nightly()
        self._maybe_log_health()

    # -- Nightly ---------------------------------------------------------------
    def _maybe_run_nightly(self) -> None:
        now = dt.datetime.now()
        today = now.date().isoformat()

        if now.hour != NIGHTLY_HOUR or now.minute >= NIGHTLY_MINUTE_WINDOW:
            return
        if self._last_nightly_date == today:
            return

        self._last_nightly_date = today
        logger.info("nightly tasks starting")
        audit("nightly.started", date=today)

        # Each step is independently guarded: a failed auto-resolve must not stop
        # the analytics rebuild, which is the step the Analytics page depends on.
        self._step("backup", lambda: backup_service.run(label="nightly"))

        if runtime_config.auto_resolve_enabled:
            self._step("auto-resolve",
                       lambda: auto_resolve_service.apply(actor="nightly").applied)

        self._step("analytics rebuild", analytics_service.rebuild_from_sheet)

        telemetry_service.record_nightly()
        logger.info("nightly tasks complete")
        audit("nightly.completed", date=today)

    @staticmethod
    def _step(label: str, action) -> None:  # noqa: ANN001 - any zero-arg callable
        try:
            result = action()
            logger.info("nightly step done", extra={"step": label, "result": result})
        except AppError as exc:
            logger.error("nightly step failed",
                         extra={"step": label, "code": exc.code,
                                "detail": exc.log_detail or ""})
        except Exception:  # noqa: BLE001 - one step must not abort the rest
            logger.exception("nightly step crashed", extra={"step": label})

    # -- Health ----------------------------------------------------------------
    def _maybe_log_health(self) -> None:
        """Log a health summary hourly, and a warning line only when warranted.

        Two separate lines so the routine summary stays greppable and a genuine
        problem stands out — the monolith's ``HEALTH`` / ``HEALTH WARNING`` split,
        which the runbook's triage steps depend on.
        """
        now = time.monotonic()
        if now - self._last_health < HEALTH_INTERVAL_SECONDS:
            return
        self._last_health = now

        snapshot = telemetry_service.health()
        logger.info("health", extra=snapshot)

        for warning in telemetry_service.health_warnings():
            logger.warning("health warning", extra={"detail": warning})


scheduler_worker = SchedulerWorker()
