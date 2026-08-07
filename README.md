# ZZ2 Tech Support Tool

Internal web app for CarPlay/Android-Auto kit diagnostics and support ticketing.
LAN-only, served from a NAS.

- **Server:** `server.py` — a threaded Python 2.7 HTTP server on the Buffalo NAS
  (`192.168.1.190:8080`). Serves the pages and a JSON API, holds a local cache of
  tickets, and syncs with a Google Apps Script / Google Sheets backend.
- **Pages:** `login`, `dashboard`, `index` (the tool), `tickets`, `analytics`,
  `kit-editor`, `users`, `settings`.
- **Auth:** PBKDF2 password hashes, session tokens, role-based permissions.

## Supabase tickets and authentication

The Tickets page reads and writes `techSupport.tickets` directly with the
signed-in user's Supabase Auth session. It uses Postgres Changes for live row
updates and Realtime Presence to show who is editing each ticket. The Python
server accepts Supabase access tokens alongside legacy sessions so the existing
dashboard and permission-aware APIs continue to work during migration.

Configuration requires `supabase_url`, `supabase_publishable_key`, and the
server-only `supabase_service_key` in `config.json`. Never send the service key
to a browser. Run `supabase_setup.sql` once in the Supabase SQL Editor and ensure
`techSupport` is listed under API Settings > Exposed schemas. Create users in
Supabase Authentication; optional `app_metadata.role` and
`app_metadata.permissions` control the existing application authorization.

The customer selector reads the `zoho.customer_performance_semesters` and
`zoho.customer_performance_months` tables through the Python server. Ensure
`zoho` is also exposed in API Settings and run `supabase_customer_history.sql`
once so the server-only `service_role` can read those two tables.

## Local kits, troubleshooting and manuals

Kit metadata and troubleshooting trees are authoritative local server files:
`data/kits.json` and `data/troubleshooting.json`. The `/api/proxy?action=csv`
compatibility endpoint always serves these JSON files and never falls back to
Google Sheets. On an older installation where they do not exist, the server
creates them once from the kit folders already present under `images/` and
`manuals/`; later changes are made through the Kit Editor and persist locally.
Empty troubleshooting trees receive a one-time standard diagnostic flow, and
`data/blueprints.json` receives a reusable “Standard interface diagnostics”
blueprint. The version marker in `data/catalog_meta.json` prevents later manual
deletions or custom edits from being overwritten by the seed process.

Uploaded manuals are stored under `manuals/<KIT KEY>/`, while extracted diagram
images remain under `images/<KIT KEY>/`. Enable `backup_include_media` if full
backups must contain both manuals and images.

For an off-server mirror, run `supabase_auxiliary_tables.sql` once. The server
then automatically upserts local kits, full troubleshooting trees, blueprints,
and manual file metadata into the matching `techSupport` tables after local
changes. Local files remain authoritative; a generation marker safely prunes
remote rows only after a complete snapshot upload succeeds. An administrator
can also force a mirror with `POST /api/system/sync-supabase`.

## ⚠️ Security — read before your first commit

This repo holds **code only**. Secrets and live data stay on the NAS and are
listed in `.gitignore`:

- **Never commit** `credentials.json`, `config.json`, `sessions.json`, or the
  other ignored files. `config.json` contains the `secret_key` used to sign
  requests to Apps Script.
- **Use a PRIVATE GitHub repo** and add the team as collaborators. The codebase,
  the LAN layout, and the auth design should not be public.
- If a secret is ever pushed by accident, **rotate `secret_key`** (change it in
  `config.json` on the NAS and in the Apps Script project) — see `RUNBOOK.md`.

## Repo layout

```
server.py              # the server (tracked)
*.html                 # the 8 pages (tracked)
chart_min.js           # charting lib (tracked)
images/                # logo + favicon (tracked)
config.example.json    # template; real config.json lives only on the NAS
deploy.sh              # deploy from a dev machine to the NAS
smoke_test.sh          # post-deploy endpoint checks
RUNBOOK.md             # operations: deploy, restart, restore, add users
.gitignore             # keeps secrets + runtime data out of git
```

Everything in `.gitignore` (credentials/config/sessions/stats/analytics/
pending_writes/dead_letters/roles/kits/troubleshooting/data/backups/drafts/logs)
is **managed on the NAS**, not in git.

## Contributing

1. Branch off `main`: `git checkout -b fix/short-description`
2. Make the change. If you touch `server.py`, validate it parses on Python 2/3:
   `python3 -c "import ast; ast.parse(open('server.py').read())"`
3. Open a Pull Request on GitHub. Another admin reviews and merges.
4. Deploy `main` with `./deploy.sh`, then run `./smoke_test.sh`.

Notes for editing `server.py`:
- It must stay **Python 2.7 compatible** (that's what the NAS runs).
- Never call `str()` on free-text ticket fields — use the py2-safe text helper.
- Bumping the `ZZ2_BUILD` constant is optional; `deploy.sh` stamps the live build
  with the git commit automatically.

## How a deploy reaches the NAS

The NAS can't pull from GitHub (no git, and its TLS is too old). So GitHub is the
source of truth, and `deploy.sh` runs on a **developer machine with the NAS share
mounted**: it pulls `main`, copies the code files onto the share, restarts the
server, and confirms the live `X-ZZ2-Build` header matches the commit. Data and
secrets on the NAS are never touched. See `RUNBOOK.md` for the details and for
the manual fallback.
