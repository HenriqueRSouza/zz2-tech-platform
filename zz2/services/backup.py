"""Backups, restores and download bundles.

Scope changed with the move to a VPS. The monolith backed up ``*.py``, ``*.html``
and ``*.js`` alongside the state, because on the NAS the code *was* the
deployment — there was no other copy. Here git is the source of truth for code and
a deploy is reproducible, so backups cover only what cannot be recreated: the
state documents and the uploaded media.

Every snapshot carries a manifest listing each file with its size and SHA-256, so
a snapshot can be verified before it is trusted and a single file can be restored
without unpacking everything.

Two retention buckets. Full snapshots are kept per the configured count; the small
single-file ``pre_*`` snapshots taken before a risky rewrite are kept separately.
Sharing one bucket lets a burst of config edits evict every real backup, which is
precisely when you most want one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zz2.core.errors import NotFound, StorageError, ValidationError
from zz2.core.jsonstore import read_json, write_json
from zz2.core.logging import audit, logger
from zz2.paths import (
    BACKUPS_DIR,
    CACHE_DIR,
    DATA_DIR,
    DRAFTS_DIR,
    MEDIA_DIR,
    STORE_DIR,
)
from zz2.services.runtime_config import runtime_config

SINGLE_SNAPSHOT_KEEP = 10
HASH_CHUNK_BYTES = 65536

#: Never included. Transient, huge, or the backup directory itself — copying that
#: into a backup is how a snapshot grows to contain every previous snapshot.
EXCLUDED_DIRS = frozenset({"backups", "cache", "logs", ".tmp"})

#: Top-level state files worth preserving. An allowlist rather than "*.json": the
#: quarantine files :mod:`zz2.core.jsonstore` writes on corruption are not backup
#: material, and neither is anything a future feature drops in the data root.
STATE_FILES = ("config.json", "roles.json", "stats.json", "analytics.json",
               "pending_writes.json", "dead_letters.json")


@dataclass(frozen=True, slots=True)
class BackupSummary:
    """One snapshot, as listed on the System page."""

    name: str
    created: str | None
    label: str
    file_count: int
    total_bytes: int
    include_media: bool

    def to_api(self) -> dict[str, Any]:
        # `size` and `mtime` are aliases for `total_bytes` and `created`: the
        # Settings page's backup table reads those names, and renaming them there
        # would be churn for no gain.
        return {
            "name": self.name,
            "created": self.created,
            "mtime": self.created,
            "label": self.label,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "size": self.total_bytes,
            "include_media": self.include_media,
        }


class BackupService:
    """Creates, lists, restores and packages backups."""

    # -- Creating --------------------------------------------------------------
    def run(self, *, label: str = "auto") -> str:
        """Create a snapshot and prune old ones. Returns its name.

        Raises:
            StorageError: The snapshot could not be written.
        """
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = BACKUPS_DIR / stamp
        if destination.exists():
            stamp = f"{stamp}_{os.urandom(2).hex()}"
            destination = BACKUPS_DIR / stamp

        include_media = runtime_config.backup_include_media
        try:
            destination.mkdir(parents=True, mode=0o750)
            manifest_files: list[dict[str, Any]] = []
            total = 0

            for name in STATE_FILES:
                source = DATA_DIR / name
                if source.is_file():
                    shutil.copy2(source, destination / name)
                    total += self._record(manifest_files, name, source)

            directories = [STORE_DIR, DRAFTS_DIR]
            if include_media:
                directories.append(MEDIA_DIR)

            for source_dir in directories:
                if not source_dir.exists() or source_dir.name in EXCLUDED_DIRS:
                    continue
                relative_root = source_dir.relative_to(DATA_DIR)
                shutil.copytree(
                    source_dir,
                    destination / relative_root,
                    ignore=shutil.ignore_patterns(*EXCLUDED_DIRS, "*.corrupt", "*.part"),
                    dirs_exist_ok=True,
                )
                for path in source_dir.rglob("*"):
                    if path.is_file() and not self._excluded(path):
                        total += self._record(
                            manifest_files, str(path.relative_to(DATA_DIR)), path
                        )

            manifest = {
                "created": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "label": label,
                "ts": stamp,
                "data_dir": str(DATA_DIR),
                "include_media": include_media,
                "file_count": len(manifest_files),
                "total_bytes": total,
                "files": sorted(manifest_files, key=lambda item: item["path"]),
            }
            write_json(destination / "manifest.json", manifest)
        except OSError as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise StorageError(log_detail=f"backup {stamp}: {exc}") from exc

        self._prune()
        logger.info("backup completed",
                    extra={"name": stamp, "label": label,
                           "files": len(manifest_files), "bytes": total})
        audit("backup.created", name=stamp, label=label, files=len(manifest_files))
        return stamp

    def snapshot_file(self, path: Path) -> str | None:
        """Copy one file into a ``pre_<ts>`` snapshot before a risky rewrite."""
        if not path.exists():
            return None
        try:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            destination = BACKUPS_DIR / f"pre_{stamp}"
            destination.mkdir(parents=True, exist_ok=True, mode=0o750)
            shutil.copy2(path, destination / path.name)
            return destination.name
        except OSError as exc:
            logger.warning("pre-write snapshot failed",
                           extra={"path": str(path), "reason": str(exc)})
            return None

    # -- Listing ---------------------------------------------------------------
    def list(self, limit: int = 15) -> list[BackupSummary]:
        """Snapshots, newest first."""
        full, singles = self._buckets()
        summaries: list[BackupSummary] = []
        for name in reversed(full + singles):
            manifest = self.manifest(name)
            if manifest is None:
                continue
            summaries.append(BackupSummary(
                name=name,
                created=manifest.get("created"),
                label=str(manifest.get("label") or "unknown"),
                file_count=int(manifest.get("file_count") or 0),
                total_bytes=int(manifest.get("total_bytes") or 0),
                include_media=bool(manifest.get("include_media")),
            ))
            if len(summaries) >= limit:
                break
        return summaries

    def manifest(self, name: str) -> dict[str, Any] | None:
        """A snapshot's manifest, synthesised for snapshots that predate them.

        Older snapshots have no ``manifest.json``. Walking the directory produces
        an equivalent listing — without hashes, which the ``legacy`` flag records
        — so those backups remain listable, downloadable and restorable instead of
        becoming invisible.
        """
        root = self._resolve(name)
        if root is None:
            return None

        stored = root / "manifest.json"
        if stored.is_file():
            manifest = read_json(stored, None)
            if isinstance(manifest, dict):
                return manifest

        files: list[dict[str, Any]] = []
        total = 0
        for path in root.rglob("*"):
            if not path.is_file() or path.name == "manifest.json":
                continue
            size = path.stat().st_size
            total += size
            files.append({"path": str(path.relative_to(root)), "size": size, "sha256": ""})

        try:
            created = dt.datetime.fromtimestamp(root.stat().st_mtime, dt.UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        except OSError:
            created = None

        return {
            "created": created, "label": "legacy", "ts": root.name,
            "file_count": len(files), "total_bytes": total,
            "files": sorted(files, key=lambda item: item["path"]), "legacy": True,
        }

    # -- Restoring -------------------------------------------------------------
    def restore(self, name: str, wanted: list[str] | str, *, actor: str) -> dict[str, Any]:
        """Restore files from a snapshot into the data directory.

        The current state is snapshotted first, labelled ``pre-restore``, so
        restoring the wrong snapshot is itself undoable.

        Args:
            wanted: Relative paths to restore, or ``"all"``.

        Raises:
            NotFound: No such snapshot.
            ValidationError: Nothing valid to restore.
        """
        manifest = self.manifest(name)
        if manifest is None:
            raise NotFound("Backup not found.")
        root = self._resolve(name)
        if root is None:
            raise NotFound("Backup not found.")

        available = {entry["path"] for entry in manifest.get("files", ())}
        if wanted in ("all", ["all"]) or not wanted:
            requested = available
        else:
            requested = {path for path in wanted if path in available}

        targets = [path for path in sorted(requested) if self._safe_relative(path)]
        if not targets:
            raise ValidationError("No valid files to restore.")

        self.run(label="pre-restore")

        restored: list[str] = []
        failed: list[dict[str, str]] = []
        for relative in targets:
            source = root / relative
            target = DATA_DIR / relative
            try:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                staged = target.with_name(f".{target.name}.restore")
                shutil.copy2(source, staged)
                staged.replace(target)
                restored.append(relative)
            except OSError as exc:
                failed.append({"path": relative, "error": type(exc).__name__})

        audit("backup.restored", actor=actor, name=name,
              restored=len(restored), failed=len(failed))
        return {"ok": True, "restored": restored, "failed": failed, "count": len(restored)}

    def bundle(self, name: str) -> Path:
        """Package a snapshot as a zip in the cache directory.

        Raises:
            NotFound: No such snapshot.
            StorageError: The archive could not be written.
        """
        root = self._resolve(name)
        if root is None:
            raise NotFound("Backup not found.")

        output = CACHE_DIR / f"zz2-backup-{root.name}.zip"
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        archive.write(path, Path(root.name) / path.relative_to(root))
        except OSError as exc:
            output.unlink(missing_ok=True)
            raise StorageError(log_detail=f"backup bundle {name}: {exc}") from exc
        return output

    # -- Internals -------------------------------------------------------------
    @staticmethod
    def _record(manifest: list[dict[str, Any]], relative: str, source: Path) -> int:
        try:
            size = source.stat().st_size
        except OSError:
            size = 0
        manifest.append({
            "path": relative,
            "size": size,
            "sha256": BackupService._sha256(source),
        })
        return size

    @staticmethod
    def _sha256(path: Path) -> str:
        try:
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(HASH_CHUNK_BYTES):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except OSError:
            return ""

    @staticmethod
    def _excluded(path: Path) -> bool:
        if path.suffix in {".corrupt", ".part"}:
            return True
        return any(part in EXCLUDED_DIRS for part in path.parts)

    @staticmethod
    def _resolve(name: str) -> Path | None:
        """Resolve a snapshot name to a directory inside ``BACKUPS_DIR``.

        ``basename`` first, then a containment check on the resolved path. Both,
        because either alone is insufficient: ``basename`` alone would accept a
        symlink named innocuously, and containment alone would accept a nested
        path. ``name`` reaches here straight from a query parameter.
        """
        safe = os.path.basename((name or "").strip())
        if not safe or safe in {".", ".."}:
            return None
        candidate = (BACKUPS_DIR / safe).resolve()
        if not candidate.is_dir():
            return None
        if candidate != BACKUPS_DIR.resolve() and \
           BACKUPS_DIR.resolve() not in candidate.parents:
            return None
        return candidate

    @staticmethod
    def _safe_relative(relative: str) -> bool:
        """Whether a manifest path is safe to write under the data directory.

        Manifest contents are data too — a snapshot could be restored from an
        older build, or hand-edited — so a path from one is validated exactly as
        a request parameter would be.
        """
        cleaned = (relative or "").replace("\\", "/").strip("/")
        if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/"):
            return False
        if Path(cleaned).is_absolute():
            return False
        return cleaned.split("/", 1)[0] not in EXCLUDED_DIRS

    def _buckets(self) -> tuple[list[str], list[str]]:
        """``(full, singles)`` snapshot names, each oldest first."""
        full: list[str] = []
        singles: list[str] = []
        if not BACKUPS_DIR.is_dir():
            return full, singles
        for entry in BACKUPS_DIR.iterdir():
            if not entry.is_dir() or entry.name == "config":
                continue
            (singles if entry.name.startswith("pre_") else full).append(entry.name)
        full.sort(key=self._age_key)
        singles.sort(key=self._age_key)
        return full, singles

    @staticmethod
    def _age_key(name: str) -> float:
        """Sort key from the embedded timestamp, falling back to mtime.

        Name-based rather than mtime-based because copying or restoring a backup
        directory rewrites its mtime, which would make an old snapshot look new
        and survive pruning ahead of a genuinely recent one.
        """
        base = name[4:] if name.startswith("pre_") else name
        try:
            return time.mktime(time.strptime(base[:15], "%Y%m%d_%H%M%S"))
        except ValueError:
            try:
                return (BACKUPS_DIR / name).stat().st_mtime
            except OSError:
                return 0.0

    def _prune(self) -> None:
        keep = runtime_config.backup_keep
        full, singles = self._buckets()
        doomed = (full[:-keep] if keep > 0 else full) + singles[:-SINGLE_SNAPSHOT_KEEP]
        for name in doomed:
            shutil.rmtree(BACKUPS_DIR / name, ignore_errors=True)
        if doomed:
            logger.info("pruned old backups", extra={"removed": len(doomed)})


backup_service = BackupService()
