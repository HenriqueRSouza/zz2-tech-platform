"""Automatic resolution of stale tickets.

A ticket that is blank or "waiting for customer feedback" and older than the
configured threshold is marked Resolved in the sheet. Runs nightly, and can be
triggered from the System page.

The age guard applies to blank tickets too. A brand-new ticket is briefly blank
while a technician is typing, and resolving it out from under them would be worse
than leaving stale rows.

Work is chunked. Apps Script has execution limits, so this asks for small batches
and loops. Every batch is idempotent — it selects candidates by their current
state — so a failure mid-run is safe to re-run and picks up where it stopped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from zz2.core.errors import UpstreamError
from zz2.core.logging import audit, logger
from zz2.gateways.appscript import appscript
from zz2.services.runtime_config import runtime_config

CHUNK_SIZE = 50
"""Tickets per Apps Script call. Small enough to stay well inside the script's
execution limit even when each write is slow."""

MAX_CHUNKS = 500
"""Safety bound: 500 x 50 = 25,000 tickets. Guards against a loop that never
reports progress."""

INTER_CHUNK_PAUSE = 0.2
"""Brief pause between calls, to stay under Apps Script's rate limits."""


@dataclass(slots=True)
class AutoResolveResult:
    """Outcome of a run."""

    applied: int = 0
    remaining: int = 0
    candidates: int | None = None
    done: bool = False
    partial: bool = False
    error: str | None = None

    def to_api(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": True,
            "applied": self.applied,
            "remaining": self.remaining,
            "done": self.done,
        }
        if self.candidates is not None:
            payload["candidates"] = self.candidates
        if self.partial:
            payload["partial"] = True
        if self.error:
            payload["error"] = self.error
        return payload


class AutoResolveService:
    """Dry-runs and applies auto-resolution."""

    def preview(self, days: int | None = None) -> dict[str, Any]:
        """Count what *would* be resolved, changing nothing."""
        threshold = days if days is not None else runtime_config.auto_resolve_days
        result = appscript.call("autoresolve",
                                {"days": int(threshold), "apply": "false", "max": 0})
        return result if isinstance(result, dict) else {"ok": True}

    def apply(
        self,
        days: int | None = None,
        *,
        time_budget: float | None = None,
        actor: str = "system",
    ) -> AutoResolveResult:
        """Resolve stale tickets in chunks.

        Args:
            days: Age threshold. Defaults to the configured value.
            time_budget: Wall-clock seconds to spend. The System page passes a
                short budget and reports ``remaining`` so the request returns
                promptly; the nightly job passes ``None`` and runs to completion.
            actor: Who triggered it, for the audit record.
        """
        threshold = days if days is not None else runtime_config.auto_resolve_days
        result = AutoResolveResult()
        started = time.monotonic()

        for _chunk in range(MAX_CHUNKS):
            try:
                response = appscript.call(
                    "autoresolve",
                    {"days": int(threshold), "apply": "true", "max": CHUNK_SIZE},
                )
            except UpstreamError as exc:
                # Stop cleanly. Idempotent, so a re-run resumes; reporting partial
                # progress is more useful than discarding what was applied.
                logger.warning("auto-resolve chunk failed",
                               extra={"applied": result.applied})
                result.partial = True
                result.error = exc.code
                break

            data = response if isinstance(response, dict) else {}
            if result.candidates is None:
                result.candidates = int(data.get("candidates") or 0)

            applied_now = int(data.get("applied") or 0)
            result.applied += applied_now
            result.remaining = int(data.get("remaining") or 0)

            if applied_now == 0 or result.remaining <= 0:
                result.done = True
                result.remaining = 0
                break

            if time_budget is not None and (time.monotonic() - started) >= time_budget:
                break

            time.sleep(INTER_CHUNK_PAUSE)

        if result.applied:
            audit("tickets.auto_resolved", actor=actor, applied=result.applied,
                  days=threshold, done=result.done)
        return result


auto_resolve_service = AutoResolveService()
