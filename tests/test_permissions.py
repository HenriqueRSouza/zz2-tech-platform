"""Authorization rules.

The privilege-escalation guards, tested directly against the pure functions in
:mod:`zz2.domain.permissions`. These are the assertions that must not regress: an
error here does not surface as a crash, it surfaces as someone holding a
permission nobody granted them.
"""

from __future__ import annotations

from zz2.domain.permissions import (
    ALL_PERMISSIONS,
    ROOT_RANK,
    Permission,
    Principal,
    Role,
    assignable_roles,
    builtin_roles,
    can_edit_role,
    can_manage_rank,
    grantable_permissions,
    parse_permissions,
    sort_permissions,
    validate_role_key,
)


def principal(
    *,
    role: str = "admin",
    rank: int = 50,
    permissions: set[Permission] | None = None,
    is_root: bool = False,
) -> Principal:
    granted = frozenset(permissions or set())
    return Principal(
        subject="s", username="a@zz2.com.br", display="A",
        role_key=role, granted=ALL_PERMISSIONS if is_root else granted,
        rank=rank, is_root=is_root,
    )


ROOT = principal(role="super_admin", rank=ROOT_RANK, is_root=True)
ADMIN = principal(
    role="admin", rank=50,
    permissions={Permission.MANAGE_USERS, Permission.MANAGE_ROLES,
                 Permission.TICKETS, Permission.DASHBOARD},
)
TECH = principal(role="tech", rank=10, permissions={Permission.TICKETS})


# -- Rank ----------------------------------------------------------------------
def test_admin_cannot_manage_equal_or_higher_rank() -> None:
    assert can_manage_rank(ADMIN, 10) is True
    assert can_manage_rank(ADMIN, 50) is False
    assert can_manage_rank(ADMIN, ROOT_RANK) is False


def test_root_can_manage_peers() -> None:
    """Otherwise the last two super-admins could never adjust each other."""
    assert can_manage_rank(ROOT, ROOT_RANK) is True


def test_manage_requires_the_permission_not_just_rank() -> None:
    high_rank_no_permission = principal(role="auditor", rank=90,
                                        permissions={Permission.ANALYTICS})
    assert can_manage_rank(high_rank_no_permission, 10) is False


# -- Role assignment: the containment rule -------------------------------------
def test_actor_cannot_assign_a_role_carrying_permissions_they_lack() -> None:
    """The core escalation guard.

    An admin with ``manage_users`` but not ``system_settings`` must not be able to
    assign a role that grants ``system_settings`` — otherwise they mint that role
    onto an account and use it to gain the permission.
    """
    privileged = Role(key="ops", label="Ops", rank=20,
                      permissions=frozenset({Permission.SYSTEM_SETTINGS}))
    modest = Role(key="viewer", label="Viewer", rank=20,
                  permissions=frozenset({Permission.TICKETS}))

    assignable = assignable_roles(ADMIN, [privileged, modest])
    assert "ops" not in assignable
    assert "viewer" in assignable


def test_actor_cannot_assign_a_role_at_or_above_their_rank() -> None:
    peer = Role(key="peer", label="Peer", rank=50,
                permissions=frozenset({Permission.TICKETS}))
    above = Role(key="above", label="Above", rank=60,
                 permissions=frozenset({Permission.TICKETS}))
    assert assignable_roles(ADMIN, [peer, above]) == []


def test_root_roles_are_never_assignable_by_non_root() -> None:
    root_role = Role(key="super_admin", label="Super Admin", rank=ROOT_RANK,
                     permissions=ALL_PERMISSIONS, is_system=True, is_root=True)
    assert assignable_roles(ADMIN, [root_role]) == []


def test_root_can_assign_anything() -> None:
    roles = list(builtin_roles())
    assert set(assignable_roles(ROOT, roles)) == {role.key for role in roles}


def test_tech_can_assign_nothing() -> None:
    assert assignable_roles(TECH, list(builtin_roles())) == []


# -- Role editing --------------------------------------------------------------
def test_root_role_definition_is_immutable_even_for_root() -> None:
    """Editing it could strip the permissions needed to undo the edit."""
    root_role = Role(key="super_admin", label="Super Admin", rank=ROOT_RANK,
                     permissions=ALL_PERMISSIONS, is_system=True, is_root=True)
    assert can_edit_role(ROOT, root_role) is False
    assert can_edit_role(ADMIN, root_role) is False


def test_role_editing_respects_rank() -> None:
    below = Role(key="below", label="Below", rank=20)
    peer = Role(key="peer", label="Peer", rank=50)
    assert can_edit_role(ADMIN, below) is True
    assert can_edit_role(ADMIN, peer) is False


def test_role_editing_requires_manage_roles() -> None:
    no_permission = principal(role="admin", rank=50,
                              permissions={Permission.MANAGE_USERS})
    assert can_edit_role(no_permission, Role(key="x", label="X", rank=10)) is False


# -- Grantable permissions -----------------------------------------------------
def test_grantable_is_bounded_by_what_the_actor_holds() -> None:
    assert grantable_permissions(ADMIN) == ADMIN.granted
    assert grantable_permissions(ROOT) == ALL_PERMISSIONS


# -- Root role resolution ------------------------------------------------------
def test_root_role_grants_every_permission_including_future_ones() -> None:
    """Resolved dynamically, so adding a permission to the catalogue grants it."""
    role = Role(key="super_admin", label="Super Admin", rank=ROOT_RANK,
                permissions=frozenset({Permission.TICKETS}), is_root=True)
    assert role.effective_permissions == ALL_PERMISSIONS


# -- Parsing -------------------------------------------------------------------
def test_unknown_permissions_are_dropped_not_rejected() -> None:
    """``app_metadata`` is edited outside this app; a stray value must not lock a
    user out of everything."""
    parsed = parse_permissions(["tickets", "not_a_permission", "analytics", 42, None])
    assert parsed == frozenset({Permission.TICKETS, Permission.ANALYTICS})


def test_parse_permissions_handles_non_list_input() -> None:
    assert parse_permissions(None) == frozenset()
    assert parse_permissions("tickets") == frozenset()
    assert parse_permissions({"tickets": True}) == frozenset()


def test_permissions_serialise_in_catalogue_order() -> None:
    """Stable API responses, regardless of set iteration order."""
    ordered = sort_permissions(frozenset({
        Permission.SYSTEM_SETTINGS, Permission.TICKETS, Permission.ANALYTICS,
    }))
    assert ordered == ["tickets", "analytics", "system_settings"]


# -- Role keys -----------------------------------------------------------------
def test_valid_role_keys_are_normalised() -> None:
    assert validate_role_key("  Ops_Lead  ") == "ops_lead"


def test_invalid_role_keys_are_rejected() -> None:
    import pytest

    for bad in ("", "a", "1abc", "has-hyphen", "has space", "x" * 33, "UPPER-CASE!"):
        with pytest.raises(ValueError):
            validate_role_key(bad)
