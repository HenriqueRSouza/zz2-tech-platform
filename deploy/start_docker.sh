#!/usr/bin/env bash
# First (or manual) Docker deployment from an already cloned repository.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.yml"
HEALTH_URL="${ZZ2_HEALTH_URL:-http://127.0.0.1:8080/healthz}"
HEALTH_TIMEOUT="${ZZ2_HEALTH_TIMEOUT:-90}"

log() { printf '==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker is not installed"
docker compose version >/dev/null 2>&1 || fail "the Docker Compose plugin is not installed"

"$ROOT_DIR/deploy/check_env.sh"

BUILD="$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || printf 'manual')"
log "building $BUILD"
ZZ2_BUILD="$BUILD" docker compose -f "$COMPOSE_FILE" config --quiet
ZZ2_BUILD="$BUILD" docker compose -f "$COMPOSE_FILE" up -d --build --remove-orphans

log "waiting for the application (up to ${HEALTH_TIMEOUT}s)"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  body="$(curl -fsS --max-time 4 "$HEALTH_URL" 2>/dev/null || true)"
  if printf '%s' "$body" | grep -q "\"build\":\"$BUILD\""; then
    log "application is healthy (build $BUILD)"
    docker compose -f "$COMPOSE_FILE" ps
    exit 0
  fi
  sleep 2
done

docker compose -f "$COMPOSE_FILE" ps >&2
docker compose -f "$COMPOSE_FILE" logs --tail=100 app >&2
fail "application did not become healthy"
