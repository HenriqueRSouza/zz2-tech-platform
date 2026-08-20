"""Loads the monolith's analytics pipeline for parity testing.

The label-consolidation algorithm in :mod:`zz2.services.normalization` decides how
tickets are grouped in every analytics chart. A behaviour change there is not a
crash — it is silently different numbers, which is far worse, because nobody
notices until a decision has been made on them.

So the port is verified against the original rather than against expectations. The
relevant functions are extracted from ``server.py`` by AST, executed in an
isolated namespace with small Python 2 shims, and run side by side with the new
implementation on generated inputs.

Only pure functions are extracted. Nothing here touches the filesystem, the
network, or the monolith's module-level state.
"""

from __future__ import annotations

import ast
import functools
import pathlib
import re
from collections import OrderedDict
from typing import Any

MONOLITH = pathlib.Path(__file__).resolve().parent.parent / "legacy" / "server.py"

#: The pure label/date/status helpers, in dependency order.
WANTED_FUNCTIONS = (
    "_an_text",
    "_pick",
    "_parse_date_str",
    "_status_canon",
    "_clean_label",
    "_expand_years",
    "_norm_key",
    "_token_set_key",
    "_num_sig",
    "_model_sig",
    "_case_score",
    "_lev",
    "_merge_ok",
    "_consolidate",
    "_normalize",
    "compute_analytics",
    "_client_match_score",
    "_best_customer_match",
)

#: Module-level names those functions close over.
WANTED_ASSIGNMENTS = (
    "_re_iso", "_re_us", "_re_named", "_MONTHS", "_YY",
)


def load() -> dict[str, Any]:
    """Execute the monolith's pure analytics helpers in a fresh namespace.

    Returns:
        The namespace, with each name in :data:`WANTED_FUNCTIONS` bound.

    Raises:
        AssertionError: A wanted name was not found — the monolith changed and
            the parity test is no longer comparing what it claims to.
    """
    source = MONOLITH.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)

    selected: list[ast.stmt] = []
    found: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANTED_FUNCTIONS:
            selected.append(node)
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets & set(WANTED_ASSIGNMENTS):
                selected.append(node)
                found |= targets

    missing = set(WANTED_FUNCTIONS) - found
    assert not missing, f"monolith no longer defines: {sorted(missing)}"

    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace: dict[str, Any] = {
        # Names the extracted code expects from the monolith's imports and its
        # Python 2 compatibility shims.
        "re": re,
        "datetime": __import__("datetime"),
        "OrderedDict": OrderedDict,
        "cmp_to_key": functools.cmp_to_key,
        "_AN_TEXT": str,  # the py3 branch of the monolith's try/except NameError
        "unicode": str,
        "__builtins__": __builtins__,
    }
    exec(compile(module, "<monolith>", "exec"), namespace)  # noqa: S102
    return namespace
