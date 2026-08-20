"""Parity between the ported analytics pipeline and the monolith's.

The comparison is on *canonical assignments* — which raw label maps to which
display label — because that is what every chart is grouped by. The merge report
is compared too, since the Analytics page shows it to the user.

Inputs are generated to hit the cases the algorithm actually turns on: two-digit
years, case variation, punctuation, near-miss model codes that must *not* merge,
and word-order permutations that must.
"""

from __future__ import annotations

import pytest

from tests.legacy_reference import load
from zz2.services import normalization as new


@pytest.fixture(scope="module")
def legacy() -> dict:
    return load()


# -- Fixture data --------------------------------------------------------------
VEHICLES = [
    "BMW 320i '18", "Bmw 320I 2018", "BMW  320i  18", "BMW 320I 2018",
    "BMW 328i 2018", "bmw 328I '18",
    "Mercedes C180 '15", "MERCEDES C180 2015", "Mercedes  C180 15",
    "Toyota Corolla XEi 2020", "TOYOTA COROLLA XEI '20", "Toyota Corolla XEI 2020",
    "Honda Civic LX 2019", "Honda Civic EX 2019", "HONDA CIVIC LX '19",
    "Ford Ranger XLT 2021", "ford ranger xlt '21",
    "", "   ", "VW Golf GTI 2017", "Vw  Golf  Gti  17",
    "Jeep Compass Longitude 2022", "JEEP COMPASS LONGITUDE '22",
    "Audi A4 2016", "AUDI A4 '16", "Audi A5 2016",
]

CLIENTS = [
    "Auto Elétrica Silva", "AUTO ELETRICA SILVA", "Auto Eletrica Silva Ltda",
    "Silva Auto Elétrica", "auto elétrica silva.",
    "Som & Cia", "SOM E CIA", "Som e Cia Ltda.",
    "Instaladora 24h", "INSTALADORA 24H", "Instaladora 24 h",
    "Car Audio Center", "CAR AUDIO CENTER", "Car Audio Centre",
    "", "Multisom", "MULTISOM", "Multi Som",
    "Eletro Auto Norte", "ELETRO AUTO NORTE", "Eletro Auto Sul",
]

TECHS = [
    "ana", "Ana", "ANA", "Ana Paula", "ana paula",
    "bruno", "Bruno", "BRUNO", "Bruno Costa",
    "carlos", "Carlos", "Carla", "", "  ", "Diego", "diego", "DIEGO",
]

STATUSES = [
    "", "Resolved", "resolved", "RESOLVED - see notes",
    "Waiting for customer feedback", "waiting", "No solution", "nosol",
    "RMA issued", "rma", "Destroy", "Distributor", "W.O.E.", "woe", "Pending",
]

DATES = [
    "2026-06-10", "6/10/26", "06/10/2026", "Wed Jun 10 2026 00:00:00 GMT-0300",
    "Jun 10 2026", "2026-6-1", "12/31/25", "", "not a date", "2026-02-30",
]


def _build_rows() -> list[dict]:
    """Cross the fixture lists into ticket rows."""
    rows = []
    for index in range(240):
        rows.append({
            "date": DATES[index % len(DATES)],
            "ticket": str(7000 + index),
            "tech": TECHS[index % len(TECHS)],
            "client": CLIENTS[index % len(CLIENTS)],
            "product": f"ITZ-{index % 17:03d}",
            "vehicle": VEHICLES[index % len(VEHICLES)],
            "issue": "No audio → checked harness",
            "resolution": "Reseated LVDS",
            "status": STATUSES[index % len(STATUSES)],
        })
    return rows


# -- Primitive parity ----------------------------------------------------------
@pytest.mark.parametrize("value", VEHICLES + CLIENTS + TECHS)
def test_norm_key_matches(legacy: dict, value: str) -> None:
    assert new.norm_key(value) == legacy["_norm_key"](value)


@pytest.mark.parametrize("value", VEHICLES + CLIENTS)
def test_expand_years_matches(legacy: dict, value: str) -> None:
    cleaned = new.clean_label(value)
    assert new.expand_years(cleaned) == legacy["_expand_years"](cleaned)


@pytest.mark.parametrize("value", VEHICLES + CLIENTS + TECHS)
def test_token_set_key_matches(legacy: dict, value: str) -> None:
    assert new.token_set_key(value) == legacy["_token_set_key"](value)


@pytest.mark.parametrize("value", VEHICLES)
def test_num_sig_matches(legacy: dict, value: str) -> None:
    assert new.num_sig(value) == legacy["_num_sig"](value)


