# Architecture

How the code is organised, and why. The short version: `legacy/server.py` was
5,475 lines in one file with one class holding roughly 130 methods. This is the
same behaviour, arranged so that each part can be read, tested and changed on its
own.

- [Layers](#layers)
- [Module map](#module-map)
- [Design patterns, and what each one bought](#design-patterns-and-what-each-one-bought)
- [Data flow: saving a ticket](#data-flow-saving-a-ticket)
- [Storage layout](#storage-layout)
- [Background workers](#background-workers)
- [What changed from the monolith](#what-changed-from-the-monolith)

---

## Layers

Each layer depends only on the ones below it. The rule is mechanical, and it is
what makes the dependency direction checkable rather than aspirational:

```
   api/          HTTP: routing, schemas, middleware, authorization dependencies
     │           Knows FastAPI. Contains no business rules.
     ▼
   services/     Business logic and orchestration.
     │           Knows nothing about HTTP.
     ▼
   repositories/ Persistence. The only layer that touches storage.
     │
     ├──────────▶ gateways/   Third-party APIs (Supabase, Google Apps Script)
     ▼
   domain/       Entities and pure rules. Depends on nothing.
     ▼
   core/         Cross-cutting primitives: logging, errors, atomic storage,
                 caching, events.

   workers/      Background loops. Compose services; owned by the app lifespan.
```

Two consequences worth stating:

**A service that imports `zz2.core.jsonstore` is a bug.** Storage access goes
through a repository. If it does not, a storage change reaches into that service.

**A service that imports from `zz2.api` is a bug.** Business logic that knows
about `Request` or `HTTPException` cannot be tested without a web framework, and
cannot be reused by a worker.

---

## Module map

### `core/` — primitives

| Module | Responsibility |
|---|---|
| `errors.py` | Exception hierarchy and its HTTP mapping. Clients get a code and a safe message; logs get the detail. |
| `logging.py` | Structured logs plus a separate audit channel. Credentials are scrubbed by a filter, not by each caller. |
| `jsonstore.py` | Atomic JSON documents: write → fsync → rename → fsync the directory. Per-file locks. Corrupt files are quarantined, not silently replaced. |
| `text.py` | Text coercion and the allowlist sanitisers for path components and filenames. |
| `cache.py` | One TTL+LRU cache, replacing five hand-rolled dicts that never evicted. |
| `events.py` | In-process event bus, so repositories can announce a change without knowing who listens. |

### `domain/` — entities and rules

| Module | Responsibility |
|---|---|
| `permissions.py` | The permission catalogue, `Role`, `Principal`, and the hierarchy rules. Pure functions — no I/O — which is what makes the escalation guards testable in isolation. |
| `models.py` | `Ticket`, `Kit`, `TroubleshootingNode`, `Blueprint`, `WriteOperation`, and the mapping between their local / API / Supabase representations. |
| `troubleshooting.py` | `TroubleshootingSession` and `SessionOutcome`: one technician walking one kit's tree. |

### `repositories/` — persistence

| Module | Responsibility |
|---|---|
| `base.py` | `JsonDocumentRepository`: read, write, `mutate()` under lock, publish a change event. |
| `tickets.py` | The local ticket store. All number matching is digits-only. |
| `kits.py` | Kits, troubleshooting trees, blueprints, catalogue metadata. |
| `roles.py` | Role definitions, with the built-in roles repaired on every read. |
| `write_queue.py` | The durable write queue and the dead-letter store. |
| `stats.py`, `drafts.py`, `kit_history.py` | Counters, per-user drafts, kit snapshots. |
| `troubleshooting_sessions.py` | Executed sessions, behind one interface with two stores: the Supabase table or a local JSON document. |

### `gateways/` — third parties

| Module | Responsibility |
|---|---|
| `http.py` | One pooled `httpx.Client` with mandatory timeouts and redirects disabled. |
| `supabase.py` | PostgREST access with the service key, plus the Auth Admin API for user management. |
| `storage.py` | Private Supabase Storage access for durable PDF manual upload, download and kit duplication. |
| `appscript.py` | Signed calls to the Google Apps Script sheet, with the host pinned. |

### `services/` — business logic

| Module | Responsibility |
|---|---|
| `auth.py` | Access token → `Principal`. Local JWT verification, remote as a fallback, or this server's own SAML session tokens. |
| `saml.py`, `sso.py`, `session_tokens.py` | SAML SP for Entra ID (request, response validation), authorization of the SAML identity, and the revocable session it receives. |
| `lockout.py` | Brute-force guard: blocks addresses presenting many distinct rejected credentials. |
| `troubleshooting_sessions.py` | Record, amend, list and aggregate executed troubleshooting sessions. |
| `tickets.py` | Create, edit, discard, search, history. Owns the write-behind ordering. |
| `analytics.py` | The three-tier view: store → consolidated view → served payload. |
| `normalization.py` | Label consolidation. Pure, and parity-tested against the monolith. |
| `kits.py`, `catalog.py` | Kit editing; catalogue bootstrap and coverage reporting. |
| `users.py`, `roles.py` | User and role administration, enforcing the hierarchy. |
| `customers.py` | Customer history and item sales from the ERP schema. |
| `uploads.py` | Media validation and storage. |
| `backup.py` | Snapshots, restores, download bundles. |
| `mirror.py` | One-directional catalogue mirror to Supabase. |
| `sheet_sync.py` | Poll and reconcile against the sheet. |
| `autoresolve.py` | Chunked auto-resolution of stale tickets. |
| `telemetry.py` | Host, process and store metrics for the System page. |
| `runtime_config.py` | Operator-tunable settings: clamped on read, validated on write. |

### `api/` — HTTP

| Module | Responsibility |
|---|---|
| `deps.py` | Authorization dependencies. `RequireKitEditor` and friends. |
| `error_handlers.py` | The single place an exception becomes a response. |
| `middleware/security.py` | Response hardening headers and the CSP. |
| `middleware/request_context.py` | Request id, body cap, access log. |
| `middleware/rate_limit.py` | Per-client read and write budgets. |
| `middleware/auth_guard.py` | Applies the brute-force lockout to every request. |
| `routers/*.py` | One router per domain, plus `pages.py` (static) and `compat.py` (the monolith's URL scheme). |

### `workers/` — background loops

| Module | Responsibility |
|---|---|
| `base.py` | Interruptible sleep, error backoff, named threads, graceful stop. |
| `write_behind.py` | Drains the queue to the sheet, FIFO, dead-lettering on exhaustion. |
| `poller.py` | Polls for sheet-direct tickets; reconciles on a longer cycle. |
| `scheduler.py` | Nightly backup → auto-resolve → analytics rebuild, plus hourly health. |
| `manager.py` | Starts and stops them all, wired to the application lifespan. |

---

## Design patterns, and what each one bought

Patterns are only worth naming when they solved a concrete problem. Each of these
did.

### Repository

**Problem.** `_handle_kit_save` opened `kits.json`, mutated a dict, called
`_save_json`, snapshotted history and triggered a Supabase upload — persistence,
business rules and remote sync in one method. Moving to Postgres would have meant
rewriting every handler.

**Now.** Services ask a repository for domain objects. Swapping JSON for Postgres
means a new subclass with the same interface and no service change.

### Service layer

**Problem.** Business logic lived in HTTP handlers, so it could not be reached
from a background worker without faking a request. The monolith's nightly job
duplicated the rebuild logic for exactly this reason.

**Now.** `AnalyticsService.rebuild_from_sheet` is called by both the scheduler and
`POST /api/system/rebuild`.

### Dependency injection

**Problem.** Seventy handlers each began with the same four lines of session
lookup and permission check. The omissions were invisible: `/api/config` had none;
`/api/proxy` served the catalogue unauthenticated.

**Now.** The requirement is a parameter. A route that takes no principal is
visibly unprotected in its own definition.

### Gateway / Adapter

**Problem.** `urllib2` calls were scattered through the file, each with its own
error handling, some with timeouts and some without — the Supabase auth check had
none, so an unresponsive peer blocked a request thread forever.

**Now.** Two gateways, one pooled client, mandatory timeouts, uniform errors.

### Event bus (Observer)

**Problem.** The lowest-level function in the file reached up two layers:

```python
if path in (KITS_JSON, TS_JSON, BLUEPRINTS_JSON) \
   and '_schedule_auxiliary_sync' in globals():
    _schedule_auxiliary_sync()
```

`_save_json` triggered a Supabase upload, and needed a `globals()` probe to avoid
exploding during startup. Every new caller inherited the behaviour.

**Now.** Repositories publish `KitCatalogChanged`. The mirror subscribes at
startup. Removing the mirror is deleting one subscription.

### Command queue

**Problem.** Saving a ticket blocked on a multi-second Apps Script round-trip.

**Now.** `WriteOperation` is serialised to disk and replayed by a worker until it
succeeds or exhausts its attempts. The user's save returns immediately; a restart
mid-flight loses nothing.

### Strategy

Two places where the alternative was a branch repeated at every call site:
authentication (local JWT verification vs. remote), and ticket numbering
(optimistic local vs. synchronous through the sheet when the store is unseeded).

### Specification

**Problem.** Each tunable setting had its bounds written twice — as a clamp when
reading and as validation when writing. The two lists drifted:
`auto_resolve_days` was writable in the range 1–365 with no read-side clamp at
all.

**Now.** One `ConfigField` per setting carries its own type, bounds and label, and
both paths derive from it.

### Facade

`KitCatalogRepository` composes kits, trees and blueprints, so the four services
that need a coherent catalogue view take one dependency instead of three — and the
invariant "every kit has a tree" lives in one place.

---

## Data flow: saving a ticket

The ordering is the design, not an accident.

```
POST /api/tickets
  │
  ├─ RequestContextMiddleware   request id, body cap, access log
  ├─ RateLimitMiddleware        write budget
  ├─ RequireCreateTickets       Authorization header → Principal → permission
  ├─ TicketPayload              schema validation
  │
  ▼
TicketService.create
  │
  ├─ 1. reserve a number      local store max, floored by Supabase's max
  │                            (the remote read happens *before* the store lock,
  │                             so a network call never blocks unrelated reads)
  │
  ├─ 2. Supabase INSERT        synchronous, authoritative
  │                            fails → 502, nothing queued, all copies consistent
  │
  ├─ 3. increment the counter  the technician's dashboard stats
  │
  ├─ 4. enqueue the sheet write  durable, retried, FIFO
  │
  └─ 5. append to the local store  so the Today pane updates immediately
  │
  ▼
201 { ok, ticketNum, queued }        (the sheet write is still in flight)
  │
  ▼
WriteBehindWorker                    seconds later
  ├─ Apps Script write
  └─ if the sheet assigned a different number:
       renumber the store AND repoint every queued operation for that ticket
       (renumbering only the store would leave a queued delete aimed at a number
        that by then belongs to somebody else's ticket)
```

---

## Storage layout

Code and state are separate roots. `APP_DIR` is read-only at runtime; `DATA_DIR`
is the only writable path, and the only one that needs backing up.

```
/opt/zz2/                      APP_DIR — read-only, replaced by a deploy
  zz2/                         application package
  web/pages/                   the eight pages
  web/static/{css,js,vendor,images}
  deploy/                      systemd, nginx, gunicorn, Docker
  legacy/                      the monolith, kept for reference and parity tests

/var/lib/zz2/                  DATA_DIR — 0750, the only writable path
  config.json                  operator-tunable settings (no secrets)
  roles.json  stats.json  analytics.json
  pending_writes.json  dead_letters.json
  store/                       kits, troubleshooting, blueprints, kit_history
  media/images/<KIT>/          diagrams (authenticated route)
  media/manuals/<KIT>/         PDFs (authenticated route)
  drafts/  backups/  cache/  logs/
```

The separation is what makes the static allowlist safe: the web root contains no
secrets at all, so a traversal bug cannot reach `config.json` the way it could
when code, secrets and pages shared one folder.

---

## Background workers

All three run as threads inside the application process, started and stopped by
the lifespan.

| Worker | Cadence | Does |
|---|---|---|
| `zz2-write-behind` | ~2s when idle, immediately while draining | Pushes queued writes to the sheet |
| `zz2-poller` | `poll_interval` (default 30s) | Appends sheet-direct tickets; reconciles the recent window on a longer cycle |
| `zz2-scheduler` | 60s tick | Nightly backup → auto-resolve → analytics rebuild; hourly health line |

**They are why the deployment runs one worker process.** Four processes means four
nightly backups, four pollers against the Apps Script quota, and four write-behind
loops racing for the head of the same queue file. `deploy/gunicorn.conf.py`
records the order of work if that ever needs to change.

Each worker sleeps on a `threading.Event`, so `SIGTERM` is honoured immediately
rather than after up to a full poll interval — the monolith's loops were
`while True: time.sleep(n)` with no way to stop them, and deploying meant
`pkill -f server.py` mid-write.

---

## What changed from the monolith

| | Before | After |
|---|---|---|
| Runtime | Python 2.7 (EOL 2020) | Python 3.11+ |
| Server | `BaseHTTPServer` + `ThreadingMixIn` | FastAPI / uvicorn behind nginx |
| Largest file | `server.py`, 5,475 lines | `normalization.py`, ~490 lines |
| Largest page | `index.html`, 5,441 lines | 308 lines HTML + separate CSS/JS |
| Routing | 130 `if path == ...` branches | 61 typed routes across 11 routers |
| Authorization | 4 lines repeated in ~70 handlers | declared in the route signature |
| Locking | one global `threading.Lock` | per-document reentrant locks |
| Secrets | `config.json` in the web root | process environment |
| Identity | hand-rolled PBKDF2 + JSON files | Supabase Auth |
| Tests | none | 668 |
| Shutdown | `pkill` | graceful, queue drains |
