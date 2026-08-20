#!/usr/bin/env python3
"""Move the NAS installation's data into the new layout.

The monolith kept code, secrets, state and uploads in one flat directory. The new
layout separates them, so a migration has to relocate files as well as copy them:

    NAS: /mnt/disk1/share/ZZ2 Tech Support Tool/
        credentials.json          -> dropped (Supabase Auth owns identities)
        sessions.json             -> dropped (Supabase Auth owns sessions)
        login_fails.json          -> dropped (Supabase Auth owns throttling)
        config.json               -> split: secrets to the environment,
                                     tunables to var/config.json
        roles.json                -> var/roles.json
        stats.json                -> var/stats.json
        analytics.json            -> var/analytics.json
        pending_writes.json       -> var/pending_writes.json
        dead_letters.json         -> var/dead_letters.json
        data/kits.json            -> var/store/kits.json
        data/troubleshooting.json -> var/store/troubleshooting.json
        data/blueprints.json      -> var/store/blueprints.json
        data/catalog_meta.json    -> var/store/catalog_meta.json
        data/kit_history/         -> var/store/kit_history/
        images/<KIT>/             -> var/media/images/<KIT>/
        manuals/<KIT>/            -> var/media/manuals/<KIT>/
        drafts/                   -> var/drafts/
        backups/, cache/, logs/   -> dropped (transient or superseded)

Copies, never moves: the NAS installation stays intact and working until someone
decides to switch it off.

Usage:

    python scripts/migrate_from_nas.py --source "/Volumes/share/ZZ2 Tech Support Tool" \\
                                       --dest /var/lib/zz2 [--dry-run]

The secrets in ``config.json`` are **printed as environment lines, never written**.
Writing them to a file would recreate the problem this migration exists to fix.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

#: ``(relative source, relative destination)``. Absent sources are skipped.
FILE_MOVES: tuple[tuple[str, str], ...] = (
    ("roles.json", "roles.json"),
    ("stats.json", "stats.json"),
    ("analytics.json", "analytics.json"),
    ("pending_writes.json", "pending_writes.json"),
    ("dead_letters.json", "dead_letters.json"),
    ("data/kits.json", "store/kits.json"),
    ("data/troubleshooting.json", "store/troubleshooting.json"),
    ("data/blueprints.json", "store/blueprints.json"),
    ("data/catalog_meta.json", "store/catalog_meta.json"),
    ("data/analytics_view.json", "store/analytics_view.json"),
)

DIRECTORY_MOVES: tuple[tuple[str, str], ...] = (
    ("data/kit_history", "store/kit_history"),
    ("images", "media/images"),
    ("manuals", "media/manuals"),
    ("drafts", "drafts"),
)

#: Deliberately not migrated, with the reason. Reported so the operator sees the
#: decision rather than wondering whether the script missed them.
DROPPED = {
    "credentials.json": "Supabase Auth owns identities; create the accounts there",
    "sessions.json": "Supabase Auth owns sessions",
    "login_fails.json": "Supabase Auth owns login throttling",
    "backups": "supersede with a fresh backup after the migration",
    "cache": "transient",
    "logs": "the new deployment writes its own",
}

#: config.json keys that are secret. Printed as environment lines, never written.
SECRET_KEYS = {
    "secret_key": "ZZ2_APPSCRIPT_SECRET",
    "supabase_service_key": "ZZ2_SUPABASE_SERVICE_KEY",
    "supabase_publishable_key": "ZZ2_SUPABASE_PUBLISHABLE_KEY",
    "supabase_anon_key": "ZZ2_SUPABASE_PUBLISHABLE_KEY",
    "supabase_url": "ZZ2_SUPABASE_URL",
    "webapp_url": "ZZ2_APPSCRIPT_URL",
    "customer_history_schema": "ZZ2_CUSTOMER_HISTORY_SCHEMA",
}

#: config.json keys that stay in config.json, because the Settings page writes
#: them at runtime and they are not secret.
TUNABLE_KEYS = {
    "poll_interval", "pane_refresh_interval", "reconcile_interval",
    "reconcile_days", "auto_resolve_days", "auto_resolve_enabled",
    "backup_keep", "backup_include_media", "work_window_enabled",
    "work_start", "work_end",
}


@dataclass
class Report:
    copied: list[str]
    skipped: list[str]
    dropped: list[str]
    environment: dict[str, str]
    unknown_config: dict[str, object]


def migrate(source: Path, dest: Path, *, dry_run: bool) -> Report:
    report = Report([], [], [], {}, {})

    for directory in ("store", "store/kit_history", "media/images", "media/manuals",
                      "drafts", "logs", "backups", "cache"):
        target = dest / directory
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True, mode=0o750)

    for relative_source, relative_dest in FILE_MOVES:
        origin = source / relative_source
        if not origin.is_file():
            report.skipped.append(relative_source)
            continue
        target = dest / relative_dest
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            shutil.copy2(origin, target)
            target.chmod(0o640)
        report.copied.append(f"{relative_source} -> {relative_dest}")

    for relative_source, relative_dest in DIRECTORY_MOVES:
        origin = source / relative_source
        if not origin.is_dir():
            report.skipped.append(relative_source + "/")
            continue
        target = dest / relative_dest
        if not dry_run:
            shutil.copytree(origin, target, dirs_exist_ok=True)
            _tighten(target)
        count = sum(1 for path in origin.rglob("*") if path.is_file())
        report.copied.append(f"{relative_source}/ -> {relative_dest}/ ({count} files)")

    for name, reason in DROPPED.items():
        if (source / name).exists():
            report.dropped.append(f"{name} — {reason}")

    _split_config(source, dest, report, dry_run=dry_run)
    return report


def _tighten(root: Path) -> None:
    """Strip world permissions from a freshly copied tree.

    ``shutil.copytree`` preserves the source's modes, and the NAS share was
    world-readable — so a straight copy would carry 0644 onto the VPS. That
    matters most for ``drafts/``, which holds unsaved ticket text, and it silently
    undoes the whole point of the 0750 state directory.
    """
    for path in root.rglob("*"):
        try:
            path.chmod(0o750 if path.is_dir() else 0o640)
        except OSError:
            continue
    try:
        root.chmod(0o750)
    except OSError:
        pass


def _split_config(source: Path, dest: Path, report: Report, *, dry_run: bool) -> None:
    """Separate ``config.json`` into environment secrets and runtime tunables."""
    origin = source / "config.json"
    if not origin.is_file():
        report.skipped.append("config.json")
        return

    try:
        config = json.loads(origin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  ! could not read config.json: {exc}", file=sys.stderr)
        return
    if not isinstance(config, dict):
        print("  ! config.json is not an object", file=sys.stderr)
        return

    for key, env_name in SECRET_KEYS.items():
        value = config.get(key)
        if value:
            # Later keys win only if the earlier one was empty — supabase_anon_key
            # and supabase_publishable_key map to the same variable.
            report.environment.setdefault(env_name, str(value))

    tunables = {key: value for key, value in config.items() if key in TUNABLE_KEYS}
    if tunables and not dry_run:
        target = dest / "config.json"
        target.write_text(json.dumps(tunables, indent=2) + "\n", encoding="utf-8")
        target.chmod(0o640)
    if tunables:
        report.copied.append(f"config.json -> config.json ({len(tunables)} tunables)")

    report.unknown_config = {
        key: value for key, value in config.items()
        if key not in TUNABLE_KEYS and key not in SECRET_KEYS
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, type=Path,
                        help="the NAS app folder (mounted)")
    parser.add_argument("--dest", required=True, type=Path,
                        help="the new data directory, e.g. /var/lib/zz2")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen and write nothing")
    args = parser.parse_args()

    if not args.source.is_dir():
        print(f"source not found: {args.source}", file=sys.stderr)
        return 1

    prefix = "[dry run] " if args.dry_run else ""
    print(f"{prefix}{args.source}  ->  {args.dest}\n")

    report = migrate(args.source, args.dest, dry_run=args.dry_run)

    print(f"copied ({len(report.copied)}):")
    for line in report.copied:
        print(f"  {line}")

    if report.skipped:
        print(f"\nnot present ({len(report.skipped)}):")
        for line in report.skipped:
            print(f"  {line}")

    if report.dropped:
        print(f"\ndeliberately not migrated ({len(report.dropped)}):")
        for line in report.dropped:
            print(f"  {line}")

    if report.unknown_config:
        print("\nconfig.json keys this script does not recognise "
              "(check whether they still matter):")
        for key in sorted(report.unknown_config):
            print(f"  {key}")

    if report.environment:
        print("\n" + "=" * 72)
        print("Add these to /etc/zz2/zz2.env — they are NOT written anywhere by")
        print("this script, because a secret in a file inside the web root is the")
        print("problem this migration exists to fix.")
        print("=" * 72)
        for name in sorted(report.environment):
            print(f"{name}={report.environment[name]}")
        print("=" * 72)
        print("\nThen rotate the Apps Script secret: it lived in a file that the")
        print("old server published on request, so treat it as already exposed.")

    print(f"\n{prefix}done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
