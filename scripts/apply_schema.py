#!/usr/bin/env python3
"""Apply sql/azure_schema.sql to ZZ2_DATABASE_URL (idempotent).

Optionally create the first administrator account:

    .venv/bin/python scripts/apply_schema.py --admin-email you@example.com
    (prompts for the password; role "super_admin" unless --admin-role is given)
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--admin-email")
    parser.add_argument("--admin-name", default="Administrador")
    parser.add_argument("--admin-role", default="super_admin")
    args = parser.parse_args()

    from zz2.settings import get_settings  # reads .env as the app does

    dsn = os.environ.get("ZZ2_DATABASE_URL") or get_settings().database_url.get_secret_value()
    if not dsn:
        sys.exit("ZZ2_DATABASE_URL is not set")
    with psycopg.connect(dsn, connect_timeout=15) as conn:
        conn.execute((ROOT / "sql" / "azure_schema.sql").read_text())
        print("schema applied")
        if args.admin_email:
            from zz2.gateways.user_directory import hash_password

            password = getpass.getpass(f"Password for {args.admin_email}: ")
            if len(password) < 12:
                sys.exit("password must have at least 12 characters")
            conn.execute(
                'INSERT INTO "techSupport".app_users (email, display_name, password_hash, role) '
                "VALUES (lower(%s), %s, %s, %s) ON CONFLICT ((lower(email))) DO UPDATE SET "
                "password_hash = EXCLUDED.password_hash, role = EXCLUDED.role, active = true",
                (args.admin_email, args.admin_name, hash_password(password), args.admin_role),
            )
            print(f"administrator {args.admin_email} ready")


if __name__ == "__main__":
    main()
