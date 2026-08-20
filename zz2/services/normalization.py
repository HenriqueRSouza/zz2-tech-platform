"""Label consolidation for analytics.

Ticket data is typed by humans into a Google Sheet, so the same vehicle appears
as ``"BMW 320i '18"``, ``"Bmw 320I 2018"`` and ``"BMW  320i 18"``. Counting those
as three vehicles makes every trend chart wrong. This module collapses them onto
one canonical label.

A faithful port of the pipeline in ``analytics.html``, which was itself ported to
Python inside the monolith. Both copies had to agree exactly, because the server
precomputes the view the page used to compute — the monolith's comment says
"parity-tested byte-for-byte (canonical assignments) against the JS version". The
algorithm is therefore reproduced *unchanged*, including choices that look odd in
isolation, and the reason each one exists is recorded rather than tidied away.

Pure functions, no I/O. That is what makes the parity testable.
"""

from __future__ import annotations

import datetime as dt
import functools
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from zz2.core.text import as_text
from zz2.domain.models import Ticket, TicketStatus

_WHITESPACE = re.compile(r"\s+")
_HAS_LETTER = re.compile(r"[a-z]", re.IGNORECASE)
_TWO_DIGIT_YEAR = re.compile(r"(^|[\s(/-])'?(\d{2})(?=[\s)/-]|$)")
_DIGITS = re.compile(r"\d+")
_ALPHA_ONLY = re.compile(r"^[a-z]+$")
_NON_ALPHA = re.compile(r"[^a-z]")

MERGE_MAX_EDIT_DISTANCE = 2
MERGE_MIN_SIMILARITY = 0.85
MERGE_MIN_LENGTH = 5

CUSTOMER_MATCH_THRESHOLD = 0.72
"""Below this, a ticket's free-text client name is treated as unmatched.

Tuned against real data. The score is always returned alongside the match so a
borderline result is visible in the response rather than silently accepted.
"""


def _current_two_digit_year() -> int:
    return dt.datetime.now().year % 100


# -- Label primitives ----------------------------------------------------------
def clean_label(value: object) -> str:
    """Trim and collapse internal whitespace."""
    return _WHITESPACE.sub(" ", as_text(value)).strip()


def expand_years(value: str) -> str:
    """Expand a two-digit year to four digits.

    ``"BMW 320i '18"`` becomes ``"BMW 320i 2018"``, so it consolidates with a row
    that spelled the year out. The century pivots on the current year plus one:
    ``18`` is 2018, but ``95`` is 1995.

    Applied only when the string contains a letter. A bare ``"2018"`` or a purely
    numeric part code must pass through untouched — rewriting digits in a value
    that is *only* digits would corrupt part numbers.

    Examples:
        >>> expand_years("BMW 320i '18")
        'BMW 320i 2018'
        >>> expand_years("12345")
        '12345'
    """
    if not _HAS_LETTER.search(value):
        return value
    pivot = _current_two_digit_year() + 1

    def replace(match: re.Match[str]) -> str:
        prefix, digits = match.group(1), match.group(2)
        number = int(digits)
        return prefix + str(2000 + number if number <= pivot else 1900 + number)

    return _TWO_DIGIT_YEAR.sub(replace, value)


def norm_key(value: object) -> str:
    """Comparison key: lowercased, whitespace-collapsed, dots and commas dropped.

    Examples:
        >>> norm_key("  Auto Elétrica S.A., Ltda  ")
        'auto elétrica sa ltda'
    """
    return clean_label(value).lower().replace(".", "").replace(",", "")


def token_set_key(value: object) -> str:
    """Sorted word set, so word order stops mattering.

    ``"Silva Auto Elétrica"`` and ``"Auto Elétrica Silva"`` become the same key.
    """
    return " ".join(sorted(word for word in norm_key(value).split(" ") if word))


def num_sig(value: object) -> str:
    """Sorted digit groups — a fingerprint of the numbers in a label.

    Two labels may only merge if this matches exactly. It is the guard that keeps
    fuzzy matching from confusing a ``320i`` with a ``328i``: they differ by one
    character, well inside the edit-distance threshold, but their number
    signatures differ, so the merge is refused.

    Examples:
        >>> num_sig("BMW 320i 2018")
        '2018,320'
    """
    return ",".join(sorted(_DIGITS.findall(as_text(value))))


