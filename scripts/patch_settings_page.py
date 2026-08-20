#!/usr/bin/env python3
"""Bring the Settings page in line with the migrated backend.

Four endpoints were removed with the move to Supabase Auth and systemd, and the
page still had controls for all of them:

``/api/system/restart``
    Re-executed the process from a web request. Restarts belong to systemd now.

``/api/system/revoke`` and ``/api/system/clear-expired``
    Managed the local ``sessions.json``, which no longer exists.

``/api/system/unlock``
    Cleared the local login-lockout table. Supabase owns login throttling.

Left alone, those controls would 404 on click and the panels behind them would
render empty — a page that looks broken rather than one that says what changed. So
the page is edited to describe the new reality: the two panels are replaced, the
two buttons removed, and the three now-dead functions deleted.

Applied as a script rather than by hand so re-running the extraction does not
quietly restore the old markup.

    python scripts/patch_settings_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "web" / "static" / "js" / "settings.3.js"
HTML = ROOT / "web" / "pages" / "settings.html"


def patch_js(source: str) -> str:
    """Remove the dead controls, repoint renamed payload fields, rebuild security."""
    source = _rebind_workers(source)
    # -- Actions: drop the two buttons whose endpoints are gone ---------------
    source = _replace(
        source,
        """    '<button class="act" onclick="act(\\'/api/system/clear-expired\\',\\'Expired sessions cleared\\',this)"><span class="ic">&#129529;</span><span>Clear expired<small>purge dead sessions</small></span></button>',
    '<button class="act warn" onclick="restartServer(this)"><span class="ic">&#9211;</span><span>Restart server<small>re-exec the process</small></span></button>'
  ].join('');""",
        """    '<button class="act" onclick="act(\\'/api/system/sync-supabase\\',\\'Catalogue mirrored\\',this)"><span class="ic">&#9729;</span><span>Mirror catalogue<small>push kits to Supabase</small></span></button>'
  ].join('');""",
        "actions",
    )

    # -- Sessions: Supabase owns them, so report that instead of an empty table
    old_sessions = source[source.index("  /* sessions */"):source.index("  /* files */")]
    source = source.replace(old_sessions, """  /* sessions -- owned by Supabase Auth, not by this server.
     There is no local session store to enumerate, so rather than render an empty
     table the panel says where sessions actually live. Revoking one is done from
     the Supabase dashboard, which is also the only place that can. */
  const sessionsPanel = $('#g-sessions-note');
  if (sessionsPanel) {
    sessionsPanel.innerHTML =
      '<div class="bd" style="padding:14px">' +
      '<b>Sessions are managed by Supabase Auth.</b>' +
      '<div class="muted" style="margin-top:6px">Sign-ins, refresh tokens and ' +
      'revocation live in your Supabase project under Authentication &rsaquo; Users. ' +
      'This server verifies each access token per request and caches the result ' +
      'for 60 seconds, so a role change or a disabled account takes effect within ' +
      'a minute without anyone re-signing in.</div></div>';
  }

""")

    # -- Security: the new payload describes configuration, not accounts ------
    old_security = source[source.index("  /* security */"):source.index("  /* backups */")]
    source = source.replace(old_security, """  /* security -- what this process can actually see and be wrong about.
     The old panel reported plaintext passwords, lockouts and failed logins: all
     facts about a local credential store that no longer exists. A panel that
     looked authoritative while reading nothing would be worse than none, so this
     reports configuration posture instead. */
  const sec = d.security || {};
  const secrets = sec.secrets_configured || {};
  const mode = sec.data_dir_mode || {};

  function flag(ok, good, bad) {
    return ok
      ? '<span class="pill up"><span class="dot"></span>' + good + '</span>'
      : '<span class="pill exp">' + bad + '</span>';
  }

  const rows = [
    ['Authentication', '<span class="role-badge">' + esc(sec.auth_provider || 'unknown') + '</span>'],
    ['Token verification', flag(sec.jwt_verification === 'local',
                                'local (no upstream call)', 'remote (per-request call)')],
    ['HTTPS', flag(sec.https_enforced, 'enforced', 'not enforced')],
    ['Rate limiting', flag(sec.rate_limit_enabled, 'on', 'off')],
    ['CORS origins', (sec.cors_origins && sec.cors_origins.length)
        ? esc(sec.cors_origins.join(', '))
        : '<span class="muted">same-origin only</span>'],
    ['Supabase service key', flag(secrets.supabase_service_key, 'configured', 'missing')],
    ['Supabase JWT secret', flag(secrets.supabase_jwt_secret, 'configured', 'missing')],
    ['Apps Script secret', flag(secrets.appscript_secret, 'configured', 'missing')],
    ['Data directory mode', mode.mode
        ? (esc(mode.mode) + ' ' + flag(!mode.world_readable, 'not world-readable',
                                       'world-readable'))
        : '<span class="muted">unknown</span>'],
  ];

  $('#g-security').innerHTML =
    '<div class="card"><div class="hd">Security posture</div>' +
    '<div class="bd" style="padding:0"><table><tbody>' +
    rows.map(function (r) {
      return '<tr><td style="width:220px">' + r[0] + '</td><td>' + r[1] + '</td></tr>';
    }).join('') +
    '</tbody></table></div></div>';

