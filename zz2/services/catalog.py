"""Kit catalogue bootstrap and health.

Two responsibilities that both need a whole-catalogue view.

**Bootstrap.** An installation upgraded from the Google-Sheets era has kit images
on disk but no ``kits.json``. This service infers kit records from the directory
names under ``media/images`` and ``media/manuals`` once, then the Kit Editor owns
the data.

**Seeding.** Kits with an empty troubleshooting tree receive a standard diagnostic
flow, once. The version marker in ``catalog_meta.json`` is what makes it once —
and that matters: without it, a tree an administrator deliberately emptied would
be refilled on the next boot, and the deletion would look like it never saved.

The inference is also gated on the catalogue *not previously existing*. Once it
does, the Kit Editor is authoritative: a kit that was deleted on purpose must not
reappear because an orphaned image folder is still on disk.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from zz2.core.errors import StorageError
from zz2.core.logging import logger
from zz2.core.text import as_text, safe_key
from zz2.paths import APP_DIR, IMAGES_DIR, MANUALS_DIR
from zz2.repositories.kits import KitCatalogRepository

STANDARD_TREE_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "standard_troubleshooting.json"
)

CURRENT_SEED_VERSION = 1

# A new Docker volume has no mutable catalogue yet. The production image ships
# the last NAS catalogue here so first boot can initialise the volume without a
# manual ``docker cp``. It is only consulted for an empty, never-before-seeded
# catalogue; after that, the Kit Editor remains authoritative.
PACKAGED_CATALOG_DIR = APP_DIR / "seed" / "catalog"
SYSTEMD_CATALOG_DIR = APP_DIR / "backups" / "20260820_000013" / "data"


def initial_catalog_dir() -> Path:
    """Locate the seed in both supported deployment layouts.

    Docker copies the two documents to ``seed/catalog`` to keep every other
    backup out of the image. A systemd/source deployment runs from the checkout,
    where the same documents remain under the last NAS backup.
    """
    for candidate in (PACKAGED_CATALOG_DIR, SYSTEMD_CATALOG_DIR):
        if (candidate / "kits.json").is_file() and (
            candidate / "troubleshooting.json"
        ).is_file():
            return candidate
    return PACKAGED_CATALOG_DIR

IMAGE_KINDS = ("dip", "wiring", "misc")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

STANDARD_BLUEPRINT_ID = "bp_standard_diagnostics"
STANDARD_BLUEPRINT_NAME = "Standard interface diagnostics"


@lru_cache(maxsize=1)
def standard_troubleshooting_tree() -> list[dict[str, Any]]:
    """The baseline diagnostic flow, loaded from ``data/``.

    Content, not code — 19 nodes of support procedure that the support team
    edits. It lived as a 130-line literal in the middle of the monolith, where
    changing a troubleshooting step meant editing the server source.

    Cached, and callers get a deep copy: the returned tree is written into per-kit
    documents, and a shared mutable default would let one kit's edit alter the
    template for every future kit.
    """
    try:
        with STANDARD_TREE_FILE.open(encoding="utf-8") as handle:
            tree = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(log_detail=f"standard troubleshooting tree: {exc}") from exc
    if not isinstance(tree, list):
        raise StorageError(log_detail="standard troubleshooting tree is not a list")
    return tree


def fresh_standard_tree() -> list[dict[str, Any]]:
    """A private copy of the standard tree, safe to mutate and store."""
    return copy.deepcopy(standard_troubleshooting_tree())


class CatalogService:
    """Ensures the local kit catalogue exists and reports on its health."""

    def __init__(
        self,
        catalog: KitCatalogRepository | None = None,
        seed_dir: Path | None = None,
    ) -> None:
        self._catalog = catalog or KitCatalogRepository()
        self._seed_dir = seed_dir or initial_catalog_dir()

    # -- Bootstrap -------------------------------------------------------------
    def ensure_initialised(self) -> dict[str, Any]:
        """Create or repair the catalogue. Idempotent; safe to call on each boot.

        Returns a summary of what exists afterwards.
        """
        kits_existed = self._catalog.kits.exists()
        trees_existed = self._catalog.trees.exists()

        kits = self._catalog.kits.all()
        trees = self._catalog.trees.all()

        # Docker creates the persistent data volume empty. Earlier builds then
        # wrote ``{}`` to kits.json during startup, so checking file existence is
        # not enough to repair an already-affected VPS. The durable marker lets
        # us seed that empty catalogue once while still respecting a later,
        # intentional deletion of every kit in the editor.
        if not kits and not self._catalog.meta.bundle_seeded():
            seeded = self._seed_from_bundle()
            if seeded is not None:
                kits, trees = seeded
                self._catalog.meta.mark_bundle_seeded()
                logger.info("seeded initial kit catalogue",
                            extra={"kits": len(kits), "source": str(self._seed_dir)})

        if not kits_existed:
            inferred = self._infer_kits_from_disk()
            if inferred:
                kits.update(inferred)
                self._catalog.kits.write(kits)
                logger.info("inferred kit catalogue from media directories",
                            extra={"kits": len(inferred)})

        # Invariant: every kit has a tree entry, even an empty one. Collapsing
        # "absent" and "empty" removes a null check from every consumer.
        trees_changed = not trees_existed
        for key in kits:
            if not isinstance(trees.get(key), list):
                trees[key] = []
                trees_changed = True

        if self._catalog.meta.seed_version() < CURRENT_SEED_VERSION:
            seeded = 0
            for key in kits:
                if not trees.get(key):
                    trees[key] = fresh_standard_tree()
                    trees_changed = True
                    seeded += 1
            self._catalog.meta.mark_seeded(CURRENT_SEED_VERSION)
            if seeded:
                logger.info("seeded standard troubleshooting trees",
                            extra={"kits": seeded})

        if trees_changed:
            self._catalog.trees.write(trees)

        if not self._catalog.blueprints.exists():
            self._seed_standard_blueprint()

        return {"kits": len(kits), "troubleshooting": len(trees)}

    def _seed_from_bundle(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Copy the packaged migration catalogue into an empty data directory.

        Both documents are validated before either is written. A missing seed is
        normal for development/source installs; an invalid seed is a build error
        and is surfaced instead of silently starting with no kits.
        """
        kits_path = self._seed_dir / "kits.json"
        trees_path = self._seed_dir / "troubleshooting.json"
        if not kits_path.is_file() and not trees_path.is_file():
            return None

        try:
            with kits_path.open(encoding="utf-8") as handle:
                kits = json.load(handle)
            with trees_path.open(encoding="utf-8") as handle:
                trees = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(log_detail=f"initial catalogue bundle: {exc}") from exc

        if not isinstance(kits, dict) or not kits:
            raise StorageError(log_detail="initial kits bundle is not a non-empty object")
        if not isinstance(trees, dict):
            raise StorageError(log_detail="initial troubleshooting bundle is not an object")

        self._catalog.kits.write(kits)
        self._catalog.trees.write(trees)
        return kits, trees

    def _infer_kits_from_disk(self) -> dict[str, dict[str, Any]]:
        """Build kit records from the media directories present on disk."""
        keys: set[str] = set()
        for root in (IMAGES_DIR, MANUALS_DIR):
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.is_dir() and safe_key(entry.name):
                    keys.add(entry.name)

        inferred: dict[str, dict[str, Any]] = {}
        for key in sorted(keys):
            record: dict[str, Any] = {
                "vehicle": "", "switch_button": "", "audio_method": "", "notes": "",
                "dip_image": "", "wiring_image": "", "misc_image": "", "manual": "",
            }
            image_dir = IMAGES_DIR / key
            for kind in IMAGE_KINDS:
                for extension in IMAGE_EXTENSIONS:
                    if (image_dir / f"{kind}{extension}").is_file():
                        record[f"{kind}_image"] = f"/images/{key}/{kind}{extension}"
                        break

            manual_dir = MANUALS_DIR / key
            if manual_dir.is_dir():
                pdfs = sorted(
                    entry.name for entry in manual_dir.iterdir()
                    if entry.is_file() and entry.suffix.lower() == ".pdf"
                )
                if pdfs:
                    record["manual"] = pdfs[0]
            inferred[key] = record
        return inferred

    def _seed_standard_blueprint(self) -> None:
        """Install the reusable standard flow as a blueprint."""
        import time

        now = int(time.time())
        self._catalog.blueprints.write({
            STANDARD_BLUEPRINT_ID: {
                "name": STANDARD_BLUEPRINT_NAME,
                "troubleshooting": fresh_standard_tree(),
                "created_at": now,
                "updated_at": now,
            }
        })
        logger.info("seeded standard diagnostics blueprint")

    # -- Health ----------------------------------------------------------------
    def coverage(self) -> dict[str, Any]:
        """Report gaps in every kit's troubleshooting tree.

        Two defects are reported, and the second is the interesting one:

        * **Empty tree** — the kit has no diagnostic content at all.
        * **Branching node with no question heading** — a node that has children
          but neither steps of its own nor a question. The tool renders a generic
          prompt for it, so the technician sees a menu with no explanation of what
          they are choosing between. It looks fine in the editor and wrong in the
          tool, which is why it needs a report rather than review.
        """
        kits, trees, _ = self._catalog.snapshot()
        kit_keys = set(kits.keys()) if isinstance(kits, dict) else set()
        all_keys = sorted(kit_keys | set(trees.keys()))

        rows: list[dict[str, Any]] = []
        total_missing = 0

        for key in all_keys:
            nodes = [node for node in (trees.get(key) or []) if isinstance(node, dict)]
            issues_with_children = {
                as_text(node.get("parent")).strip()
                for node in nodes
                if as_text(node.get("parent")).strip()
            }

            missing: list[str] = []
            for node in nodes:
                issue = as_text(node.get("issue")).strip()
                if not issue:
                    continue
                has_steps = any(as_text(step).strip() for step in (node.get("steps") or []))
                has_question = bool(as_text(node.get("question")).strip())
                if issue in issues_with_children and not has_steps and not has_question:
                    missing.append(issue)

            total_missing += len(missing)
            rows.append({
                "kit": key,
                "nodes": len(nodes),
                "top_level": sum(1 for n in nodes if not as_text(n.get("parent")).strip()),
                "empty": not nodes,
                "missing_headings": missing,
                "missing_count": len(missing),
            })

        # Worst first: most gaps, then empty trees, then alphabetical. Puts the
        # kits that need work at the top of the page.
        rows.sort(key=lambda row: (-row["missing_count"], 0 if row["empty"] else 1, row["kit"]))

        return {
            "ok": True,
            "kits_total": len(rows),
            "kits_empty": sum(1 for row in rows if row["empty"]),
            "kits_with_gaps": sum(1 for row in rows if row["missing_count"] > 0),
            "missing_headings_total": total_missing,
            "rows": rows,
        }


catalog_service = CatalogService()
