# Runbook

Operational procedures, so the service does not depend on one person. Anyone with
`sudo` on the VPS and access to the Supabase project should be able to follow
these.

**Key facts**

| | |
|---|---|
| Service | `zz2.service` (systemd) |
| Code | `/opt/zz2` — read-only to the service user |
| State | `/var/lib/zz2` — the only writable path, and the only one to back up |
| Secrets | `/etc/zz2/zz2.env` — `0640 root:zz2` |
| Live build | every response carries `X-ZZ2-Build` |
| Logs | `journalctl -u zz2`, plus `/var/lib/zz2/logs/{server,audit}.log` |

---

## Contents

- [Is it healthy?](#is-it-healthy)
- [Deploy a change](#deploy-a-change)
- [Restart](#restart)
- [Add, disable or remove a user](#add-disable-or-remove-a-user)
- [Change a setting](#change-a-setting)
- [Backups and restore](#backups-and-restore)
- [Rotate a secret](#rotate-a-secret)
- [Common problems](#common-problems)
- [Emergency: take the site offline](#emergency-take-the-site-offline)

---

## Is it healthy?

```bash
curl -s https://suporte.example.com.br/healthz          # is the process alive?
curl -s https://suporte.example.com.br/readyz | jq      # can it serve?
systemctl status zz2
./deploy/smoke_test.sh                                  # full check, read-only
```

`/readyz` reports each dependency separately, which is what makes it useful — it
distinguishes "the disk is full" from "Supabase is not configured" from "the
ticket store has not been seeded" (the last is fine; reads fall through to
Supabase).

In the app: **System Settings → Health** shows host and process metrics, worker
liveness, and the write-pipeline state.

**What to watch.** The two lines worth alerting on:

```bash
# Things that need a human. Empty is the normal state.
grep '"msg":"health warning"' /var/lib/zz2/logs/server.log

# The hourly summary: queue depth, dead letters, last poll and reconcile.
grep '"msg":"health"' /var/lib/zz2/logs/server.log | tail -5
```

A warning fires when dead-letter writes exist, or the write queue exceeds 25
pending. Normal steady state is zero to three pending.

---

## Deploy a change

```bash
sudo -u zz2 /opt/zz2/deploy/deploy.sh
```

It fast-forwards `main`, installs dependencies, runs the test suite and the
frontend contract audit, stamps the build, restarts, and waits for `/healthz` to
report the new build. **If it does not become healthy in 60 seconds it rolls back**
to the previous commit and restarts onto that.

Then:

```bash
ZZ2_URL=https://suporte.example.com.br ./deploy/smoke_test.sh
```

**If the deploy refuses to fast-forward,** someone edited the code on the server.
Do not force it — find out what changed first:

```bash
cd /opt/zz2 && git status && git diff
```

---

## Restart

```bash
sudo systemctl restart zz2
```

Graceful: in-flight requests finish, the write-behind queue drains, and the
poller stops between iterations rather than mid-write. Allow up to 45 seconds.

There is deliberately **no restart button in the app**. The old server had one
that called `os.execv`, which made one bug in the auth check a remote code
execution primitive.

To reload only the environment file after editing it, a restart is still required
— systemd reads `EnvironmentFile` at start.

---

## Add, disable or remove a user

Identity lives in **Supabase**, not on this server. Two routes:

**Preferred — the Users page** (needs `manage_users`). Add a user, set their role
and per-user extra permissions, reset a password, or toggle Active. The page
enforces the hierarchy: you can only act on accounts ranked below your own, and
only grant permissions you hold yourself.

**If the app is down — the Supabase dashboard.** Authentication → Users. Create
the account, then set `app_metadata`:

```json
{ "role": "tech", "permissions": [] }
```

`role` must match a key in `/var/lib/zz2/roles.json` (`super_admin`, `admin`,
`tech`, or a custom one). **It must be `app_metadata`, never `user_metadata`** —
the latter is writable by the user's own session, and a role read from there would
be self-service.

**To disable an account:** toggle Active in the app, or ban the user in Supabase.
The application refuses to deactivate or delete the **last active super-admin** —
losing it is unrecoverable from inside the app.

A role or permission change takes effect within 60 seconds without the user
signing in again (that is the principal cache TTL).

---

## Change a setting

**System Settings → Settings** (super-admin only). Poll intervals, the reconcile
window, auto-resolve threshold, backup retention, the work window. Applied live —
no restart.

Every value is validated against declared bounds and **rejected** with a message
if out of range, rather than silently clamped. The previous `config.json` is
snapshotted to `/var/lib/zz2/backups/config/` before each write, so a bad edit is
recoverable.

Secrets are **not** editable here, by design. They live in
`/etc/zz2/zz2.env`; changing one means editing that file and restarting.

---

## Backups and restore

**What is backed up.** State and media from `/var/lib/zz2` — the state documents,
the kit store, kit history, drafts, and (when `backup_include_media` is on) images
and manuals. Code is *not*: git is the source of truth and a deploy is
reproducible.

**Automatic.** Nightly, just after midnight, before the auto-resolve and analytics
rebuild — so if a later step goes wrong there is a snapshot from before it.
Retention follows `backup_keep` (default 30), with the small pre-write snapshots
kept in their own bucket so they cannot crowd out a real backup.

**Manual.** System Settings → Backups & files → Run backup, or:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     https://suporte.example.com.br/api/system/backup
```

**Restore.** In the app: System Settings → Backups & files → View / restore, pick
the snapshot, pick the files. Requires a **super-admin** — a restore overwrites
live state. The current state is snapshotted first as `pre-restore`, so restoring
the wrong snapshot is itself undoable.

By hand, if the app is down:

```bash
sudo systemctl stop zz2
sudo -u zz2 cp /var/lib/zz2/backups/20260820_000100/store/kits.json \
               /var/lib/zz2/store/kits.json
sudo systemctl start zz2
```

**Off-box copies.** Backups on the same VPS do not survive losing the VPS. Ship
them somewhere else:

```bash
rsync -az --delete /var/lib/zz2/backups/ backup-host:/srv/zz2-backups/
```

And restore-test it. A backup that has never been restored is a hypothesis.

---

## Rotate a secret

```bash
sudo $EDITOR /etc/zz2/zz2.env
sudo systemctl restart zz2
curl -s https://suporte.example.com.br/readyz | jq
```

**Apps Script secret** (`ZZ2_APPSCRIPT_SECRET`): must change in *both* places or
the sheet integration stops. Update the Apps Script project's matching key too.
While they disagree, ticket writes accumulate in the queue and retry — they are
not lost, but drain them before assuming all is well.

**Supabase service key** (`ZZ2_SUPABASE_SERVICE_KEY`): rotate in Supabase
(Settings → API), then here. This key bypasses row-level security; treat it as
root.

**Supabase JWT secret** (`ZZ2_SUPABASE_JWT_SECRET`): rotating it invalidates every
issued token, so **everyone is signed out**. Do it during a quiet window.

---

## Common problems

### A page change did not appear

Confirm the live build changed:

```bash
curl -sI https://suporte.example.com.br/login.html | grep -i x-zz2-build
```

If the header still shows the old commit, the deploy did not land — check
`journalctl -u zz2 -n 100`. If the header is correct but the page behaves as
before, it is a browser cache: assets are served with `no-cache` plus an ETag, so
a normal reload picks up the change. A user with an older cached copy from before
that change may need one hard reload (Ctrl-Shift-R).

### Tickets are not reaching the Google Sheet

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
     https://suporte.example.com.br/api/system/sync-health | jq
```

- **`pending_writes` climbing, `dead_letters` zero** — Apps Script is unreachable
  or slow. The queue is retrying with backoff; nothing is lost. Check the Apps
  Script deployment is still published and `ZZ2_APPSCRIPT_URL` is right.
- **`dead_letters` above zero** — writes exhausted their 25 attempts. Fix the
  cause, then System Settings → replay them. They are kept, not dropped, so the
  divergence is visible and recoverable.

### A user cannot sign in

1. Does the account exist and is it un-banned in Supabase Authentication?
2. Does `app_metadata.role` name a role in `roles.json`? An unknown role grants
   **zero** permissions — the user signs in and can open nothing.
3. Check the audit log: `grep authz.denied /var/lib/zz2/logs/audit.log`.

### Everything returns 401 after a deploy

Almost always `ZZ2_SUPABASE_JWT_SECRET` — missing, or wrong for the project. Check
`/readyz`, then:

```bash
grep '"msg":"token rejected"' /var/lib/zz2/logs/server.log | tail
```

### 502 from nginx

The application is not running or not listening:

```bash
systemctl status zz2
journalctl -u zz2 -n 100 --no-pager
ss -tlnp | grep 8080
```

A `ValueError: Unsafe production configuration` at startup means a production
guardrail refused the config — the message names each problem. That is deliberate:
a misconfigured deploy should never reach the serving state.

### Analytics is empty or stale

The view is rebuilt nightly. To force it:

- **Consolidation only** (fast): Analytics page → rebuild, or
  `POST /api/analytics/rebuild`.
- **Full re-export from the sheet** (slow, minutes): System Settings → Rebuild
  store, or `POST /api/system/rebuild`.

### A discarded ticket still shows for someone else

It clears on their next pane refresh, or the Today "Refresh" button. The server
removes it from the store immediately and suppresses it while the sheet delete is
still queued.

---

## Emergency: take the site offline

```bash
sudo systemctl stop zz2
```

nginx then returns 502. To show something friendlier, or to keep the site up
read-only while investigating:

```bash
# Stop only the background work; keep serving reads and writes.
sudo sed -i 's/^ZZ2_ENABLE_BACKGROUND_WORKERS=.*/ZZ2_ENABLE_BACKGROUND_WORKERS=false/' \
     /etc/zz2/zz2.env
sudo systemctl restart zz2
```

That stops the poller, the nightly job and the write-behind drain while leaving
the app usable. Queued writes stay on disk and resume when it is re-enabled.
