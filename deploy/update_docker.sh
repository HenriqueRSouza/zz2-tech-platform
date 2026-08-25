#!/usr/bin/env bash
# Update a Docker VPS deployment from origin/main, with automatic rollback.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.yml"
BRANCH="${ZZ2_BRANCH:-main}"
HEALTH_URL="${ZZ2_HEALTH_URL:-http://127.0.0.1:8080/healthz}"
HEALTH_TIMEOUT="${ZZ2_HEALTH_TIMEOUT:-90}"

log() { printf '==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cd "$ROOT_DIR"
command -v docker >/dev/null 2>&1 || fail "Docker is not installed"
docker compose version >/dev/null 2>&1 || fail "the Docker Compose plugin is not installed"
"$ROOT_DIR/deploy/check_env.sh"

[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || fail "tracked files were changed on the VPS; commit or restore them before deploying"

PREVIOUS="$(git rev-parse HEAD)"
log "fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH"
git merge --ff-only "origin/$BRANCH" \
  || fail "cannot fast-forward to origin/$BRANCH"

BUILD="$(git rev-parse --short HEAD)"
log "deploying build $BUILD"
if ZZ2_BUILD="$BUILD" docker compose -f "$COMPOSE_FILE" up -d --build --remove-orphans; then
  deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    body="$(curl -fsS --max-time 4 "$HEALTH_URL" 2>/dev/null || true)"
    if printf '%s' "$body" | grep -q "\"build\":\"$BUILD\""; then
      log "deploy complete; live build is $BUILD"
      exit 0
    fi
    sleep 2
  done
fi

printf 'ERROR: build %s failed; rolling back to %s\n' "$BUILD" "${PREVIOUS:0:8}" >&2
git reset --hard "$PREVIOUS" --quiet
ROLLBACK_BUILD="$(git rev-parse --short HEAD)"
ZZ2_BUILD="$ROLLBACK_BUILD" docker compose -f "$COMPOSE_FILE" up -d --build --remove-orphans
fail "deployment rolled back; inspect: docker compose -f deploy/docker-compose.yml logs --tail=200 app"
