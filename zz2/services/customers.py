"""Customer purchase history and item sales.

Reads two tables in the company's ``zoho`` schema — a different dataset from the
support tables, populated by the ERP. Row-level security blocks the publishable
key there, so every query runs server-side with the service key. That is the whole
reason this goes through the application instead of the browser talking to
Supabase directly, and the reason the service key must never be sent to a page.

Matching is the hard part. Tickets record the client as free text, typed by a
technician; the ERP has canonical customer names. Joining them is fuzzy, so every
response carries the match score and the name that was matched. A silent wrong
match would attribute one shop's return rate to another.
"""

from __future__ import annotations

from typing import Any

from zz2.core.cache import TTLCache
from zz2.core.errors import UpstreamError, ValidationError
from zz2.core.logging import logger
from zz2.core.text import as_text
from zz2.gateways.supabase import encode_value, supabase
from zz2.services.normalization import best_customer_match, norm_key
from zz2.settings import get_settings

HISTORY_TTL = 600.0
SUMMARIES_TTL = 600.0
ITEM_SALES_TTL = 600.0

RETURNS_RATE_ALERT_THRESHOLD = 0.15
"""A return rate at or above this raises the ticket-form alert.

Fifteen percent. Chosen with the support team, not derived — it is the point at
which a technician should look at the customer's history before assuming the kit
is at fault.
"""

RECENT_PERIODS_FOR_ALERT = 3
MIN_RETURNING_PERIODS = 2

_history_cache: TTLCache[str, dict[str, Any]] = TTLCache(HISTORY_TTL, max_entries=512)
_summaries_cache: TTLCache[str, dict[str, Any]] = TTLCache(SUMMARIES_TTL, max_entries=4)
_item_sales_cache: TTLCache[str, dict[str, Any]] = TTLCache(ITEM_SALES_TTL, max_entries=512)


