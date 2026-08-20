"""User administration endpoints.

Backed by the Supabase Auth Admin API. Every route requires ``manage_users``, and
:mod:`zz2.services.users` additionally enforces the rank and containment rules —
the permission alone is not sufficient to act on any given account.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from zz2.api.deps import RequireManageUsers
from zz2.domain.permissions import Principal
from zz2.services.users import MIN_PASSWORD_LENGTH, user_service

router = APIRouter(prefix="/api/users", tags=["users"])


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    display: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)
    role: str = Field(min_length=1, max_length=32)
    permissions: list[str] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    """A partial update.

    Every field is optional and ``None`` means "leave unchanged" — distinct from
    an empty value, which means "clear it". Without that distinction, a form that
    edits only the display name would also blank the permission list.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    display: str | None = Field(default=None, max_length=120)
    role: str | None = Field(default=None, max_length=32)
    permissions: list[str] | None = None
    active: bool | None = None


class PasswordRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


@router.get("")
def list_users(actor: Annotated[Principal, RequireManageUsers]) -> dict[str, Any]:
    """Accounts, roles and the permission catalogue for the Users page."""
    return user_service.directory(actor)


@router.post("", status_code=201)
def create_user(
    payload: UserCreateRequest,
    actor: Annotated[Principal, RequireManageUsers],
) -> dict[str, Any]:
    return user_service.create(
        actor,
        email=str(payload.email),
        display=payload.display,
        password=payload.password,
        role=payload.role,
        permissions=payload.permissions,
    )


@router.patch("/{user_id}")
def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    actor: Annotated[Principal, RequireManageUsers],
) -> dict[str, Any]:
    return user_service.update(
        actor,
        user_id=user_id,
        display=payload.display,
        role=payload.role,
        permissions=payload.permissions,
        active=payload.active,
    )


@router.put("/{user_id}/password")
def set_user_password(
    user_id: str,
    payload: PasswordRequest,
    actor: Annotated[Principal, RequireManageUsers],
) -> dict[str, Any]:
    return user_service.set_password(actor, user_id=user_id, password=payload.password)


@router.delete("/{user_id}")
def delete_user(
    user_id: str,
    actor: Annotated[Principal, RequireManageUsers],
) -> dict[str, Any]:
    return user_service.delete(actor, user_id=user_id)
