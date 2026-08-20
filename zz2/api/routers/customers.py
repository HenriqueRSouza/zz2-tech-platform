"""Customer history and item sales endpoints.

All read-only, all against the ERP ``zoho`` schema through the service key.
Gated on ``tickets``: this is customer commercial data, and the technicians who
need it are the ones working tickets.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from zz2.api.deps import RequireTickets
from zz2.domain.permissions import Principal
from zz2.services.customers import customer_service

router = APIRouter(tags=["customers"])


@router.get("/api/customers")
def list_customers(actor: Annotated[Principal, RequireTickets]) -> dict[str, Any]:
    """Customer names with lifetime purchase and return totals."""
    return customer_service.summaries()


@router.get("/api/customer-history")
def customer_history(
    actor: Annotated[Principal, RequireTickets],
    client: Annotated[str, Query(min_length=1, max_length=200)],
) -> dict[str, Any]:
    """Purchase and return history for the client on an open ticket.

    Always answers 200; ``ok`` and ``error`` live in the body. The ticket form
    treats "no match", "not configured" and "upstream down" identically — it hides
    the panel — so a single response shape keeps one branch instead of four.
    """
    return customer_service.history(client)


@router.get("/api/item-sales")
def item_sales(
    actor: Annotated[Principal, RequireTickets],
    product: Annotated[str, Query(min_length=1, max_length=200)],
) -> dict[str, Any]:
    """Lifetime quantity sold for one item or kit."""
    return customer_service.item_sales(product)
