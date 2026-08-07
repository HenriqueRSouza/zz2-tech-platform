#!/usr/bin/env bash
#
# ZZ2 Tech Support Tool — post-deploy smoke test.
#
# Logs in once, then checks the endpoints that the app depends on. Run it right
# after deploy.sh. Any FAIL means a tech would hit a broken feature.
#
#   export ZZ2_SERVER_URL="http://192.168.1.190:8080"   # optional; default shown
#   ./smoke_test.sh
#
set -uo pipefail
SERVER_URL="${ZZ2_SERVER_URL:-http://192.168.1.190:8080}"

pass=0; fail=0
check() {  # check "name" "curl-args..."  -> expects HTTP 200 and (optionally) a needle
  local name="$1"; shift
  local needle="$1"; shift
  local out code
  out="$(curl -s -w $'\n%{http_code}' "$@")" || { echo "FAIL  $name (no response)"; fail=$((fail+1)); return; }
  code="$(printf '%s' "$out" | tail -n1)"
  body="$(printf '%s' "$out" | sed '$d')"
  if [ "$code" = "200" ] && { [ -z "$needle" ] || printf '%s' "$body" | grep -q "$needle"; }; then
    echo "PASS  $name"; pass=$((pass+1))
  else
    echo "FAIL  $name (http $code)"; fail=$((fail+1))
  fi
}

echo "== ZZ2 smoke test against $SERVER_URL =="

# 1) login page serves + carries a build stamp (GET — the X-ZZ2-Build header is
#    only emitted on GET responses, never on HEAD)
BUILD="$(curl -fsS -D - "$SERVER_URL/login.html" -o /dev/null 2>/dev/null | tr -d '\r' | tr 'A-Z' 'a-z' | sed -n 's/^x-zz2-build: //p')"
if [ -n "${BUILD:-}" ]; then echo "PASS  login.html serves (build=$BUILD)"; pass=$((pass+1));
else echo "FAIL  login.html / build header"; fail=$((fail+1)); fi

# 2) authenticate
read -r -p "Username: " U
read -r -s -p "Password: " P; echo
TOKEN="$(curl -s "$SERVER_URL/login" -H 'Content-Type: application/json' \
         -d "{\"username\":\"$U\",\"password\":\"$P\"}" \
         | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if [ -n "$TOKEN" ]; then echo "PASS  login (token issued)"; pass=$((pass+1));
else echo "FAIL  login — cannot continue authed checks"; echo; echo "Result: $pass passed, $((fail+1)) failed"; exit 1; fi
AUTH=(-H "Authorization: Bearer $TOKEN")

# Like check(), but retries once after a pause. Used for the one endpoint that
# makes a live round-trip to Apps Script (dashboard stats), which can be slow.
check_remote() {  # name needle url...
  local name="$1"; shift; local needle="$1"; shift
  local i out code body
  for i in 1 2; do
    out="$(curl -s -w $'\n%{http_code}' "$@")" || true
    code="$(printf '%s' "$out" | tail -n1)"
    body="$(printf '%s' "$out" | sed '$d')"
    if [ "$code" = "200" ] && { [ -z "$needle" ] || printf '%s' "$body" | grep -q "$needle"; }; then
      echo "PASS  $name"; pass=$((pass+1)); return
    fi
    [ "$i" = 1 ] && sleep 3
  done
  echo "FAIL  $name (http ${code:-?} after retry — Apps Script slow or unreachable)"; fail=$((fail+1))
}

# 3) the endpoints the app actually calls
check "verify token"        '"ok": true'  "$SERVER_URL/verify" -H 'Content-Type: application/json' -d "{\"token\":\"$TOKEN\"}"
check "tickets/recent"      '"ok": true'  "${AUTH[@]}" "$SERVER_URL/api/tickets/recent"
check "tickets/search"      '"ok": true'  "${AUTH[@]}" "$SERVER_URL/api/tickets/search?q=test"
check_remote "dashboard stats" '"ok": true'  "${AUTH[@]}" "$SERVER_URL/api/proxy?action=stats"
check "analytics"           ''            "${AUTH[@]}" "$SERVER_URL/api/analytics"
check "kit data (csv)"      ''            "${AUTH[@]}" "$SERVER_URL/api/proxy?action=csv&sheet=kits"
check "sync health"         '"ok": true'  "${AUTH[@]}" "$SERVER_URL/api/system/sync-health"

echo
echo "Result: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
