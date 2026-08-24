#!/usr/bin/env bash
#
# Start the application locally.
#
#   ./scripts/run_dev.sh
#
# Loads .env if it exists, then fills in development defaults for anything
# missing — so the app starts and every page renders without any configuration at
# all. What needs real Supabase credentials is listed on startup, because the
# alternative is a page that fails with no explanation of why.
#
# Never use this to run a real deployment: it forces ZZ2_ENVIRONMENT=development,
# which disables the production guardrails (https, CORS, docs).

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "!! no virtualenv. Create it first:" >&2
  echo "   python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# .env wins over the defaults below, so a real Supabase project can be dropped in
# without editing this script.
if [ -f .env ]; then
  echo "==> loading .env"
  set -a; . ./.env; set +a
fi

export ZZ2_ENVIRONMENT=development
export ZZ2_BUILD="${ZZ2_BUILD:-dev-$(git rev-parse --short HEAD 2>/dev/null || echo local)}"
export ZZ2_DATA_DIR="${ZZ2_DATA_DIR:-$PWD/var}"
export ZZ2_HOST="${ZZ2_HOST:-127.0.0.1}"
export ZZ2_PORT="${ZZ2_PORT:-8080}"
export ZZ2_PUBLIC_URL="${ZZ2_PUBLIC_URL:-http://127.0.0.1:${ZZ2_PORT}}"
export ZZ2_LOG_JSON="${ZZ2_LOG_JSON:-false}"

# A local signing secret, so scripts/dev_token.py can mint a usable token. Only
# valid for this machine; a real deployment takes it from Supabase.
export ZZ2_SUPABASE_JWT_SECRET="${ZZ2_SUPABASE_JWT_SECRET-local-development-jwt-secret-not-for-production}"
export ZZ2_SUPABASE_URL="${ZZ2_SUPABASE_URL:-https://placeholder.supabase.co}"
export ZZ2_SUPABASE_PUBLISHABLE_KEY="${ZZ2_SUPABASE_PUBLISHABLE_KEY:-sb_publishable_placeholder}"
export ZZ2_SUPABASE_SERVICE_KEY="${ZZ2_SUPABASE_SERVICE_KEY:-sb_secret_placeholder}"

# Off by default locally: the poller and the nightly job would hammer Apps Script
# (or log failures every 30 seconds) with no real integration configured.
export ZZ2_ENABLE_BACKGROUND_WORKERS="${ZZ2_ENABLE_BACKGROUND_WORKERS:-false}"
export ZZ2_ENABLE_SUPABASE_MIRROR="${ZZ2_ENABLE_SUPABASE_MIRROR:-false}"
export ZZ2_ENABLE_APPSCRIPT_SYNC="${ZZ2_ENABLE_APPSCRIPT_SYNC:-false}"

echo "==> data directory: $ZZ2_DATA_DIR"

placeholders=()
case "$ZZ2_SUPABASE_URL" in *placeholder*) placeholders+=("ZZ2_SUPABASE_URL");; esac
case "$ZZ2_SUPABASE_SERVICE_KEY" in *placeholder*) placeholders+=("ZZ2_SUPABASE_SERVICE_KEY");; esac

if [ ${#placeholders[@]} -gt 0 ]; then
  cat <<'NOTE'

    Running with placeholder Supabase credentials.

    Works:        every page renders, the kit editor, the kit catalogue,
                  troubleshooting trees, drafts, roles, settings, backups
    Does not:     signing in through the login form, ticket create/edit/search,
                  customer history, item sales, the Users page
                  (all of those need a reachable Supabase project)

    To explore the app now, mint a local token:
        .venv/bin/python scripts/dev_token.py --browser

    For the real thing, put your project's values in .env
    (see deploy/.env.example).

NOTE
fi

echo "==> http://${ZZ2_HOST}:${ZZ2_PORT}/   (Ctrl-C to stop)"
echo

exec .venv/bin/python -m uvicorn zz2.main:app \
    --host "$ZZ2_HOST" --port "$ZZ2_PORT" \
    --reload --reload-dir zz2 \
    --log-level info --no-access-log
