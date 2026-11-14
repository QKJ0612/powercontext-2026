"""Order query helpers for the SDD demo project (before state)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Order:
    order_id: str
    customer: str
    status: str
    amount_cents: int
    created_at: str


def find_orders(orders: Iterable[Order], *, status: str | None = None) -> list[Order]:
    """Return orders matching an optional status filter, preserving input order."""
    result = list(orders)
    if status is not None:
        result = [order for order in result if order.status == status]
    return result


def total_amount(orders: Iterable[Order]) -> int:
    """Return the sum of order amounts in cents."""
    return sum(order.amount_cents for order in orders)
