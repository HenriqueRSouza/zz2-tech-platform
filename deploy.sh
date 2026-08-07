#!/usr/bin/env bash
#
# ZZ2 Tech Support Tool — deploy.
#
# GitHub is the source of truth; the NAS is just a file host that can't reach
# GitHub itself. So this runs on a DEVELOPER'S machine that has the NAS share
# mounted: it pulls the latest code from GitHub, copies the app files onto the
# NAS, optionally restarts the server, and verifies the result. It NEVER touches
# data or secrets on the NAS (credentials, config, sessions, tickets, kits,
# backups) — only the code files below.
#
# Restart guidance:
#   - server.py changes need a restart to take effect (answer y).
#   - HTML / CSS / JS changes do NOT — a browser refresh is enough (answer N).
# The script verifies the copy either way, and only waits on the build header
# when you actually restart.
#
# One-time setup (per machine):
#   export ZZ2_NAS_PATH="/mnt/naz/ZZ2 Tech Support Tool"   # your mounted share path
#   export ZZ2_SERVER_URL="http://192.168.1.190:8080"     # optional; this is the default
#
# Usage:   ./deploy.sh
#
set -euo pipefail

NAS_PATH="${ZZ2_NAS_PATH:?Set ZZ2_NAS_PATH to the mounted NAS app folder}"
SERVER_URL="${ZZ2_SERVER_URL:-http://192.168.1.190:8080}"

ASSET_FILES=( images/zz2-logo.png images/favicon.jpg )

live_build() {  # print the running server's build (lowercased), or nothing
  curl -fsS -D - "$SERVER_URL/login.html" -o /dev/null 2>/dev/null \
    | tr -d '\r' | tr 'A-Z' 'a-z' | sed -n 's/^x-zz2-build: //p' || true
}

echo "==> Pulling latest from GitHub"
git pull --ff-only

STAMP="$(git rev-parse --short HEAD)"
git diff --quiet || STAMP="${STAMP}-dirty"
STAMP_LC="$(echo "$STAMP" | tr 'A-Z' 'a-z')"
echo "==> Build to deploy: ${STAMP}"

[ -d "$NAS_PATH" ] || { echo "!! NAS_PATH not found / share not mounted: $NAS_PATH"; exit 1; }

echo "==> Copying app files to the NAS (no deletes; data & secrets untouched)"
for f in *.html;    do [ -f "$f" ] && cp "$f" "$NAS_PATH/" || true; done
for f in chart*.js; do [ -f "$f" ] && cp "$f" "$NAS_PATH/" || true; done
mkdir -p "$NAS_PATH/images" || true
for f in "${ASSET_FILES[@]}"; do [ -f "$f" ] && cp "$f" "$NAS_PATH/images/" || true; done

# server.py: stamp the build on a LOCAL temp copy, then copy it over. Doing the
# edit locally avoids the "operation not permitted" warnings sed -i throws on
# the network mount.
if [ -f server.py ]; then
  TMP="/tmp/zz2_server.$$.py"
  sed "s/^ZZ2_BUILD .*/ZZ2_BUILD    = '${STAMP}'/" server.py > "$TMP"
  cp "$TMP" "$NAS_PATH/server.py" || true
  rm -f "$TMP"
fi

# --- Verify the copy actually landed (catches silent network-mount cp failures) ---
echo "==> Verifying files on the NAS"
mismatch=0
verify_file() {  # $1 = local file, $2 = NAS file
  if cmp -s "$1" "$2"; then return 0; fi
  echo "    !! did not copy: $1"
  mismatch=$((mismatch + 1))
}
for f in *.html;    do [ -f "$f" ] && verify_file "$f" "$NAS_PATH/$f"; done
for f in chart*.js; do [ -f "$f" ] && verify_file "$f" "$NAS_PATH/$f"; done
for f in "${ASSET_FILES[@]}"; do [ -f "$f" ] && verify_file "$f" "$NAS_PATH/images/$(basename "$f")"; done
# server.py is stamped on the NAS, so a byte-compare would always differ; instead
# confirm the stamped build line landed.
if [ -f server.py ]; then
  NAS_STAMP="$(sed -n "s/^ZZ2_BUILD[[:space:]]*=[[:space:]]*'\([^']*\)'.*/\1/p" "$NAS_PATH/server.py" 2>/dev/null | head -1)"
  if [ "$NAS_STAMP" != "$STAMP" ]; then
    echo "    !! server.py did not land (NAS build='${NAS_STAMP:-none}')"
    mismatch=$((mismatch + 1))
  fi
fi
if [ "$mismatch" -ne 0 ]; then
  echo "==> ✗ ${mismatch} file(s) did not copy correctly. Check the mount and re-run."
  exit 1
fi
echo "    All files match the local copy."

echo "==> Restart the server?"
read -r -p "    Restart via API now (needed only for server.py changes)? [y/N] " yn
if [[ "${yn:-}" =~ ^[Yy]$ ]]; then
  read -r -p "    Super-admin username: " U
  read -r -s -p "    Password: " P; echo
  TOKEN="$(curl -s "$SERVER_URL/login" -H 'Content-Type: application/json' \
           -d "{\"username\":\"$U\",\"password\":\"$P\"}" \
           | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  if [ -z "$TOKEN" ]; then
    echo "    !! Login failed — restart the server manually (see RUNBOOK.md)."
  else
    curl -s "$SERVER_URL/api/system/restart" -H "Authorization: Bearer $TOKEN" \
         -H 'Content-Type: application/json' -d '{}' >/dev/null \
      && echo "    Restart requested."
  fi

  echo "==> Waiting for build ${STAMP} to go live..."
  for _ in $(seq 1 30); do
    if [ "$(live_build)" = "$STAMP_LC" ]; then
      echo "==> ✓ Live build is now ${STAMP}. Deploy complete."
      exit 0
    fi
    sleep 2
  done
  echo "==> ✗ Timed out after 60s — live build is not ${STAMP} yet."
  echo "    Check that the server restarted. Current header:"
  curl -fsS -D - "$SERVER_URL/login.html" -o /dev/null 2>/dev/null | tr -d '\r' | grep -i 'x-zz2-build' || true
  exit 1
fi

# --- No restart: files are deployed; HTML/CSS/JS are live on a browser refresh. ---
echo "==> ✓ Files deployed (${STAMP}). No restart requested."
echo "    HTML / CSS / JS changes are live now — hard-refresh the browser (Ctrl-Shift-R)."

# If server.py actually changed vs the running build, the new server code is on
# disk but NOT live yet — point that out so a server change isn't missed.
LIVE="$(live_build)"
if [ -f server.py ] && [ -n "$LIVE" ] && [ "$LIVE" != "$STAMP_LC" ]; then
  if git cat-file -e "${LIVE}^{commit}" 2>/dev/null; then
    if ! git diff --quiet "$LIVE" -- server.py 2>/dev/null; then
      echo "    NOTE: server.py changed since the running build (${LIVE})."
      echo "          Re-run and choose 'y' to restart so those changes take effect."
    fi
  else
    echo "    (Running build is ${LIVE}; restart if this deploy included server.py changes.)"
  fi
fi
exit 0
