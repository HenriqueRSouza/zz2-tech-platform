"""System telemetry for the System Settings page.

Host and process metrics, store state and worker liveness. Read from ``/proc``,
which exists on the Linux VPS and not on a developer's macOS machine — every
reader therefore degrades to ``None`` rather than raising, so the page renders
locally with the host panel blank instead of returning a 500.

The security panel changed with the auth migration. It used to report locked
accounts, failing login attempts and any credentials still stored as plaintext —
all facts about a local credential store that no longer exists. Supabase owns
authentication now, so what is reported here is what *this* process can still see
and be wrong about: rate-limiter state, whether secrets are configured, and
whether the mode of the data directory is safe.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

from zz2.core.logging import logger
from zz2.paths import (
    ANALYTICS_FILE,
    DATA_DIR,
    DEAD_LETTERS_FILE,
    PENDING_FILE,
    ROLES_FILE,
    SERVER_LOG,
    STATS_FILE,
    STORE_DIR,
)
from zz2.repositories.tickets import TicketStoreRepository
from zz2.repositories.write_queue import DeadLetterRepository, PendingWriteRepository
from zz2.services.backup import backup_service
from zz2.services.runtime_config import runtime_config
from zz2.settings import get_settings

PROCESS_STARTED_AT = time.time()

#: Files reported on the System page, with their sizes and modification times.
TRACKED_FILES = (
    ("roles.json", ROLES_FILE),
    ("stats.json", STATS_FILE),
    ("analytics.json", ANALYTICS_FILE),
    ("pending_writes.json", PENDING_FILE),
    ("dead_letters.json", DEAD_LETTERS_FILE),
)

WORKER_THREAD_NAMES = ("zz2-poller", "zz2-write-behind", "zz2-scheduler")

RECENT_LOG_LINES = 40
"""Log lines included in the System page payload. Small on purpose: the payload
is polled every twelve seconds, and the full log is a separate endpoint."""

PENDING_QUEUE_WARN_THRESHOLD = 25
"""Queue depth that raises a health warning.

