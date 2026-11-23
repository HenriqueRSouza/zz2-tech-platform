#!/usr/bin/env python3
"""Copy every table and every user account from Supabase to Azure PostgreSQL.

Reads Supabase through its REST API with the service key (no direct database
password is needed) and writes to Azure with plain SQL. Idempotent: rows are
upserted on their primary key, so the script can be re-run to pick up changes
made in Supabase until the cut-over.

    SUPABASE_URL=https://xxxx.supabase.co \\
    SUPABASE_SERVICE_KEY=sb_secret_... \\
    ZZ2_DATABASE_URL=postgresql://...?sslmode=require \\
    .venv/bin/python scripts/migrate_supabase_to_azure.py [--dry-run] [--skip-users]

Falls back to ZZ2_SUPABASE_URL / ZZ2_SUPABASE_SERVICE_KEY (the old .env names).

Users: Supabase never reveals password hashes through its API, so accounts are
created **without a password**. Each user then signs in with Microsoft (same
email) or an administrator sets a new password on the Users page. Account IDs,
roles, permissions, display names and the active flag are preserved.

Run sql/azure_schema.sql first (scripts/apply_schema.py does it).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
PAGE = 1000

# (schema, table, conflict columns, jsonb columns)
TABLES: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("techSupport", "tickets", ("id",), ()),
    ("techSupport", "kits", ("kit_key",), ()),
    ("techSupport", "troubleshooting", ("kit_key",), ("tree",)),
    ("techSupport", "blueprints", ("id",), ("troubleshooting",)),
    ("techSupport", "manual_assets", ("kit_key", "filename"), ()),
    ("techSupport", "troubleshooting_sessions", ("id",), ("path", "checked_steps")),
    ("zoho", "customer_performance_semesters", ("customer_name", "period_year", "semester"), ()),
    ("zoho", "customer_performance_months", ("customer_name", "period_year", "month"), ()),
    ("zoho", "sales_by_item", ("item_sku", "period_start"), ()),
]


def env(*names: str) -> str:
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    sys.exit(f"missing environment variable: {' or '.join(names)}")


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


class Supabase:
    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.client = httpx.Client(timeout=60, headers={"apikey": key,
                                                        "Authorization": f"Bearer {key}"})

    def rows(self, schema: str, table: str) -> list[dict[str, Any]] | None:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            r = self.client.get(f"{self.url}/rest/v1/{table}",
                                params={"select": "*", "limit": PAGE, "offset": offset},
                                headers={"Accept-Profile": schema})
            if r.status_code in (404, 406) or (r.status_code == 400 and "PGRST" in r.text):
                print(f"  ! {schema}.{table}: not available in Supabase ({r.status_code}); skipped")
                return None
            r.raise_for_status()
            batch = r.json()
            out.extend(batch)
            if len(batch) < PAGE:
                return out
            offset += PAGE

    def users(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 100):
            r = self.client.get(f"{self.url}/auth/v1/admin/users",
                                params={"page": page, "per_page": 200})
            r.raise_for_status()
            batch = r.json().get("users") or []
            out.extend(batch)
            if len(batch) < 200:
                return out
        return out


def target_columns(conn: psycopg.Connection, schema: str, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s", (schema, table)).fetchall()
    return {r[0] for r in rows}


def copy_table(conn: psycopg.Connection, sb: Supabase, schema: str, table: str,
               conflict: tuple[str, ...], jsonb: tuple[str, ...], dry_run: bool) -> int:
    rows = sb.rows(schema, table)
    if rows is None:
        return 0
    if not rows:
        print(f"  {schema}.{table}: 0 rows")
        return 0
    allowed = target_columns(conn, schema, table)
    columns = [c for c in rows[0] if c in allowed]
    dropped = sorted(set(rows[0]) - allowed)
    if dropped:
        print(f"  ! {schema}.{table}: ignoring columns not in Azure schema: {dropped}")
    updates = [c for c in columns if c not in conflict]
    overriding = sql.SQL("OVERRIDING SYSTEM VALUE ") if table == "tickets" else sql.SQL("")
    statement = sql.SQL("INSERT INTO {t} ({cols}) {ov}VALUES ({vals}) ON CONFLICT ({key}) ").format(
        t=sql.Identifier(schema, table),
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        ov=overriding,
        vals=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
        key=sql.SQL(", ").join(map(sql.Identifier, conflict)),
    )
    statement += (sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(
        sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in updates)
    ) if updates else sql.SQL("DO NOTHING")

    def value(column: str, raw: Any) -> Any:
        return Jsonb(raw) if column in jsonb and raw is not None else raw

    params = [[value(c, row.get(c)) for c in columns] for row in rows]
    if not dry_run:
        with conn.cursor() as cur:
            cur.executemany(statement, params)
            if table == "tickets":
                cur.execute(
                    "SELECT setval(pg_get_serial_sequence('\"techSupport\".tickets', 'id'), "
                    "GREATEST((SELECT max(id) FROM \"techSupport\".tickets), 1))")
    print(f"  {schema}.{table}: {len(rows)} rows")
    return len(rows)


def copy_users(conn: psycopg.Connection, sb: Supabase, dry_run: bool) -> int:
    users = sb.users()
    n = 0
    for u in users:
        email = (u.get("email") or "").strip().lower()
        if not email:
            continue
        app = u.get("app_metadata") or {}
        meta = u.get("user_metadata") or {}
        perms = app.get("permissions") if isinstance(app.get("permissions"), list) else []
        banned = bool(u.get("banned_until"))
        display = meta.get("display_name") or meta.get("full_name") or meta.get("name") or ""
        if not dry_run:
            conn.execute(
                "INSERT INTO \"techSupport\".app_users (id, email, display_name, role, permissions, "
                "active, created_at, last_sign_in_at) VALUES (%s, %s, %s, %s, %s, %s, "
                "coalesce(%s::timestamptz, now()), %s::timestamptz) "
                "ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, "
                "display_name = EXCLUDED.display_name, role = EXCLUDED.role, "
                "permissions = EXCLUDED.permissions, active = EXCLUDED.active, updated_at = now()",
                (u["id"], email, display, app.get("role") or "tech", Jsonb(perms), not banned,
                 u.get("created_at"), u.get("last_sign_in_at")),
            )
        n += 1
        print(f"  user {email:<40} role={app.get('role') or 'tech':<10} active={not banned}")
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-users", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    sb = Supabase(env("SUPABASE_URL", "ZZ2_SUPABASE_URL"),
                  env("SUPABASE_SERVICE_KEY", "ZZ2_SUPABASE_SERVICE_KEY"))
    with psycopg.connect(env("ZZ2_DATABASE_URL"), connect_timeout=15) as conn:
        print("Tables:")
        total = sum(copy_table(conn, sb, *spec, dry_run=args.dry_run) for spec in TABLES)
        users = 0
        if not args.skip_users:
            print("Users:")
            users = copy_users(conn, sb, args.dry_run)
        if args.dry_run:
            conn.rollback()
        print(f"\nDone{' (dry run, nothing written)' if args.dry_run else ''}: "
              f"{total} rows, {users} users.")


if __name__ == "__main__":
    main()
