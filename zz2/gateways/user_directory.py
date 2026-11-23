"""User directory — accounts in the application database.

Replaces the Supabase Auth Admin API. Accounts live in ``techSupport.app_users``
on Azure Database for PostgreSQL. Two ways in:

* **Password.** scrypt hashes (``hashlib.scrypt``, standard library), compared in
  constant time. Used for staff accounts an administrator creates.
* **Microsoft.** No password at all: the account is matched by email or by the
  Entra object ID and linked on first sign-in. See :mod:`zz2.services.sso`.

The read methods return dictionaries in the shape the Supabase admin API used
(``id``, ``email``, ``app_metadata``, ``user_metadata``, ``banned_until``,
``last_sign_in_at``) so the Users page, the role service and SSO read accounts the
same way they always did. ``app_metadata`` is still the only source of
authorization: nothing a user can edit about themselves feeds into it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Any

from zz2.core.errors import NotConfigured, UpstreamError
from zz2.core.text import as_text
from zz2.gateways.database import as_json, db

# scrypt cost: 2**15 * 8 * 128 bytes = 32 MiB per hash, ~50 ms on a small VM.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

_COLUMNS = ("id, email, display_name, password_hash IS NOT NULL AS has_password, role, "
            "permissions, active, microsoft_oid, created_at, updated_at, last_sign_in_at")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                            p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM, dklen=32)
    encode = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${encode(salt)}${encode(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check. Unknown formats and null hashes simply fail."""
    if not stored:
        # Burn comparable time so "no such account" and "wrong password" are not
        # distinguishable by latency.
        hash_password(password)
        return False
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64),
                                n=int(n), r=int(r), p=int(p), maxmem=_SCRYPT_MAXMEM,
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """Database row → the account dictionary every caller reads."""
    def iso(value: Any) -> str | None:
        return value.isoformat() if value is not None and hasattr(value, "isoformat") else value

    permissions = row.get("permissions") or []
    return {
        "id": str(row["id"]),
        "email": as_text(row.get("email")),
        "app_metadata": {"role": as_text(row.get("role")) or "tech",
                         "permissions": permissions if isinstance(permissions, list) else []},
        "user_metadata": {"display_name": as_text(row.get("display_name"))},
        "banned_until": None if row.get("active", True) else "infinity",
        "last_sign_in_at": iso(row.get("last_sign_in_at")),
        "created_at": iso(row.get("created_at")),
        "has_password": bool(row.get("has_password")),
        "microsoft_linked": bool(row.get("microsoft_oid")),
    }


