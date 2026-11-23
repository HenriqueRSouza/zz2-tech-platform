#!/usr/bin/env python3
"""Discover folder-based manuals and import them into Azure Blob Storage.

The source may contain any number of nested folders. A PDF is assigned to a kit
when one of its parent folder names matches a catalogue key (case-insensitive,
with spaces/hyphens ignored). For example both ``IT3-AUDI/guide.pdf`` and
``Audi/IT3 AUDI/guide.pdf`` resolve to ``IT3-AUDI``.

Every matched PDF is uploaded to the kit's private Storage folder and copied to
the local cache. The best filename is then written to the kit's ``manual`` field
so the Manual tab appears immediately. Existing object paths are replaced, which
makes the operation idempotent. Use ``--dry-run`` first to review every match.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

# Direct execution sets sys.path to scripts/, not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zz2.core.errors import UpstreamError
from zz2.core.text import safe_filename
from zz2.gateways.storage import storage_gateway
from zz2.paths import MANUALS_DIR
from zz2.repositories.kits import KitRepository


def _token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def discover_manuals(
    root: Path,
    kit_keys: list[str],
) -> tuple[dict[str, list[Path]], list[Path]]:
    """Match recursive PDFs to catalogue keys without guessing ambiguities."""
    exact = {key.casefold(): key for key in kit_keys}
    by_token: dict[str, list[str]] = defaultdict(list)
    for key in kit_keys:
        by_token[_token(key)].append(key)

    matched: dict[str, list[Path]] = defaultdict(list)
    unmatched: list[Path] = []
    if not root.is_dir():
        return {}, []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            continue

        relative = path.relative_to(root)
        folder_candidates = list(reversed(relative.parts[:-1]))
        candidates = folder_candidates + [path.stem]
        resolved = ""
        for candidate in candidates:
            resolved = exact.get(candidate.casefold(), "")
            if resolved:
                break
            possibilities = by_token.get(_token(candidate), [])
            if len(possibilities) == 1:
                resolved = possibilities[0]
                break

        if resolved:
            matched[resolved].append(path)
        else:
            unmatched.append(path)

    return dict(matched), unmatched


def choose_primary(kit_key: str, paths: list[Path]) -> Path:
    """Choose one deterministic PDF for the catalogue's single manual field."""
    kit_token = _token(kit_key)

    def rank(path: Path) -> tuple[int, int, str]:
        stem = _token(path.stem)
        if stem == kit_token:
            preference = 0
        elif "MANUAL" in stem or "INSTALL" in stem:
            preference = 1
        else:
            preference = 2
        return preference, len(path.name), path.name.casefold()

    return min(paths, key=rank)


def destination_name(kit_key: str, path: Path) -> str:
    """Return the same safe filename used by authenticated download routes."""
    return safe_filename(path.name, fallback=f"{kit_key}.pdf")


def _cache_manual(kit_key: str, path: Path, filename: str) -> None:
    directory = MANUALS_DIR / kit_key
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    target = directory / filename
    if path.resolve() != target.resolve():
        shutil.copy2(path, target)
    target.chmod(0o640)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=MANUALS_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--ensure-bucket",
        action="store_true",
        help="create/verify the bucket even when there are no local PDFs",
    )
    parser.add_argument(
        "--no-local-cache",
        action="store_true",
        help="upload only; do not copy PDFs into the VPS data directory",
    )
    parser.add_argument(
        "--no-update-catalog",
        action="store_true",
        help="upload only; do not select a primary manual for each kit",
    )
    args = parser.parse_args()

    repository = KitRepository()
    kits = repository.all()
    if not kits:
        print("The kit catalogue is empty; initialise it before importing manuals.",
              file=sys.stderr)
        return 1

    groups, unmatched = discover_manuals(args.root, list(kits))
    rows = [(key, path) for key in sorted(groups) for path in groups[key]]
    print(f"manuals found: {len(rows)} across {len(groups)} matched kits")
    for path in unmatched:
        print(f"unmatched: {path.relative_to(args.root)}")
    if args.dry_run:
        for kit_key, path in rows:
            marker = " [primary]" if path == choose_primary(kit_key, groups[kit_key]) else ""
            filename = destination_name(kit_key, path)
            print(f"  {path.relative_to(args.root)} -> {kit_key}/{filename}{marker}")
        print(f"unmatched PDFs: {len(unmatched)}")
        return 0

    if not storage_gateway.configured:
        print("Azure Blob Storage is not configured.", file=sys.stderr)
        return 1
    if args.ensure_bucket or rows:
        try:
            storage_gateway.ensure_bucket()
        except UpstreamError as exc:
            print(exc.log_detail or exc.message, file=sys.stderr)
            return 1

    uploaded = 0
    updated = 0
    for kit_key in sorted(groups):
        destinations: dict[str, Path] = {}
        for path in groups[kit_key]:
            filename = destination_name(kit_key, path)
            collision = destinations.get(filename.casefold())
            if collision is not None:
                print(
                    f"filename collision for {kit_key}/{filename}: "
                    f"{collision} and {path}",
                    file=sys.stderr,
                )
                return 1
            destinations[filename.casefold()] = path

        valid_paths: list[tuple[Path, str]] = []
        for path in groups[kit_key]:
            filename = destination_name(kit_key, path)
            data = path.read_bytes()
            if not data.startswith(b"%PDF-"):
                print(f"skipped non-PDF bytes: {path}", file=sys.stderr)
                continue
            try:
                storage_gateway.upload_manual(kit_key, filename, data)
            except UpstreamError as exc:
                print(f"failed: {kit_key}/{filename}: {exc.message}", file=sys.stderr)
                return 1
            if not args.no_local_cache:
                _cache_manual(kit_key, path, filename)
            valid_paths.append((path, filename))
            uploaded += 1
            print(f"uploaded: {kit_key}/{filename}")

        if valid_paths and not args.no_update_catalog:
            primary = choose_primary(kit_key, [path for path, _ in valid_paths])
            primary_name = next(filename for path, filename in valid_paths if path == primary)
            record = dict(kits[kit_key])
            record["manual"] = primary_name
            repository.save(kit_key, record)
            updated += 1
            print(f"catalogued: {kit_key} -> {primary_name}")

    print(f"uploaded: {uploaded}")
    print(f"catalogued kits: {updated}")
    print(f"unmatched PDFs: {len(unmatched)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
