# Security

This document is written to be *useful*, which means it is honest about what is
not fixed. The old deployment was a LAN-only server on a NAS; the new one is
reachable from the internet — Azure App Service, with the scripts under `deploy/`
still supporting a self-managed VPS. That change invalidates most of the
assumptions the original code was written under, and this file tracks the
consequences.

In November 2026 the backing services moved from Supabase to Azure (see
[`docs/AZURE_MIGRATION.md`](docs/AZURE_MIGRATION.md)): PostgreSQL Flexible Server
replaces Supabase's Postgres/PostgREST, an `app_users` table plus server-issued
sessions replace Supabase Auth, Microsoft sign-in (OpenID Connect) was added,
Azure Blob Storage replaces Supabase Storage, and a backend presence endpoint
replaces Supabase Realtime. The browser now talks to this origin only.

- [Threat model](#threat-model)
- [Fixed](#fixed)
- [Known weaknesses](#known-weaknesses)
- [Deployment checklist](#deployment-checklist)
- [Reporting](#reporting)

---

## Threat model

**What we assume.** The application is reachable from the public internet.
Attackers can send arbitrary requests, know the software is open source, and can
read this document. Staff accounts are held in the application's own
`techSupport.app_users` table (scrypt password hashes) or linked to a Microsoft
Entra ID identity. Kit diagrams, manuals, customer purchase history and ticket
text are confidential business data.

**What we defend against.** Unauthenticated access to any data; privilege
escalation by an authenticated user; credential theft through logs, referrers or
stored XSS; source and configuration disclosure; resource exhaustion by a single
client; and a compromised third-party CDN.

**What we do not defend against.** A distributed denial of service (the VPS
provider's edge is the right layer); an attacker holding `ZZ2_DATABASE_URL`,
`ZZ2_AZURE_STORAGE_CONNECTION_STRING` or `ZZ2_SESSION_SECRET` (each is
equivalent to root — the database has no row-level security, the storage
connection string reads every manual, and the session secret mints any
identity); a malicious super-admin; a compromised Azure subscription owner; and
physical or hypervisor-level compromise of the host.

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

**Now:** secrets come from the process environment — App Service application
settings (ideally Key Vault references) on Azure, or a systemd `EnvironmentFile`
at `0640 root:zz2` / Docker secrets on a VPS. The production validator refuses
to boot without `ZZ2_DATABASE_URL` or with a `ZZ2_SESSION_SECRET` shorter than
32 characters. `config.json` survives only for non-secret,
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

`user_metadata` was writable by the user's own Supabase session. That fallback
let any signed-in account set its own role to anything.

**Now:** the role comes from the `role` and `permissions` columns of `app_users`,
which only an administrator can change through the Users API — there is no
self-service profile write that reaches them. (The user directory still exposes
them as `app_metadata` for compatibility.) An unknown role resolves to zero
permissions rather than a default, and a deactivated or deleted account is
refused whenever its principal is resolved (principals are cached for up to
60 s), not only at sign-in. If the database is unreachable at that moment, the
role recorded in the token is used instead. See `zz2/services/auth.py` and
`zz2/gateways/user_directory.py`.

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
plus nginx-level limits in front on a VPS deployment (App Service has no nginx
layer — the application limits are the only ones there). See
`zz2/api/middleware/rate_limit.py`.

### 14. Unbounded resource use

One thread per connection with no timeouts (slowloris), a 32 MB body cap that
still let a handful of concurrent requests exhaust a small VPS, five caches that
never evicted, and an unbounded log.

**Now:** an 8 MB default body cap rejected from `Content-Length` before buffering,
explicit connect/read/write timeouts on every outbound call, TTL+LRU caches with
hard entry limits, rotating logs, and `MemoryMax`/`TasksMax` in the systemd unit
(on App Service, the plan's instance size is the ceiling).

### 15. Remote process restart

`POST /api/system/restart` called `os.execv`. One bug in the auth check made it a
remote code execution primitive.

**Now:** removed. Restarts go through the platform — `az webapp restart` on
Azure, `systemctl restart zz2` on a VPS.

### 16. CDN supply chain

`login.html`, `index.html` and `tickets.html` loaded the Supabase client from
jsDelivr; `kit-editor.html` loaded pdf.js from cdnjs. No Subresource Integrity.
A compromised CDN response executes with full access to the page.

**Now:** pdf.js and Chart.js are vendored under `web/static/vendor/`, and the
Supabase client is gone entirely since the Azure migration — the pages call this
origin's API instead. `script-src` and `connect-src` are both `'self'`.

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

**Then (first rewrite):** deleted, with Supabase Auth owning identities and
passwords.

**Now (Azure):** Supabase Auth is gone, so password accounts are back in-house —
but on the standard library, not hand-written primitives. Hashes are
`hashlib.scrypt` (N=2^15, r=8, p=1, 16-byte random salt), compared with
`hmac.compare_digest`; a lookup for a non-existent account still computes a hash
so latency does not reveal which emails exist. The minimum password length is
12 characters (it was 6). Sessions are HS256 JWTs signed with
`ZZ2_SESSION_SECRET`, 8 h by default, with a required `jti` so sign-out can
revoke them. Microsoft sign-in accounts have no password at all. See
`zz2/gateways/user_directory.py`, `zz2/services/users.py` and
`zz2/services/session_tokens.py`.

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

Since Supabase no longer throttles sign-in, the same guard records failed
password logins (`POST /api/auth/login`), failed Microsoft callbacks and invalid
SSO hand-off codes (`POST /api/auth/sso/exchange`). See `zz2/api/routers/auth.py`.

### 20. University single sign-on (SAML 2.0, Microsoft Entra ID)

New capability rather than a fix: students and staff sign in with their
university Microsoft account, inheriting the university's MFA and account
lifecycle. Implemented as a SAML SP with the checks a SAML library performs —
signature over the element actually read (anti-wrapping), issuer, audience,
recipient, validity window, `InResponseTo` bound to the browser by a cookie,
assertion-ID replay cache, no DTDs. Sessions are this server's own HS256 tokens,
revoked on sign-out. Details, Entra setup and limitations:
[`docs/SAML_AZURE.md`](docs/SAML_AZURE.md).

### 21. Sign-out did not end anything server-side

`POST /api/session/logout` documented evicting the principal cache but did not
call it, so a token kept working for up to 60 s after sign-out. It now evicts the
cache and revokes the token's `jti` — for every session, since all sessions are
now issued by this server.

### 22. Sign in with Microsoft (OpenID Connect)

New capability, added with the Azure migration: "Entrar com Microsoft" for
Outlook / Microsoft 365 accounts. Authorization-code flow with PKCE (S256), run
server-side with a confidential client. `state` is single-use and bound to the
browser by a cookie holding a separate nonce (only its hash is kept
server-side); the `id_token` is verified with RS256 against Microsoft's JWKS,
with audience, issuer-matches-`tid`, expiry and `nonce` checks. The browser never
sees the Microsoft tokens — it receives a one-time hand-off code in the URL
fragment (not the query string, so it is not logged or sent as a `Referer`) and
exchanges it for a session. Account rules are shared with SAML: domain
allowlist, an existing deactivated account is refused, group-to-role mapping.
See `zz2/services/microsoft.py` and `zz2/services/sso.py`.

### 23. Fewer third parties in the browser

Under Supabase, every page held a Supabase client and talked to Supabase
directly for auth, tickets (PostgREST) and presence (Realtime), so the
authorization model was split between this server and RLS policies the browser
could probe. Now every call goes to this origin: tickets through
`/api/ticket-records` with the same permission dependencies as the rest of the
API, presence through `/api/presence/...`. `connect-src` dropped to `'self'`, and
`/api/config` no longer returns any key — only feature flags.

Kit media lives in a **private** Blob container read with a server-side
connection string; browsers only reach it through the authenticated
`/images/<kit>/<file>` and `/manuals/<kit>/<file>` routes, and the connection
string is never logged. Database connections use TLS (`sslmode=require`), and
every statement uses bound parameters with driver-quoted identifiers. See
`zz2/gateways/storage.py` and `zz2/gateways/database.py`.

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

**Mitigation meanwhile:** `connect-src` is `'self'` only, so an injected script
cannot `fetch` to another host; `object-src 'none'` and `base-uri 'none'` close
two common escalation routes.

### Access tokens in `localStorage`

**Severity: medium.** A successful XSS can read the token. `HttpOnly` cookies
would not be readable by script.

**Why:** the pages send the token as a bearer header, and
`web/static/js/shared/session.js` keeps it in `localStorage`. This was dictated
by the Supabase client; it no longer is, but the front-end was kept as it was.

**Fix:** since the server now issues every session itself, the remaining work is
on the page side: set the token as an `HttpOnly; Secure; SameSite=Strict` cookie
at sign-in / SSO exchange, accept it in `current_principal`, and add CSRF
protection for state-changing requests (cookies need it; bearer tokens do not).
Worth doing after the CSP work, since that is what makes XSS unlikely in the
first place.

### In-process state: rate limits, lockouts, revocations, presence

**Severity: low.** The rate-limit counters, the lockout table, the revoked-token
list and ticket presence all live in memory. The deployment runs one worker (see
`deploy/gunicorn.conf.py`) on one App Service instance, so they are correct
today, with two consequences:

- Raising `workers` or scaling out the App Service plan would silently multiply
  the limits and split the revocation list between instances.
- A restart forgets revocations: a token signed out before the restart is valid
  again until its `exp` (8 h at most by default).

**Fix:** move this state to Redis (or a table) before scaling horizontally; until
then, rotating `ZZ2_SESSION_SECRET` is the way to revoke every session at once.

### No row-level security in the database

**Severity: low.** Supabase enforced row-level security per table; the Azure
schema (`sql/azure_schema.sql`) does not, and the application connects with a
single role that can read and write every table. Authorization is enforced only
in the API layer (`zz2/api/deps.py`). Since the browser no longer has a direct
database path, this is defence in depth rather than an exposure — but an SQL
injection or a leaked `ZZ2_DATABASE_URL` would reach everything.

**Fix:** a dedicated application role with `GRANT`s limited to the
`techSupport` and `zoho` tables it uses (no DDL, no ownership), separate from the
admin login used for migrations.

### Uploads travel as base64 in a JSON body

**Severity: low.** A 25 MB PDF becomes a ~34 MB JSON string, fully buffered in
memory before validation.

**Why:** the Kit Editor's existing JavaScript posts base64. Changing both sides
at once is a larger, riskier change than the security fix needed, and the size
limits bound the exposure.

**Fix:** `multipart/form-data` with streaming validation.

### `/api/config` is unauthenticated

**Severity: informational, by design.** The sign-in page needs it before any
session exists. Since the migration it returns only the build, the environment
and which sign-in methods are enabled — no URL or key. The payload comes from an
explicit allowlist (`Settings.public_config`), so adding a secret to `Settings`
cannot leak it here by accident, and a test asserts no secret appears in the
response.

### The Apps Script integration authenticates with a shared secret in a URL

**Severity: low, inherited.** Every call appends `key=<secret>` to a query
string. That is the deployment's own protocol, not a choice made here.

**Mitigations:** the host is pinned, the URL is never logged, redirects are
disabled on the shared client, and callers cannot inject their own `key`
parameter.

**Fix:** retire the Google Sheet. Tickets already live in PostgreSQL; the sheet,
the write-behind queue and the poller all exist to keep it in step.

---

## Deployment checklist

Run through this before the first public request. Most of it is verified
automatically by `deploy/smoke_test.sh`.

**Secrets**

- [ ] Every secret is an App Service application setting (preferably a Key Vault
      reference), or on a VPS in `/etc/zz2/zz2.env` at `0640 root:zz2`:
      `ZZ2_DATABASE_URL`, `ZZ2_SESSION_SECRET`,
      `ZZ2_AZURE_STORAGE_CONNECTION_STRING`, `ZZ2_MICROSOFT_CLIENT_SECRET`.
- [ ] `ZZ2_SESSION_SECRET` is random and at least 32 characters (48+
      recommended); the process refuses to boot in production otherwise.
- [ ] No `ZZ2_SUPABASE_*` variable remains set, and the old Supabase keys are
      revoked if the project still exists.
- [ ] The Apps Script secret has been **rotated**. The old server published
      `config.json` on request; treat the previous value as already exposed.
- [ ] The Microsoft client secret's expiry date is on someone's calendar.
- [ ] No secret appears in `config.json`, in git, or in any log.

**Transport**

- [ ] `ZZ2_ENVIRONMENT=production` and `ZZ2_PUBLIC_URL` uses `https://`.
- [ ] App Service: *HTTPS Only* on, minimum TLS 1.2, FTP/FTPS deployment
      disabled. VPS: certificate renewing (`certbot renew --dry-run`) and the
      application bound to `127.0.0.1` only (`ss -tlnp | grep 8080`).
- [ ] `ZZ2_DATABASE_URL` ends in `sslmode=require` (or `verify-full`).
- [ ] `ZZ2_TRUSTED_HOSTS` names real hostnames, not `*`.
- [ ] `ZZ2_CORS_ORIGINS` is empty unless a different origin genuinely needs it.

**Azure resources**

- [ ] PostgreSQL firewall allows only the App Service's outbound IPs (and, when
      needed, a named admin IP) — not "Allow public access from any Azure
      service" and never `0.0.0.0–255.255.255.255`.
- [ ] The application connects with a role that is not the server admin (see
      *No row-level security* above).
- [ ] The storage account has *Allow Blob anonymous access* disabled and the
      `zz2-manuals` container is private.
- [ ] Storage account keys are rotated after anyone outside the team has seen
      the connection string.
- [ ] Automated backups are enabled on the PostgreSQL server, and a restore has
      been tested.

**Identity**

- [ ] Every technician exists in `app_users` with the right `role`, or will be
      auto-provisioned by Microsoft sign-in with the intended default role.
- [ ] At least two accounts hold `super_admin` — the application refuses to
      remove the last one, and one is a single point of lockout.
- [ ] `ZZ2_SAML_ALLOWED_DOMAINS` (shared by SAML and Microsoft sign-in) names
      only the domains meant to get in; with it empty,
      `ZZ2_MICROSOFT_TENANT=organizations` admits any work or school tenant.
- [ ] The Entra app registration's redirect URI is exactly
      `<ZZ2_PUBLIC_URL>/api/auth/microsoft/callback`.

**Filesystem**

- [ ] `ZZ2_DATA_DIR` is `0750`, owned by the service user (on App Service, under
      `/home`, which is not web-served). The System page reports whether it is
      world-readable.
- [ ] The application code is **not** writable by the service user (VPS).
- [ ] A backup of `ZZ2_DATA_DIR` runs off-box and has been restore-tested.

**Monitoring**

- [ ] `/healthz` is configured as the App Service health check, or watched by
      something that will page a human.
- [ ] Application logs are shipped (App Service log stream / Log Analytics) and
      `"level":"WARNING"` entries are reviewed.
- [ ] The audit log is retained somewhere the application cannot rewrite.

---

## Reporting

Report a suspected vulnerability to the repository owner directly, not in a
public issue. Include the request id from the response body if you have one — it
locates the exact log entry.