Normal steady state is zero to three. Twenty-five means the sheet has been
unreachable for minutes and someone should look before the retries reach their
limit and start dead-lettering.
"""


class TelemetryService:
    """Collects operational metrics."""

    def __init__(self) -> None:
        self._store = TicketStoreRepository()
        self._pending = PendingWriteRepository()
        self._dead = DeadLetterRepository()
        self._cpu_previous: tuple[float, float] | None = None
        self._lock = threading.Lock()

        # Timestamps the workers update. Held here rather than in the workers so
        # the System page has one place to read from and the workers stay free of
        # reporting concerns.
        self.last_poll_at: float = 0.0
        self.last_reconcile_at: float = 0.0
        self.last_nightly_date: str | None = None
        self.poll_added_total: int = 0

    # -- Aggregate -------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """The full System page payload."""
        settings = get_settings()
        now = time.time()
        store = self._store.raw()
        tickets = store.get("tickets") or []
        backups = backup_service.list()

        return {
            "ok": True,
            "now": now,
            "server": {
                "python": ".".join(str(part) for part in sys.version_info[:3]),
                "build": settings.build,
                "environment": settings.environment,
                "pid": os.getpid(),
                "started": PROCESS_STARTED_AT,
                "process_uptime_sec": now - PROCESS_STARTED_AT,
                "workers": {name: self._thread_alive(name) for name in WORKER_THREAD_NAMES},
            },
            "host": self._host_metrics(),
            "storage": self._storage_metrics(),
            "store": {
                "ticket_count": len(tickets),
                "generated": store.get("generated"),
                "file_size_kb": _size_kb(ANALYTICS_FILE),
                "last_nightly": self.last_nightly_date,
                "last_poll": self.last_poll_at or None,
                "last_reconcile": self.last_reconcile_at or None,
                "poll_added_total": self.poll_added_total,
                "pending_writes": self._pending.count(),
                "pending_deletes": len(self._pending.pending_deletes()),
                "dead_letters": self._dead.count(),
            },
            "config": {
                **runtime_config.as_dict(),
                "public_url": settings.public_url,
                "data_dir": str(DATA_DIR),
            },
            "data_files": [
                {"name": name, "size_kb": _size_kb(path), "mtime": _mtime(path)}
                for name, path in TRACKED_FILES
            ],
            "security": self.security_summary(),
            # Shaped as {count, recent} rather than a bare list: the Settings page
            # shows the count in a header and the rows in a table, and a bare list
            # gives it no place to read the total from.
            "backups": {
                "count": len(backups),
                "recent": [summary.to_api() for summary in backups],
            },
            # The tail of the operational log, so the page's log panel has content
            # without a second request. Capped small; /api/system/logs serves more.
            "recent_log": self.tail_log(SERVER_LOG, RECENT_LOG_LINES),
        }

    # -- Health ----------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """Cheap view of the write pipeline, for the periodic health log."""
        pending = self._pending.count()
        return {
            "pending": pending,
            "oldest_pending_age_sec": self._pending.oldest_age_seconds(),
            "dead": self._dead.count(),
            "tickets": len(self._store.rows()),
            "last_poll": self.last_poll_at or None,
            "last_reconcile": self.last_reconcile_at or None,
        }

    def health_warnings(self) -> list[str]:
        """Conditions that need a human.

        Separated from :meth:`health` so the log records a plain summary every
        hour and a warning line only when there is something to act on — the
        signal stays findable with ``grep``.
        """
        snapshot = self.health()
        warnings: list[str] = []
        if snapshot["dead"] > 0:
            warnings.append(f"{snapshot['dead']} dead-letter write(s) need attention")
        if snapshot["pending"] >= PENDING_QUEUE_WARN_THRESHOLD:
            age = snapshot["oldest_pending_age_sec"]
            age_text = f", oldest {int(age)}s old" if age else ""
            warnings.append(f"write queue backing up ({snapshot['pending']} pending{age_text})")
        return warnings

    # -- Security --------------------------------------------------------------
    def security_summary(self) -> dict[str, Any]:
        """What this process can report about its own security posture.

        Deliberately narrow. It reports configuration facts that are checkable
        here, and does not claim to describe account security — Supabase owns
        that, and a panel that looked authoritative while reading nothing would be
        worse than no panel.
        """
        settings = get_settings()
        return {
            "auth_provider": "supabase",
            "jwt_verification": "local" if settings.supabase_jwt_secret.get_secret_value()
                                else "remote",
            "https_enforced": settings.public_url.startswith("https://"),
            "rate_limit_enabled": settings.rate_limit_enabled,
            "cors_origins": list(settings.cors_origins),
            "secrets_configured": {
                "supabase_service_key": bool(settings.supabase_service_key.get_secret_value()),
                "supabase_jwt_secret": bool(settings.supabase_jwt_secret.get_secret_value()),
                "appscript_secret": bool(settings.appscript_secret.get_secret_value()),
            },
            "data_dir_mode": self._data_dir_mode(),
        }

    @staticmethod
    def _data_dir_mode() -> dict[str, Any]:
        """Permissions on the data directory.

        Surfaced because it is the one filesystem mistake that silently undoes
        everything else: a world-readable data directory on a shared VPS exposes
        state to every other account on the box, and nothing in the application
        would misbehave to reveal it.
        """
        try:
            mode = stat.S_IMODE(DATA_DIR.stat().st_mode)
        except OSError:
            return {"mode": None, "world_readable": None}
        return {"mode": f"{mode:04o}", "world_readable": bool(mode & stat.S_IROTH)}

    # -- Host ------------------------------------------------------------------
    def _host_metrics(self) -> dict[str, Any]:
        memory = _read_meminfo()
        total = memory.get("MemTotal", 0)
        available = memory.get(
            "MemAvailable",
            memory.get("MemFree", 0) + memory.get("Cached", 0) + memory.get("Buffers", 0),
        )
        used = (total - available) if total else 0

        try:
            load = [round(value, 2) for value in os.getloadavg()]
        except OSError:
            load = [0.0, 0.0, 0.0]

        return {
            "uptime_sec": _host_uptime(),
            "load": load,
            "mem_total_kb": total,
            "mem_used_kb": used,
            "mem_avail_kb": available,
            "mem_pct": round(100.0 * used / total, 1) if total else 0,
            "proc_rss_kb": _process_rss_kb(),
            "cpu_pct": self._cpu_percent(),
        }

    @staticmethod
    def _storage_metrics() -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(DATA_DIR)
        except OSError:
            return {"total": 0, "used": 0, "free": 0, "pct": 0}
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "pct": round(100.0 * usage.used / usage.total, 1) if usage.total else 0,
            "store_bytes": _directory_size(STORE_DIR),
        }

    def _cpu_percent(self) -> float | None:
        """Busy percentage since the previous call.

        Reported over the interval between two calls, which is the page's refresh
        period — more useful than an instantaneous reading. On the very first call
        there is no previous sample, so it takes a brief inline one rather than
        returning nothing.
        """
        current = _read_cpu_sample()
        if current is None:
            return None

        with self._lock:
            previous = self._cpu_previous
            if previous is None:
                time.sleep(0.25)
                second = _read_cpu_sample()
                if second is None:
                    return None
                previous, current = current, second
            self._cpu_previous = current

        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return None
        return round(min(100.0, max(0.0, 100.0 * (1.0 - idle_delta / total_delta))), 1)

    @staticmethod
    def _thread_alive(name: str) -> bool:
        return any(thread.name == name and thread.is_alive()
                   for thread in threading.enumerate())

    # -- Logs ------------------------------------------------------------------
    @staticmethod
    def tail_log(path: Path, lines: int = 40) -> list[str]:
        """Last ``lines`` lines of a log file.

        Reads only the final 64 KB rather than the whole file, which matters once
        a log has been running for weeks.
        """
        try:
            if not path.is_file():
                return []
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(max(0, size - 65536))
                data = handle.read()
            return data.decode("utf-8", "replace").splitlines()[-lines:]
        except OSError as exc:
            logger.warning("could not tail log", extra={"path": str(path),
                                                        "reason": str(exc)})
            return []

    # -- Worker callbacks ------------------------------------------------------
    def record_poll(self, added: int) -> None:
        self.last_poll_at = time.time()
        self.poll_added_total += added

    def record_reconcile(self) -> None:
        self.last_reconcile_at = time.time()

    def record_nightly(self) -> None:
        self.last_nightly_date = dt.date.today().isoformat()


# -- /proc readers -------------------------------------------------------------
def _read_meminfo() -> dict[str, int]:
    """``/proc/meminfo`` as kilobyte values. Empty off Linux."""
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                label, _, rest = line.partition(":")
                parts = rest.strip().split()
                if parts:
                    try:
                        values[label.strip()] = int(parts[0])
                    except ValueError:
                        continue
    except OSError:
        pass
    return values


def _process_rss_kb() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _host_uptime() -> float | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_cpu_sample() -> tuple[float, float] | None:
    """``(total_jiffies, idle_jiffies)`` from ``/proc/stat``.

    Idle includes iowait: time blocked on disk is not the CPU being busy, and
    counting it as busy would make every backup look like a CPU spike.
    """
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            fields = [float(value) for value in handle.readline().split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
        return sum(fields), idle
    except (OSError, ValueError, IndexError):
        return None


def _size_kb(path: Path) -> float | None:
    try:
        return round(path.stat().st_size / 1024.0, 1)
    except OSError:
        return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total


telemetry_service = TelemetryService()