@pytest.mark.parametrize("value", VEHICLES)
def test_model_sig_matches(legacy: dict, value: str) -> None:
    assert new.model_sig(value) == legacy["_model_sig"](value)


@pytest.mark.parametrize("value", VEHICLES + CLIENTS)
def test_case_score_matches(legacy: dict, value: str) -> None:
    assert new.case_score(value) == legacy["_case_score"](value)


@pytest.mark.parametrize("value", DATES)
def test_parse_date_matches(legacy: dict, value: str) -> None:
    assert new.parse_date(value) == legacy["_parse_date_str"](value)


@pytest.mark.parametrize("value", STATUSES)
def test_status_bucket_matches(legacy: dict, value: str) -> None:
    assert new.status_bucket(value) == legacy["_status_canon"](value)


def test_levenshtein_matches(legacy: dict) -> None:
    pairs = [(a, b) for a in TECHS[:8] for b in TECHS[:8]]
    pairs += [(new.norm_key(a), new.norm_key(b)) for a in VEHICLES[:8] for b in VEHICLES[:8]]
    for left, right in pairs:
        assert new.levenshtein(left, right) == legacy["_lev"](left, right)


def test_may_merge_matches(legacy: dict) -> None:
    keys = sorted({new.norm_key(v) for v in VEHICLES + CLIENTS if v.strip()})
    for left in keys:
        for right in keys:
            assert new.may_merge(left, right) == legacy["_merge_ok"](left, right), (
                f"disagreement on {left!r} vs {right!r}"
            )


# -- Consolidation parity ------------------------------------------------------
@pytest.mark.parametrize(
    ("values", "expand", "fuzzy"),
    [
        (VEHICLES, True, True),
        (CLIENTS, False, True),
        (TECHS, False, False),
        (VEHICLES + CLIENTS, True, True),
        (VEHICLES, False, False),
    ],
)
def test_consolidate_matches(legacy: dict, values: list[str], expand: bool, fuzzy: bool) -> None:
    ported = new.consolidate(values, expand_two_digit_years=expand, fuzzy=fuzzy)
    reference = legacy["_consolidate"](values, expand_years=expand, fuzzy=fuzzy)

    assert ported.mapping == reference["map"]
    assert ported.merges == reference["merges"]
    assert ported.before == reference["before"]
    assert ported.after == reference["after"]


# -- Whole-pipeline parity -----------------------------------------------------
def test_compute_analytics_matches(legacy: dict) -> None:
    """The full payload must match, row for row and merge for merge."""
    rows = _build_rows()

    ported = new.compute_analytics(rows, generated="2026-08-20T00:00:00Z")
    reference = legacy["compute_analytics"](rows, "2026-08-20T00:00:00Z")

    assert ported["count"] == reference["count"]
    assert ported["rows"] == reference["rows"]
    assert ported["norm"] == reference["norm"]


def test_compute_analytics_drops_the_same_empty_rows(legacy: dict) -> None:
    """A row with no ticket, no parseable date and no client is not a ticket."""
    rows = [
        {"ticket": "", "date": "", "client": "", "vehicle": "BMW 320i"},
        {"ticket": "", "date": "garbage", "client": "", "tech": "ana"},
        {"ticket": "7001", "date": "", "client": ""},
        {"ticket": "", "date": "2026-01-01", "client": ""},
        {"ticket": "", "date": "", "client": "Multisom"},
    ]
    ported = new.compute_analytics(rows)
    reference = legacy["compute_analytics"](rows, None)

    assert ported["count"] == reference["count"] == 3
    assert ported["rows"] == reference["rows"]


# -- Customer matching parity --------------------------------------------------
@pytest.mark.parametrize("left", CLIENTS[:10])
@pytest.mark.parametrize("right", CLIENTS[:10])
def test_client_match_score_matches(legacy: dict, left: str, right: str) -> None:
    assert new.client_match_score(left, right) == pytest.approx(
        legacy["_client_match_score"](left, right)
    )


def test_best_customer_match_matches(legacy: dict) -> None:
    candidates = ["Auto Elétrica Silva", "Som & Cia", "Multisom", "Car Audio Center"]
    for query in CLIENTS:
        ported_name, ported_score = new.best_customer_match(query, candidates)
        reference_name, reference_score = legacy["_best_customer_match"](query, candidates)
        assert ported_name == reference_name, f"different match for {query!r}"
        assert ported_score == pytest.approx(reference_score)
