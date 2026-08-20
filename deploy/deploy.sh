#!/usr/bin/env bash
#
# Deploy on the VPS.
#
# Replaces the NAS deploy, which ran on a developer's machine and copied files
# onto a mounted SMB share because the NAS had no git and TLS too old to reach
# GitHub. The VPS has both, so the deploy runs where the code runs — which
# removes the whole class of failure the old script had to defend against
# (silent cp failures onto a network mount, verified afterwards by byte-comparing
# every file).
#
#   sudo -u zz2 /opt/zz2/deploy/deploy.sh
#
# What it does, and stops on the first failure:
#   1. fetch and fast-forward the deploy branch
#   2. install dependencies into the virtualenv
#   3. run the test suite and the frontend contract audit
#   4. restart the service and wait for it to report healthy
#   5. roll back to the previous commit if it does not
#
# Data in $ZZ2_DATA_DIR is never touched.

set -euo pipefail

APP_DIR="${ZZ2_APP_DIR:-/opt/zz2}"
SERVICE="${ZZ2_SERVICE:-zz2}"
BRANCH="${ZZ2_BRANCH:-main}"
HEALTH_URL="${ZZ2_HEALTH_URL:-http://127.0.0.1:8080/healthz}"
HEALTH_TIMEOUT="${ZZ2_HEALTH_TIMEOUT:-60}"

log()  { printf '==> %s\n' "$*"; }
fail() { printf '!!  %s\n' "$*" >&2; exit 1; }

cd "$APP_DIR" || fail "app directory not found: $APP_DIR"

# --- 1. Update the code -------------------------------------------------------
PREVIOUS="$(git rev-parse HEAD)"
log "current commit: ${PREVIOUS:0:8}"

log "fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH"

# --ff-only on purpose: a deploy must never create a merge commit, and a
# divergence means someone edited the code on the server. Better to stop and
# find out than to merge blindly.
git merge --ff-only "origin/$BRANCH" \
  || fail "cannot fast-forward — the working tree has diverged from origin/$BRANCH"

BUILD="$(git rev-parse --short HEAD)"
log "deploying build: $BUILD"

if [ "$PREVIOUS" = "$(git rev-parse HEAD)" ]; then
  log "already up to date; continuing anyway (dependencies or config may have changed)"
fi

# --- 2. Dependencies ----------------------------------------------------------
log "installing dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt gunicorn

# --- 3. Verify before restarting ----------------------------------------------
# Cheap relative to an outage. A test suite that only runs in CI does not stop a
# bad deploy on the box where it matters.
log "running the test suite"
.venv/bin/python -m pytest -q || fail "tests failed — not restarting"

log "auditing the frontend/backend contract"
.venv/bin/python scripts/audit_frontend.py >/dev/null \
  || fail "the frontend references something the backend no longer serves"

# --- 4. Restart ---------------------------------------------------------------
# The build stamp reaches the app through the environment; systemd re-reads the
# file on restart, so this is how the X-ZZ2-Build header ends up correct.
log "stamping the build"
if [ -w /etc/zz2/zz2.env ]; then
  sed -i "s/^ZZ2_BUILD=.*/ZZ2_BUILD=${BUILD}/" /etc/zz2/zz2.env
else
  log "cannot write /etc/zz2/zz2.env — set ZZ2_BUILD there by hand"
fi

log "restarting $SERVICE"
sudo systemctl restart "$SERVICE"

log "waiting for health (up to ${HEALTH_TIMEOUT}s)"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
    LIVE="$(curl -fsS --max-time 3 "$HEALTH_URL" | sed -n 's/.*"build":"\([^"]*\)".*/\1/p')"
    if [ "$LIVE" = "$BUILD" ]; then
      log "✓ live build is $LIVE"
      log "deploy complete"
      exit 0
    fi
    log "healthy, but reporting build '$LIVE' (expected '$BUILD') — waiting"
  fi
  sleep 2
done

# --- 5. Roll back -------------------------------------------------------------
# A deploy that leaves the service down is worse than no deploy. Return to the
# last commit that was serving traffic and restart onto it.
printf '!!  did not become healthy in %ss — rolling back to %s\n' \
       "$HEALTH_TIMEOUT" "${PREVIOUS:0:8}" >&2
git reset --hard "$PREVIOUS" --quiet
if [ -w /etc/zz2/zz2.env ]; then
  sed -i "s/^ZZ2_BUILD=.*/ZZ2_BUILD=$(git rev-parse --short HEAD)/" /etc/zz2/zz2.env
fi
sudo systemctl restart "$SERVICE"
printf '!!  rolled back. Check: journalctl -u %s -n 100 --no-pager\n' "$SERVICE" >&2
exit 1
