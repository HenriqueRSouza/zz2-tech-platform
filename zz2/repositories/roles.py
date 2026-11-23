"""Role definitions.

Roles are data, not hard-coded constants: administrators create custom roles and
choose each one's permission set. That flexibility needs a guard, and this
repository is where it lives — :meth:`RoleRepository.all` repairs the built-in
roles on every read.

Why repair rather than trust the file: ``roles.json`` is operator-editable and
restorable from a backup, so it can legitimately contain a ``super_admin`` entry
written by an older build with a stale permission list. If that list were trusted,
adding a permission to the catalogue would silently deny it to super-admins, and
the only account that could fix the file would be one that no longer has
``system_settings``. Reconciling on read makes that state unreachable.
"""

from __future__ import annotations

from typing import Any

from zz2.core.logging import logger
from zz2.domain.permissions import (
    ALL_PERMISSIONS,
    ROOT_RANK,
    Permission,
    Role,
    builtin_roles,
)
from zz2.paths import ROLES_FILE
from zz2.repositories.base import JsonDocumentRepository


class RoleRepository(JsonDocumentRepository[list]):
    """Reads and writes ``roles.json`` as a list of role definitions."""

    def __init__(self) -> None:
        super().__init__(ROLES_FILE, list)

    def all(self) -> list[Role]:
        """Every role, with the system roles seeded and reconciled.

        Repairs applied, and why each is necessary:

        * **Missing system role** — re-seeded. A deleted ``tech`` role would
          leave existing users pointing at nothing, which resolves to zero
          permissions and looks like a mysterious lockout.
        * **``super_admin`` drift** — forced back to root, :data:`ROOT_RANK` and
          every permission. See the module docstring.
        * **Missing ``dashboard``** — granted to non-root system roles. Every
          role gained this permission after ``roles.json`` was first written, and
          without it a tech signs in to a page they cannot open.
        """
        raw = self.read()
        if not isinstance(raw, list) or not raw:
            seeded = list(builtin_roles())
            self.write([role.to_dict() for role in seeded])
            logger.info("seeded built-in roles", extra={"count": len(seeded)})
            return seeded

        roles = [Role.from_dict(item) for item in raw if isinstance(item, dict)]
        roles = [role for role in roles if role.key]
        present = {role.key for role in roles}
        repaired = False

        for builtin in builtin_roles():
            if builtin.key not in present:
                roles.append(builtin)
                repaired = True

        reconciled: list[Role] = []
        for role in roles:
            if role.key == "super_admin" and not (
                role.is_root
                and role.rank == ROOT_RANK
                and role.effective_permissions == ALL_PERMISSIONS
            ):
                role = Role(key="super_admin", label=role.label or "Super Admin",
                            rank=ROOT_RANK, permissions=ALL_PERMISSIONS,
                            is_system=True, is_root=True)
                repaired = True
            elif (role.is_system and not role.is_root
                  and Permission.DASHBOARD not in role.permissions):
                role = Role(key=role.key, label=role.label, rank=role.rank,
                            permissions=role.permissions | {Permission.DASHBOARD},
                            is_system=role.is_system, is_root=role.is_root)
                repaired = True
            reconciled.append(role)

        if repaired:
            self.write([role.to_dict() for role in reconciled])
            logger.info("repaired role definitions")
        return reconciled

    def by_key(self, key: str) -> Role | None:
        return next((role for role in self.all() if role.key == key), None)

    def as_map(self) -> dict[str, Role]:
        return {role.key: role for role in self.all()}

    def save_all(self, roles: list[Role]) -> None:
        self.write([role.to_dict() for role in roles])

    def upsert(self, role: Role) -> None:
        """Insert or replace one role, preserving list order."""
        roles = self.all()
        for index, existing in enumerate(roles):
            if existing.key == role.key:
                roles[index] = role
                break
        else:
            roles.append(role)
        self.save_all(roles)

    def delete(self, key: str) -> bool:
        """Remove a role. Returns whether anything was removed."""
        roles = self.all()
        remaining = [role for role in roles if role.key != key]
        if len(remaining) == len(roles):
            return False
        self.save_all(remaining)
        return True

    def resolve(self, role_key: str) -> tuple[frozenset[Permission], int, bool]:
        """Resolve a role key to ``(permissions, rank, is_root)``.

        An unknown key resolves to no permissions and rank ``0``. Failing closed
        matters here: a token whose ``app_metadata.role`` names a role that was
        deleted must grant nothing, not fall back to a default.
        """
        role = self.by_key(role_key)
        if role is None:
            return frozenset(), 0, False
        return role.effective_permissions, role.rank, role.is_root

    def count_by_role(self) -> dict[str, int]:
        """Placeholder for user counts per role.

        The user directory lives in the database, not on disk, so counting is
        the user-directory gateway's job. Kept as an explicit stub so the Roles
        page has one place to call rather than reimplementing the query.
        """
        return {}


def role_payload(roles: list[Role]) -> list[dict[str, Any]]:
    """Serialise roles for the Roles page, ordered by descending rank."""
    return [role.to_dict() for role in sorted(roles, key=lambda r: (-r.rank, r.key))]
