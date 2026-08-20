"""User administration.

Enforces the two-axis authorization model from :mod:`zz2.domain.permissions` over
the Supabase user directory. Every rule here exists to close a specific escalation
path, and each is annotated with the one it closes.

The invariant that matters most: **at least one active root account must always
exist**. Lose it and nobody can grant the permission needed to create another,
which is unrecoverable from inside the application.
"""

from __future__ import annotations

import re
from typing import Any

from zz2.core.errors import Conflict, NotFound, PermissionDenied, ValidationError
from zz2.core.logging import audit
from zz2.core.text import as_text
from zz2.domain.permissions import (
    PERMISSION_CATALOG,
    Permission,
    Principal,
    Role,
    assignable_roles,
    can_manage_rank,
    grantable_permissions,
    parse_permissions,
    sort_permissions,
)
from zz2.gateways.supabase import supabase_admin
from zz2.repositories.roles import RoleRepository

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

MIN_PASSWORD_LENGTH = 12
"""Raised from the monolith's 6.

Six characters was defensible for a LAN-only tool behind a login page nobody
outside the building could reach. These accounts are now reachable from the
internet through Supabase, where an offline-feasible guess against a short
password is the realistic attack.
"""


class UserService:
    """Lists and mutates accounts, subject to the role hierarchy."""

    def __init__(self, roles: RoleRepository | None = None) -> None:
        self._roles = roles or RoleRepository()

    # -- Reads -----------------------------------------------------------------
    def directory(self, actor: Principal) -> dict[str, Any]:
        """The full Users page payload."""
        roles = self._roles.all()
        role_map = {role.key: role for role in roles}
        accounts = supabase_admin.list_users()

        users = [self._describe(account, actor, role_map) for account in accounts]
        # Highest rank first, then by display name. Puts administrators at the top,
        # which is where someone auditing access looks first.
        users.sort(key=lambda user: (-user["role_rank"],
                                     (user["display"] or user["username"]).lower()))

        return {
            "ok": True,
            "actor_username": actor.username,
            "actor_role": actor.role_key,
            "actor_perms": sort_permissions(actor.granted),
            "can_manage_roles": actor.has(Permission.MANAGE_ROLES),
            "assignable_roles": assignable_roles(actor, roles),
            "roles": [role.to_dict() for role in sorted(roles, key=lambda r: -r.rank)],
            "permission_catalog": [
                {"key": info.key.value, "label": info.label, "desc": info.description}
                for info in PERMISSION_CATALOG
            ],
            "users": users,
        }

    def _describe(
        self, account: dict[str, Any], actor: Principal, role_map: dict[str, Role]
    ) -> dict[str, Any]:
        app_metadata = account.get("app_metadata") or {}
        user_metadata = account.get("user_metadata") or {}
        role_key = as_text(app_metadata.get("role")) or "tech"
        role = role_map.get(role_key)
        email = as_text(account.get("email"))

        return {
            "id": as_text(account.get("id")),
            "username": email,
            "display": as_text(user_metadata.get("display_name")
                               or user_metadata.get("full_name") or email),
            "role": role_key,
            "role_label": role.label if role else role_key,
            "role_rank": role.rank if role else 0,
            "active": not account.get("banned_until"),
            "permissions": sort_permissions(parse_permissions(app_metadata.get("permissions"))),
            "last_sign_in": account.get("last_sign_in_at"),
            "manageable": can_manage_rank(actor, role.rank if role else 0),
            "is_self": email.lower() == actor.username.lower(),
        }

    # -- Writes ----------------------------------------------------------------
    def create(
        self,
        actor: Principal,
        *,
        email: str,
        display: str,
        password: str,
        role: str,
        permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an account.

        Raises:
            ValidationError: Bad email, empty display name, or weak password.
            PermissionDenied: ``role`` is not one the actor may assign — the
                containment rule in
                :func:`~zz2.domain.permissions.assignable_roles`.
        """
        email = as_text(email).strip().lower()
        display = as_text(display).strip()

        if not EMAIL_PATTERN.match(email):
            raise ValidationError("Enter a valid email address.")
        if not display:
            raise ValidationError("A display name is required.")
        self._check_password(password)

        allowed_roles = assignable_roles(actor, self._roles.all())
        if role not in allowed_roles:
            raise PermissionDenied("You cannot create a user with that role.")

        granted = self._filter_grants(actor, permissions)
        account = supabase_admin.create_user(
            email=email, password=password, display=display,
            role=role, permissions=granted,
        )

        audit("user.created", actor=actor.username, target=email, role=role,
              permissions=granted)
        return {"ok": True, "username": email, "id": as_text(account.get("id"))}

    def update(
        self,
        actor: Principal,
        *,
        user_id: str,
        display: str | None = None,
        role: str | None = None,
        permissions: list[str] | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]:
        """Change an account's profile, authorization or active state."""
        account = self._require_account(user_id)
        target = self._describe(account, actor, self._roles.as_map())

        if not target["manageable"]:
            raise PermissionDenied("You cannot manage that user.")

        changes: dict[str, Any] = {}
        applied: list[str] = []

        if display is not None:
            text = as_text(display).strip()
            if not text:
                raise ValidationError("A display name cannot be empty.")
            changes["user_metadata"] = {**(account.get("user_metadata") or {}),
                                        "display_name": text}
            applied.append("display")

        new_role = target["role"]
        if role is not None and role != target["role"]:
            if role not in assignable_roles(actor, self._roles.all()):
                raise PermissionDenied("You cannot assign that role.")
            new_role = role
            applied.append("role")

        new_permissions = target["permissions"]
        if permissions is not None:
            new_permissions = self._filter_grants(actor, permissions)
            applied.append("permissions")

        if "role" in applied or "permissions" in applied:
            # app_metadata is replaced wholesale by Supabase, so role and
            # permissions always travel together.
            changes["app_metadata"] = {"role": new_role, "permissions": new_permissions}

        if active is not None and active != target["active"]:
            if not active:
                self._guard_last_root(target, action="deactivate")
            changes["ban_duration"] = "none" if active else supabase_admin.PERMANENT_BAN
            applied.append("active")

        if not applied:
            return {"ok": True, "changed": []}

        supabase_admin.update_user(user_id, changes)
        audit("user.updated", actor=actor.username, target=target["username"],
              changed=applied)
        return {"ok": True, "changed": applied}

    def set_password(self, actor: Principal, *, user_id: str, password: str) -> dict[str, Any]:
        """Reset an account's password.

        The current password is not required. An administrator resetting a
        colleague's password does not have it — this is a reset, not a change, and
        it is gated by ``manage_users`` plus rank.
        """
        account = self._require_account(user_id)
        target = self._describe(account, actor, self._roles.as_map())
        if not target["manageable"] and not target["is_self"]:
            raise PermissionDenied("You cannot change that user's password.")

        self._check_password(password)
        supabase_admin.set_password(user_id, password)

        audit("user.password_reset", actor=actor.username, target=target["username"])
        return {"ok": True, "username": target["username"]}

    def delete(self, actor: Principal, *, user_id: str) -> dict[str, Any]:
        """Delete an account permanently."""
        account = self._require_account(user_id)
        target = self._describe(account, actor, self._roles.as_map())

        if target["is_self"]:
            # Not a hierarchy rule — a usability one. Deleting your own account
            # mid-session leaves a live token belonging to nobody, and the operator
            # almost never means it.
            raise ValidationError("You cannot delete your own account.")
        if not target["manageable"]:
            raise PermissionDenied("You cannot delete that user.")

        self._guard_last_root(target, action="delete")
        supabase_admin.delete_user(user_id)

        audit("user.deleted", actor=actor.username, target=target["username"])
        return {"ok": True, "username": target["username"]}

    # -- Guards ----------------------------------------------------------------
    def _require_account(self, user_id: str) -> dict[str, Any]:
        """Fetch one account by id.

        Filtered from the full listing because the admin API's single-user
        endpoint is not consistently available across Supabase versions. The
        directory is small — tens of accounts — so the cost is irrelevant.
        """
        for account in supabase_admin.list_users():
            if as_text(account.get("id")) == user_id:
                return account
        raise NotFound("User not found.")

    def _guard_last_root(self, target: dict[str, Any], *, action: str) -> None:
        """Refuse to remove the last active root account.

        Checked against the live directory rather than a cached count: the whole
        point is to be correct at the moment of the change.
        """
        role = self._roles.by_key(target["role"])
        if role is None or not role.is_root:
            return

        role_map = self._roles.as_map()
        active_roots = 0
        for account in supabase_admin.list_users():
            if account.get("banned_until"):
                continue
            key = as_text((account.get("app_metadata") or {}).get("role"))
            candidate = role_map.get(key)
            if candidate is not None and candidate.is_root:
                active_roots += 1

        if active_roots <= 1:
            raise Conflict(
                f"You cannot {action} the last active super-admin. "
                "Create another one first."
            )

    @staticmethod
    def _check_password(password: str) -> None:
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise ValidationError(
                f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
            )

    @staticmethod
    def _filter_grants(actor: Principal, requested: list[str] | None) -> list[str]:
        """Reduce requested extra permissions to those the actor may grant.

        Filtered rather than rejected. The Users page sends the full checkbox
        state, which can legitimately include a permission the actor cannot grant
        because it came from the target's role — rejecting the whole request would
        make the form unusable, while granting it would be escalation.
        """
        wanted = parse_permissions(requested or [])
        return sort_permissions(wanted & grantable_permissions(actor))


user_service = UserService()
