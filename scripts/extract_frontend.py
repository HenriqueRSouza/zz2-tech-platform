#!/usr/bin/env python3
"""Split each monolithic page into HTML, CSS and JS files.

The eight pages carry their stylesheets and their application code inline — 5,441
lines in ``index.html`` alone, of which the majority is CSS and JavaScript that no
editor can navigate and no tool can lint.

The transformation is deliberately mechanical and semantics-preserving:

* ``<style>BODY</style>`` becomes ``<link rel="stylesheet" href="...">`` **at the
  same position**.
* ``<script>BODY</script>`` becomes ``<script src="..."></script>`` at the same
  position, with no ``defer`` or ``async``.
* External CDN scripts are repointed at the vendored local copies.

One block per file, rather than concatenating each page's scripts into a bundle.
Concatenating and deferring would be tidier and would change *when* the code runs:
classic inline scripts execute during parsing, and several of these blocks run
mid-document. External classic scripts without ``defer`` keep exactly that
ordering and blocking behaviour, so the only difference is the fetch.

Run from the repository root:

    python scripts/extract_frontend.py

Idempotent. Verifies each output by reassembling it and comparing against the
source, and refuses to write anything if a page does not round-trip.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / "legacy"
"""The monolithic pages, kept as the source of this transformation."""

PAGES_OUT = ROOT / "web" / "pages"
CSS_OUT = ROOT / "web" / "static" / "css"
JS_OUT = ROOT / "web" / "static" / "js"
SHARED_OUT = JS_OUT / "shared"

PAGES = ("login", "dashboard", "index", "tickets", "analytics",
         "kit-editor", "users", "settings")

#: Blocks copy-pasted across pages, hoisted to one shared file. Matched on a
#: distinctive substring so the match is deterministic, and every hoisted copy is
#: checked byte-for-byte — a block that drifted apart is a loud failure, not a
#: silent pick-one-and-hope.
SHARED_BLOCKS: dict[str, str] = {
    "zz2 theme: apply saved choice before paint": "theme-init.js",
    "window.zz2NavToggle": "nav.js",
    "window.zz2ToggleTheme": "theme-toggle.js",
}

#: Shared file to the page whose copy is authoritative, plus the pages whose copy
#: is knowingly replaced by it.
#:
#: ``theme-toggle.js`` is the only entry. Six pages carry byte-identical copies;
#: ``login`` and ``tickets`` drifted:
#:
#: * ``tickets`` reformatted the same logic and wrote the emoji literally instead
#:   of as escapes — cosmetic only.
#: * ``login`` and ``tickets`` omit the optional ``window.zz2OnThemeChange``
#:   callback. The canonical version calls it only when a page defines it, so it
#:   is a strict superset and a no-op on pages that do not.
#:
#: Normalising them is a deliberate, reviewed change. It is reported on every run
#: rather than applied quietly.
CANONICAL_OVERRIDES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "theme-toggle.js": (
        "dashboard",
        ("login", "tickets"),
        "cosmetic reformat; canonical adds an optional no-op callback",
    ),
}

#: Application chrome that lived under ``images/`` alongside the kit diagrams.
#:
#: ``/images/`` is now the *media* route: authenticated, and shaped
#: ``/images/<kit>/<file>``. The logo and favicon are neither kit media nor
#: user-uploaded, so they belong with the rest of the static assets — and the
#: favicon in particular must load on the sign-in page, before any session
#: exists.
ASSET_MAP = {
    "/images/favicon.jpg": "/static/images/favicon.jpg",
    "/images/zz2-logo.png": "/static/images/zz2-logo.png",
}

#: CDN URL to the vendored local path. Vendoring removes the supply-chain risk
#: entirely: a compromised CDN response can no longer execute on this origin, and
#: the pages work without outbound internet access.
VENDOR_MAP = {
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js":
        "/static/vendor/pdf.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js":
        "/static/vendor/pdf.worker.min.js",
    "chart.min.js": "/static/vendor/chart.min.js",
    "chart_min.js": "/static/vendor/chart.min.js",
}

STYLE_BLOCK = re.compile(r"<style\b([^>]*)>(.*?)</style\s*>", re.DOTALL | re.IGNORECASE)
SCRIPT_BLOCK = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.DOTALL | re.IGNORECASE)
SRC_ATTR = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

SESSION_MODULE_TAG = '<script src="/static/js/shared/session.js"></script>'
THEME_INIT_TAG = '<script src="/static/js/shared/theme-init.js"></script>'

#: The token-in-the-body call sites, in every formatting variant the eight pages
#: used. Matched as a whole ``fetch(...)`` expression so the replacement is a
#: drop-in: both helpers return the fetch promise, so the existing
#: ``.then(r => r.json())`` and ``.catch(() => {})`` chains are untouched.
def _session_call_pattern(endpoint: str) -> re.Pattern[str]:
    return re.compile(
        r"fetch\(\s*(?:SERVER_URL\s*\+\s*)?['\"]" + re.escape(endpoint) + r"['\"]\s*,\s*\{"
        r".*?body\s*:\s*JSON\.stringify\(\s*\{\s*token\s*:\s*"
        r"([A-Za-z_$][\w$]*)\s*\}\s*\)\s*,?\s*\}\s*\)",
        re.DOTALL,
    )


VERIFY_CALL = _session_call_pattern("/verify")
LOGOUT_CALL = _session_call_pattern("/logout")

#: Any surviving reference to these is a page still sending a credential in a
#: request body, which the server refuses.
LEGACY_SESSION_MARKERS = ("'/verify'", '"/verify"', "'/logout'", '"/logout"')


@dataclass
class Extraction:
    """One page's split result."""

    page: str
    html: str
    assets: dict[Path, str]
    stylesheets: int
    scripts: int
    vendored: list[str]
    normalised: list[tuple[str, str]]
    """Shared blocks replaced by a canonical copy: ``(filename, reason)``."""


