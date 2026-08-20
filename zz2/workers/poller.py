"""Poll worker: discovers changes made directly in the sheet.

Runs the cheap additive poll every interval, and the more expensive reconcile on a
longer cycle. Two cadences in one worker rather than two threads, because they
share the same rate-limit budget against Apps Script and must not overlap.

The work window, when enabled, pauses polling outside configured hours to spare
Apps Script quota. Disabled by default, so behaviour matches the monolith unless
an operator turns it on.
"""

from __future__ import annotations

import time

from zz2.core.logging import logger
from zz2.services.runtime_config import runtime_config
from zz2.services.sheet_sync import sheet_sync_service
from zz2.services.telemetry import telemetry_service
from zz2.settings import get_settings
from zz2.workers.base import BackgroundWorker


class PollWorker(BackgroundWorker):
    """Polls for new tickets and periodically reconciles the recent window."""

    name = "zz2-poller"

    def __init__(self) -> None:
        super().__init__()
        self._last_reconcile = 0.0

    @property
    def interval_seconds(self) -> float:
        # Read live, so a changed poll interval applies from the next iteration.
        return float(runtime_config.poll_interval)

    def run_once(self) -> None:
        if not get_settings().enable_appscript_sync:
            return
        if not runtime_config.within_work_window():
            return

        added = sheet_sync_service.poll()
        telemetry_service.record_poll(added)

        now = time.monotonic()
        if now - self._last_reconcile >= runtime_config.reconcile_interval:
            self._last_reconcile = now
            removed = sheet_sync_service.reconcile()
            telemetry_service.record_reconcile()
            if removed:
                logger.info("reconcile completed", extra={"removed": removed})


poll_worker = PollWorker()
