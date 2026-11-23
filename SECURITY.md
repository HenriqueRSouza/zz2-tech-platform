# Security

This document is written to be *useful*, which means it is honest about what is
not fixed. The old deployment was a LAN-only server on a NAS; the new one is on a
VPS reachable from the internet. That change invalidates most of the assumptions
the original code was written under, and this file tracks the consequences.

- [Threat model](#threat-model)
- [Fixed](#fixed)
- [Known weaknesses](#known-weaknesses)
- [Deployment checklist](#deployment-checklist)
- [Reporting](#reporting)

---

## Threat model

**What we assume.** The application is reachable from the public internet.
Attackers can send arbitrary requests, know the software is open source, and can
read this document. Staff accounts are held in Supabase Auth. Kit diagrams,
manuals, customer purchase history and ticket text are confidential business data.

**What we defend against.** Unauthenticated access to any data; privilege
escalation by an authenticated user; credential theft through logs, referrers or
stored XSS; source and configuration disclosure; resource exhaustion by a single
client; and a compromised third-party CDN.

**What we do not defend against.** A distributed denial of service (the VPS
provider's edge is the right layer); an attacker with the Supabase service key
(it bypasses row-level security by design — treat it as root); a malicious
super-admin; and physical or hypervisor-level compromise of the VPS.

---

## Fixed

Each entry names the specific weakness in `legacy/server.py`, what replaced it,
and where. The regression tests in `tests/test_security.py` are organised to
match — a refactor that reintroduces any of these fails there rather than in
production.

### 1. Python 2.7 → 3.11+

Python 2.7 stopped receiving security patches in January 2020. The NAS could run
nothing newer, so the monolith stayed on it — and carried a hand-written
PBKDF2 implementation, a hand-written constant-time comparison, and a README
warning never to call `str()` on a free-text field. All three are gone.

### 2. Source and configuration disclosure

`GET /server.py` returned all 5,475 lines of the server, including the auth
design, the Supabase table layout and the write-queue protocol.
`supabase_setup.sql`, `config.example.json`, `deploy.sh` and `gitignore` were
served too. The static handler was a *denylist* over the whole application
directory, so anything not on a hard-coded list of nine filenames was public.

Worse: `_save_json` wrote `config.json.tmp.<pid>` beside the real file, and that
name was not on the list. A crash between write and rename left the Apps Script
secret and the Supabase service key downloadable by anyone who guessed the PID.

**Now:** an allowlist. Only `web/` is served, and `web/` contains nothing but
pages and assets — no code, no state, no secrets. Temp files are written to a
sibling directory outside anything served. Adding a secret to the deployment
can no longer expose it through this route, because the directory it would live
in is not reachable. See `zz2/api/routers/pages.py` and `zz2/core/jsonstore.py`.

### 3. Secrets in a web-served file

`config.json` held `secret_key`, `supabase_service_key` and the Supabase URL,
inside the directory the static handler served from.

**Now:** secrets come from the process environment — systemd `EnvironmentFile` at
`0640 root:zz2`, or Docker secrets. `config.json` survives only for non-secret,
operator-tunable values, and `RuntimeConfigService.apply` **rejects** any attempt
to write a secret key through the API. See `zz2/settings.py` and
`zz2/services/runtime_config.py`.

### 4. Session tokens in query strings

`_get_token` accepted `?token=<session token>`. Query strings land in access
logs, `Referer` headers on every outbound link, browser history, and any
intermediate proxy's logs.

**Now:** the `Authorization` header only. A query-string token is ignored, and
the compatibility shim for the old `POST /verify` refuses a body-supplied token
too. See `zz2/services/auth.py` and `web/static/js/shared/session.js`.

### 5. Wildcard CORS

`Access-Control-Allow-Origin: *` on every response, authenticated ones included.

**Now:** no CORS headers unless `ZZ2_CORS_ORIGINS` names specific origins, and
the settings validator refuses `*` in production. Same-origin is correct here:
this process serves the pages *and* the API. See `zz2/main.py`.

### 6. Missing security headers

The monolith sent four headers: three CORS and a build stamp.

**Now:** `Content-Security-Policy`, `Strict-Transport-Security` (production
only), `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
`Permissions-Policy`, and the two Cross-Origin isolation headers. See
`zz2/api/middleware/security.py`.

### 7. Exception text returned to clients

`except Exception as e: self._json(500, {'error': 'Server error: %s' % e})`
disclosed filesystem paths, internal field names and upstream database messages.
Supabase failures returned 500 bytes of the PostgREST response verbatim, naming
tables and constraints.

**Now:** clients get a stable code and a message written to be read by a user;
the log gets the traceback and the upstream detail, correlated by request id. See
`zz2/core/errors.py` and `zz2/api/error_handlers.py`.

### 8. No access log

```python
def log_message(self, fmt, *args):
    pass  # Suppress request logs to keep things clean
```

No record of who requested what — no way to investigate an incident, spot
scanning, or prove a request was never served.

**Now:** structured JSON access logs with a request id, the authenticated actor,
status and duration; plus a separate append-only audit log for sign-ins,
permission denials, and every privileged action. Credentials are scrubbed by a
logging filter rather than at each call site — the monolith had a `_redact`
helper that callers had to remember, and the one path that forgot leaked the Apps
Script key inside an exception message. See `zz2/core/logging.py`.

### 9. Unvalidated uploads (stored XSS)

The upload endpoint checked the declared `kind` and appended `.pdf` if the
filename lacked it. Nothing verified the bytes. Media is served from the
application's own origin, so an "image" whose contents are `<script>` is stored
cross-site scripting with the victim's session in `localStorage`.

**Now:** magic-byte validation, server-chosen filenames for images, per-format
size limits enforced on the decoded length, and `nosniff` plus an explicit
`Content-Type` on every media response. See `zz2/services/uploads.py`.

### 10. Path traversal surface

`_safe_key` rejected only `/`, `\` and `..`, letting through NUL bytes, leading
dots, control characters and unbounded length. Kit keys become directory names.

**Now:** allowlist validation (`^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$`), Unicode
normalisation checks, and a resolved-path containment check on every file route.
See `zz2/core/text.py` and `zz2/api/routers/pages.py`.

### 11. Self-promotion through user metadata

```python
role = app_metadata.get('role') or user_metadata.get('role') or 'tech'
```

`user_metadata` is writable by the user's own Supabase session. That fallback let
any signed-in account set its own role to anything.

**Now:** `app_metadata` only, with no fallback. An unknown role resolves to zero
permissions rather than a default. See `zz2/services/auth.py`.

### 12. Missing and inconsistent authorization

Seventy handlers each opened with the same four lines of session lookup, so the
omissions were invisible: `/api/config` had no check at all; `/api/proxy` served
the entire kit catalogue and every diagnostic tree unauthenticated; `/api/draft`
checked only that *a* session existed; one `create_tickets` check gated creates,
edits *and* deletes.

**Now:** the requirement is declared in the route signature, so an unprotected
route is visible in its own definition. Irreversible operations (restore,
configuration writes) additionally require a root role. See `zz2/api/deps.py`.

### 13. No rate limiting

Only failed logins were throttled. `/api/analytics` — which could rebuild a
multi-megabyte view — was unlimited.

**Now:** per-client fixed-window limits with separate read and write budgets,
keyed on identity before IP so one office behind a NAT does not share a budget;
plus nginx-level limits in front. See `zz2/api/middleware/rate_limit.py`.

### 14. Unbounded resource use

One thread per connection with no timeouts (slowloris), a 32 MB body cap that
still let a handful of concurrent requests exhaust a small VPS, five caches that
never evicted, and an unbounded log.

**Now:** an 8 MB default body cap rejected from `Content-Length` before buffering,
explicit connect/read/write timeouts on every outbound call, TTL+LRU caches with
hard entry limits, rotating logs, and `MemoryMax`/`TasksMax` in the systemd unit.

### 15. Remote process restart

`POST /api/system/restart` called `os.execv`. One bug in the auth check made it a
remote code execution primitive.

**Now:** removed. Restarts are `systemctl restart zz2`.

### 16. CDN supply chain

`login.html`, `index.html` and `tickets.html` loaded the Supabase client from
jsDelivr; `kit-editor.html` loaded pdf.js from cdnjs. No Subresource Integrity.
A compromised CDN response executes with full access to the page.

**Now:** both are vendored under `web/static/vendor/`, and `script-src` is
`'self'`. The pages also work with no outbound internet access.

### 17. Server-side request forgery

`webapp_url` was operator-editable and fetched server-side with the shared secret
attached. Pointed at `http://169.254.169.254/`, it would fetch cloud instance
metadata and return the body — on a VPS, a credential disclosure.

**Now:** the host is pinned to `script.google.com` over https, checked both where
the setting is written and again in the gateway before every request. See
`zz2/gateways/appscript.py`.

### 18. Hand-rolled credential handling

A pure-Python PBKDF2 implementation, `credentials.json`, `sessions.json`, a
disk-persisted lockout table, and a fallback that compared plaintext passwords —
roughly 600 lines of security-critical code maintained in-house because Python
2.7 could not do better.

**Now:** deleted. Supabase Auth owns identities, passwords, password policy,
session refresh and login throttling. The minimum password length for accounts
created through this application rose from 6 characters to 12.

### 19. Token spraying walked around the rate limiter

`RateLimitMiddleware` keys its budget on a hash of the bearer token, so a client
sending a *different* random token on every request got a fresh budget each time.
Guessing tokens, probing for a leaked signing key, or forging SAML responses was
unbounded.

**Now:** `AuthGuardMiddleware` + `zz2/services/lockout.py`. Per client address, it
counts **distinct rejected** credentials — forged or garbled, not merely expired
(`CredentialExpired`), and not absent. At `ZZ2_AUTH_LOCKOUT_THRESHOLD` (20) within
`ZZ2_AUTH_LOCKOUT_WINDOW_SECONDS` (10 min) the address gets `429 locked_out` on
every request for `ZZ2_AUTH_LOCKOUT_SECONDS` (15 min), audited as `auth.lockout`.
Counting *distinct* credentials is what keeps a broken tab retrying one expired
token from locking out an office behind a NAT. Blocks are listed and released at
`GET /api/system/security/lockouts` and `POST /api/system/security/lockouts/release`
(`system_settings`).

### 20. University single sign-on (SAML 2.0, Microsoft Entra ID)

New capability rather than a fix: students and staff sign in with their
university Microsoft account, inheriting the university's MFA and account
lifecycle. Implemented as a SAML SP with the checks a SAML library performs —
signature over the element actually read (anti-wrapping), issuer, audience,
recipient, validity window, `InResponseTo` bound to the browser by a cookie,
assertion-ID replay cache, no DTDs. Sessions are this server's own HS256 tokens
under a key distinct from Supabase's, revoked on sign-out. Details, Entra setup
and limitations: [`docs/SAML_AZURE.md`](docs/SAML_AZURE.md).

### 21. Sign-out did not end anything server-side

`POST /api/session/logout` documented evicting the principal cache but did not
call it, so a token kept working for up to 60 s after sign-out. It now evicts the
cache, and revokes SAML sessions outright.

---

## Known weaknesses

Open, with the reason and the fix. Nothing here is hidden behind a header that
looks strict.

### `'unsafe-inline'` in the Content-Security-Policy

**Severity: medium.** `script-src` includes `'unsafe-inline'`, which means a
successful HTML injection can execute. This is the largest remaining gap.

**Why:** the pages contain 209 inline event handlers (`onclick="..."`), most of
them generated inside `innerHTML` template strings in `index.js` and
`kit-editor.js`. `style-src` needs it for roughly 300 inline `style=` attributes.
Every one requires the directive.

**Fix:** rewrite the handlers as delegated `addEventListener` calls on a
container, and move inline styles to classes. Mechanical but large — it touches
most of the two biggest page scripts, and each change risks a behaviour
regression in code with no test coverage. It should be done page by page, with
the CSP tightened per page once that page is clean.

**Mitigation meanwhile:** `connect-src` is limited to self and Supabase, so an
injected script cannot exfiltrate to an arbitrary host; `object-src 'none'` and
`base-uri 'none'` close two common escalation routes.

### Access tokens in `localStorage`

**Severity: medium.** A successful XSS can read the token. `HttpOnly` cookies
would not be readable by script.

**Why:** the Supabase JavaScript client manages the session and stores it in
`localStorage` by design. Moving to cookies means the server brokers sign-in
rather than the browser, which is a different authentication architecture — and
it would need CSRF protection, which cookies require and bearer tokens do not.

**Fix:** a server-side session broker that exchanges a Supabase sign-in for an
`HttpOnly; Secure; SameSite=Strict` cookie. Worth doing after the CSP work, since
that is what makes XSS unlikely in the first place.

### Rate limiting is per process

**Severity: low.** The counters are in memory. The deployment runs one worker
(see `deploy/gunicorn.conf.py`), so the configured limit is the real limit today —
but raising `workers` would silently multiply it.

**Fix:** move the counters to Redis. Required before scaling horizontally, for
this reason and because the background workers would also duplicate.

### Uploads travel as base64 in a JSON body

**Severity: low.** A 25 MB PDF becomes a ~34 MB JSON string, fully buffered in
memory before validation.

**Why:** the Kit Editor's existing JavaScript posts base64. Changing both sides
at once is a larger, riskier change than the security fix needed, and the size
limits bound the exposure.

**Fix:** `multipart/form-data` with streaming validation.

### `/api/config` is unauthenticated

**Severity: informational, by design.** It returns the Supabase URL and the
publishable key, because the sign-in page needs both before any session exists.
Both are intended to be public — the publishable key is what the browser
authenticates with. The payload comes from an explicit allowlist
(`Settings.public_config`), so adding a secret to `Settings` cannot leak it here
by accident, and a test asserts no secret appears in the response.

### The Apps Script integration authenticates with a shared secret in a URL

**Severity: low, inherited.** Every call appends `key=<secret>` to a query
string. That is the deployment's own protocol, not a choice made here.

**Mitigations:** the host is pinned, the URL is never logged, redirects are
disabled on the shared client, and callers cannot inject their own `key`
parameter.

**Fix:** retire the Google Sheet. Tickets already live in Supabase; the sheet,
the write-behind queue and the poller all exist to keep it in step.

---

## Deployment checklist

Run through this before the first public request. Most of it is verified
automatically by `deploy/smoke_test.sh`.

**Secrets**

- [ ] `/etc/zz2/zz2.env` is `0640 root:zz2` and holds every secret.
- [ ] `ZZ2_SUPABASE_JWT_SECRET` is set, so tokens are verified locally.
- [ ] The Apps Script secret has been **rotated**. The old server published
      `config.json` on request; treat the previous value as already exposed.
- [ ] No secret appears in `config.json`, in git, or in any log.

**Transport**

- [ ] `ZZ2_ENVIRONMENT=production` and `ZZ2_PUBLIC_URL` uses `https://`.
- [ ] TLS certificate installed and renewing (`certbot renew --dry-run`).
- [ ] The application binds `127.0.0.1` only: `ss -tlnp | grep 8080`.
- [ ] `ZZ2_TRUSTED_HOSTS` names real hostnames, not `*`.
- [ ] `ZZ2_CORS_ORIGINS` is empty unless a different origin genuinely needs it.

**Identity**

- [ ] Every technician exists in Supabase Auth with `app_metadata.role` set.
- [ ] At least two accounts hold `super_admin` — the application refuses to
      remove the last one, and one is a single point of lockout.
- [ ] Supabase Auth email confirmation and password policy are configured.
- [ ] Row-level security is enabled on every `techSupport` table.

**Filesystem**

- [ ] `ZZ2_DATA_DIR` is `0750`, owned by the service user. The System page
      reports whether it is world-readable.
- [ ] The application code is **not** writable by the service user.
- [ ] A backup of `ZZ2_DATA_DIR` runs off-box and has been restore-tested.

**Monitoring**

- [ ] `/healthz` is watched by something that will page a human.
- [ ] `grep '"level":"WARNING"'` on the server log is checked, or shipped.
- [ ] The audit log is retained somewhere the application cannot rewrite.

---

## Reporting

Report a suspected vulnerability to the repository owner directly, not in a
public issue. Include the request id from the response body if you have one — it
locates the exact log entry.