def model_sig(value: object) -> str:
    """Sorted short alphabetic words — a fingerprint of model codes.

    Words of four letters or fewer, which is where trim and model codes live
    (``GT``, ``Sport``, ``LX``). Like :func:`num_sig`, an equality requirement for
    a merge: ``"Civic LX"`` and ``"Civic EX"`` are one edit apart but must stay
    separate.
    """
    return ",".join(sorted(
        word for word in norm_key(value).split(" ")
        if word and _ALPHA_ONLY.match(word) and len(word) <= 4
    ))


def case_score(value: object) -> int:
    """Heuristic for which spelling looks most deliberately typed.

    Used to pick the display form among variants that consolidated together, once
    frequency has tied. Title Case scores highest, a short all-caps acronym is
    neutral-positive, and longer SHOUTING scores negative — so ``"BMW 320i"`` wins
    over ``"BMW 320I"``.
    """
    score = 0
    for word in re.split(r"\s+", as_text(value)):
        if not _HAS_LETTER.search(word):
            continue
        if re.match(r"[A-Z][a-z]", word):
            score += 2
        elif re.match(r"^[A-Z]+$", word) and len(word) <= 4:
            score += 1
        elif re.match(r"^[A-Z0-9]+$", word):
            score -= 1
    return score


def levenshtein(left: str, right: str) -> int:
    """Edit distance, computed with two rows rather than a full matrix.

    Called O(n²) times per consolidation pass over thousands of labels, so the
    ``O(min(m,n))`` space matters more than the elegance of the full table.
    """
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i] + [0] * len(right)
        for j, right_char in enumerate(right, start=1):
            current[j] = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (0 if left_char == right_char else 1),
            )
        previous = current
    return previous[len(right)]


def may_merge(left: str, right: str) -> bool:
    """Whether two normalised keys describe the same thing.

    Four gates, in cost order — the cheap exact checks reject most pairs before
    the expensive edit distance runs:

    1. Identical keys merge.
    2. :func:`num_sig` must match. Different numbers, different thing.
    3. :func:`model_sig` must match. Different model code, different thing.
    4. Same word set merges; otherwise the labels must be at least
       :data:`MERGE_MIN_LENGTH` long, within :data:`MERGE_MAX_EDIT_DISTANCE`
       edits, and :data:`MERGE_MIN_SIMILARITY` similar.

    The length floor exists because two edits on a four-character string can
    change its meaning entirely.
    """
    if left == right:
        return True
    if num_sig(left) != num_sig(right):
        return False
    if model_sig(left) != model_sig(right):
        return False
    if token_set_key(left) == token_set_key(right):
        return True

    longest = max(len(left), len(right))
    if longest < MERGE_MIN_LENGTH:
        return False
    distance = levenshtein(left, right)
    return (
        distance <= MERGE_MAX_EDIT_DISTANCE
        and (1 - distance / longest) >= MERGE_MIN_SIMILARITY
    )


@dataclass(slots=True)
class Consolidation:
    """Result of consolidating one column of labels.

    Attributes:
        mapping: Original value to canonical display label.
        merges: ``[original, canonical]`` pairs where the label changed, shown in
            the Analytics page's normalisation report so a wrong merge is visible
            rather than invisible.
        before: Distinct keys before merging.
        after: Distinct keys after.
    """

    mapping: dict[str, str] = field(default_factory=dict)
    merges: list[list[str]] = field(default_factory=list)
    before: int = 0
    after: int = 0

    def summary(self) -> dict[str, Any]:
        return {"before": self.before, "after": self.after, "merges": self.merges}


