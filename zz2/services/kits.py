"""Kit editing.

Every mutation follows the same shape: validate the key, snapshot the previous
state to history, write kit and tree together, audit. Concentrating that here is
what makes it consistent — in the monolith each of ``_handle_kit_save``,
``_handle_kit_delete``, ``_handle_kit_new`` and ``_handle_kit_duplicate``
open-coded the sequence, and ``_handle_kit_new`` skipped the snapshot entirely.

Kit and tree are separate documents, so a save writes two files and cannot be
atomic across both. The kit is written first: a kit with a stale tree renders and
is editable, whereas a tree with no kit record is invisible to the UI and looks
like data loss.
"""

from __future__ import annotations

import copy
import shutil
import time
from typing import Any

from zz2.core.errors import Conflict, NotFound, UpstreamError, ValidationError
from zz2.core.events import MediaChanged, bus
from zz2.core.logging import audit, logger
from zz2.core.text import as_text, safe_key
from zz2.gateways.storage import storage_gateway
from zz2.paths import IMAGES_DIR, MANUALS_DIR
from zz2.repositories.kit_history import KitHistoryRepository
from zz2.repositories.kits import KitCatalogRepository
from zz2.repositories.write_queue import new_operation_id

EDITABLE_FIELDS = ("vehicle", "switch_button", "audio_method", "notes")
"""Free-text fields the editor may set directly."""

ASSET_FIELDS = ("dip_image", "wiring_image", "misc_image", "manual")
"""Asset references. Set by the upload flow, not typed by hand."""

IMAGE_KINDS = ("dip", "wiring", "misc")