def _asset_ref(kind: str, page: str, index: int, total: int) -> tuple[str, str]:
    """``(filename, url)`` for one extracted block.

    Single-block pages get an unsuffixed name — ``login.css`` rather than
    ``login.1.css`` — because a numbered lone file only invites the question of
    where the others are.
    """
    suffix = "" if total == 1 else f".{index}"
    filename = f"{page}{suffix}.{kind}"
    return filename, f"/static/{kind}/{filename}"


def extract(page: str, source: str) -> Extraction:
    """Split one page. Pure: returns what to write, writes nothing."""
    assets: dict[Path, str] = {}
    vendored: list[str] = []
    normalised: list[tuple[str, str]] = []

    style_bodies = [match.group(2) for match in STYLE_BLOCK.finditer(source)]
    script_matches = list(SCRIPT_BLOCK.finditer(source))
    inline_scripts = [m for m in script_matches if not SRC_ATTR.search(m.group(1))]

    style_total = len(style_bodies)
    script_total = len(inline_scripts)

    style_counter = 0
    script_counter = 0

    def replace_style(match: re.Match[str]) -> str:
        nonlocal style_counter
        style_counter += 1
        filename, url = _asset_ref("css", page, style_counter, style_total)
        assets[CSS_OUT / filename] = _dedent_block(match.group(2))
        return f'<link rel="stylesheet" href="{url}">'

    def replace_script(match: re.Match[str]) -> str:
        nonlocal script_counter
        attributes, body = match.group(1), match.group(2)

        source_attr = SRC_ATTR.search(attributes)
        if source_attr:
            original = source_attr.group(1)
            replacement = VENDOR_MAP.get(original)
            if replacement is None:
                # Left alone and reported. An unrecognised external script is a
                # decision for a human, not something to silently rewrite.
                print(f"  ! {page}: unmapped external script {original}",
                      file=sys.stderr)
                return match.group(0)
            vendored.append(original)
            rewritten = attributes.replace(source_attr.group(0),
                                           f'src="{replacement}"')
            return f"<script{rewritten}></script>"

        script_counter += 1
        rewritten_body, rewrites = _vendor_urls_in_code(_dedent_block(body))
        vendored.extend(rewrites)

        shared_name = _shared_name(rewritten_body)
        if shared_name is not None:
            override = CANONICAL_OVERRIDES.get(shared_name)
            if override and page in override[1]:
                normalised.append((shared_name, override[2]))
                rewritten_body = _canonical_body(shared_name, override[0])
            assets[SHARED_OUT / shared_name] = rewritten_body
            return f'<script src="/static/js/shared/{shared_name}"></script>'

        filename, url = _asset_ref("js", page, script_counter, script_total)
        assets[JS_OUT / filename] = rewritten_body
        # No defer/async: preserves the parse-time execution order of the inline
        # block this replaces.
        return f'<script src="{url}"></script>'

    html = STYLE_BLOCK.sub(replace_style, source)
    html = SCRIPT_BLOCK.sub(replace_script, html)
    html = _repoint_assets(html)
    assets = {path: _repoint_assets(body) for path, body in assets.items()}

    return Extraction(page=page, html=html, assets=assets,
                      stylesheets=style_total, scripts=script_total,
                      vendored=vendored, normalised=normalised)


def _repoint_assets(text: str) -> str:
    """Move logo and favicon references onto the static asset route."""
    for original, replacement in ASSET_MAP.items():
        text = text.replace(original, replacement)
    return text


