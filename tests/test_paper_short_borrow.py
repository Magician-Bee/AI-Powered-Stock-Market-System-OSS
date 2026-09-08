from __future__ import annotations

import pytest

from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai.data_platform.borrow_history_receipts import short_borrow_history_coverage


def _broker(tmp_path) -> PaperBrokerSimulator:
    store = SQLiteStore(db_path=tmp_path / "short-borrow.sqlite")
    oms = PaperOMS(
        store=store,
        account_id="short-borrow-test",
        initial_cash=10_000.0,
        commission_bps=0.0,
        sell_tax_bps=0.0,
        slippage_bps=0.0,
        lot_size=1.0,
    )
    return PaperBrokerSimulator(store=store, oms=oms)


def _market(*, price: float = 100.0, at: str = "2026-07-15T02:00:00+00:00") -> dict[str, object]:
    return {
        "symbol": "2330.TW",
        "market": "TW",
        "price": price,
        "source_timestamp": at,
        "is_realtime": True,
        "is_fallback": False,
        "odd_lot_auction_matched": True,
    }


def _locate(*, receipt_id: str = "LOC-2330-1", recall_at: str | None = None) -> dict[str, object]:
    return {
        "receipt_id": receipt_id,
        "symbol": "2330.TW",
        "source": "paper-fixture-verified-locate",
        "verified_at": "2026-07-14T01:00:00+00:00",
        "expires_at": "2026-07-30T01:00:00+00:00",
        "recall_at": recall_at,
        "available_quantity": 20,
        "annual_fee_bps": 1_600,
    }


def _ticket(order_id: str, *, side: str, quantity: int = 10, locate: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "order_id": order_id,
        "symbol": "2330.TW",
        "market": "TW",
        "side": side,
        "order_type": "market",
        "time_in_force": "rod",
        "lot_type": "odd_lot",
        "session": "regular",
        "quantity_shares": quantity,
        "actor": "user",
        "borrow_receipt": locate,
    }


def _historical_coverage() -> dict[str, object]:
    common = {
        "symbol": "2330.TW",
        "as_of": "2026-07-15T02:00:00+00:00",
        "source_id": "broker-history-fixture",
        "acquired_at": "2026-07-15T02:01:00+00:00",
        "provider_verified": True,
    }
    return short_borrow_history_coverage(
        borrow_availability=[{**common, "available_quantity": 1000}],
        borrow_rate=[{**common, "annual_rate_bps": 350}],
        borrow_recall=[{**common, "recall_at": "2026-07-30T02:00:00+00:00"}],
        borrow_fee_history=[{**common, "fee_bps": 25}],
    )


def test_short_sale_fails_closed_without_a_borrow_locate(tmp_path) -> None:
    broker = _broker(tmp_path)
    preview = broker.preview(_ticket("PB-SHORT-NO-LOCATE", side="short_sell"), _market())

    assert preview["can_submit"] is False
    assert preview["validation"]["reason"] == "borrow_locate_required"
    rejected = broker.submit(_ticket("PB-SHORT-NO-LOCATE", side="short_sell"), _market())
    assert rejected["order"]["status"] == "rejected"
    assert rejected["portfolio"]["positions"] == []


