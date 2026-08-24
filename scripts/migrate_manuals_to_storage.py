#!/usr/bin/env python3
"""Upload local kit manuals into the configured private Supabase bucket.

The operation is idempotent: existing object paths are replaced with the local
PDF bytes. Use ``--dry-run`` to inspect paths without contacting Supabase.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Direct execution sets sys.path to scripts/, not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zz2.core.errors import UpstreamError
from zz2.gateways.storage import storage_gateway
from zz2.paths import MANUALS_DIR


def manual_files(root: Path) -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    if not root.is_dir():
        return rows
    for kit_dir in sorted(root.iterdir()):
        if not kit_dir.is_dir():
            continue
        for path in sorted(kit_dir.glob("*.pdf")):
            if path.is_file():
                rows.append((kit_dir.name, path))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=MANUALS_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--ensure-bucket",
        action="store_true",
        help="create/verify the bucket even when there are no local PDFs",
    )
    args = parser.parse_args()

    rows = manual_files(args.root)
    print(f"manuals found: {len(rows)}")
    if args.dry_run:
        for kit_key, path in rows:
            print(f"  {kit_key}/{path.name}")
        return 0

    if not storage_gateway.configured:
        print("Supabase Storage is not configured.", file=sys.stderr)
        return 1
    if args.ensure_bucket or rows:
        try:
            storage_gateway.ensure_bucket()
        except UpstreamError as exc:
            print(exc.log_detail or exc.message, file=sys.stderr)
            return 1

    uploaded = 0
    for kit_key, path in rows:
        data = path.read_bytes()
        if not data.startswith(b"%PDF-"):
            print(f"skipped non-PDF bytes: {kit_key}/{path.name}", file=sys.stderr)
            continue
        try:
            storage_gateway.upload_manual(kit_key, path.name, data)
        except UpstreamError as exc:
            print(f"failed: {kit_key}/{path.name}: {exc.message}", file=sys.stderr)
            return 1
        uploaded += 1
        print(f"uploaded: {kit_key}/{path.name}")

    print(f"uploaded: {uploaded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
