#!/usr/bin/env python3
"""Check that the frontend and the backend still agree.

Two silent-failure modes this catches, both of which cost real debugging time
during the migration:

**A path the backend no longer serves.** The button 404s and the panel behind it
renders empty. Nothing throws, so the page looks broken rather than saying what
changed.

**A payload field the backend no longer sends.** ``d.server.threads`` became
``d.server.workers``; reading the old key yields ``undefined``, which rendered
every background worker as *down* whether it was or not. A monitoring panel that
lies is worse than one that is missing.

Both checks are deliberately broad — they match string literals and property
accesses anywhere in the page and script files, because several pages pass paths
to helpers (``act('/api/system/rebuild')``) and read payload fields through local
bindings.

Run from the repository root:

    python scripts/audit_frontend.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("ZZ2_ENVIRONMENT", "development")
os.environ.setdefault("ZZ2_DATA_DIR", tempfile.mkdtemp(prefix="zz2-audit-"))
sys.path.insert(0, str(ROOT))

SEARCH_DIRS = (ROOT / "web" / "static" / "js", ROOT / "web" / "pages")

#: Any quoted literal that looks like a server path this application owns. The
#: tail is permissive so ``'/api/tickets/recent?days=' + days`` is caught too.
PATH_LITERAL = re.compile(
    r"""['"`](/(?:api/|verify|logout|login\b|healthz|readyz)[^'"`\s]*)"""
)

#: Paths deliberately removed, with what replaced them. Referencing one is an
#: error with an explanation rather than a bare "unmatched".
REMOVED_PATHS = {
    "/api/system/restart": "restarts belong to systemd (systemctl restart zz2)",
    "/api/system/revoke": "sessions are signed tokens; sign out or disable the account",
    "/api/system/unlock": "lockouts moved to /api/system/security/lockouts",
    "/api/system/clear-expired": "there is no local session store to purge",
}

#: Property names that are JavaScript builtins or array methods, not payload
#: fields. Without this the audit reports `data_files.map` as missing.
JS_BUILTINS = frozenset({
    "map", "filter", "forEach", "join", "length", "slice", "reduce", "sort",
    "toLocaleString", "toFixed", "indexOf", "push", "concat", "some", "every",
})


def _clean(url: str) -> str:
    """Reduce a matched literal to a comparable path."""
    url = url.split("?")[0].split("#")[0]
    url = url.rstrip("\\").rstrip("+ ")
    return url.rstrip("/") or "/"


# -- Check 1: every referenced path resolves ----------------------------------
def check_routes() -> int:
    from zz2.api.routers import compat, pages
    from zz2.main import create_app

    app = create_app()
    patterns = set(app.openapi().get("paths", {}))
    for router in (compat.router, pages.router):
        for route in router.routes:
            if path := getattr(route, "path", ""):
                patterns.add(path)

    matchers = [
        re.compile("^" + re.sub(r"\\\{[^}]*\\\}", r"[^/]+", re.escape(p)) + "$")
        for p in patterns
    ]

    references: dict[str, set[str]] = {}
    #: Paths written as a prefix to concatenate an id onto
    #: (``'/api/presence/tickets/' + num``): they resolve if the route has one
    #: more parameterised segment.
    prefixes: set[str] = set()
    for directory in SEARCH_DIRS:
        for path in sorted(directory.rglob("*")):
            if path.suffix not in {".js", ".html"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in PATH_LITERAL.finditer(text):
                url = _clean(match.group(1))
                if url.startswith("/"):
                    references.setdefault(url, set()).add(path.name)
                    if match.group(1).split("?")[0].endswith("/"):
                        prefixes.add(url)

    unmatched: list[str] = []
    print(f"routes: {len(references)} distinct paths referenced")
    for url in sorted(references):
        candidates = [url] + ([url + "/_"] if url in prefixes else [])
        if any(matcher.match(c) for matcher in matchers for c in candidates):
            continue
        reason = REMOVED_PATHS.get(url)
        detail = f" — removed: {reason}" if reason else ""
        print(f"  !!  {url}  ({', '.join(sorted(references[url]))}){detail}")
        unmatched.append(url)

    if unmatched:
        print(f"  {len(unmatched)} path(s) do not resolve\n")
        return 1
    print("  all resolve\n")
    return 0


# -- Check 2: every telemetry field the Settings page reads is sent ------------
def check_telemetry_payload() -> int:
    """Compare the System page's field accesses against the real payload."""
    from zz2.paths import ensure_runtime_dirs
    from zz2.services.telemetry import telemetry_service

    ensure_runtime_dirs()
    payload = telemetry_service.snapshot()

    def flatten(obj: object, prefix: str = "") -> set[str]:
        found: set[str] = set()
        if isinstance(obj, dict):
            for key, value in obj.items():
                found.add(prefix + key)
                found |= flatten(value, f"{prefix}{key}.")
        return found

    available = flatten(payload)
    source = (ROOT / "web" / "static" / "js" / "settings.3.js").read_text(encoding="utf-8")

    # Local bindings: `const sv=d.server||{}` means `sv.x` is `server.x`.
    bindings: dict[str, str] = {}
    for pattern in (r"const\s+(\w+)\s*=\s*d\.(\w+)\s*\|\|", r"const\s+(\w+)\s*=\s*d\.(\w+)\b"):
        for match in re.finditer(pattern, source):
            bindings.setdefault(match.group(1), match.group(2))

    used: set[str] = set()
    for match in re.finditer(r"\bd\.(\w+)\.(\w+)", source):
        used.add(f"{match.group(1)}.{match.group(2)}")
    for local, group in bindings.items():
        for match in re.finditer(rf"\b{local}\.(\w+)", source):
            used.add(f"{group}.{match.group(1)}")

    relevant = {
        field for field in used
        if field.split(".")[0] in payload and field.split(".")[1] not in JS_BUILTINS
    }
    missing = sorted(field for field in relevant if field not in available)

    print(f"telemetry: page reads {len(relevant)} fields, payload provides {len(available)}")
    for field in missing:
        print(f"  !!  d.{field} is read but not sent")
    if missing:
        print(f"  {len(missing)} field(s) missing\n")
        return 1
    print("  all present\n")
    return 0


# -- Check 3: bundled kit metadata is applied on every load path ---------------
def check_kit_extras() -> int:
    """Catch a silent loss of Switch/Audio/Vehicle metadata.

    The local catalogue intentionally leaves some legacy fields empty and the
    frontend fills those gaps from ``KIT_EXTRA``. Both the localStorage path and
    the fresh API path must call the common merger; copying only diagrams makes
    the page look valid while rendering dashes for the text metadata.
    """
    source = (ROOT / "web" / "static" / "js" / "index.3.js").read_text(
        encoding="utf-8"
    )
    calls = source.count("applyKitExtras();")
    merger_fields = "['vehicle', 'switch_button', 'audio_method', 'notes']"
    problems: list[str] = []
    if calls < 2:
        problems.append(
            f"applyKitExtras is called {calls} time(s); cache and API paths both need it"
        )
    if merger_fields not in source:
        problems.append("applyKitExtras no longer merges all four metadata fields")

    print(f"kit metadata: common merger called on {calls} load paths")
    for problem in problems:
        print(f"  !!  {problem}")
    if problems:
        print(f"  {len(problems)} kit metadata problem(s)\n")
        return 1
    print("  all load paths apply metadata\n")
    return 0


def main() -> int:
    status = check_routes() | check_telemetry_payload() | check_kit_extras()
    print("frontend and backend agree." if status == 0
          else "frontend/backend mismatch — see above.")
    return status


if __name__ == "__main__":
    sys.exit(main())
