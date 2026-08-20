#!/usr/bin/env python3
"""Mint a local access token, for development only.

Without real Supabase credentials nobody can sign in, which makes the app
impossible to explore locally — every page redirects to a sign-in form that
cannot succeed. This signs a token with the *local* ``ZZ2_SUPABASE_JWT_SECRET``,
so the application's normal verification path accepts it.

    python scripts/dev_token.py                       # a super-admin token
    python scripts/dev_token.py --role tech           # a technician
    python scripts/dev_token.py --browser             # a snippet to paste in devtools

**Refuses to run when ``ZZ2_ENVIRONMENT`` is ``production``.** The guard is the
point: this bypasses Supabase entirely, so it must be impossible to point at a
real deployment by accident.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

ALLOWED_ENVIRONMENTS = {"development", "staging"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", default="super_admin",
                        help="role key, e.g. super_admin, admin, tech")
    parser.add_argument("--email", default="dev@zz2.com.br")
    parser.add_argument("--name", default="Dev User")
    parser.add_argument("--permissions", default="",
                        help="comma-separated extra permissions on top of the role")
    parser.add_argument("--hours", type=float, default=8.0,
                        help="lifetime in hours (default: a working day)")
    parser.add_argument("--browser", action="store_true",
                        help="print a devtools snippet instead of the bare token")
    args = parser.parse_args()

    environment = os.environ.get("ZZ2_ENVIRONMENT", "production")
    if environment not in ALLOWED_ENVIRONMENTS:
        print(f"refusing to run with ZZ2_ENVIRONMENT={environment!r}.\n"
              "This bypasses Supabase authentication and is for local use only.",
              file=sys.stderr)
        return 1

    secret = os.environ.get("ZZ2_SUPABASE_JWT_SECRET", "")
    if not secret:
        print("ZZ2_SUPABASE_JWT_SECRET is not set — source your dev environment "
              "first (scripts/run_dev.sh exports it).", file=sys.stderr)
        return 1

    try:
        from jose import jwt
    except ImportError:
        print("python-jose is not installed. Use the project's virtualenv:\n"
              "  .venv/bin/python scripts/dev_token.py", file=sys.stderr)
        return 1

    extra = [p.strip() for p in args.permissions.split(",") if p.strip()]
    token = jwt.encode(
        {
            "sub": f"dev-{uuid.uuid4().hex[:8]}",
            "email": args.email,
            "aud": os.environ.get("ZZ2_SUPABASE_JWT_AUDIENCE", "authenticated"),
            "exp": int(time.time() + args.hours * 3600),
            "app_metadata": {"role": args.role, "permissions": extra},
            "user_metadata": {"display_name": args.name},
        },
        secret,
        algorithm="HS256",
    )

    if args.browser:
        print("Open the app, then paste this into the browser console:\n")
        print(f"localStorage.setItem('zz2_auth_token', '{token}');")
        print(f"localStorage.setItem('zz2_auth_role', '{args.role}');")
        print("location.href = '/dashboard.html';")
    else:
        print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
