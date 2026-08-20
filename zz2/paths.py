"""Filesystem layout.

Single source of truth for every path the application touches. The monolith
scattered ``os.path.join(BASE_DIR, ...)`` calls across 5,000 lines; centralising
them here means a deployment can relocate state with one environment variable
and nothing else has to know.

Two roots, deliberately separate:

``APP_DIR``
    Read-only code and web assets shipped by a deploy. Never written at runtime.

``DATA_DIR``
    Everything mutable: state, uploads, logs, backups. On a VPS this is the only
    directory that needs write permission and the only one that needs backing up.

Keeping them apart is what makes the static-file allowlist in
:mod:`zz2.api.routers.pages` safe: the web root contains no secrets at all, so a
path-traversal bug cannot reach ``config.json`` the way it could when code,
secrets and pages all shared one folder.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR: Path = Path(__file__).resolve().parent.parent
"""Repository / installation root (read-only at runtime)."""

WEB_DIR: Path = APP_DIR / "web"
PAGES_DIR: Path = WEB_DIR / "pages"
STATIC_DIR: Path = WEB_DIR / "static"

DATA_DIR: Path = Path(os.environ.get("ZZ2_DATA_DIR") or (APP_DIR / "var")).resolve()
"""Mutable state root. Override with ``ZZ2_DATA_DIR`` (systemd/Docker do)."""

# -- State files ---------------------------------------------------------------
CONFIG_FILE = DATA_DIR / "config.json"
ROLES_FILE = DATA_DIR / "roles.json"
STATS_FILE = DATA_DIR / "stats.json"
ANALYTICS_FILE = DATA_DIR / "analytics.json"
PENDING_FILE = DATA_DIR / "pending_writes.json"
DEAD_LETTERS_FILE = DATA_DIR / "dead_letters.json"

# -- State directories ---------------------------------------------------------
LOGS_DIR = DATA_DIR / "logs"
DRAFTS_DIR = DATA_DIR / "drafts"
BACKUPS_DIR = DATA_DIR / "backups"
CACHE_DIR = DATA_DIR / "cache"
STORE_DIR = DATA_DIR / "store"
MEDIA_DIR = DATA_DIR / "media"

IMAGES_DIR = MEDIA_DIR / "images"
MANUALS_DIR = MEDIA_DIR / "manuals"

KITS_JSON = STORE_DIR / "kits.json"
TS_JSON = STORE_DIR / "troubleshooting.json"
BLUEPRINTS_JSON = STORE_DIR / "blueprints.json"
CATALOG_META_JSON = STORE_DIR / "catalog_meta.json"
ANALYTICS_VIEW_FILE = STORE_DIR / "analytics_view.json"
KIT_HISTORY_DIR = STORE_DIR / "kit_history"

SERVER_LOG = LOGS_DIR / "server.log"
AUDIT_LOG = LOGS_DIR / "audit.log"

#: Created at startup. Order is irrelevant; ``parents=True`` handles nesting.
RUNTIME_DIRS: tuple[Path, ...] = (
    DATA_DIR,
    LOGS_DIR,
    DRAFTS_DIR,
    BACKUPS_DIR,
    CACHE_DIR,
    STORE_DIR,
    MEDIA_DIR,
    IMAGES_DIR,
    MANUALS_DIR,
    KIT_HISTORY_DIR,
)


def ensure_runtime_dirs() -> None:
    """Create the mutable tree if it is missing.

    Directories are created ``0o750``: the service user reads and writes, its
    group reads, everyone else is shut out. The monolith relied on the NAS
    share's umask, which left state world-readable.
    """
    for directory in RUNTIME_DIRS:
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
