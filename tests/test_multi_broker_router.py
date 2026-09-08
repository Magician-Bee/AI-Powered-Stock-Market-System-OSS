from __future__ import annotations

import pytest

from open_stock_ai.execution.multi_broker_router import (
    MultiBrokerRouter,
    verify_multi_broker_routing_receipt,
)


def _candidate(
    broker_id: str,
    *,
    ask: float = 100.0,
    bid: float = 99.9,
    fee_bps: float = 10.0,
    latency_ms: float = 20.0,
    **overrides: object,
) -> dict[str, object]:
    return {
        "broker_id": broker_id,
        "health_status": "healthy",
        "route_permission": True,
        "quote_at": "2026-08-26T02:00:00+00:00",
        "available_quantity": 1000,
        "ask_price": ask,
        "bid_price": bid,
        "fee_bps": fee_bps,
        "latency_ms": latency_ms,
        **overrides,
    }


def test_buy_route_minimizes_notional_plus_fee_with_deterministic_tie_break() -> None:
    receipt = MultiBrokerRouter().route(
        symbol="2330.TW",
        side="buy",
        quantity=100,
        as_of="2026-08-26T02:00:01+00:00",
        candidates=[
            _candidate("broker-b", ask=100.1, fee_bps=5.0),
            _candidate("broker-a", ask=100.0, fee_bps=15.0),
        ],
    )

    assert receipt["routing_status"] == "routed"
    assert receipt["selected_broker_id"] == "broker-a"
    assert receipt["execution_authority"] == "none"
    assert verify_multi_broker_routing_receipt(receipt) is True


def test_sell_route_maximizes_net_proceeds_and_rejects_stale_or_unhealthy_candidates() -> None:
    receipt = MultiBrokerRouter().route(
        symbol="2330.TW",
        side="sell",
        quantity=100,
        as_of="2026-08-26T02:00:01+00:00",
        candidates=[
            _candidate("stale", bid=101.0, quote_at="2026-08-25T02:00:00+00:00"),
            _candidate("unhealthy", bid=105.0, health_status="degraded"),
            _candidate("healthy", bid=100.5, fee_bps=5.0),
        ],
    )

    assert receipt["selected_broker_id"] == "healthy"
    by_broker = {item["broker_id"]: item for item in receipt["candidates"]}
    assert "broker_quote_stale_or_invalid" in by_broker["stale"]["blockers"]
    assert "broker_health_not_healthy" in by_broker["unhealthy"]["blockers"]


def test_router_withholds_when_permission_or_liquidity_is_missing() -> None:
    receipt = MultiBrokerRouter().route(
        symbol="2330.TW",
        side="buy",
        quantity=2000,
        as_of="2026-08-26T02:00:01+00:00",
        candidates=[_candidate("broker-a", available_quantity=1000, route_permission=False)],
    )

    assert receipt["routing_status"] == "withheld"
    assert receipt["selected_broker_id"] is None
    assert receipt["blockers"] == ["no_eligible_broker_route"]
    assert "broker_route_permission_missing" in receipt["candidates"][0]["blockers"]
    assert "broker_available_quantity_insufficient" in receipt["candidates"][0]["blockers"]


def test_router_rejects_invalid_order_context() -> None:
    with pytest.raises(ValueError, match="quantity_invalid"):
        MultiBrokerRouter().route(
            symbol="2330.TW",
            side="buy",
            quantity=0,
            as_of="2026-08-26T02:00:00+00:00",
            candidates=[],
        )