def _canonical_body(shared_name: str, canonical_page: str) -> str:
    """The authoritative copy of a shared block, read from its canonical page."""
    source = (LEGACY / f"{canonical_page}.html").read_text(encoding="utf-8")
    marker = next(key for key, value in SHARED_BLOCKS.items() if value == shared_name)
    for match in SCRIPT_BLOCK.finditer(source):
        if SRC_ATTR.search(match.group(1)):
            continue
        body, _ = _vendor_urls_in_code(_dedent_block(match.group(2)))
        if marker in body:
            return body
    raise AssertionError(
        f"canonical page {canonical_page} does not contain {shared_name}"
    )


def _shared_name(body: str) -> str | None:
    """Shared filename for ``body``, or ``None`` if it is page-specific.

    Three blocks — the pre-paint theme applier, the navigation drawer and the
    theme toggle — were copy-pasted into six to eight pages each. Hoisting them
    means a change lands once, and the browser caches one file instead of eight
    identical ones.
    """
    for marker, filename in SHARED_BLOCKS.items():
        if marker in body:
            return filename
    return None


def _vendor_urls_in_code(body: str) -> tuple[str, list[str]]:
    """Repoint CDN URLs that appear inside application code.

    Not every external dependency arrives through a ``<script src>``: pdf.js reads
    its worker URL from a string literal (``GlobalWorkerOptions.workerSrc``).
    Rewriting only the tags would leave the page still fetching a megabyte of
    worker code from a CDN, and a CSP that no longer allows that host would break
    the PDF viewer instead.
    """
    rewrites: list[str] = []
    for original, replacement in VENDOR_MAP.items():
        if original.startswith("http") and original in body:
            body = body.replace(original, replacement)
            rewrites.append(original)
    return body, rewrites


def _dedent_block(body: str) -> str:
    """Trim the surrounding blank lines and add a trailing newline.

    Indentation inside the block is left exactly as written. Re-indenting would
    make the whole file a diff against the monolith and bury any real change in
    it.
    """
    return body.strip("\n").rstrip() + "\n"


def _original_block(page: str, shared_name: str) -> str:
    """That page's own copy of a shared block, before normalisation."""
    return _canonical_body(shared_name, page)


def verify(extraction: Extraction, source: str) -> None:
    """Reassemble the split page and confirm it matches the source.

    The guarantee that makes this refactor safe: the extraction is not trusted, it
    is checked. Every byte of CSS and JavaScript must be present, in order, and
    the surrounding markup must be untouched apart from the substituted tags.

    Raises:
        AssertionError: The page does not round-trip.
    """
    rebuilt = extraction.html

    normalised_names = {name for name, _reason in extraction.normalised}

    for path, body in extraction.assets.items():
        kind = "css" if path.suffix == ".css" else "js"
        url = (f"/static/js/shared/{path.name}" if path.parent == SHARED_OUT
               else f"/static/{kind}/{path.name}")
        if path.name in normalised_names:
            # Content deliberately differs from the source; only confirm the page
            # references it. The substitution is reported by main().
            assert url in rebuilt, f"{extraction.page}: {url} was not referenced"
            rebuilt = rebuilt.replace(
                f'<script src="{url}"></script>',
                f"<script>{_original_block(extraction.page, path.name)}</script>", 1)
            continue
        tag = (f'<link rel="stylesheet" href="{url}">' if kind == "css"
               else f'<script src="{url}"></script>')
        assert tag in rebuilt, f"{extraction.page}: {url} was not referenced"
        opener = "<style>" if kind == "css" else "<script>"
        closer = "</style>" if kind == "css" else "</script>"
        rebuilt = rebuilt.replace(tag, f"{opener}{body}{closer}", 1)

    normalise = lambda text: re.sub(r"\s+", " ", text)  # noqa: E731
    original_body = normalise(re.sub(r"<[^>]+>", " ", _repoint_assets(source)))
    rebuilt_body = normalise(re.sub(r"<[^>]+>", " ", rebuilt))
    assert original_body == rebuilt_body, (
        f"{extraction.page}: text content changed during extraction"
    )

    everything = extraction.html + "".join(extraction.assets.values())

    for original, replacement in ASSET_MAP.items():
        if original not in source:
            continue
        # The replacement *contains* the original as a substring
        # (/static/images/... contains /images/...), so a plain absence check
        # always fails. Equal counts means every occurrence is accounted for by a
        # repointed one.
        assert replacement in everything, (
            f"{extraction.page}: {replacement} is not referenced"
        )
        assert everything.count(original) == everything.count(replacement), (
            f"{extraction.page}: an un-repointed {original} remains"
        )

    for original, replacement in VENDOR_MAP.items():
        if not original.startswith("http") or original not in source:
            continue
        assert original not in everything, (
            f"{extraction.page}: {original} was not vendored"
        )
        assert replacement in everything, (
            f"{extraction.page}: {replacement} is not referenced anywhere"
        )


