# ZZ2 Tech Support Platform

Internal web app for CarPlay / Android Auto kit diagnostics and support
ticketing. Technicians work through per-kit troubleshooting trees, raise and edit
tickets, and see a customer's purchase and return history alongside the ticket
they are working on.

Version 2 is a restructuring of the original single-file server for deployment on
a public VPS. Same behaviour, different shape — and a different threat model,
which is why [`SECURITY.md`](SECURITY.md) is worth reading before the first
deploy.

- [What it does](#what-it-does)
- [Stack](#stack)
- [Repository layout](#repository-layout)
- [Running it locally](#running-it-locally)
- [Deploying](#deploying)
- [Migrating from the NAS](#migrating-from-the-nas)
- [Development](#development)
- [Documentation](#documentation)

---

## What it does

**Eight pages.** Sign-in, dashboard, the diagnostic tool, tickets, analytics, kit
editor, users and roles, system settings.

**Kit catalogue.** Kit metadata, wiring and DIP-switch diagrams, PDF manuals, and
a troubleshooting tree per kit. Local files are authoritative; Supabase receives a
one-directional mirror for off-server backup.

**Tickets.** Created and edited through the tool or the Tickets page. Supabase is
the shared source of truth, with a local store for fast reads and a durable
write-behind queue that keeps a legacy Google Sheet in step.

**Analytics.** Ticket trends, kit trouble scores and technician throughput, over a
consolidated view that merges the spelling variants humans typed into the sheet.

**Authorization.** Supabase Auth for identity, plus a two-axis model here:
permissions gate features, rank gates who you may act upon.

---

## Stack

| | |
|---|---|
| Runtime | Python 3.11+ |
| Web | FastAPI, uvicorn workers under gunicorn |
| Proxy | nginx, TLS via Let's Encrypt |
| Identity | Supabase Auth (JWT) |
| Data | JSON documents on disk, behind a repository layer; Supabase Postgres for tickets and the catalogue mirror |
| Frontend | Vanilla JavaScript, no build step |
| Process | systemd, or Docker Compose |

No build step for the frontend on purpose: the pages are classic scripts and
stylesheets served directly, so a change is a file edit and a refresh.

---

## Repository layout

```
zz2/            the application (see ARCHITECTURE.md for the layer rules)
web/            pages, stylesheets, scripts, vendored libraries
deploy/         systemd unit, nginx config, gunicorn config, Docker, deploy scripts
scripts/        one-off migration and audit tooling
sql/            Supabase schema setup, run once per project
tests/          668 tests, including a parity suite against the old server
legacy/         the original server.py and pages — reference, and the source the
                extraction script regenerates the frontend from
```

---

## Running it locally

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp deploy/.env.example .env      # then edit it
```

The minimum for a local run — the app refuses to start in `production` without
real values, and starts in `development` with these:

```bash
export ZZ2_ENVIRONMENT=development
export ZZ2_DATA_DIR=./var
export ZZ2_SUPABASE_URL=https://YOUR-PROJECT.supabase.co
export ZZ2_SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
export ZZ2_SUPABASE_SERVICE_KEY=sb_secret_...
export ZZ2_SUPABASE_JWT_SECRET=...        # from Supabase → Settings → API
export ZZ2_ENABLE_BACKGROUND_WORKERS=false
export ZZ2_LOG_JSON=false
```

```bash
.venv/bin/python -m zz2.main
```

Then open http://127.0.0.1:8080/. Interactive API docs are at `/docs` — disabled
in production, because they are a complete and accurate map of every endpoint.

---

## Deploying

One-time setup on the VPS:

```bash
sudo useradd --system --home /opt/zz2 --shell /usr/sbin/nologin zz2
sudo mkdir -p /opt/zz2 /etc/zz2 /var/lib/zz2
sudo chown zz2:zz2 /var/lib/zz2 && sudo chmod 750 /var/lib/zz2

sudo -u zz2 git clone https://github.com/YOUR-ORG/zz2-tech-support.git /opt/zz2
cd /opt/zz2
sudo -u zz2 python3.11 -m venv .venv
sudo -u zz2 .venv/bin/pip install -r requirements.txt gunicorn

sudo cp deploy/.env.example /etc/zz2/zz2.env
sudo $EDITOR /etc/zz2/zz2.env            # fill in the real values
sudo chown root:zz2 /etc/zz2/zz2.env && sudo chmod 640 /etc/zz2/zz2.env

sudo cp deploy/systemd/zz2.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now zz2

sudo cp deploy/nginx/zz2.conf /etc/nginx/sites-available/zz2
sudo ln -s /etc/nginx/sites-available/zz2 /etc/nginx/sites-enabled/
sudo certbot --nginx -d suporte.example.com.br
sudo nginx -t && sudo systemctl reload nginx
```

Then work through the checklist in [`SECURITY.md`](SECURITY.md#deployment-checklist)
and verify:

```bash
ZZ2_URL=https://suporte.example.com.br ./deploy/smoke_test.sh
```

Every deploy after that:

```bash
sudo -u zz2 /opt/zz2/deploy/deploy.sh
```

It fast-forwards the branch, installs dependencies, runs the tests and the
frontend contract audit, restarts, waits for the health endpoint to report the new
build, and **rolls back** if it does not.

Docker Compose is an alternative to systemd — one or the other, not both. See
`deploy/docker-compose.yml`.

---

## Migrating from the NAS

The old installation kept code, secrets, state and uploads in one flat directory.
Mount the share and run:

```bash
python scripts/migrate_from_nas.py \
    --source "/Volumes/share/ZZ2 Tech Support Tool" \
    --dest /var/lib/zz2 \
    --dry-run          # inspect first
```

It relocates state and media into the new layout, drops what Supabase Auth now
owns (`credentials.json`, `sessions.json`, `login_fails.json`), splits
`config.json` into environment secrets and runtime tunables, and **prints** the
secrets rather than writing them anywhere.

Two things it cannot do for you:

1. **Create the Supabase accounts.** Password hashes do not transfer. Create each
   technician in Supabase Authentication and set `app_metadata.role`.
2. **Rotate the Apps Script secret.** The old server published `config.json` on
   request, so treat the previous value as already exposed.

---

## Development

```bash
.venv/bin/python -m pytest              # 668 tests, ~7 seconds
.venv/bin/ruff check zz2/ tests/ scripts/
.venv/bin/python scripts/audit_frontend.py
```

The frontend audit is the one worth running before every commit that touches a
page or a route: it fails when the frontend references a path the backend no
longer serves, or reads a payload field the backend no longer sends. Both of those
fail silently in a browser — an empty panel, or a monitoring pill that reads
*down* when the worker is up.

**Regenerating the frontend.** The pages under `web/` are generated from
`legacy/*.html` by:

```bash
python scripts/extract_frontend.py      # split HTML / CSS / JS, vendor CDNs
python scripts/patch_settings_page.py   # drop the controls whose endpoints are gone
```

The extraction verifies every page by reassembling it and comparing against the
source, and writes nothing if any page fails to round-trip. Hand-edits to
`web/` will be overwritten — edit `legacy/` and re-run, or stop using the
generator and delete it.

**Tests worth knowing about:**

- `tests/test_normalization_parity.py` — 409 assertions comparing the ported
  analytics pipeline against the functions extracted from `legacy/server.py`. The
  consolidation algorithm decides how tickets are grouped in every chart, and a
  change there is not a crash — it is silently different numbers.
- `tests/test_security.py` — one test per vulnerability the old server shipped
  with, named after it.
- `tests/test_permissions.py` — the privilege-escalation guards.

---

## Documentation

| | |
|---|---|
| [`SECURITY.md`](SECURITY.md) | Threat model, what was fixed, what is still open, deployment checklist |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Layers, module map, the patterns and what each one bought |
| [`RUNBOOK.md`](RUNBOOK.md) | Operations: deploy, restart, restore, add a user, triage |
