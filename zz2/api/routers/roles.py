"""Role administration endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from zz2.api.deps import RequireManageRoles
from zz2.domain.permissions import ROOT_RANK, Principal
from zz2.services.roles import role_service

router = APIRouter(prefix="/api/roles", tags=["roles"])


class RoleCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    key: str = Field(min_length=2, max_length=32)
    label: str = Field(min_length=1, max_length=64)
    # Bounded below ROOT_RANK at the schema level as well as in the service. The
    # service applies the actor-relative ceiling; this stops a nonsensical value
    # from ever reaching it.
    rank: int = Field(ge=1, le=ROOT_RANK - 1)
    permissions: list[str] = Field(default_factory=list)


class RoleUpdateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str | None = Field(default=None, max_length=64)
    rank: int | None = Field(default=None, ge=1, le=ROOT_RANK - 1)
    permissions: list[str] | None = None


@router.get("")
def list_roles(actor: Annotated[Principal, RequireManageRoles]) -> dict[str, Any]:
    return role_service.listing(actor)


@router.post("", status_code=201)
def create_role(
    payload: RoleCreateRequest,
    actor: Annotated[Principal, RequireManageRoles],
) -> dict[str, Any]:
    return role_service.create(
        actor,
        key=payload.key,
        label=payload.label,
        rank=payload.rank,
        permissions=payload.permissions,
    )


@router.patch("/{role_key}")
def update_role(
    role_key: str,
    payload: RoleUpdateRequest,
    actor: Annotated[Principal, RequireManageRoles],
) -> dict[str, Any]:
    return role_service.update(
        actor,
        key=role_key,
        label=payload.label,
        rank=payload.rank,
        permissions=payload.permissions,
    )


@router.delete("/{role_key}")
def delete_role(
    role_key: str,
    actor: Annotated[Principal, RequireManageRoles],
) -> dict[str, Any]:
    return role_service.delete(actor, key=role_key)