def consolidate(
    values: list[str], *, expand_two_digit_years: bool = False, fuzzy: bool = False
) -> Consolidation:
    """Collapse a column of free-text labels onto canonical forms.

    Args:
        values: Raw labels, one per ticket, duplicates included — frequency is
            what decides which spelling wins.
        expand_two_digit_years: Apply :func:`expand_years` first. On for vehicles,
            off for client and technician names.
        fuzzy: Allow near-match merging via :func:`may_merge`. On for vehicles and
            clients, off for technicians — that list is small and closed, and a
            fuzzy merge there would silently attribute one person's tickets to
            another.

    The canonical label for a group is the most frequent spelling, breaking ties
    by :func:`case_score`. Keys are processed most-frequent-first so the busiest
    spelling becomes the group's canonical form rather than whichever happened to
    appear first.
    """
    entries: list[tuple[str, str, str]] = []
    for raw in values:
        expanded = clean_label(raw)
        if not expanded:
            continue
        if expand_two_digit_years:
            expanded = expand_years(expanded)
        entries.append((raw, expanded, norm_key(expanded)))

    frequency: OrderedDict[str, int] = OrderedDict()
    spellings: OrderedDict[str, OrderedDict[str, int]] = OrderedDict()
    for _raw, expanded, key in entries:
        frequency[key] = frequency.get(key, 0) + 1
        variants = spellings.setdefault(key, OrderedDict())
        variants[expanded] = variants.get(expanded, 0) + 1

    display_for_key: dict[str, str] = {}
    for key, variants in spellings.items():
        display_for_key[key] = sorted(
            variants,
            key=functools.cmp_to_key(
                lambda a, b, v=variants: (v[b] - v[a]) or (case_score(b) - case_score(a))
            ),
        )[0]

    ordered_keys = sorted(frequency, key=functools.cmp_to_key(
        lambda a, b: frequency[b] - frequency[a]
    ))

    def bucket_of(key: str) -> str:
        """Blocking key: number signature plus first letter.

        Without it, fuzzy consolidation compares every key against every other
        one. Since :func:`may_merge` already requires an identical number
        signature, restricting comparisons to the same bucket cannot change the
        result — it only avoids the comparisons that were guaranteed to fail.
        """
        letters = _NON_ALPHA.sub("", key)
        return num_sig(key) + "|" + (letters[0] if letters else "")

    canonical_of: dict[str, str] = {}
    canonical_keys: list[str] = []
    buckets: dict[str, list[str]] = {}
    for key in ordered_keys:
        target: str | None = None
        if fuzzy:
            for candidate in buckets.get(bucket_of(key), ()):
                if may_merge(key, candidate):
                    target = candidate
                    break
        if target is not None:
            canonical_of[key] = target
        else:
            canonical_of[key] = key
            canonical_keys.append(key)
            buckets.setdefault(bucket_of(key), []).append(key)

    result = Consolidation(before=len(frequency), after=len(canonical_keys))
    seen: set[str] = set()
    for raw, _expanded, key in entries:
        label = display_for_key[canonical_of[key]]
        result.mapping[raw] = label
        if clean_label(raw) != label:
            signature = f"{raw}>>{label}"
            if signature not in seen:
                seen.add(signature)
                result.merges.append([raw, label])
    return result


# -- Date parsing --------------------------------------------------------------
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_US_DATE = re.compile(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})")
_NAMED_DATE = re.compile(r"\b([A-Za-z]{3})[a-z]*\s+(\d{1,2})\s+(\d{4})\b")
_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date(value: object) -> str:
    """Parse a ticket date to ``YYYY-MM-DD``, or ``""`` if unparseable.

    Three formats, because the sheet contains all three: ISO, US ``M/D/Y``, and
    the ``"Wed Jun 10 2026 ..."`` form JavaScript's ``Date.toString`` produces.

    ``M/D/Y`` — month first — matches how the sheet's locale writes dates. Reading
    those as day-first would silently shift every ticket dated before the 13th of
    a month into the wrong month.

    Examples:
        >>> parse_date("2026-06-10")
        '2026-06-10'
        >>> parse_date("6/10/26")
        '2026-06-10'
        >>> parse_date("Wed Jun 10 2026 00:00:00 GMT-0300")
        '2026-06-10'
        >>> parse_date("not a date")
        ''
    """
    text = as_text(value).strip()
    if not text:
        return ""

    if match := _ISO_DATE.match(text):
        year, month, day = int(match[1]), int(match[2]), int(match[3])
    elif match := _US_DATE.match(text):
        year = int(match[3])
        year = year + 2000 if year < 100 else year
        month, day = int(match[1]), int(match[2])
    elif match := _NAMED_DATE.search(text):
        month_number = _MONTH_NAMES.get(match[1].lower())
        if not month_number:
            return ""
        year, month, day = int(match[3]), month_number, int(match[2])
    else:
        return ""

    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return ""


