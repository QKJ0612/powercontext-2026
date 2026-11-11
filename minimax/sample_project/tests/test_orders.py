"""Acceptance and regression tests for the order query module."""

from __future__ import annotations

from app.orders import Order, find_orders, total_amount


def _order(order_id: str, status: str, amount_cents: int) -> Order:
    return Order(
        order_id=order_id,
        customer="c-" + order_id,
        status=status,
        amount_cents=amount_cents,
        created_at="2026-01-01",
    )


ORDERS = (
    _order("o1", "paid", 1200),
    _order("o2", "paid", 5000),
    _order("o3", "refunded", 900),
    _order("o4", "paid", 5000),
)


def test_filters_by_minimum_amount() -> None:
    """Target behaviour: keep orders whose amount is at least the minimum."""
    result = find_orders(ORDERS, min_amount_cents=5000)
    assert [order.order_id for order in result] == ["o2", "o4"]


def test_minimum_amount_is_inclusive_boundary() -> None:
    """Target behaviour: the boundary value itself is included."""
    result = find_orders(ORDERS, min_amount_cents=1200)
    assert "o1" in {order.order_id for order in result}


def test_combines_status_and_minimum_amount() -> None:
    """Target behaviour: both filters apply together."""
    result = find_orders(ORDERS, status="paid", min_amount_cents=5000)
    assert [order.order_id for order in result] == ["o2", "o4"]


def test_status_filter_still_works() -> None:
    """Regression: the existing status filter keeps its ordering and semantics."""
    assert [order.order_id for order in find_orders(ORDERS, status="refunded")] == ["o3"]


def test_default_returns_every_order() -> None:
    """Regression: no filters keeps the full input order."""
    assert [order.order_id for order in find_orders(ORDERS)] == ["o1", "o2", "o3", "o4"]


def test_total_amount_unchanged() -> None:
    """Regression: the amount helper keeps summing every order."""
    assert total_amount(ORDERS) == 12100
