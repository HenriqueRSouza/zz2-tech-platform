"""Role administration.

Custom roles are what make the permission model useful — and what make it
dangerous, because creating a role is creating a permission set. Two rules bound
it, and both are enforced here rather than at the API edge, so they hold for any
caller:

**Rank ceiling.** A role may be created or edited only *below* the actor's own
rank, and never at :data:`~zz2.domain.permissions.ROOT_RANK`. Without the ceiling
an admin could create a role ranked above their own and then be unable to manage
the users holding it — or worse, assign it to themselves.

**Permission containment.** A role may only carry permissions the actor holds.
This is the escalation guard: it stops an actor with ``manage_roles`` but not
``system_settings`` from minting a role that has ``system_settings``.

Deletion additionally requires the role be unused. Deleting a role in use would
leave those accounts pointing at nothing, which resolves to zero permissions —
indistinguishable, from the user's side, from being locked out for no reason.
"""

from __future__ import annotations

from typing import Any

from zz2.core.errors import Conflict, NotFound, PermissionDenied, ValidationError
from zz2.core.logging import audit
from zz2.core.text import as_text
from zz2.domain.permissions import (
    PERMISSION_CATALOG,
    ROOT_RANK,
    Permission,
    Principal,
    Role,
    can_edit_role,
    grantable_permissions,
    parse_permissions,
    sort_permissions,
    validate_role_key,
)
from zz2.gateways.user_directory import user_directory
from zz2.repositories.roles import RoleRepository


