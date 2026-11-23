"""Per-user in-progress ticket drafts.

One file per user so a draft save never contends with another user's, and a
corrupt draft can only ever cost one person their unsaved form.

The filename is derived from the username through :func:`safe_filename`. That is
a security boundary, not tidiness: the username comes from the session's
``email`` claim, and an address like ``../../config`` would otherwise write
outside the drafts directory. The allowlist reduces it to one path component.
"""

from __future__ import annotations

from typing import Any

from zz2.core.jsonstore import read_json, write_json
from zz2.core.text import safe_filename
from zz2.paths import DRAFTS_DIR
from zz2.repositories.base import Repository


class DraftRepository(Repository):
    """Reads, writes and clears one draft document per user."""

    def exists(self) -> bool:
        return DRAFTS_DIR.exists()

    def _path_for(self, username: str) -> Any:
        return DRAFTS_DIR / f"{safe_filename(username, fallback='anonymous')}.json"

    def get(self, username: str) -> Any:
        """The stored draft, or ``None`` when there is none."""
        return read_json(self._path_for(username), None)

    def save(self, username: str, draft: Any) -> None:
        write_json(self._path_for(username), draft)

    def clear(self, username: str) -> bool:
        path = self._path_for(username)
        if path.exists():
            path.unlink(missing_ok=True)
            return True
        return False
