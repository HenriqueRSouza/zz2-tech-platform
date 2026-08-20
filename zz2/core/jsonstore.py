"""Durable JSON document storage.

The persistence primitive the whole repository layer is built on. Three problems
with the monolith's ``_load_json`` / ``_save_json`` pair are fixed here.

**One global lock.** Every state write in the monolith serialised behind a single
``threading.Lock``. Saving a ticket blocked a role edit, which blocked a stats
increment. Locks are now per file, so unrelated documents never contend.

**Temp files inside the web root.** ``_save_json`` wrote ``<path>.tmp.<pid>``
next to the real file, and the static handler's denylist did not cover that name.
Temp files are now written to a sibling directory outside anything served, and
cleaned up on failure.

**Silent corruption.** ``except: return default`` meant a truncated file looked
exactly like a missing one, and the next save happily overwrote whatever was
left. A file that fails to parse is now quarantined and logged before the
default is returned, so the data is recoverable and the operator finds out.

The atomic-write sequence is write → ``fsync`` → ``rename`` → ``fsync`` the
directory. The last step is the one usually forgotten: without it the rename can
still be lost on a power cut, which is exactly the failure mode a VPS reboot
reproduces.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from zz2.core.errors import StorageError
from zz2.core.logging import logger

T = TypeVar("T")

_FILE_MODE = 0o640
"""Owner read/write, group read. State must not be world-readable."""


class _LockRegistry:
    """Hands out one reentrant lock per resolved path.

    Reentrant because services legitimately nest: a kit save takes the kits lock
    and, inside that critical section, snapshots history which reads kits again.
    A plain ``Lock`` deadlocks there — and the monolith's workaround was a
    comment on ``_bump_reserved`` warning callers not to acquire ``_lock`` twice.
    """

    def __init__(self) -> None:
        self._locks: dict[Path, threading.RLock] = {}
        self._guard = threading.Lock()

    def get(self, path: Path) -> threading.RLock:
        key = path.resolve()
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock


_locks = _LockRegistry()


def _quarantine(path: Path) -> None:
    """Move an unparseable file aside so the next save cannot destroy it."""
    try:
        target = path.with_suffix(path.suffix + ".corrupt")
        counter = 1
        while target.exists():
            target = path.with_suffix(f"{path.suffix}.corrupt.{counter}")
            counter += 1
        path.rename(target)
        logger.error("quarantined unparseable state file", extra={"path": str(path),
                                                                 "moved_to": str(target)})
    except OSError as exc:
        logger.error("could not quarantine state file", extra={"path": str(path),
                                                              "reason": str(exc)})


def read_json(path: Path, default: T) -> Any | T:
    """Load a JSON document, returning ``default`` when it is absent.

    A *missing* file is normal and quiet. An *unparseable* file is not: it is
    quarantined and logged, because silently replacing real data with a default
    is how a transient disk error turns into permanent data loss.

    Raises:
        StorageError: The path is unusable or the read failed for a reason other
            than the file being absent.
    """
    try:
        lock = _locks.get(path)
    except (OSError, ValueError) as exc:
        raise StorageError(log_detail=f"resolve {path!r}: {exc}") from exc
    with lock:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, UnicodeDecodeError):
            _quarantine(path)
            return default
        except (OSError, ValueError) as exc:
            # ValueError covers an embedded NUL byte, which os.path.realpath
            # raises rather than reporting as an OSError. Every caller validates
            # its own path components, so reaching here means a bug — but it must
            # surface as a StorageError, not as an unhandled 500.
            raise StorageError(log_detail=f"read {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    """Atomically replace a JSON document.

    Raises:
        StorageError: The write did not land. Callers must treat this as a hard
            failure — the monolith returned ``False`` here and most call sites
            ignored it, so a full disk looked like a successful save.
    """
    try:
        lock = _locks.get(path)
    except (OSError, ValueError) as exc:
        raise StorageError(log_detail=f"resolve {path!r}: {exc}") from exc
    with lock:
        tmp_path: Path | None = None
        try:
            # Inside the guarded block: a read-only parent or a full disk fails
            # here, and every failure on this path must surface as a StorageError
            # rather than an unhandled OSError.
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            tmp_dir = path.parent / ".tmp"
            tmp_dir.mkdir(exist_ok=True, mode=0o700)
            tmp_path = tmp_dir / f"{path.name}.{os.getpid()}.{threading.get_ident()}"

            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, _FILE_MODE)
            os.replace(tmp_path, path)

            # Persist the rename itself. Without this the directory entry can be
            # lost on power failure even though the file contents were fsynced.
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, ValueError) as exc:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise StorageError(log_detail=f"write {path}: {exc}") from exc


@contextmanager
def mutate(path: Path, default: T) -> Iterator[Any]:
    """Read-modify-write a document while holding its lock.

    The mutable value is yielded; it is written back when the block exits
    normally. An exception propagates and nothing is written, so a half-applied
    change cannot reach disk.

    Example:
        >>> with mutate(STATS_FILE, {}) as stats:
        ...     stats.setdefault("ana", 0)
        ...     stats["ana"] += 1
    """
    lock = _locks.get(path)
    with lock:
        document = read_json(path, default)
        yield document
        write_json(path, document)


def file_mtime(path: Path) -> float | None:
    """Modification time, or ``None`` if the file does not exist.

    Used as a cache key by the analytics view: cheaper and more reliable than
    hashing a multi-megabyte document on every request.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None