def test_short_locate_reservation_fee_and_cover_are_durable(tmp_path) -> None:
    broker = _broker(tmp_path)
    locate = _locate()
    opened = broker.submit(_ticket("PB-SHORT-OPEN", side="short_sell", locate=locate), _market())

    assert opened["filled"] is True
    assert opened["portfolio"]["cash_balance"] == 11_000.0
    assert opened["portfolio"]["positions"] == [
        {
            **opened["portfolio"]["positions"][0],
            "quantity": -10.0,
            "position_side": "short",
        }
    ]
    assert opened["portfolio"]["borrow"]["open_short_quantity"] == 10.0
    assert opened["portfolio"]["borrow"]["locates"][0]["reserved_quantity"] == 10.0
    exhausted = broker.preview(
        _ticket("PB-SHORT-OVER-LOCATE", side="short_sell", quantity=15, locate=locate), _market(),
    )
    assert exhausted["can_submit"] is False
    assert exhausted["validation"]["reason"] == "borrow_locate_quantity_exhausted"

    tick = broker.process_market_tick("2330.TW", _market(at="2026-07-17T02:00:00+00:00"))
    assert tick["borrow_fees"]["accrued_fee"] == pytest.approx(0.876712, abs=1e-6)
    # Repeating the same observed day cannot charge a second fee.
    repeated = broker.oms.accrue_borrow_fees(as_of="2026-07-17T02:00:00+00:00")
    assert repeated["accrued_fee"] == 0.0

    covered = broker.submit(
        _ticket("PB-SHORT-COVER", side="buy_to_cover"),
        _market(price=90.0, at="2026-07-17T02:00:00+00:00"),
    )
    assert covered["filled"] is True
    assert covered["portfolio"]["cash_balance"] == 10_099.12
    assert covered["portfolio"]["position_count"] == 0
    assert covered["portfolio"]["borrow"]["open_short_quantity"] == 0.0
    assert covered["portfolio"]["borrow"]["locates"][0]["reserved_quantity"] == 0.0
    assert covered["portfolio"]["realized_pnl"] == 99.12

    restarted = PaperOMS(store=broker.store, account_id="short-borrow-test")
    persisted = restarted.portfolio_summary()
    assert persisted["cash_balance"] == 10_099.12
    assert persisted["borrow"]["open_short_borrows"] == []


def test_recall_blocks_new_short_sales_but_keeps_cover_available(tmp_path) -> None:
    broker = _broker(tmp_path)
    locate = _locate(receipt_id="LOC-RECALL", recall_at="2026-07-16T01:00:00+00:00")
    opened = broker.submit(_ticket("PB-SHORT-RECALL-OPEN", side="short_sell", locate=locate), _market())
    assert opened["filled"] is True

    recall_tick = broker.process_market_tick("2330.TW", _market(at="2026-07-16T02:00:00+00:00"))
    assert recall_tick["borrow_fees"]["schema_version"] == "open_stock_ai.paper_borrow_fee_batch.v1"
    assert broker.oms.portfolio_summary()["borrow"]["recall_due_count"] == 1
    preview = broker.preview(
        _ticket("PB-SHORT-RECALL-BLOCKED", side="short_sell", quantity=1, locate=locate),
        _market(at="2026-07-16T02:00:00+00:00"),
    )
    assert preview["can_submit"] is False
    assert preview["validation"]["reason"] == "borrow_locate_recalled"

    covered = broker.submit(
        _ticket("PB-SHORT-RECALL-COVER", side="buy_to_cover"),
        _market(price=100.0, at="2026-07-16T02:00:00+00:00"),
    )
    assert covered["filled"] is True
    assert covered["portfolio"]["position_count"] == 0


def test_historical_short_replay_requires_verified_coverage_and_pit_window(tmp_path) -> None:
    broker = _broker(tmp_path)
    historical_ticket = _ticket(
        "PB-SHORT-HISTORICAL",
        side="short_sell",
        locate={
            **_locate(receipt_id="LOC-HISTORICAL"),
            "execution_mode": "historical",
            "historical_borrow_history": _historical_coverage(),
        },
    )
    allowed = broker.preview(historical_ticket, _market())
    assert allowed["can_submit"] is True

    outside = broker.preview(
        historical_ticket,
        _market(at="2026-07-16T02:00:00+00:00"),
    )
    assert outside["can_submit"] is False
    assert outside["validation"]["reason"] == "borrow_history_pit_date_outside_coverage"

    incomplete = _ticket(
        "PB-SHORT-HISTORICAL-BLOCKED",
        side="short_sell",
        locate={
            **_locate(receipt_id="LOC-HISTORICAL-BLOCKED"),
            "execution_mode": "historical",
            "historical_borrow_history": short_borrow_history_coverage(),
        },
    )
    blocked = broker.preview(incomplete, _market())
    assert blocked["can_submit"] is False
    assert blocked["validation"]["reason"] == "borrow_history_execution_replay_not_eligible"
