"""The permission catalogue and the rules of the role hierarchy.

Pure domain logic: no I/O, no framework, no persistence. Everything here is a
function of values passed in, which is what makes the privilege-escalation rules
testable in isolation — they are the part of an authorization system that must be
provably right, and in the monolith they were interleaved with ``_load_json``
calls that made them awkward to exercise.

Two independent axes control what an actor may do:

**Permissions** are capabilities (``manage_users``, ``kit_editor``). They gate
*features*.

**Rank** is a number that orders roles. It gates *who you may act upon*: you can
only manage principals below your own rank. Rank is what stops an admin from
editing a super-admin, which no permission check alone can express.

The escalation rule that matters most is in :func:`assignable_roles`: an actor may
only grant a role whose permission set is a subset of their own. Without it, an
admin holding ``manage_users`` but not ``system_settings`` could mint a role that
*does* have ``system_settings``, assign it to themselves, and be root.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final


class Permission(StrEnum):
    """Every capability on the site.

    A ``StrEnum`` so the wire format stays the plain strings the existing
    ``roles.json`` and Supabase ``app_metadata`` already contain, while typos
    become import-time errors instead of silently-never-granted permissions.
    """

    TICKETS = "tickets"
    CREATE_TICKETS = "create_tickets"
    KIT_EDITOR = "kit_editor"
    ANALYTICS = "analytics"
    DASHBOARD = "dashboard"
    MANAGE_USERS = "manage_users"
    MANAGE_ROLES = "manage_roles"
    SYSTEM_SETTINGS = "system_settings"


@dataclass(frozen=True, slots=True)
class PermissionInfo:
    """Catalogue entry, rendered by the Users and Roles pages."""

    key: Permission
    label: str
    description: str


PERMISSION_CATALOG: Final[tuple[PermissionInfo, ...]] = (
    PermissionInfo(Permission.TICKETS, "Tickets view",
                   "Open the Tickets spreadsheet and search ticket history."),
    PermissionInfo(Permission.CREATE_TICKETS, "Create & edit tickets",
                   "Create new support tickets and edit existing ones from the tool."),
    PermissionInfo(Permission.KIT_EDITOR, "Kit editor",
                   "Create and edit kits, DIP/wiring images, and troubleshooting trees."),
    PermissionInfo(Permission.ANALYTICS, "Analytics dashboard",
                   "View ticket trends, kit trouble scores, and tech performance."),
    PermissionInfo(Permission.DASHBOARD, "Admin dashboard",
                   "Open the admin dashboard and its at-a-glance ticket stats."),
    PermissionInfo(Permission.MANAGE_USERS, "Manage users",
                   "Create, edit, deactivate, and delete user accounts."),
    PermissionInfo(Permission.MANAGE_ROLES, "Manage roles",
                   "Create roles and decide which permissions each role grants."),
    PermissionInfo(Permission.SYSTEM_SETTINGS, "System settings",
                   "Server status, backups, logs, and system configuration."),
)

ALL_PERMISSIONS: Final[frozenset[Permission]] = frozenset(Permission)

#: Catalogue order, used to render permission lists deterministically.
_CATALOG_ORDER: Final[dict[Permission, int]] = {
    info.key: index for index, info in enumerate(PERMISSION_CATALOG)
}

ROOT_RANK: Final[int] = 100
"""Rank of the built-in super-admin role. Nothing may be created above it."""

ROLE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


@dataclass(frozen=True, slots=True)
class Role:
    """A role definition as stored in ``roles.json``.

    Attributes:
        key: Stable identifier referenced by user records.
        label: Display name.
        rank: Position in the management hierarchy; higher outranks lower.
        permissions: Granted capabilities. Ignored when ``is_root`` is set.
        is_system: Built in; cannot be deleted.
        is_root: Holds every permission, present and future, and may manage
            anyone including rank peers.
    """

    key: str
    label: str
    rank: int
    permissions: frozenset[Permission] = frozenset()
    is_system: bool = False
    is_root: bool = False

    @property
    def effective_permissions(self) -> frozenset[Permission]:
        """What this role actually grants.

        A root role resolves to :data:`ALL_PERMISSIONS` rather than a stored
        list, so adding a permission to the catalogue grants it to super-admins
        automatically. The monolith stored the full list on disk and needed
        repair logic in ``_load_roles`` to re-sync it after every change.
        """
        return ALL_PERMISSIONS if self.is_root else self.permissions

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "rank": self.rank,
            "permissions": sort_permissions(self.effective_permissions),
            "system": self.is_system,
            "root": self.is_root,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Role:
        """Build a role from stored JSON, dropping unknown permission keys.

        Tolerant by design: a role written by an older build that referenced a
        since-removed permission must still load, minus that permission.
        """
        return cls(
            key=str(raw.get("key") or ""),
            label=str(raw.get("label") or raw.get("key") or ""),
            rank=int(raw.get("rank") or 0),
            permissions=parse_permissions(raw.get("permissions")),
            is_system=bool(raw.get("system")),
            is_root=bool(raw.get("root")),
        )


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller.

    Deliberately not called "User": it may be backed by a Supabase identity, and
    it carries only what authorization needs. Handlers receive a ``Principal``
    and never see a raw token or a credentials record.

    Attributes:
        subject: Stable identity (Supabase ``sub`` claim).
        username: Login identifier, an email address under Supabase Auth.
        display: Human-readable name for audit lines and the UI.
        role_key: Role this principal holds.
        extra_permissions: Per-user additive grants, on top of the role.
        granted: Resolved permission set, computed once at authentication.
        rank: Resolved rank of ``role_key``.
    """

    subject: str
    username: str
    display: str
    role_key: str
    extra_permissions: frozenset[Permission] = frozenset()
    granted: frozenset[Permission] = frozenset()
    rank: int = 0
    is_root: bool = False
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def has(self, permission: Permission) -> bool:
        return permission in self.granted

    def has_any(self, *permissions: Permission) -> bool:
        return any(p in self.granted for p in permissions)

    def to_public_dict(self) -> dict[str, Any]:
        """Shape the frontend's ``/verify`` response expects."""
        return {
            "username": self.username,
            "display": self.display,
            "role": self.role_key,
            "rank": self.rank,
            "permissions": sort_permissions(self.granted),
            "root": self.is_root,
        }


