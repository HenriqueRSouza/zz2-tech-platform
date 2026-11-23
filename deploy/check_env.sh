#!/usr/bin/env bash
# Validate the production environment without printing any secret values.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${1:-$ROOT_DIR/deploy/.env}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[ -f "$ENV_FILE" ] || fail "missing $ENV_FILE (copy deploy/.env.example first)"

# deploy/.env is an operator-owned shell-compatible file. Exporting it here lets
# us validate the effective values while keeping them out of command output.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

required=(
  ZZ2_PUBLIC_URL
  ZZ2_TRUSTED_HOSTS
  ZZ2_DATABASE_URL
  ZZ2_SESSION_SECRET
)

for name in "${required[@]}"; do
  value="${!name:-}"
  [ -n "$value" ] || fail "$name is empty"
  case "$value" in
    *REPLACE*|*YOUR-PROJECT*|*example.com*)
      fail "$name still contains an example/placeholder value"
      ;;
  esac
done

case "$ZZ2_PUBLIC_URL" in
  https://*) ;;
  *) fail "ZZ2_PUBLIC_URL must begin with https:// in production" ;;
esac

case "$ZZ2_DATABASE_URL" in
  postgresql://*sslmode=require*|postgres://*sslmode=require*) ;;
  *) fail "ZZ2_DATABASE_URL must be a postgresql:// URL with sslmode=require" ;;
esac

[ "${#ZZ2_SESSION_SECRET}" -ge 32 ] || fail "ZZ2_SESSION_SECRET must be at least 32 characters"

if [ "${ZZ2_ENABLE_APPSCRIPT_SYNC:-false}" = "true" ]; then
  case "${ZZ2_APPSCRIPT_URL:-}:${ZZ2_APPSCRIPT_SECRET:-}" in
    *REPLACE*|:|*:)
      fail "Apps Script sync is enabled but its URL/secret is not configured"
      ;;
  esac
fi

printf 'Environment OK: %s\n' "$ENV_FILE"
