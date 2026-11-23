"""Kit catalogue and troubleshooting trees.

Local files are authoritative for kit data. The Kit Editor writes here; the database
receives a mirror afterwards for off-server backup. Getting that direction right
matters — the mirror must never be able to resurrect a kit an administrator
deleted locally.

Kits and trees are separate documents keyed by the same kit key. They are read
and written together often enough that :class:`KitCatalogRepository` composes
both, giving services one object to depend on and one place where the invariant
"every kit has a tree, even an empty one" is enforced.
"""

from __future__ import annotations

import time
from typing import Any

from zz2.core.events import KitCatalogChanged
from zz2.core.text import safe_key
from zz2.domain.models import Kit
from zz2.paths import BLUEPRINTS_JSON, CATALOG_META_JSON, KITS_JSON, TS_JSON
from zz2.repositories.base import JsonDocumentRepository


class KitRepository(JsonDocumentRepository[dict]):
    """``store/kits.json`` — a mapping of kit key to metadata."""

    def __init__(self) -> None:
        super().__init__(KITS_JSON, dict, KitCatalogChanged("kits"))

    def all(self) -> dict[str, dict[str, Any]]:
        document = self.read()
        return document if isinstance(document, dict) else {}

    def keys(self) -> list[str]:
        return sorted(self.all().keys())

    def get(self, key: str) -> dict[str, Any] | None:
        return self.all().get(key)

    def save(self, key: str, kit: Kit | dict[str, Any]) -> None:
        """Insert or replace one kit, stamping ``updated_at``.

        Raises:
            ValueError: ``key`` is not a valid path component. Validated here as
                well as at the API edge because the key becomes a directory name
                under ``media/`` — the check belongs next to the write, not only
                next to the request parsing.
        """
        validated = safe_key(key)
        if not validated:
            raise ValueError(f"Invalid kit key: {key!r}")
        record = kit.model_dump() if isinstance(kit, Kit) else dict(kit)
        record["updated_at"] = int(time.time())
        with self.mutate() as document:
            document[validated] = record

    def delete(self, key: str) -> bool:
        with self.mutate() as document:
            return document.pop(key, None) is not None

    def rename(self, old_key: str, new_key: str) -> None:
        validated = safe_key(new_key)
        if not validated:
            raise ValueError(f"Invalid kit key: {new_key!r}")
        with self.mutate() as document:
            if old_key in document:
                document[validated] = document.pop(old_key)


class TroubleshootingRepository(JsonDocumentRepository[dict]):
    """``store/troubleshooting.json`` — kit key to flat list of tree nodes."""

    def __init__(self) -> None:
        super().__init__(TS_JSON, dict, KitCatalogChanged("troubleshooting"))

    def all(self) -> dict[str, list[dict[str, Any]]]:
        document = self.read()
        return document if isinstance(document, dict) else {}

    def get(self, key: str) -> list[dict[str, Any]]:
        """A kit's tree, or an empty list.

        Never ``None``: an absent tree and an intentionally empty one behave
        identically for every consumer, and collapsing them removes a null check
        from each of the eleven call sites the monolith had.
        """
        nodes = self.all().get(key)
        return nodes if isinstance(nodes, list) else []

    def save(self, key: str, nodes: list[dict[str, Any]]) -> None:
        validated = safe_key(key)
        if not validated:
            raise ValueError(f"Invalid kit key: {key!r}")
        with self.mutate() as document:
            document[validated] = nodes

    def delete(self, key: str) -> bool:
        with self.mutate() as document:
            return document.pop(key, None) is not None

    def rename(self, old_key: str, new_key: str) -> None:
        validated = safe_key(new_key)
        if not validated:
            raise ValueError(f"Invalid kit key: {new_key!r}")
        with self.mutate() as document:
            if old_key in document:
                document[validated] = document.pop(old_key)


class BlueprintRepository(JsonDocumentRepository[dict]):
    """``store/blueprints.json`` — reusable troubleshooting trees by id."""

    def __init__(self) -> None:
        super().__init__(BLUEPRINTS_JSON, dict, KitCatalogChanged("blueprints"))

    def all(self) -> dict[str, dict[str, Any]]:
        document = self.read()
        return document if isinstance(document, dict) else {}

    def get(self, blueprint_id: str) -> dict[str, Any] | None:
        return self.all().get(blueprint_id)

    def save(self, blueprint_id: str, record: dict[str, Any]) -> None:
        with self.mutate() as document:
            document[blueprint_id] = record

    def delete(self, blueprint_id: str) -> bool:
        with self.mutate() as document:
            return document.pop(blueprint_id, None) is not None


class CatalogMetaRepository(JsonDocumentRepository[dict]):
    """``store/catalog_meta.json`` — one-time migration markers.

    Small but load-bearing. The seed routine populates empty troubleshooting
    trees with a standard diagnostic flow; the version marker here is what stops
    it running twice. Without it, a tree an administrator deliberately emptied
    would be refilled on the next boot, and the deletion would look like it never
    saved.
    """

    def __init__(self) -> None:
        super().__init__(CATALOG_META_JSON, dict)

    def seed_version(self) -> int:
        document = self.read()
        try:
            return int(document.get("troubleshooting_seed_version", 0))
        except (TypeError, ValueError):
            return 0

    def mark_seeded(self, version: int = 1) -> None:
        with self.mutate() as document:
            document["troubleshooting_seed_version"] = version
            document["seeded_at"] = int(time.time())

    def bundle_seeded(self) -> bool:
        """Whether the packaged first-install catalogue has been imported."""
        return bool(self.read().get("initial_catalog_seeded"))

    def mark_bundle_seeded(self) -> None:
        with self.mutate() as document:
            document["initial_catalog_seeded"] = True
            document["initial_catalog_seeded_at"] = int(time.time())

    def media_seeded(self) -> bool:
        return bool(self.read().get("initial_media_seeded"))

    def mark_media_seeded(self) -> None:
        with self.mutate() as document:
            document["initial_media_seeded"] = True
            document["initial_media_seeded_at"] = int(time.time())


class KitCatalogRepository:
    """Facade over kits, trees and blueprints.

    Not a repository itself — it composes three. Services that need a coherent
    view of the catalogue (the mirror, the coverage report, kit duplication) take
    this instead of wiring three dependencies and re-deriving the relationship
    between them.
    """

    def __init__(
        self,
        kits: KitRepository | None = None,
        trees: TroubleshootingRepository | None = None,
        blueprints: BlueprintRepository | None = None,
        meta: CatalogMetaRepository | None = None,
    ) -> None:
        self.kits = kits or KitRepository()
        self.trees = trees or TroubleshootingRepository()
        self.blueprints = blueprints or BlueprintRepository()
        self.meta = meta or CatalogMetaRepository()

    def initialised(self) -> bool:
        return self.kits.exists() and self.trees.exists()

    def snapshot(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """One consistent read of all three documents.

        Read in dependency order and returned together so a caller that iterates
        kits and looks up trees cannot observe a kit whose tree was written by a
        concurrent save it did not see.
        """
        return self.kits.all(), self.trees.all(), self.blueprints.all()
