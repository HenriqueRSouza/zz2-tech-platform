# Legacy

The original single-file application, kept for three concrete reasons rather than
sentiment. Nothing here runs in production.

## `server.py`

The Python 2.7 monolith: 5,475 lines, one `ZZ2Handler` class with roughly 130
methods, covering HTTP, authentication, business rules, JSON persistence, a
write-behind queue and two third-party integrations.

**Why it is still here.** `tests/test_normalization_parity.py` extracts the pure
analytics helpers from this file by AST and runs them side by side with the ported
implementation in `zz2/services/normalization.py` — 409 assertions on canonical
label assignments, merge reports and the full payload.

That matters because the label-consolidation algorithm decides how tickets are
grouped in every analytics chart. A behaviour change there is not a crash; it is
silently different numbers, and nobody notices until a decision has been made on
them. This file is the reference the port is checked against.

Delete it and that test suite goes with it.

## The eight `*.html` pages

The source the frontend is generated from. `scripts/extract_frontend.py` splits
each page into `web/pages/`, `web/static/css/` and `web/static/js/`, vendors the
CDN dependencies, and verifies every page by reassembling it and comparing against
the original.

**Edit these, not `web/`.** Files under `web/` are regenerated and hand-edits are
overwritten. The one exception is the Settings page, where
`scripts/patch_settings_page.py` applies a reviewed set of changes on top —
removing the controls whose endpoints no longer exist.

If the generator ever stops being useful, the honest move is to delete both
scripts and this directory and start editing `web/` directly. Keeping a generator
nobody runs is worse than not having one.

## `config.example.json`, `gitignore`, `chart.min.js`, `images/`

* `config.example.json` — the old configuration template. Superseded by
  `deploy/.env.example`; kept so the migration script's key mapping can be checked
  against what the file actually contained.
* `gitignore` — an untracked-name duplicate of `.gitignore` that existed in the
  original repository. Kept only so its absence is not mistaken for a deletion.
* `chart.min.js` — the charting library, now vendored at
  `web/static/vendor/chart.min.js`.
* `images/` — the kit diagrams and DIP tables, plus the logo and favicon. These
  are *data*: a deployment gets them from `scripts/migrate_from_nas.py`, or by
  copying them into `$ZZ2_DATA_DIR/media/images/`. They stay tracked here so the
  repository is not the only place they exist.

## Historical context

The original design was reasonable for what it was: a LAN-only tool on a Buffalo
NAS whose Python was 2.7 and whose TLS was too old to reach GitHub. Deploying
meant mounting an SMB share from a developer's machine and byte-comparing every
copied file afterwards, because a network-mount `cp` could fail silently.

Most of what `SECURITY.md` lists as fixed was not a mistake in that setting — a
wildcard CORS header and an unauthenticated kit catalogue are unremarkable on an
isolated network. They became problems when the deployment target changed.
