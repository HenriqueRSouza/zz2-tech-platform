"""Per-technician ticket counters.

Rolling totals for today, this week and this month, plus a lifetime total. The
rollover trick is worth noting: instead of a scheduled job clearing counters at
midnight, each record stores the period label it was last written in
(``last_date``, ``last_week``, ``last_month``). On the next increment a changed
label means the period rolled over, so the counter resets first.

That makes the counters correct with no scheduler at all — including across a
restart, a clock change, or a week where nobody logged a ticket.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from zz2.paths import STATS_FILE
from zz2.repositories.base import JsonDocumentRepository


def _period_labels(today: dt.date | None = None) -> tuple[str, str, str]:
    """``(day, week, month)`` labels for rollover comparison."""
    day = today or dt.date.today()
    return day.isoformat(), day.strftime("%Y-W%W"), day.strftime("%Y-%m")


def _empty_record() -> dict[str, Any]:
    return {
        "total": 0, "today": 0, "this_week": 0, "this_month": 0,
        "last_date": "", "last_week": "", "last_month": "",
    }


class StatsRepository(JsonDocumentRepository[dict]):
    """``stats.json`` — username to counter record."""

    def __init__(self) -> None:
        super().__init__(STATS_FILE, dict)

    def all(self) -> dict[str, dict[str, Any]]:
        document = self.read()
        return document if isinstance(document, dict) else {}

    def increment(self, username: str, *, today: dt.date | None = None) -> dict[str, Any]:
        """Add one to a user's counters, resetting any rolled-over period.

        Returns the updated record.
        """
        day, week, month = _period_labels(today)
        with self.mutate() as document:
            record = document.get(username)
            if not isinstance(record, dict):
                record = _empty_record()

            if record.get("last_date") != day:
                record["today"] = 0
                record["last_date"] = day
            if record.get("last_week") != week:
                record["this_week"] = 0
                record["last_week"] = week
            if record.get("last_month") != month:
                record["this_month"] = 0
                record["last_month"] = month

            for field in ("total", "today", "this_week", "this_month"):
                record[field] = int(record.get(field) or 0) + 1

            document[username] = record
            return dict(record)
