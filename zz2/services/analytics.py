"""Analytics view: build, cache and serve.

Consolidating thousands of free-text labels with fuzzy matching is expensive —
seconds of CPU, not milliseconds. Doing it per request would make the Analytics
page the most costly endpoint on the server and a trivial denial of service.

Three tiers instead:

**Store** (``analytics.json``) — every ticket, raw. The source of truth locally.

**View** (``store/analytics_view.json``) — the consolidated result. Rebuilt in the
background when the store changes.

**Served payload** — the view with tickets created since the last build folded in
by a cheap incremental pass, memoised on both files' modification times.

Two locks, for two different reasons. The *compute* lock ensures only one
consolidation runs at a time; a background caller that finds it held gives up
rather than queueing, which is what stops a burst of page loads from stacking
CPU-bound rebuilds. The *serve* lock guards the much cheaper memoised merge.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from zz2.core.errors import UpstreamError
from zz2.core.jsonstore import read_json, write_json
from zz2.core.logging import logger
from zz2.core.text import as_text
from zz2.gateways.appscript import appscript
from zz2.paths import ANALYTICS_VIEW_FILE
from zz2.repositories.tickets import TicketStoreRepository
from zz2.services.normalization import (
    canonical_index,
    compute_analytics,
    delta_row,
    status_bucket,
)


class AnalyticsService:
    """Owns the analytics view lifecycle."""

    def __init__(self, store: TicketStoreRepository | None = None) -> None:
        self._store = store or TicketStoreRepository()
        self._compute_lock = threading.Lock()
        self._serve_lock = threading.Lock()
        self._serve_cache: tuple[tuple[float | None, float | None], str] | None = None

    # -- Building --------------------------------------------------------------
    def build_view(self, *, background: bool = False) -> dict[str, Any] | None:
        """Recompute the consolidated view and persist it.

        Args:
            background: When true, return ``None`` immediately if a build is
                already running instead of waiting. Request-triggered rebuilds
                pass true; an explicit operator rebuild passes false and waits.

        Returns:
            The built view, or ``None`` if skipped or the store is unusable.
        """
        acquired = self._compute_lock.acquire(blocking=not background)
        if not acquired:
            logger.debug("analytics view build already in progress; skipped")
            return None
        try:
            document = self._store.raw()
            rows = document.get("tickets")
            if not isinstance(rows, list):
                return None

            view = compute_analytics(rows, document.get("generated"))
            view["source_generated"] = document.get("generated")
            view["source_count"] = document.get("count", len(rows))
            write_json(ANALYTICS_VIEW_FILE, view)
            logger.info("analytics view built", extra={"rows": view.get("count", 0)})
            return view
        finally:
            self._compute_lock.release()

    def build_view_async(self) -> None:
        """Kick off a background build and return immediately."""
        thread = threading.Thread(
            target=lambda: self.build_view(background=True),
            name="analytics-view-build",
            daemon=True,
        )
        thread.start()

    def ensure_view(self) -> None:
        """Build the view once at startup if it is missing but the store exists."""
        view = read_json(ANALYTICS_VIEW_FILE, None)
        if isinstance(view, dict) and isinstance(view.get("rows"), list):
            return
        if self._store.exists():
            self.build_view_async()

    # -- Serving ---------------------------------------------------------------
    def payload(self) -> str | None:
        """The served analytics JSON, or ``None`` when no view exists yet.

        Returns pre-serialised JSON rather than a dict. The payload is megabytes
        of rows that are identical between requests, so serialising once per
        change instead of once per request removes the dominant cost from the
        endpoint.
        """
        view_mtime = _mtime(ANALYTICS_VIEW_FILE)
        if view_mtime is None:
            return None
        cache_key = (self._store.mtime(), view_mtime)

        with self._serve_lock:
            if self._serve_cache is not None and self._serve_cache[0] == cache_key:
                return self._serve_cache[1]

            base = read_json(ANALYTICS_VIEW_FILE, None)
            if not (isinstance(base, dict) and isinstance(base.get("rows"), list)):
                return None

            merged = self._merge_recent(base)
            serialised = json.dumps(merged, ensure_ascii=False, default=str)
            self._serve_cache = (cache_key, serialised)
            return serialised

    def _merge_recent(self, base: dict[str, Any]) -> dict[str, Any]:
        """Fold tickets newer than the view into it, without reconsolidating.

        Two distinct cases, and conflating them was a real bug source:

        * A ticket already in the view whose *status* changed — patch the status
          in place. Its labels were consolidated in the last full build and must
          not be recomputed, or the row's vehicle could flip to a different
          canonical spelling between two page loads.
        * A ticket the view has never seen — project it with
          :func:`~zz2.services.normalization.delta_row`, reusing existing
          canonical labels where its normalised key already matches one.
        """
        rows: list[dict[str, Any]] = base["rows"]
        store_rows = self._store.rows()
        if not store_rows:
            return base

        index = {row["ticket"]: position
                 for position, row in enumerate(rows) if row.get("ticket")}
        vehicles, clients, techs = canonical_index(rows)

        merged = list(rows)
        appended = updated = 0

        for row in store_rows:
            ticket_id = as_text(row.get("ticket") or row.get("ticketNum"))
            position = index.get(ticket_id) if ticket_id else None

            if position is not None:
                canon = status_bucket(row.get("status") or row.get("Status"))
                existing = merged[position]
                if existing.get("canon") != canon:
                    patched = dict(existing)
                    patched["canon"] = canon
                    patched["status"] = as_text(row.get("status") or row.get("Status")).strip()
                    merged[position] = patched
                    updated += 1
                continue

            projected = delta_row(row, vehicles, clients, techs)
            if projected is not None:
                merged.append(projected)
                appended += 1

        if not (appended or updated):
            return base

        result = dict(base)
        result["rows"] = merged
        result["count"] = len(merged)
        result["generated"] = self._store.generated_at() or base.get("generated")
        result["live_appended"] = appended
        result["live_updated"] = updated
        return result

    def invalidate_serve_cache(self) -> None:
        """Drop the memoised payload. Called when the store changes."""
        with self._serve_lock:
            self._serve_cache = None

    # -- Full rebuild from the sheet -------------------------------------------
    def rebuild_from_sheet(self) -> int:
        """Re-export every ticket from the sheet and replace the local store.

        The nightly full pass. Returns the ticket count.

        Raises:
            UpstreamError: The export failed or returned an unusable shape. The
                existing store is left untouched — replacing thousands of rows
                with a partial or empty export is worse than serving stale data.
        """
        parsed = appscript.call("export")

        if isinstance(parsed, list):
            rows = parsed
        elif isinstance(parsed, dict):
            rows = parsed.get("tickets") or parsed.get("results") or []
        else:
            raise UpstreamError(log_detail="sheet export returned neither list nor object")

        if not isinstance(rows, list):
            raise UpstreamError(log_detail="sheet export 'tickets' was not a list")

        self._store.replace_all(rows, source="sheet")
        logger.info("ticket store rebuilt from sheet", extra={"tickets": len(rows)})
        self.build_view()
        self.invalidate_serve_cache()
        return len(rows)


def _mtime(path: Any) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


analytics_service = AnalyticsService()
