"""Kit version snapshots.

Every kit save writes the *previous* state to ``store/kit_history/<key>/`` first,
so an accidental overwrite in the Kit Editor is recoverable. Snapshots are capped
per kit; the oldest are pruned.

Filenames are timestamps, which makes lexicographic order chronological order —
useful, and the reason a collision suffix is appended rather than a random name:
two saves within the same second must still sort correctly relative to each
other.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from zz2.core.jsonstore import read_json, write_json
from zz2.core.logging import logger
from zz2.core.text import safe_filename, safe_key
from zz2.paths import KIT_HISTORY_DIR
from zz2.repositories.base import Repository

MAX_SNAPSHOTS_PER_KIT = 30


class KitHistoryRepository(Repository):
    """Point-in-time copies of a kit record and its troubleshooting tree."""

    def exists(self) -> bool:
        return KIT_HISTORY_DIR.exists()

    def _dir_for(self, key: str) -> Path:
        validated = safe_key(key)
        if not validated:
            raise ValueError(f"Invalid kit key: {key!r}")
        return KIT_HISTORY_DIR / validated

    def snapshot(self, key: str, kit: Any, tree: list[dict[str, Any]]) -> str | None:
        """Store the current state and prune old versions.

        Returns the version label, or ``None`` if the snapshot could not be
        written. A failure here must not abort the save that triggered it: losing
        an undo point is worse than nothing, but far better than refusing the
        user's edit.
        """
        try:
            directory = self._dir_for(key)
            directory.mkdir(parents=True, exist_ok=True, mode=0o750)

            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = directory / f"{stamp}.json"
            suffix = 1
            while path.exists():
                path = directory / f"{stamp}-{suffix}.json"
                suffix += 1

            write_json(path, {"saved_at": stamp, "kit": kit, "troubleshooting": tree})
            self._prune(directory)
            return path.stem
        except (OSError, ValueError) as exc:
            logger.warning("kit snapshot failed", extra={"kit": key, "reason": str(exc)})
            return None

    def versions(self, key: str) -> list[str]:
        """Version labels, newest first."""
        try:
            directory = self._dir_for(key)
        except ValueError:
            return []
        if not directory.is_dir():
            return []
        return sorted(
            (path.stem for path in directory.glob("*.json")),
            reverse=True,
        )

    def load(self, key: str, version: str) -> dict[str, Any] | None:
        """Read one snapshot, or ``None`` if it does not exist.

        ``version`` arrives from the client, so it is reduced to a single safe
        filename component before being joined to the path.
        """
        try:
            directory = self._dir_for(key)
        except ValueError:
            return None
        label = safe_filename(version, fallback="")
        if not label:
            return None
        path = directory / f"{label}.json"
        if not path.is_file():
            return None
        return read_json(path, None)

    def rename(self, old_key: str, new_key: str) -> None:
        """Follow a kit key rename so its history is not orphaned."""
        try:
            source = self._dir_for(old_key)
            target = self._dir_for(new_key)
        except ValueError:
            return
        if source.is_dir() and not target.exists():
            source.rename(target)

    def _prune(self, directory: Path) -> None:
        snapshots = sorted(directory.glob("*.json"))
        for stale in snapshots[:-MAX_SNAPSHOTS_PER_KIT]:
            stale.unlink(missing_ok=True)