class RoleService:
    """Lists and mutates role definitions."""

    def __init__(self, roles: RoleRepository | None = None) -> None:
        self._roles = roles or RoleRepository()

    # -- Reads -----------------------------------------------------------------
    def listing(self, actor: Principal) -> dict[str, Any]:
        """The Roles page payload, including per-role capability flags.

        ``editable`` and ``deletable`` are computed server-side and sent to the
        client. The page uses them to disable controls, but the authoritative
        check still runs on every mutation — a disabled button is a courtesy, not
        a boundary.
        """
        counts = self._user_counts()
        roles = self._roles.all()

        rows: list[dict[str, Any]] = []
        for role in sorted(roles, key=lambda item: -item.rank):
            in_use = counts.get(role.key, 0)
            editable = can_edit_role(actor, role)
            rows.append({
                **role.to_dict(),
                "user_count": in_use,
                "editable": editable,
                "deletable": editable and not role.is_system and in_use == 0,
            })

        return {
            "ok": True,
            "actor_role": actor.role_key,
            "actor_perms": sort_permissions(actor.granted),
            "max_assignable_rank": self._rank_ceiling(actor),
            "permission_catalog": [
                {"key": info.key.value, "label": info.label, "desc": info.description}
                for info in PERMISSION_CATALOG
            ],
            "roles": rows,
        }

    def _user_counts(self) -> dict[str, int]:
        """Accounts per role, from the user directory.

        Best effort: if the database is unreachable the page still renders, with every
        count at zero. ``deletable`` is derived from these counts, so a mutation
        re-checks before acting — a stale zero must not become permission to
        delete a role that is in use.
        """
        counts: dict[str, int] = {}
        try:
            for account in user_directory.list_users():
                key = as_text((account.get("app_metadata") or {}).get("role")) or "tech"
                counts[key] = counts.get(key, 0) + 1
        except Exception:  # noqa: BLE001 - the page must render regardless
            return {}
        return counts

    @staticmethod
    def _rank_ceiling(actor: Principal) -> int:
        """Highest rank ``actor`` may give a role.

        One below their own; root stops one below :data:`ROOT_RANK`, so nothing can
        ever be created at root rank.
        """
        ceiling = (ROOT_RANK - 1) if actor.is_root else (actor.rank - 1)
        return max(1, ceiling)

    # -- Writes ----------------------------------------------------------------
    def create(
        self,
        actor: Principal,
        *,
        key: str,
        label: str,
        rank: int,
        permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a custom role."""
        try:
            validated_key = validate_role_key(key)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        label = as_text(label).strip()
        granted = self._validate(actor, label=label, rank=rank, permissions=permissions)

        if self._roles.by_key(validated_key) is not None:
            raise Conflict("A role with that ID already exists.")

        self._roles.upsert(Role(key=validated_key, label=label, rank=int(rank),
                                permissions=granted, is_system=False))

        audit("role.created", actor=actor.username, role=validated_key,
              rank=int(rank), permissions=sort_permissions(granted))
        return {"ok": True, "key": validated_key}

    def update(
        self,
        actor: Principal,
        *,
        key: str,
        label: str | None = None,
        rank: int | None = None,
        permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Edit a role's label, rank or permissions."""
        existing = self._roles.by_key(as_text(key).strip().lower())
        if existing is None:
            raise NotFound("Role not found.")
        if not can_edit_role(actor, existing):
            raise PermissionDenied("You cannot edit this role.")

        new_label = as_text(label).strip() if label is not None else existing.label
        # System roles keep their fixed rank. Their rank is what the hierarchy is
        # calibrated against; letting it move would reorder every comparison.
        new_rank = existing.rank if existing.is_system else (
            int(rank) if rank is not None else existing.rank
        )
        requested = permissions if permissions is not None else sort_permissions(
            existing.permissions
        )
        granted = self._validate(actor, label=new_label, rank=new_rank,
                                 permissions=requested)

        self._roles.upsert(Role(key=existing.key, label=new_label, rank=new_rank,
                                permissions=granted, is_system=existing.is_system,
                                is_root=existing.is_root))

        audit("role.updated", actor=actor.username, role=existing.key,
              rank=new_rank, permissions=sort_permissions(granted))
        return {"ok": True, "key": existing.key}

    def delete(self, actor: Principal, *, key: str) -> dict[str, Any]:
        """Delete an unused custom role."""
        existing = self._roles.by_key(as_text(key).strip().lower())
        if existing is None:
            raise NotFound("Role not found.")
        if existing.is_system:
            raise PermissionDenied("Built-in roles cannot be deleted.")
        if not can_edit_role(actor, existing):
            raise PermissionDenied("You cannot delete this role.")

        # Re-checked at the point of deletion, not read from the listing payload.
        in_use = self._user_counts().get(existing.key, 0)
        if in_use:
            plural = "" if in_use == 1 else "s"
            raise Conflict(f"Reassign the {in_use} user{plural} on this role first.")

        self._roles.delete(existing.key)
        audit("role.deleted", actor=actor.username, role=existing.key)
        return {"ok": True, "key": existing.key}

    # -- Shared validation -----------------------------------------------------
    def _validate(
        self,
        actor: Principal,
        *,
        label: str,
        rank: object,
        permissions: list[str] | None,
    ) -> frozenset[Permission]:
        """Validate label, rank and permissions. Returns the granted set.

        Raises:
            ValidationError: Empty label, non-numeric or out-of-range rank.
            PermissionDenied: A requested permission the actor does not hold.
        """
        if not label:
            raise ValidationError("A role name is required.")

        try:
            numeric_rank = int(rank)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValidationError("The level must be a number.") from exc

        ceiling = self._rank_ceiling(actor)
        if not 1 <= numeric_rank <= ceiling:
            raise ValidationError(f"The level must be between 1 and {ceiling}.")

        requested = parse_permissions(permissions or [])
        excess = requested - grantable_permissions(actor)
        if excess:
            # Rejected, not filtered. Unlike a user's extra grants — where the
            # form legitimately submits permissions inherited from a role — a role
            # definition is exactly the set being requested, so silently dropping
            # part of it would save something the operator did not ask for.
            raise PermissionDenied("You cannot grant permissions you do not have.")
        return requested


role_service = RoleService()
