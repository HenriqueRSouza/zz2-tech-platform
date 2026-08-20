"""Off-server mirror of the local kit catalogue.

Local files are authoritative; this pushes a copy to Supabase so the catalogue
survives loss of the VPS. One-directional by design — nothing here ever reads from
Supabase into local files, because that would let the mirror resurrect a kit an
administrator deleted.

The generation marker makes a partial upload safe. Every row in a snapshot carries
the same ``sync_generation``; rows from older generations are deleted *only after
every upsert in the batch has succeeded*. A transient failure therefore leaves
stale-but-complete remote data rather than a half-pruned table.

Triggered by :class:`~zz2.core.events.KitCatalogChanged` and debounced: a single
kit save writes three documents, and each write publishes an event. Uploading three
times would be wasteful and could interleave into an inconsistent remote state.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import threading
import time
from typing import Any

from zz2.core.errors import UpstreamError
from zz2.core.events import Event, KitCatalogChanged, bus
from zz2.core.logging import logger
from zz2.gateways.supabase import supabase
from zz2.paths import MANUALS_DIR
from zz2.repositories.kits import KitCatalogRepository
from zz2.settings import get_settings

DEBOUNCE_SECONDS = 2.0
"""Delay before uploading after a change.

Long enough to coalesce the writes of one editor action, short enough that the
mirror is effectively current. A save that lands during the window restarts it.
"""

HASH_CHUNK_BYTES = 1024 * 1024


class MirrorService:
    """Uploads coherent catalogue snapshots to Supabase."""

    def __init__(self, catalog: KitCatalogRepository | None = None) -> None:
        self._catalog = catalog or KitCatalogRepository()
        self._upload_lock = threading.Lock()
        self._timer_lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # -- Event wiring ----------------------------------------------------------
    def register(self) -> None:
        """Subscribe to catalogue changes. Called once during startup."""
        if not get_settings().enable_supabase_mirror:
            logger.info("supabase mirror disabled by configuration")
            return
        bus.subscribe(KitCatalogChanged, self._on_catalog_changed)
        logger.info("supabase mirror subscribed to catalogue changes")

    def _on_catalog_changed(self, event: Event) -> None:
        reason = getattr(event, "reason", "unknown")
        self.schedule(reason=reason)

    def schedule(self, *, reason: str = "manual", delay: float = DEBOUNCE_SECONDS) -> None:
        """Queue an upload, restarting the debounce window."""
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._run_scheduled, kwargs={"reason": reason})
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        """Cancel a pending upload. Called during shutdown."""
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _run_scheduled(self, *, reason: str) -> None:
        with self._timer_lock:
            self._timer = None
        try:
            self.upload(reason=reason)
        except UpstreamError:
            # Already logged by the gateway. A failed mirror is not an
            # application error: local data, which is authoritative, is intact.
            logger.warning("scheduled mirror upload failed", extra={"reason": reason})

    # -- Upload ----------------------------------------------------------------
    def upload(self, *, reason: str = "manual") -> dict[str, int]:
        """Push one coherent snapshot and prune older generations.

        Returns per-table row counts.

        Raises:
            UpstreamError: An upsert or the prune failed. Nothing is pruned in
                that case, so remote data is stale rather than incomplete.
        """
        with self._upload_lock:
            generation, batches = self._snapshot()

            for table, conflict_target, rows in batches:
                if rows:
                    supabase.upsert(table, rows, on_conflict=conflict_target)

            # Prune only after every upsert succeeded. Reverse dependency order
            # so a child table is emptied before its parent.
            for table in ("manual_assets", "troubleshooting", "blueprints", "kits"):
                supabase.delete_where(table, f"sync_generation=neq.{generation}")

            counts = {table: len(rows) for table, _conflict, rows in batches}
            logger.info("catalogue mirrored to supabase",
                        extra={"reason": reason, **counts})
            return counts

    def _snapshot(self) -> tuple[int, list[tuple[str, str, list[dict[str, Any]]]]]:
        """Build one consistent snapshot of every mirrored table.

        The generation is a millisecond timestamp — monotonic enough to order
        snapshots, and readable in the remote table when diagnosing a stuck
        mirror.
        """
        generation = int(time.time() * 1000)
        synced_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        stamp = {"sync_generation": generation, "synced_at": synced_at}

        kits, trees, blueprints = self._catalog.snapshot()

        kit_rows: list[dict[str, Any]] = []
        tree_rows: list[dict[str, Any]] = []
        manual_rows: list[dict[str, Any]] = []

        for key, value in kits.items():
            record = value if isinstance(value, dict) else {}
            kit_rows.append({
                "kit_key": key,
                "vehicle": record.get("vehicle", ""),
                "switch_button": record.get("switch_button", ""),
                "audio_method": record.get("audio_method", ""),
                "notes": record.get("notes", ""),
                "dip_image": record.get("dip_image", ""),
                "wiring_image": record.get("wiring_image", ""),
                "misc_image": record.get("misc_image", ""),
                "manual": record.get("manual", ""),
                "local_updated_at": record.get("updated_at"),
                **stamp,
            })
            tree_rows.append({"kit_key": key, "tree": trees.get(key) or [], **stamp})
            manual_rows.extend(self._manual_rows(key, stamp))

        blueprint_rows = [
            {
                "id": blueprint_id,
                "name": (record or {}).get("name", ""),
                "troubleshooting": (record or {}).get("troubleshooting") or [],
                "created_at": (record or {}).get("created_at"),
                "updated_at": (record or {}).get("updated_at"),
                **stamp,
            }
            for blueprint_id, record in blueprints.items()
        ]

        return generation, [
            ("kits", "kit_key", kit_rows),
            ("troubleshooting", "kit_key", tree_rows),
            ("blueprints", "id", blueprint_rows),
            ("manual_assets", "kit_key,filename", manual_rows),
        ]

    @staticmethod
    def _manual_rows(kit_key: str, stamp: dict[str, Any]) -> list[dict[str, Any]]:
        """Metadata for a kit's manual files.

        File *metadata* only — size and SHA-256, never contents. The mirror
        records which manuals exist and whether the local copy still matches, so a
        restore knows what to look for. Uploading tens of megabytes of PDF to a
        metadata table would be the wrong tool.
        """
        directory = MANUALS_DIR / kit_key
        if not directory.is_dir():
            return []

        rows: list[dict[str, Any]] = []
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            digest = ""
            size = 0
            try:
                hasher = hashlib.sha256()
                with entry.open("rb") as handle:
                    while chunk := handle.read(HASH_CHUNK_BYTES):
                        hasher.update(chunk)
                digest = hasher.hexdigest()
                size = entry.stat().st_size
            except OSError as exc:
                logger.warning("could not hash manual",
                               extra={"kit": kit_key, "file": entry.name,
                                      "reason": str(exc)})
            rows.append({
                "kit_key": kit_key,
                "filename": entry.name,
                "local_path": f"manuals/{kit_key}/{entry.name}",
                "size_bytes": size,
                "sha256": digest,
                **stamp,
            })
        return rows


mirror_service = MirrorService()
