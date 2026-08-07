# ZZ2 Tech Support Tool — Runbook

Operational procedures so the tool doesn't depend on one person. Anyone with
admin access and the NAS login should be able to follow these.

Key facts:
- Server: `server.py` on the NAS at `192.168.1.190:8080`, Python 2.7.
- App folder on the NAS: `/mnt/disk1/share/ZZ2 Tech Support Tool`
  (also reachable over the SMB share).
- Which build is live: every response carries an `X-ZZ2-Build` header.
  Check it: `curl -sI http://192.168.1.190:8080/login.html | grep -i x-zz2-build`

---

## Deploy a change

Normal path — from a dev machine with the share mounted and the repo cloned:

```bash
export ZZ2_NAS_PATH="/Volumes/share/ZZ2 Tech Support Tool"   # your mount
./deploy.sh        # pulls main, copies code, restarts, verifies the live build
./smoke_test.sh    # confirms the key endpoints still work
```

`deploy.sh` only copies code (`server.py`, `*.html`, `chart_min.js`, logo/
favicon). It never touches data or secrets on the NAS.

## Restart the server (manual)

When you change `server.py`, the server must be **restarted** to load it (HTML/JS
changes only need a browser refresh). Two ways:

- **From the app:** Settings → System has a restart action (super-admin), or
  `POST /api/system/restart` with a super-admin token.
- **Over telnet:** telnet into the NAS and run:
  ```
  pkill -f server.py
  python '/mnt/disk1/share/ZZ2 Tech Support Tool/server.py' &
  ```
  The server also **auto-starts when the NAS reboots**, so a NAS power-cycle is a
  heavier-handed fallback if telnet isn't available.
  Confirm the new build loaded:
  `curl -sI http://192.168.1.190:8080/login.html | grep -i x-zz2-build`

## Add, disable, or reset a user

- **Preferred:** Users page in the app (admin/super-admin). Add user, set role,
  set/reset password, or toggle Active.
- **If the UI is down:** edit `credentials.json` on the NAS (it's a list under
  `technicians`). To disable someone, set `"active": false`. Do **not** hand-edit
  password hashes; create the user via the UI so the hash is generated correctly.
  Restart after editing.

## Restore from backup

- Backups are written nightly to the `backups/` folder on the NAS (kept per
  `backup_keep` in config). The app's System page can list and download them.
- To restore a file (e.g. a bad `kits.json` or `config.json`): stop the server,
  copy the good copy from the backup over the live file, restart.
- Config edits are also snapshotted before each save under a backups subfolder,
  so a bad settings change can be rolled back the same way.

## Rotate the secret_key (do this if it ever leaked, e.g. pushed to git)

1. Pick a new long random string.
2. Update `secret_key` in `config.json` on the NAS.
3. Update the matching key in the Google Apps Script project so the server and
   Apps Script still agree.
4. Restart the server.

## Monitoring / "is it healthy?"

The server logs an hourly `HEALTH` line and a `HEALTH WARNING` line when the
write queue backs up or dead-letter writes appear. Check the log:

```
grep HEALTH <the server log file>          # hourly summaries
grep "HEALTH WARNING" <the server log file> # things that need a human
```

Also available in the app (super-admin): System → sync health (pending writes,
dead letters, last poll/reconcile) and the dead-letter replay/clear actions.

## Common issues

- **Login is slow.** PBKDF2 runs in pure Python on 2.7. The default is now 15,000
  iterations and accounts auto-migrate on next login; watch for "Upgraded
  password hash for …" in the log. (Lower it further in `_DEFAULT_PBKDF2_ITERS`
  if needed.)
- **A page change didn't show up.** Confirm the live build header changed. HTML
  is revalidated by content hash, so a real change always shows on refresh; if
  the header didn't change, the file didn't actually deploy.
- **A discarded/edited ticket lingers in someone else's Today pane.** It clears on
  their next pane refresh (or the Today "Refresh" button); the server removes it
  from the store immediately on delete.
- **Writes not reaching the Sheet.** Check `HEALTH WARNING` / dead letters; use
  the System page to replay dead letters once the cause is fixed.