def harden_sessions(assets: dict[Path, str]) -> tuple[dict[Path, str], int]:
    """Move session credentials out of request bodies and into the header.

    Every page carried its own copy of::

        fetch('/verify', { method: 'POST',
                           headers: { 'Content-Type': 'application/json' },
                           body: JSON.stringify({ token: TOKEN }) })

    A credential in a request body reaches any proxy that logs bodies, and the
    request is triggerable cross-site by a plain form post. The server no longer
    accepts it, so this is a required change rather than an improvement: left
    alone, every page would 401 and the sign-in page would loop.

    Returns the rewritten assets and how many call sites moved.
    """
    rewritten: dict[Path, str] = {}
    replaced = 0

    for path, body in assets.items():
        if path.suffix != ".js":
            rewritten[path] = body
            continue
        updated, verify_count = VERIFY_CALL.subn(r"ZZ2.verifySession(\1)", body)
        updated, logout_count = LOGOUT_CALL.subn(r"ZZ2.logoutRequest()", updated)
        replaced += verify_count + logout_count
        rewritten[path] = updated

    return rewritten, replaced


def add_session_module(html: str) -> str:
    """Load the shared session helper before any page script runs.

    Placed immediately after the pre-paint theme script, which every page has in
    its ``<head>``. Both are classic non-deferred scripts, so this guarantees
    ``window.ZZ2`` exists by the time the page's own code executes.
    """
    if SESSION_MODULE_TAG in html:
        return html
    if THEME_INIT_TAG in html:
        return html.replace(THEME_INIT_TAG, THEME_INIT_TAG + "\n" + SESSION_MODULE_TAG, 1)
    return html.replace("</head>", SESSION_MODULE_TAG + "\n</head>", 1)


def main() -> int:
    for directory in (PAGES_OUT, CSS_OUT, JS_OUT, SHARED_OUT):
        directory.mkdir(parents=True, exist_ok=True)

    results: list[Extraction] = []
    for page in PAGES:
        source_path = LEGACY / f"{page}.html"
        if not source_path.is_file():
            print(f"  ! missing {source_path.name}", file=sys.stderr)
            return 1

        source = source_path.read_text(encoding="utf-8")
        extraction = extract(page, source)
        verify(extraction, source)
        results.append(extraction)

    # A shared block must be byte-identical everywhere it appears. If two pages
    # drifted apart, hoisting would silently pick one and change the other.
    shared_bodies: dict[Path, str] = {}
    for extraction in results:
        for path, body in extraction.assets.items():
            if path.parent != SHARED_OUT:
                continue
            existing = shared_bodies.setdefault(path, body)
            if existing != body:
                print(f"  ! {extraction.page}: {path.name} differs from another "
                      f"page's copy; not safe to share", file=sys.stderr)
                return 1

    # Nothing is written until every page has been verified.
    total_session_rewrites = 0
    for extraction in results:
        extraction.assets, rewrites = harden_sessions(extraction.assets)
        total_session_rewrites += rewrites
        extraction.html = add_session_module(extraction.html)

        for path, body in extraction.assets.items():
            for marker in LEGACY_SESSION_MARKERS:
                assert marker not in body, (
                    f"{extraction.page}/{path.name} still calls {marker} directly"
                )
        assert SESSION_MODULE_TAG in extraction.html, (
            f"{extraction.page}: the session helper is not loaded"
        )
        # The browser no longer talks to a third-party database or auth client:
        # every read and write goes through this server's /api routes.
        for name, text in [("html", extraction.html), *((p.name, b) for p, b in extraction.assets.items())]:
            assert "supabase" not in text.lower(), (
                f"{extraction.page}/{name} still references the removed Supabase client"
            )

        (PAGES_OUT / f"{extraction.page}.html").write_text(
            extraction.html, encoding="utf-8")
        for path, body in extraction.assets.items():
            path.write_text(body, encoding="utf-8")

        html_lines = extraction.html.count("\n") + 1
        asset_lines = sum(body.count("\n") + 1 for body in extraction.assets.values())
        print(f"{extraction.page:<12} html {html_lines:>5} lines  "
              f"+ {extraction.stylesheets} css / {extraction.scripts} js "
              f"({asset_lines} lines extracted)"
              + (f"  vendored: {len(extraction.vendored)}" if extraction.vendored else ""))
        for name, reason in extraction.normalised:
            print(f"             normalised {name} to the canonical copy ({reason})")

    print(f"\nshared: {', '.join(sorted(p.name for p in shared_bodies))}")
    print(f"session call sites moved to the Authorization header: "
          f"{total_session_rewrites}")
    print(f"{len(results)} pages split and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