class KitService:
    """Kit and blueprint mutations."""

    def __init__(
        self,
        catalog: KitCatalogRepository | None = None,
        history: KitHistoryRepository | None = None,
    ) -> None:
        self._catalog = catalog or KitCatalogRepository()
        self._history = history or KitHistoryRepository()

    # -- Reads -----------------------------------------------------------------
    def catalog_payload(self) -> dict[str, Any]:
        """Kits and trees, as the tool and Kit Editor consume them."""
        kits, trees, _ = self._catalog.snapshot()
        return {"kits": kits, "troubleshooting": trees}

    # -- Kit mutations ---------------------------------------------------------
    def create(self, key: str, fields: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """Create an empty kit.

        Raises:
            ValidationError: The key is not a valid path component.
            Conflict: A kit with that key exists. Reported rather than merged —
                silently overwriting would destroy the existing kit's data.
        """
        validated = self._validate_key(key)
        if self._catalog.kits.get(validated) is not None:
            raise Conflict(f"A kit named {validated} already exists.")

        record = {field: as_text(fields.get(field)) for field in EDITABLE_FIELDS}
        record.update({field: "" for field in ASSET_FIELDS})

        self._catalog.kits.save(validated, record)
        self._catalog.trees.save(validated, [])

        audit("kit.created", actor=actor, kit=validated)
        return {"ok": True, "key": validated}

    def save(
        self,
        key: str,
        *,
        fields: dict[str, Any] | None = None,
        assets: dict[str, Any] | None = None,
        troubleshooting: list[dict[str, Any]] | None = None,
        actor: str,
    ) -> dict[str, Any]:
        """Update an existing kit.

        Only supplied fields are touched, so the editor can save the metadata pane
        without sending — and risking clobbering — the troubleshooting tree.

        Raises:
            NotFound: No such kit.
        """
        validated = self._validate_key(key)
        existing = self._catalog.kits.get(validated)
        if existing is None:
            raise NotFound(f"Kit not found: {validated}")

        self._snapshot(validated)

        record = dict(existing)
        for field in EDITABLE_FIELDS:
            if fields and field in fields:
                record[field] = as_text(fields[field])
        for field in ASSET_FIELDS:
            if assets and field in assets:
                record[field] = as_text(assets[field])

        saved_at = int(time.time())
        record["updated_at"] = saved_at

        self._catalog.kits.save(validated, record)
        if troubleshooting is not None:
            self._catalog.trees.save(validated, troubleshooting)

        audit("kit.saved", actor=actor, kit=validated)
        return {"ok": True, "key": validated, "updated_at": saved_at}

    def delete(self, key: str, *, actor: str) -> dict[str, Any]:
        """Delete a kit and its tree.

        Media files are left on disk deliberately. They are large, the delete is
        recoverable from history, and an accidental deletion that also destroyed
        the wiring diagrams would be unrecoverable.
        """
        validated = self._validate_key(key)
        if self._catalog.kits.get(validated) is None:
            raise NotFound(f"Kit not found: {validated}")

        self._snapshot(validated)
        self._catalog.kits.delete(validated)
        self._catalog.trees.delete(validated)

        audit("kit.deleted", actor=actor, kit=validated)
        return {"ok": True, "key": validated}

    def duplicate(self, source: str, key: str, *, actor: str) -> dict[str, Any]:
        """Copy a kit — fields, tree and media files — to a new key."""
        source_key = self._validate_key(source)
        target_key = self._validate_key(key)
        if source_key == target_key:
            raise ValidationError("The new kit key must differ from the source.")

        original = self._catalog.kits.get(source_key)
        if original is None:
            raise NotFound(f"Source kit not found: {source_key}")
        if self._catalog.kits.get(target_key) is not None:
            raise Conflict(f"A kit named {target_key} already exists.")

        record = copy.deepcopy(original)
        self._copy_media(source_key, target_key, record)
        record["updated_at"] = int(time.time())

        self._catalog.kits.save(target_key, record)
        self._catalog.trees.save(target_key,
                                 copy.deepcopy(self._catalog.trees.get(source_key)))

        audit("kit.duplicated", actor=actor, kit=target_key, source=source_key)
        return {"ok": True, "key": target_key, "source": source_key}

    def restore(self, key: str, version: str, *, actor: str) -> dict[str, Any]:
        """Roll a kit back to a stored snapshot.

        The current state is snapshotted first, so the restore is itself
        reversible — a restore to the wrong version must not be a dead end.
        """
        validated = self._validate_key(key)
        snapshot = self._history.load(validated, version)
        if snapshot is None:
            raise NotFound("That version is not available.")

        self._snapshot(validated)

        if snapshot.get("kit") is not None:
            self._catalog.kits.save(validated, snapshot["kit"])
        self._catalog.trees.save(validated, snapshot.get("troubleshooting") or [])

        audit("kit.restored", actor=actor, kit=validated, version=version)
        return {"ok": True, "key": validated, "restored": version}

    def versions(self, key: str) -> dict[str, Any]:
        validated = self._validate_key(key)
        return {"ok": True, "key": validated, "versions": self._history.versions(validated)}

    # -- Blueprints ------------------------------------------------------------
    def list_blueprints(self) -> dict[str, Any]:
        return {"ok": True, "blueprints": self._catalog.blueprints.all()}

    def save_blueprint(
        self,
        blueprint_id: str,
        name: str,
        troubleshooting: list[dict[str, Any]] | None,
        *,
        actor: str,
    ) -> dict[str, Any]:
        """Create or update a reusable troubleshooting template."""
        name = as_text(name).strip()
        if not name:
            raise ValidationError("A blueprint name is required.")

        nodes = troubleshooting if isinstance(troubleshooting, list) else []
        now = int(time.time())
        blueprint_id = as_text(blueprint_id).strip()
        existing = self._catalog.blueprints.get(blueprint_id) if blueprint_id else None

        if existing is None:
            blueprint_id = blueprint_id or f"bp_{new_operation_id()}"
            record = {"name": name, "troubleshooting": nodes,
                      "created_at": now, "updated_at": now}
        else:
            record = {**existing, "name": name, "troubleshooting": nodes,
                      "updated_at": now}

        self._catalog.blueprints.save(blueprint_id, record)
        audit("blueprint.saved", actor=actor, blueprint=blueprint_id)
        return {"ok": True, "id": blueprint_id, "name": name, "updated_at": now}

    def delete_blueprint(self, blueprint_id: str, *, actor: str) -> dict[str, Any]:
        blueprint_id = as_text(blueprint_id).strip()
        if not blueprint_id:
            raise ValidationError("A blueprint id is required.")
        if not self._catalog.blueprints.delete(blueprint_id):
            raise NotFound("Blueprint not found.")
        audit("blueprint.deleted", actor=actor, blueprint=blueprint_id)
        return {"ok": True, "id": blueprint_id}

    def duplicate_blueprint(self, source_id: str, name: str, *, actor: str) -> dict[str, Any]:
        source_id = as_text(source_id).strip()
        name = as_text(name).strip()
        if not source_id or not name:
            raise ValidationError("A source blueprint and a new name are required.")

        source = self._catalog.blueprints.get(source_id)
        if source is None:
            raise NotFound("Source blueprint not found.")

        now = int(time.time())
        new_id = f"bp_{new_operation_id()}"
        self._catalog.blueprints.save(new_id, {
            "name": name,
            "troubleshooting": copy.deepcopy(source.get("troubleshooting") or []),
            "created_at": now,
            "updated_at": now,
        })
        audit("blueprint.duplicated", actor=actor, blueprint=new_id, source=source_id)
        return {"ok": True, "id": new_id, "name": name}

    # -- Internals -------------------------------------------------------------
    @staticmethod
    def _validate_key(key: str) -> str:
        validated = safe_key(key)
        if not validated:
            raise ValidationError(
                "A kit key may contain only letters, digits, spaces, dots, "
                "hyphens and underscores."
            )
        return validated

    def _snapshot(self, key: str) -> None:
        """Record the current state before changing it."""
        self._history.snapshot(key, self._catalog.kits.get(key),
                               self._catalog.trees.get(key))

    def _copy_media(self, source: str, target: str, record: dict[str, Any]) -> None:
        """Copy image and manual files, repointing the record's image paths.

        The ``manual`` field holds a bare filename rather than a path, so copying
        the file into the new key's folder is enough — no rewrite needed. Image
        fields are absolute URLs and must be repointed.

        A copy failure is logged, not raised: the duplicated kit is still useful
        without its diagrams, and its media can be re-uploaded.
        """
        for root in (IMAGES_DIR, MANUALS_DIR):
            source_dir = root / source
            if not source_dir.is_dir():
                continue
            target_dir = root / target
            try:
                target_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
                for entry in source_dir.iterdir():
                    if entry.is_file():
                        shutil.copy2(entry, target_dir / entry.name)
            except OSError as exc:
                logger.warning("kit media copy failed",
                               extra={"source": source, "target": target,
                                      "root": root.name, "reason": str(exc)})

        manual = as_text(record.get("manual"))
        if manual and storage_gateway.configured:
            try:
                storage_gateway.copy_manual(source, target, manual)
            except (NotFound, UpstreamError) as exc:
                logger.warning("kit Storage manual copy failed",
                               extra={"source": source, "target": target,
                                      "reason": exc.log_detail or exc.message})

        for kind in IMAGE_KINDS:
            field = f"{kind}_image"
            if record.get(field):
                record[field] = f"/images/{target}/{kind}.jpg"

        bus.publish(MediaChanged(target, "duplicate"))


kit_service = KitService()
