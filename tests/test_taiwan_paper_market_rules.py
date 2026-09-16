from __future__ import annotations

from decimal import Decimal

from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.taiwan_market_rules import TaiwanPaperMarketRules, is_valid_tick, tick_size
from open_stock_ai.storage.sqlite_store import SQLiteStore


def ticket(**overrides):
    value = {
        "order_id": "RULE-ORDER-1",
        "symbol": "2330.TW",
        "market": "TW",
        "side": "buy",
        "order_type": "limit",
        "time_in_force": "rod",
        "lot_type": "board_lot",
        "session": "regular",
        "requested_quantity": 1000,
        "quantity_shares": 1000,
        "limit_price": 100,
    }
    value.update(overrides)
    return value


def market(**overrides):
    value = {
        "price": 100,
        "limit_up": 110,
        "limit_down": 90,
        "source_timestamp": "2026-07-15T02:00:00+00:00",  # 10:00 Taipei
        "trading_state": "trading",
        "exchange_rules_enforced": True,
    }
    value.update(overrides)
    return value


def test_twse_tpex_tick_table_has_exact_boundary_values():
    assert tick_size(9.99) == Decimal("0.01")
    assert tick_size(10) == Decimal("0.05")
    assert tick_size(50) == Decimal("0.1")
    assert tick_size(100) == Decimal("0.5")
    assert tick_size(500) == Decimal("1")
    assert tick_size(1000) == Decimal("5")
    assert is_valid_tick(49.95) is True
    assert is_valid_tick(49.96) is False
    assert is_valid_tick(100.5) is True
    assert is_valid_tick(100.1) is False


def test_taiwan_rules_enforce_board_odd_lot_tick_and_daily_limits():
    rules = TaiwanPaperMarketRules()

    valid = rules.evaluate(ticket=ticket(), market=market())
    bad_board_lot = rules.evaluate(ticket=ticket(requested_quantity=1500), market=market())
    bad_odd_lot = rules.evaluate(
        ticket=ticket(lot_type="odd_lot", requested_quantity=10, quantity_shares=10, order_type="market"),
        market=market(odd_lot_auction_matched=True),
    )
    bad_tick = rules.evaluate(ticket=ticket(limit_price=100.1), market=market())
    bad_limit = rules.evaluate(ticket=ticket(limit_price=111), market=market())
    missing_bounds = rules.evaluate(ticket=ticket(), market=market(limit_up=None, limit_down=None))

    assert valid["allowed"] is True
    assert valid["session"]["name"] == "continuous"
    assert bad_board_lot["reason"] == "board_lot_quantity_must_be_multiple_of_1000"
    assert bad_odd_lot["reason"] == "odd_lot_requires_limit_order_and_rod"
    assert bad_tick["reason"] == "limit_price_is_off_tick"
    assert bad_limit["reason"] == "limit_price_outside_daily_price_limit"
    assert missing_bounds["reason"] == "daily_price_limits_required_for_exchange_simulation"


def test_odd_lot_and_auction_orders_need_their_own_match_receipt():
    rules = TaiwanPaperMarketRules()
    odd_ticket = ticket(lot_type="odd_lot", requested_quantity=10, quantity_shares=10)
    before_opening_auction = rules.evaluate(
        ticket=odd_ticket,
        market=market(source_timestamp="2026-07-15T01:05:00+00:00"),
    )
    auction_match = rules.evaluate(
        ticket=odd_ticket,
        market=market(odd_lot_auction_matched=True),
    )
    after_hours_ticket = ticket(session="after_hours")
    after_hours_match = rules.evaluate(
        ticket=after_hours_ticket,
        market=market(
            source_timestamp="2026-07-15T06:30:00+00:00",
            closing_price=100,
            after_hours_fixed_price_matched=True,
        ),
    )

    assert before_opening_auction["entry_allowed"] is True
    assert before_opening_auction["matching_allowed"] is False
    assert auction_match["matching_allowed"] is True
    assert after_hours_match["order_valid"] is True
    assert after_hours_match["entry_allowed"] is False
    assert after_hours_match["matching_allowed"] is True


def test_explicit_local_training_fill_uses_verified_mark_without_claiming_exchange_session():
    rules = TaiwanPaperMarketRules()

    result = rules.evaluate(
        ticket=ticket(order_type="market", limit_price=None),
        market=market(
            source_timestamp="2026-07-15T06:00:00+00:00",
            trading_state="closed",
            paper_training_fill_override=True,
        ),
    )

    assert result["allowed"] is True
    assert result["session"]["mode"] == "local_paper_training"
    assert "not an exchange queue" in result["matching_boundary"]


def test_local_training_fill_allows_an_integral_practice_quantity_without_claiming_board_lot_submission():
    rules = TaiwanPaperMarketRules()

    result = rules.evaluate(
        ticket=ticket(order_type="market", limit_price=None, requested_quantity=100, quantity_shares=100),
        market=market(
            source_timestamp="2026-07-15T06:00:00+00:00",
            trading_state="closed",
            paper_training_fill_override=True,
        ),
    )

    assert result["allowed"] is True
    assert not result["blockers"]


def test_local_training_fill_accepts_verified_close_without_next_session_price_bands():
    rules = TaiwanPaperMarketRules()

    result = rules.evaluate(
        ticket=ticket(order_type="market", limit_price=None, requested_quantity=100, quantity_shares=100),
        market=market(
            limit_up=None,
            limit_down=None,
            source_timestamp="2026-07-15T06:00:00+00:00",
            trading_state="closed",
            paper_training_fill_override=True,
        ),
    )

    assert result["allowed"] is True
    assert result["reason"] is None
    assert {item["code"] for item in result["warnings"]} == {"daily_price_limits_not_supplied_by_quote"}


def test_broker_rests_preopen_order_and_expires_rod_at_session_end(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "1_000_000")
    store = SQLiteStore(db_path=tmp_path / "market-rules.sqlite")
    oms = PaperOMS(store=store)
    broker = PaperBrokerSimulator(store=store, oms=oms)
    order = broker.submit(
        {
            **ticket(order_id="RULE-PREOPEN"),
            "actor": "user",
        },
        market(source_timestamp="2026-07-15T00:40:00+00:00"),  # 08:40 Taipei
    )

    assert order["order"]["status"] == "open"
    no_fill = broker.process_market_tick(
        "2330.TW", market(source_timestamp="2026-07-15T00:50:00+00:00")
    )
    assert no_fill["results"][0]["order"]["status"] == "open"
    filled = broker.process_market_tick("2330.TW", market())
    assert filled["results"][0]["order"]["status"] == "filled"

    second = broker.submit(
        {
            **ticket(order_id="RULE-EXPIRE", limit_price=90),
            "actor": "user",
        },
        market(),
    )
    assert second["order"]["status"] == "open"
    expired = broker.process_market_tick(
        "2330.TW", market(source_timestamp="2026-07-15T05:31:00+00:00")
    )
    assert expired["expired"][0]["order"]["status"] == "expired"
    assert expired["expired"][0]["order"]["rejection_reason"] == "rod_exchange_session_expired"
