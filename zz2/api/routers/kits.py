"""Kit catalogue, blueprint and upload endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from zz2.api.deps import CurrentPrincipal, RequireKitEditor
from zz2.domain.permissions import Principal
from zz2.services.catalog import catalog_service
from zz2.services.kits import kit_service
from zz2.services.uploads import upload_service

router = APIRouter(tags=["kits"])


# -- Schemas -------------------------------------------------------------------
class KitFields(BaseModel):
    """Free-text metadata for a kit."""

    model_config = ConfigDict(str_strip_whitespace=True)

    vehicle: str = ""
    switch_button: str = ""
    audio_method: str = ""
    notes: str = ""


class KitCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=64)
    fields: KitFields = Field(default_factory=KitFields)


class KitSaveRequest(BaseModel):
    """A partial kit update.

    Every group is optional so the editor can save one pane without sending — and
    risking overwriting — the others. ``troubleshooting=None`` means "leave the
    tree alone"; ``troubleshooting=[]`` means "empty it".
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=64)
    fields: KitFields | None = None
    dip_image: str | None = None
    wiring_image: str | None = None
    misc_image: str | None = None
    manual: str | None = None
    troubleshooting: list[dict[str, Any]] | None = None

    def assets(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in (
                ("dip_image", self.dip_image),
                ("wiring_image", self.wiring_image),
                ("misc_image", self.misc_image),
                ("manual", self.manual),
            )
            if value is not None
        }


class KitKeyRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=64)


class KitDuplicateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=64)


class KitRestoreRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)


class BlueprintSaveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = ""
    name: str = Field(min_length=1, max_length=200)
    troubleshooting: list[dict[str, Any]] = Field(default_factory=list)


class BlueprintIdRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=128)


class BlueprintDuplicateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)


class UploadRequest(BaseModel):
    """A base64 media upload.

    ``data`` is capped at roughly 34 MB of base64, which decodes to about 25 MB —
    the per-file limit. Bounding the *field* as well as the decoded bytes means an
    oversized payload is rejected during parsing rather than after a 34 MB string
    has been materialised.
    """

    model_config = ConfigDict(str_strip_whitespace=False)

    key: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=16)
    data: str = Field(min_length=1, max_length=34_000_000)
    filename: str = Field(default="", max_length=200)


# -- Catalogue -----------------------------------------------------------------
@router.get("/api/catalog")
def catalog(actor: Annotated[Principal, CurrentPrincipal]) -> dict[str, Any]:
    """Kits and troubleshooting trees.

    Authenticated. The monolith served this through ``/api/proxy?action=csv``
    with an explicit "allowed without auth (kit data is not sensitive)" comment —
    reasonable on a closed LAN, but on a public VPS it publishes the full product
    catalogue and every diagnostic procedure to anyone who finds the host.
    """
    catalog_service.ensure_initialised()
    return {"ok": True, **kit_service.catalog_payload()}


@router.get("/api/kit/coverage")
def coverage(actor: Annotated[Principal, RequireKitEditor]) -> dict[str, Any]:
    """Troubleshooting-tree health for every kit."""
    return catalog_service.coverage()


@router.get("/api/kit/history")
def kit_versions(
    actor: Annotated[Principal, RequireKitEditor],
    key: Annotated[str, Query(min_length=1, max_length=64)],
) -> dict[str, Any]:
    """Snapshot versions available for one kit."""
    return kit_service.versions(key)


# -- Kit mutations -------------------------------------------------------------
@router.post("/api/kit/new", status_code=201)
def create_kit(
    payload: KitCreateRequest,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    return kit_service.create(payload.key, payload.fields.model_dump(),
                              actor=actor.username)


@router.post("/api/kit/save")
def save_kit(
    payload: KitSaveRequest,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    return kit_service.save(
        payload.key,
        fields=payload.fields.model_dump() if payload.fields else None,
        assets=payload.assets() or None,
        troubleshooting=payload.troubleshooting,
        actor=actor.username,
    )


@router.post("/api/kit/delete")
def delete_kit(
    payload: KitKeyRequest,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    return kit_service.delete(payload.key, actor=actor.username)


@router.post("/api/kit/duplicate", status_code=201)
def duplicate_kit(
    payload: KitDuplicateRequest,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    return kit_service.duplicate(payload.source, payload.key, actor=actor.username)


@router.post("/api/kit/restore")
def restore_kit(
    payload: KitRestoreRequest,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    return kit_service.restore(payload.key, payload.version, actor=actor.username)


# -- Blueprints ----------------------------------------------------------------
@router.get("/api/blueprints")
def list_blueprints(actor: Annotated[Principal, RequireKitEditor]) -> dict[str, Any]:
    return kit_service.list_blueprints()


@router.post("/api/blueprints/save")
def save_blueprint(
    payload: BlueprintSaveRequest,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    return kit_service.save_blueprint(payload.id, payload.name,
                                      payload.troubleshooting, actor=actor.username)


@router.post("/api/blueprints/delete")
def delete_blueprint(
    payload: BlueprintIdRequest,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    return kit_service.delete_blueprint(payload.id, actor=actor.username)


@router.post("/api/blueprints/duplicate", status_code=201)
def duplicate_blueprint(
    payload: BlueprintDuplicateRequest,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    return kit_service.duplicate_blueprint(payload.id, payload.name,
                                           actor=actor.username)


# -- Uploads -------------------------------------------------------------------
@router.post("/api/upload", status_code=201)
def upload_media(
    payload: UploadRequest,
    actor: Annotated[Principal, RequireKitEditor],
) -> dict[str, Any]:
    """Store a diagram image or a PDF manual.

    Content is validated by magic bytes, not by the declared type — see
    :mod:`zz2.services.uploads`.
    """
    stored = upload_service.store(
        kit_key=payload.key,
        kind=payload.kind,
        encoded=payload.data,
        filename=payload.filename,
        actor=actor.username,
    )
    return stored.to_api()