class UserDirectory:
    """CRUD over ``app_users``. Every mutating caller must already have passed a
    ``manage_users`` check and the rank comparison in :mod:`zz2.domain.permissions`."""

    PERMANENT_BAN = "infinity"
    """Kept for callers written against the Supabase API: any truthy
    ``ban_duration`` other than ``"none"`` deactivates the account."""

    @property
    def configured(self) -> bool:
        return db.configured

    def _require(self) -> None:
        if not db.configured:
            raise NotConfigured("User management is not configured (no database).")

    # -- Reads -------------------------------------------------------------------
    def list_users(self, **_ignored: Any) -> list[dict[str, Any]]:
        self._require()
        rows = db.fetch_all(f"SELECT {_COLUMNS} FROM app_users ORDER BY lower(email)")  # noqa: S608 - constant columns
        return [_shape(row) for row in rows]

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        self._require()
        try:
            row = db.fetch_one(f"SELECT {_COLUMNS} FROM app_users WHERE id = %s::uuid",  # noqa: S608
                               (user_id,))
        except UpstreamError as exc:
            if "invalid input syntax for type uuid" in (exc.log_detail or ""):
                return None
            raise
        return _shape(row) if row else None

    def find_by_email(self, email: str) -> dict[str, Any] | None:
        self._require()
        row = db.fetch_one(f"SELECT {_COLUMNS} FROM app_users WHERE lower(email) = lower(%s)",  # noqa: S608
                           (email.strip(),))
        return _shape(row) if row else None

    # -- Sign-in -----------------------------------------------------------------
    def authenticate(self, email: str, password: str) -> dict[str, Any] | None:
        """The account for a correct email + password, else ``None``.

        A deactivated account returns ``None`` too — the caller must not reveal
        which of the three conditions failed.
        """
        self._require()
        row = db.fetch_one(
            f"SELECT {_COLUMNS}, password_hash FROM app_users WHERE lower(email) = lower(%s)",  # noqa: S608
            (email.strip(),),
        )
        if not verify_password(password, row.get("password_hash") if row else None):
            return None
        if not row or not row.get("active", True):
            return None
        self.touch_sign_in(str(row["id"]))
        return _shape(row)

    def touch_sign_in(self, user_id: str, *, microsoft_oid: str | None = None) -> None:
        if microsoft_oid:
            db.execute("UPDATE app_users SET last_sign_in_at = now(), "
                       "microsoft_oid = coalesce(microsoft_oid, %s) WHERE id = %s::uuid",
                       (microsoft_oid, user_id))
        else:
            db.execute("UPDATE app_users SET last_sign_in_at = now() WHERE id = %s::uuid",
                       (user_id,))

    # -- Writes ------------------------------------------------------------------
    def create_user(self, *, email: str, password: str | None, display: str, role: str,
                    permissions: list[str], microsoft_oid: str | None = None) -> dict[str, Any]:
        self._require()
        try:
            row = db.fetch_one(
                f"INSERT INTO app_users (email, display_name, password_hash, role, permissions, "  # noqa: S608
                f"microsoft_oid) VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                (email.strip().lower(), display, hash_password(password) if password else None,
                 role, as_json(list(permissions)), microsoft_oid),
            )
        except UpstreamError as exc:
            if "app_users_email_key" in (exc.log_detail or ""):
                raise UpstreamError("An account with that email address already exists.",
                                    log_detail=exc.log_detail) from exc
            raise
        return _shape(row) if row else {}

    def update_user(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply a Supabase-style change set: ``user_metadata.display_name``,
        ``app_metadata.{role,permissions}``, ``ban_duration``, ``password``."""
        self._require()
        sets: list[str] = []
        params: list[Any] = []
        if "user_metadata" in changes:
            sets.append("display_name = %s")
            params.append(as_text((changes["user_metadata"] or {}).get("display_name")))
        if "app_metadata" in changes:
            meta = changes["app_metadata"] or {}
            sets += ["role = %s", "permissions = %s"]
            params += [as_text(meta.get("role")) or "tech",
                       as_json(list(meta.get("permissions") or []))]
        if "ban_duration" in changes:
            sets.append("active = %s")
            params.append(changes["ban_duration"] in (None, "", "none"))
        if changes.get("password"):
            sets.append("password_hash = %s")
            params.append(hash_password(changes["password"]))
        if not sets:
            return self.get_user(user_id) or {}
        sets.append("updated_at = now()")
        row = db.fetch_one(
            f"UPDATE app_users SET {', '.join(sets)} WHERE id = %s::uuid RETURNING {_COLUMNS}",  # noqa: S608
            (*params, user_id),
        )
        return _shape(row) if row else {}

    def set_active(self, user_id: str, *, active: bool) -> dict[str, Any]:
        return self.update_user(user_id, {"ban_duration": "none" if active else "infinity"})

    def set_password(self, user_id: str, password: str) -> dict[str, Any]:
        return self.update_user(user_id, {"password": password})

    def set_authorization(self, user_id: str, *, role: str,
                          permissions: list[str]) -> dict[str, Any]:
        return self.update_user(user_id, {"app_metadata": {"role": role,
                                                           "permissions": permissions}})

    def delete_user(self, user_id: str) -> None:
        self._require()
        db.execute("DELETE FROM app_users WHERE id = %s::uuid", (user_id,))


user_directory = UserDirectory()