# -- The full pipeline ---------------------------------------------------------
def compute_analytics(rows: list[dict[str, Any]], generated: str | None = None) -> dict[str, Any]:
    """Build the precomputed analytics payload the Analytics page consumes.

    Rows with no ticket number, no date *and* no client are dropped: those are
    spreadsheet artefacts — spacer rows and stray notes — not tickets.
    """
    tickets = [Ticket.from_store(row) for row in rows]
    tickets = [t for t in tickets if t.number or parse_date(t.date) or t.client]

    vehicles = consolidate([t.vehicle for t in tickets],
                           expand_two_digit_years=True, fuzzy=True)
    clients = consolidate([t.client for t in tickets],
                          expand_two_digit_years=False, fuzzy=True)
    techs = consolidate([t.tech for t in tickets],
                        expand_two_digit_years=False, fuzzy=False)

    output_rows = [
        {
            "date": parse_date(t.date),
            "ticket": t.number,
            "tech": techs.mapping.get(t.tech, t.tech),
            "client": clients.mapping.get(t.client, t.client),
            "product": t.product,
            "vehicle": vehicles.mapping.get(t.vehicle, t.vehicle),
            "canon": t.status_bucket.value,
            "status": t.status,
            "issue": t.issue,
            "resolution": t.resolution,
        }
        for t in tickets
    ]

    return {
        "precomputed": True,
        "generated": generated,
        "count": len(output_rows),
        "rows": output_rows,
        "norm": {
            "vehicle": vehicles.summary(),
            "client": clients.summary(),
            "tech": techs.summary(),
        },
    }


def canonical_index(rows: list[dict[str, Any]]) -> tuple[dict[str, str], ...]:
    """Build ``normKey -> canonical label`` maps from an already-built view.

    Lets a newly created ticket reuse the canonical label an existing group
    already settled on, instead of introducing its own spelling and waiting for
    the nightly rebuild to merge it. Only genuine typo variants — which need the
    fuzzy pass — wait.
    """
    vehicles: dict[str, str] = {}
    clients: dict[str, str] = {}
    techs: dict[str, str] = {}
    for row in rows:
        for value, target in ((row.get("vehicle"), vehicles),
                              (row.get("client"), clients),
                              (row.get("tech"), techs)):
            if value:
                key = norm_key(value)
                if key and key not in target:
                    target[key] = value
    return vehicles, clients, techs


def delta_row(
    row: dict[str, Any],
    vehicles: dict[str, str],
    clients: dict[str, str],
    techs: dict[str, str],
) -> dict[str, Any] | None:
    """Project one new ticket into view-row shape without a full consolidation.

    Returns ``None`` for a row that :func:`compute_analytics` would have dropped,
    so the cheap incremental path and the full rebuild agree on which rows exist.
    """
    ticket = Ticket.from_store(row)
    date = parse_date(ticket.date)
    if not (ticket.number or date or ticket.client):
        return None

    vehicle_display = ""
    if ticket.vehicle:
        expanded = clean_label(expand_years(clean_label(ticket.vehicle)))
        vehicle_display = vehicles.get(norm_key(expanded), expanded)

    return {
        "date": date,
        "ticket": ticket.number,
        "tech": techs.get(norm_key(ticket.tech), clean_label(ticket.tech)),
        "client": clients.get(norm_key(ticket.client), clean_label(ticket.client)),
        "product": ticket.product,
        "vehicle": vehicle_display,
        "canon": ticket.status_bucket.value,
        "status": ticket.status,
        "issue": ticket.issue,
        "resolution": ticket.resolution,
    }


def client_match_score(left: str, right: str) -> float:
    """Similarity between a ticket's client name and a customer record name.

    Skips :func:`num_sig` and :func:`model_sig` on purpose — those fingerprint
    product model codes and are meaningless for company names, where requiring an
    identical digit set would reject ``"Auto Elétrica 24h"`` against
    ``"Auto Elétrica"``.
    """
    if not left or not right:
        return 0.0
    if norm_key(left) == norm_key(right):
        return 1.0

    left_tokens, right_tokens = token_set_key(left), token_set_key(right)
    if left_tokens == right_tokens:
        return 0.97

    longest = max(len(left_tokens), len(right_tokens))
    if longest == 0:
        return 0.0
    return max(0.0, 1 - levenshtein(left_tokens, right_tokens) / longest)


def best_customer_match(client_name: str, candidates: list[str]) -> tuple[str | None, float]:
    """Best candidate above :data:`CUSTOMER_MATCH_THRESHOLD`, with its score.

    Returns ``(None, score)`` when nothing clears the bar. The score comes back
    either way so the caller can surface a near-miss rather than reporting "no
    history" for a customer who is in the data under a slightly different name.
    """
    best: str | None = None
    best_score = 0.0
    for candidate in candidates:
        score = client_match_score(client_name, candidate)
        if score > best_score:
            best, best_score = candidate, score
    if best_score < CUSTOMER_MATCH_THRESHOLD:
        return None, best_score
    return best, best_score


def status_bucket(raw: object) -> str:
    """Canonical status string. Thin wrapper for callers holding raw rows."""
    return TicketStatus.canonicalise(raw).value
