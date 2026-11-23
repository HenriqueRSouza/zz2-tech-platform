"""Text coercion and safe-name helpers.

Python 3 removes the reason ``_u`` and ``_an_text`` existed in the monolith — on
2.7 calling ``str()`` on a ticket ``issue`` field containing U+2192 raised
``UnicodeEncodeError``, so the codebase carried two parallel coercion helpers and
a README warning never to use ``str()``. :func:`as_text` keeps a single tolerant
entry point for values arriving from JSON, HTTP and upstream APIs.

The sanitisers are the security-relevant half. Both are *allowlists*: they define
what a value may contain rather than trying to enumerate what it may not. A
denylist for path components has to anticipate ``..``, ``%2e%2e``, NUL bytes,
Windows separators, reserved device names and unicode look-alikes; an allowlist
of ``[A-Za-z0-9 ._-]`` is correct without knowing any of them.
"""

from __future__ import annotations

import re
import unicodedata

_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MULTI_DOT = re.compile(r"\.{2,}")
_WHITESPACE = re.compile(r"\s+")


def as_text(value: object) -> str:
    """Coerce any value to ``str`` without raising.

    ``bytes`` are decoded as UTF-8, then Latin-1 as a last resort, so a
    mis-encoded upstream field degrades to mojibake instead of a 500.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).decode("latin-1", "replace")
    return str(value)


def clean_text(value: object) -> str:
    """Trim and collapse internal whitespace."""
    return _WHITESPACE.sub(" ", as_text(value)).strip()


def safe_key(value: object) -> str:
    """Validate a kit key for use as a single path component.

    Kit keys become directory names under ``media/images`` and ``media/manuals``,
    so a bad one is a path traversal. Returns ``""`` when the value is not
    acceptable — callers must treat empty as a rejection.

    The monolith's version only checked for ``/``, ``\\`` and ``..``, which let
    through NUL bytes, leading dots, control characters and names of unbounded
    length.

    Examples:
        >>> safe_key("ITZ-GX1-A")
        'ITZ-GX1-A'
        >>> safe_key("../../etc")
        ''
        >>> safe_key(".hidden")
        ''
    """
    text = as_text(value).strip()
    if not text or "\x00" in text:
        return ""
    # Reject anything that normalises to a different string: a unicode form that
    # decomposes into a separator would otherwise slip past the pattern.
    if unicodedata.normalize("NFKC", text) != text:
        return ""
    if not _SAFE_KEY_RE.match(text):
        return ""
    if _MULTI_DOT.search(text):
        return ""
    return text


def safe_filename(value: object, *, fallback: str = "file") -> str:
    """Reduce a client-supplied filename to one safe path component.

    Everything outside ``[A-Za-z0-9._-]`` becomes ``_``, directory components are
    discarded, and the result is length-capped so it cannot exceed the
    filesystem's per-component limit.
    """
    text = as_text(value).strip().replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    text = _UNSAFE_FILENAME_CHARS.sub("_", text)
    text = _MULTI_DOT.sub(".", text).lstrip(".")
    if not text or text in {".", ".."}:
        return fallback
    if len(text) > 120:
        stem, _, extension = text.rpartition(".")
        text = (stem[:100] + "." + extension[:19]) if stem else text[:120]
    return text


def digits_only(value: object) -> str:
    """Every decimal digit in ``value``, in order.

    Ticket numbers appear as ``TKT-7388``, ``7388``, ``" 7388 "`` and ``7388``
    (int) across the sheet, the database and the local store; comparing the digit
    strings is the only match that works on all four.
    """
    return "".join(char for char in as_text(value) if char.isdigit())


def parse_int(value: object, default: int | None = None) -> int | None:
    """Best-effort integer parse, returning ``default`` on anything unparseable."""
    try:
        return int(as_text(value).strip())
    except (TypeError, ValueError):
        return default