# -- Parsing / ordering --------------------------------------------------------
def parse_permissions(raw: object) -> frozenset[Permission]:
    """Convert stored or client-supplied permission data to a validated set.

    Unknown keys are dropped silently rather than rejected: this runs on data
    from Supabase ``app_metadata``, which is edited outside this application, and
    a stray value there must not lock a user out of the whole site.
    """
    if not isinstance(raw, list | tuple | set | frozenset):
        return frozenset()
    valid: set[Permission] = set()
    for item in raw:
        try:
            valid.add(Permission(str(item)))
        except ValueError:
            continue
    return frozenset(valid)


def sort_permissions(permissions: frozenset[Permission] | set[Permission]) -> list[str]:
    """Permissions as strings in catalogue order (stable API responses)."""
    return [p.value for p in sorted(permissions, key=lambda p: _CATALOG_ORDER.get(p, 99))]


def builtin_roles() -> tuple[Role, ...]:
    """Seed definitions for the three system roles."""
    return (
        Role(key="super_admin", label="Super Admin", rank=ROOT_RANK,
             permissions=ALL_PERMISSIONS, is_system=True, is_root=True),
        Role(key="admin", label="Admin", rank=50, is_system=True,
             permissions=frozenset({
                 Permission.TICKETS, Permission.CREATE_TICKETS, Permission.KIT_EDITOR,
                 Permission.ANALYTICS, Permission.DASHBOARD, Permission.MANAGE_USERS,
                 Permission.MANAGE_ROLES,
             })),
        Role(key="tech", label="Tech", rank=10, is_system=True,
             permissions=frozenset({
                 Permission.TICKETS, Permission.CREATE_TICKETS, Permission.DASHBOARD,
             })),
    )


# -- Hierarchy rules -----------------------------------------------------------
def can_manage_rank(actor: Principal, target_rank: int) -> bool:
    """May ``actor`` act on a principal or role at ``target_rank``?

    Requires ``manage_users`` and a strictly higher rank. Root is exempt from the
    rank comparison so a super-admin can manage peers — otherwise the last two
    super-admins could never adjust each other.
    """
    if not actor.has(Permission.MANAGE_USERS):
        return False
    if actor.is_root:
        return True
    return target_rank < actor.rank


def can_edit_role(actor: Principal, role: Role) -> bool:
    """May ``actor`` modify or delete this role *definition*?

    Root roles are immutable to everyone. Allowing an edit would let a
    super-admin lower ``ROOT_RANK`` or strip permissions from the only role that
    can undo the change — an irrecoverable state.
    """
    if not actor.has(Permission.MANAGE_ROLES):
        return False
    if role.is_root:
        return False
    if actor.is_root:
        return True
    return role.rank < actor.rank


def assignable_roles(actor: Principal, roles: list[Role]) -> list[str]:
    """Role keys ``actor`` may assign to a user.

    Two conditions, both required:

    1. **Rank.** The role must sit strictly below the actor's own.
    2. **Containment.** The role's permissions must be a subset of the actor's.

    Condition 2 is the privilege-escalation guard. Without it an actor holding
    ``manage_users`` could assign a role carrying ``system_settings`` — a
    permission they do not have — and then use that account to gain it.
    """
    if not actor.has(Permission.MANAGE_USERS):
        return []
    if actor.is_root:
        return [role.key for role in roles]
    return [
        role.key
        for role in roles
        if not role.is_root
        and role.rank < actor.rank
        and role.effective_permissions <= actor.granted
    ]


def grantable_permissions(actor: Principal) -> frozenset[Permission]:
    """Permissions ``actor`` may grant — never more than they hold themselves."""
    return ALL_PERMISSIONS if actor.is_root else actor.granted


def validate_role_key(key: str) -> str:
    """Return a validated role key.

    Raises:
        ValueError: The key does not match :data:`ROLE_KEY_PATTERN`.
    """
    candidate = (key or "").strip().lower()
    if not ROLE_KEY_PATTERN.match(candidate):
        raise ValueError(
            "Role key must start with a letter and use only lowercase letters, "
            "digits and underscores (2-32 characters)."
        )
    return candidate