class CustomerService:
    """Customer and item queries against the read-only ERP schema."""

    def __init__(self) -> None:
        self._schema = get_settings().customer_history_schema

    # -- Customer picker -------------------------------------------------------
    def summaries(self) -> dict[str, Any]:
        """Every customer with lifetime sales and return totals.

        Feeds the searchable client picker on the ticket form. Paged explicitly:
        PostgREST caps a response at 1,000 rows and returns the truncation as a
        success, so an unpaged query would silently drop every customer past the
        first thousand — and the drop would look like those customers do not
        exist.
        """
        cached = _summaries_cache.get("all")
        if cached is not None:
            return cached

        try:
            rows = supabase.select_paged(
                "customer_performance_semesters?select=customer_name,sales_quantity,"
                "returns_quantity&order=customer_name.asc&limit={limit}&offset={offset}",
                schema=self._schema,
            )
        except UpstreamError as exc:
            # Answered in the body rather than as an HTTP error: the picker
            # degrades to free-text entry, which is better than a broken form.
            logger.warning("customer summaries unavailable")
            return {"ok": False, "error": "Customer data is unavailable.",
                    "customers": [], "detail": exc.code}

        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = as_text(row.get("customer_name")).strip()
            if not name:
                continue
            key = norm_key(name)
            entry = grouped.setdefault(
                key, {"name": name, "products_purchased": 0.0, "rmas": 0.0}
            )
            entry["products_purchased"] += _as_float(row.get("sales_quantity"))
            entry["rmas"] += _as_float(row.get("returns_quantity"))

        customers = sorted(grouped.values(), key=lambda item: norm_key(item["name"]))
        for entry in customers:
            # Quantities are whole units; render them as integers when they are.
            for field in ("products_purchased", "rmas"):
                if entry[field] == int(entry[field]):
                    entry[field] = int(entry[field])

        result = {"ok": True, "customers": customers}
        _summaries_cache.set("all", result)
        return result

    # -- Per-customer history --------------------------------------------------
    def history(self, client_name: str) -> dict[str, Any]:
        """Purchase and return history for the client on an open ticket.

        Always returns ``ok`` in the body, including for failures, so the page has
        one code path instead of separate branches for HTTP errors, no-match and
        not-configured.
        """
        client_name = as_text(client_name).strip()
        if not client_name:
            raise ValidationError("A client name is required.")

        cache_key = norm_key(client_name)
        cached = _history_cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._build_history(client_name)
        _history_cache.set(cache_key, result)
        return result

    def _build_history(self, client_name: str) -> dict[str, Any]:
        empty = {
            "ok": False, "matched_customer": None, "match_score": 0,
            "alert": False, "semesters": [], "months": [],
        }
        try:
            candidates = self._candidate_names(client_name)
        except UpstreamError:
            logger.warning("customer history: candidate lookup failed")
            return {**empty, "error": "Customer history is unavailable."}

        matched, score = best_customer_match(client_name, candidates)
        if not matched:
            return {
                "ok": True, "matched_customer": None, "match_score": round(score, 2),
                "alert": False, "semesters": [], "months": [],
                "message": "No matching customer found",
            }

        encoded = encode_value(matched)
        try:
            semesters = supabase.select(
                f"customer_performance_semesters?customer_name=eq.{encoded}"
                "&select=period_year,semester,period_start,period_end,sales_quantity,"
                "returns_quantity,returns_rate,bcy_total"
                "&order=period_year.desc,semester.desc&limit=6",
                schema=self._schema,
            )
            months = supabase.select(
                f"customer_performance_months?customer_name=eq.{encoded}"
                "&select=period_year,month,sales_quantity,returns_quantity,"
                "returns_rate,bcy_total&order=period_year.desc,month.desc&limit=12",
                schema=self._schema,
            )
        except UpstreamError:
            logger.warning("customer history: period lookup failed")
            return {**empty, "matched_customer": matched,
                    "match_score": round(score, 2),
                    "error": "Customer history is unavailable."}

        return {
            "ok": True,
            "matched_customer": matched,
            "match_score": round(score, 2),
            "alert": self._returns_alert(semesters, months),
            "semesters": semesters,
            "months": months,
        }

    def _candidate_names(self, client_name: str) -> list[str]:
        """Narrow the candidate set before fuzzy matching.

        An ``ilike`` on the longest word of the client name, rather than pulling
        the whole customer table for every ticket. The longest word is the most
        selective one available without any knowledge of the data.
        """
        words = sorted(
            (word for word in norm_key(client_name).split(" ") if word),
            key=len,
            reverse=True,
        )
        if not words:
            return []
        rows = supabase.select(
            f"customer_performance_semesters?customer_name=ilike.*{encode_value(words[0])}*"
            "&select=customer_name&limit=100",
            schema=self._schema,
        )
        return sorted({
            as_text(row.get("customer_name")) for row in rows if row.get("customer_name")
        })

    @staticmethod
    def _returns_alert(
        semesters: list[dict[str, Any]], months: list[dict[str, Any]]
    ) -> bool:
        """Whether to flag this customer on the ticket form.

        Two independent triggers, because one signal alone misreads two different
        situations:

        * The latest period's return *rate* is at or above the threshold — a
          currently bad relationship.
        * Returns occurred in at least two of the last three periods — a
          persistent pattern whose individual rates may each look acceptable.
        """
        latest = months[0] if months else (semesters[0] if semesters else None)
        if not latest:
            return False

        if _as_float(latest.get("returns_rate")) >= RETURNS_RATE_ALERT_THRESHOLD:
            return True

        recent = (months or semesters)[:RECENT_PERIODS_FOR_ALERT]
        periods_with_returns = sum(
            1 for period in recent if _as_float(period.get("returns_quantity")) > 0
        )
        return periods_with_returns >= MIN_RETURNING_PERIODS

    # -- Item sales ------------------------------------------------------------
    def item_sales(self, product: str) -> dict[str, Any]:
        """Lifetime quantity sold for one item or kit.

        Matched against SKU *or* name, because technicians type either.
        """
        product = as_text(product).strip()
        if not product:
            raise ValidationError("A product is required.")

        cache_key = norm_key(product)
        cached = _item_sales_cache.get(cache_key)
        if cached is not None:
            return cached

        encoded = encode_value(product)
        try:
            rows = supabase.select_paged(
                f"sales_by_item?or=(item_sku.ilike.{encoded},item_name.ilike.{encoded})"
                "&select=item_sku,item_name,quantity_sold,period_start,period_end,"
                "semester,source&order=period_start.desc&limit={limit}&offset={offset}",
                schema=self._schema,
            )
        except UpstreamError:
            logger.warning("item sales unavailable", extra={"product": product})
            return {"ok": False, "error": "Sales data is unavailable.",
                    "product": product, "quantity_sold": None, "period_rows": 0}

        quantity = sum(_as_float(row.get("quantity_sold")) for row in rows)
        result = {
            "ok": True,
            "product": product,
            "quantity_sold": int(quantity) if quantity == int(quantity) else quantity,
            "period_rows": len(rows),
            "scope": "all_time",
        }
        _item_sales_cache.set(cache_key, result)
        return result


def _as_float(value: object) -> float:
    """Numeric coercion that treats missing and unparseable as zero.

    The ERP columns are nullable and occasionally hold empty strings. Summing them
    must not raise, and a null quantity genuinely means none were sold.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


customer_service = CustomerService()