""")

    # -- Worker status: the payload key is `workers`, not `threads` -----------
    source = _replace(
        source,
        """    worker('Nightly scheduler', th.scheduler)+
    worker('Ticket poller', th.poller)+
    worker('Write-behind queue', th.write_behind)+'</div></div>';""",
        """    worker('Nightly scheduler', w['zz2-scheduler'])+
    worker('Ticket poller', w['zz2-poller'])+
    worker('Write-behind queue', w['zz2-write-behind'])+'</div></div>';""",
        "worker status",
    )

    # -- Store tiles: drop the two whose data no longer exists ---------------
    source = _replace(
        source,
        """    statCard('Active sessions', s.sessions_active||0, 'signed in now'),
""",
        "",
        "active-sessions tile",
    )
    source = _replace(
        source,
        """    statCard('Store generated', isoNice(s.generated), 'snapshot time'),
    statCard('CSV cache', s.csv_cache||0, 'cached sheets')""",
        """    statCard('Store generated', isoNice(s.generated), 'snapshot time')""",
        "csv-cache tile",
    )

    # -- Backups no longer include application code -------------------------
    source = _replace(
        source,
        """    (b.has_code?' <span style="background:#eef0ff;color:#4f46e5;font-size:10px;font-weight:700;padding:1px 6px;border-radius:6px">code</span>':'')+'</td>'+""",
        """    (b.include_media?' <span style="background:#eef0ff;color:#4f46e5;font-size:10px;font-weight:700;padding:1px 6px;border-radius:6px">media</span>':'')+'</td>'+""",
        "backup code badge",
    )

    # -- Remove the three now-dead functions ---------------------------------
    for name in ("restartServer", "revoke", "unlock"):
        source = _remove_function(source, name)

    return source


def _rebind_workers(source: str) -> str:
    """Point the worker pills at ``server.workers``.

    The telemetry payload reports worker liveness under ``workers``, keyed by real
    thread name, rather than the monolith's ``threads`` keyed by a short label.
    Reading the old key yields ``undefined`` for all three, which renders every
    worker as *down* whether it is or not — a monitoring panel that lies is worse
    than one that is missing.
    """
    for old, new in (
        ("const th=sv.threads||{};", "const w=sv.workers||{};"),
        ("const th = sv.threads || {};", "const w = sv.workers || {};"),
        ("const th=(d.server||{}).threads||{};", "const w=(d.server||{}).workers||{};"),
    ):
        if old in source:
            return source.replace(old, new, 1)
    raise SystemExit("could not find the worker-status binding")


def patch_html(source: str) -> str:
    """Swap the sessions table for a note container."""
    old = """  <div class="sec-title">Active sessions</div>
  <div class="card">
    <div class="hd">Signed-in users <span class="muted" id="sess-count" style="font-weight:400"></span></div>
    <div class="bd" style="padding:0"><table><thead><tr>
      <th>User</th><th>Role</th><th>IP</th><th>Signed in</th><th>Expires</th><th>Status</th><th></th>
    </tr></thead><tbody id="g-sessions"></tbody></table></div>
  </div>"""
    new = """  <div class="sec-title">Sessions</div>
  <div class="card" id="g-sessions-note"></div>"""
    return _replace(source, old, new, "sessions panel")


def _replace(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise SystemExit(f"could not find the {label} block; has the page changed?")
    return source.replace(old, new, 1)


def _remove_function(source: str, name: str) -> str:
    """Delete ``async function <name>(...) { ... }`` by brace matching.

    Brace matching rather than a regex: these bodies contain nested braces,
    template strings and object literals, and a regex either stops at the first
    ``}`` or swallows the rest of the file.
    """
    for prefix in (f"async function {name}(", f"function {name}("):
        start = source.find(prefix)
        if start != -1:
            break
    else:
        raise SystemExit(f"could not find function {name}")

    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                while end < len(source) and source[end] in "\n\r":
                    end += 1
                return source[:start].rstrip("\n") + "\n\n" + source[end:]
    raise SystemExit(f"unbalanced braces while removing {name}")


def main() -> int:
    js = JS.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")

    js_patched = patch_js(js)
    html_patched = patch_html(html)

    for removed in ("/api/system/restart", "/api/system/revoke",
                    "/api/system/unlock", "/api/system/clear-expired"):
        assert removed not in js_patched, f"{removed} is still referenced"
    for dead in ("restartServer", "revoke(", "unlock(",
                 "sessions_active", "csv_cache", "has_code", "server.threads"):
        assert dead not in js_patched, f"{dead} is still referenced"

    JS.write_text(js_patched, encoding="utf-8")
    HTML.write_text(html_patched, encoding="utf-8")

    print(f"settings.3.js  {js.count(chr(10))} -> {js_patched.count(chr(10))} lines")
    print(f"settings.html  {html.count(chr(10))} -> {html_patched.count(chr(10))} lines")
    print("removed: restart, revoke, unlock, clear-expired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
