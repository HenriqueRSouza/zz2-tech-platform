#!/usr/bin/env bash
#
# Post-deploy checks. Run after deploy.sh, or any time the site looks wrong.
#
#   ZZ2_URL=https://suporte.example.com.br ./deploy/smoke_test.sh
#   ZZ2_URL=... ZZ2_TOKEN=<access token> ./deploy/smoke_test.sh   # includes authenticated checks
#
# Exits non-zero on the first failure. Every check is read-only.

set -uo pipefail

URL="${ZZ2_URL:-http://127.0.0.1:8080}"
TOKEN="${ZZ2_TOKEN:-}"
failures=0

check() {  # check <description> <expected status> <path> [curl args...]
  local description="$1" expected="$2" path="$3"; shift 3
  local status
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" "$URL$path")"
  if [ "$status" = "$expected" ]; then
    printf '  ok   %-52s %s\n' "$description" "$status"
  else
    printf '  FAIL %-52s got %s, expected %s\n' "$description" "$status" "$expected"
    failures=$((failures + 1))
  fi
}

header() {  # header <description> <header name> <expected substring> <path>
  local description="$1" name="$2" expected="$3" path="$4"
  local value
  value="$(curl -s -D - -o /dev/null --max-time 10 "$URL$path" \
           | tr -d '\r' | sed -n "s/^$name: //Ip" | head -1)"
  case "$value" in
    *"$expected"*) printf '  ok   %-52s %s\n' "$description" "${value:0:40}" ;;
    *) printf '  FAIL %-52s got %s\n' "$description" "${value:-<missing>}"
       failures=$((failures + 1)) ;;
  esac
}

echo "smoke test against $URL"

echo
echo "availability"
check "liveness" 200 /healthz
check "readiness" 200 /readyz
check "root redirects to sign-in" 302 /
check "sign-in page" 200 /login.html

echo
echo "pages"
for page in dashboard index tickets analytics kit-editor users settings; do
  check "$page.html" 200 "/$page.html"
done

echo
echo "assets"
check "shared session helper" 200 /static/js/shared/session.js
check "vendored supabase client" 200 /static/vendor/supabase.js
check "favicon" 200 /static/images/favicon.jpg

echo
echo "public API"
check "public config" 200 /api/config

echo
echo "authorization (these MUST be rejected)"
check "analytics without a token" 401 /api/analytics
check "kit catalogue without a token" 401 /api/catalog
check "system info without a token" 401 /api/system/info
check "users without a token" 401 /api/users
check "token in the query string is ignored" 401 "/api/analytics?token=anything"

echo
echo "source disclosure (these MUST be absent)"
for path in /server.py /config.json /.env /requirements.txt /pyproject.toml \
            /supabase_setup.sql /zz2/settings.py "/static/../../server.py"; do
  check "$path" 404 "$path"
done

echo
echo "security headers"
header "content security policy" "content-security-policy" "default-src 'self'" /login.html
header "no MIME sniffing" "x-content-type-options" "nosniff" /login.html
header "frame denied" "x-frame-options" "DENY" /login.html
header "build stamp present" "x-zz2-build" "" /login.html
case "$URL" in
  https://*) header "HSTS" "strict-transport-security" "max-age=" /login.html ;;
  *) echo "  skip HSTS (not https)" ;;
esac

if [ -n "$TOKEN" ]; then
  echo
  echo "authenticated"
  AUTH=(-H "Authorization: Bearer $TOKEN")
  check "session verify" 200 /api/session/verify -X POST "${AUTH[@]}"
  check "kit catalogue" 200 /api/catalog "${AUTH[@]}"
  check "recent tickets" 200 /api/tickets/recent "${AUTH[@]}"
  check "sync health" 200 /api/system/sync-health "${AUTH[@]}"
else
  echo
  echo "(set ZZ2_TOKEN to include authenticated checks)"
fi

echo
if [ "$failures" -eq 0 ]; then
  echo "✓ all checks passed"
  exit 0
fi
echo "✗ $failures check(s) failed"
exit 1
