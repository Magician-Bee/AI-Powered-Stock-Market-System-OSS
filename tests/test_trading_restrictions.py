from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from stock_ai.main import app
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.trading_restrictions import TradingRestrictionLedger
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.storage.trade_store import TradeStore


OFFICIAL_URL = "https://openapi.twse.com.tw/v1/announcement/notice"


def restriction(
    restriction_type: str,
    *,
    symbol: str = "2330.TW",
    restriction_id: str | None = None,
    effective_from: str = "2026-07-01",
    effective_until: str | None = "2026-08-31",
    **terms,
):
    return {
        "restriction_id": restriction_id or f"TEST-{restriction_type}",
        "symbol": symbol,
        "restriction_type": restriction_type,
        "status": "active",
        "effective_from": effective_from,
        "effective_until": effective_until,
        "official_verified": True,
        "source_id": "twse_openapi",
        "source_url": OFFICIAL_URL,
        "acquired_at": "2026-07-28T04:00:00Z",
        "source_payload": {"Code": symbol.split(".")[0], "Type": restriction_type},
        **terms,
    }


def broker(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "trading-restrictions.sqlite")
    oms = PaperOMS(store=store, initial_cash=1_000_000)
    return store, oms, PaperBrokerSimulator(store=store, oms=oms)


def ticket(order_type: str = "market", **overrides):
    value = {
        "order_id": f"ORDER-{order_type}",
        "symbol": "2330.TW",
        "side": "buy",
        "order_type": order_type,
        "time_in_force": "rod",
        "lot_type": "odd_lot",
        "session": "regular",
        "quantity_shares": 10,
        "actor": "user",
    }
    value.update(overrides)
    return value


def market(**overrides):
    value = {
        "price": 100.0,
        "restriction_as_of": "2026-07-28T05:00:00Z",
        "limit_up": 110.0,
        "limit_down": 90.0,
        "trading_state": "trading",
        "source_timestamp": "2026-07-28T02:00:00Z",
        "odd_lot_auction_matched": True,
    }
    value.update(overrides)
    return value


def test_attention_warns_without_blocking_and_import_is_revisioned(tmp_path):
    store, _, simulator = broker(tmp_path)
    ledger = TradingRestrictionLedger(store)
    first = ledger.import_restrictions([restriction("attention_stock")])
    same = ledger.import_restrictions([restriction("attention_stock")])
    corrected = ledger.import_restrictions(
        [restriction("attention_stock", source_payload={"severity": "revised"})]
    )

    preview = simulator.preview(ticket(), market())
    assert first["created_count"] == 1
    assert same["idempotent_count"] == 1
    assert corrected["items"][0]["revision"] == 2
    assert preview["can_submit"] is True
    assert preview["trading_restrictions"]["warnings"][0]["code"] == "attention_stock"

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._connect() as conn:
            conn.execute(
                "update trading_restriction_revisions set status='cancelled'"
            )


def test_disposition_requires_limit_order_but_limit_order_can_fill(tmp_path):
    store, _, simulator = broker(tmp_path)
    TradingRestrictionLedger(store).import_restrictions(
        [
            restriction(
                "disposition_stock",
                auction_interval_minutes=20,
                full_cash_delivery=True,
            )
        ]
    )

    rejected = simulator.submit(ticket(), market())
    assert rejected["order"]["status"] == "rejected"
    assert rejected["order"]["rejection_reason"] == "disposition_requires_limit_order"
    assert simulator.preview(ticket(), market())["execution_expectation"] == "blocked_by_trading_restriction"

    accepted = simulator.submit(
        ticket("limit", order_id="ORDER-DISPOSITION-LIMIT", limit_price=101),
        market(),
    )
    assert accepted["order"]["status"] == "filled"


def test_halt_blocks_preview_and_open_fill_until_official_resume(tmp_path):
    store, _, simulator = broker(tmp_path)
    ledger = TradingRestrictionLedger(store)
    open_order = simulator.submit(
        ticket("limit", order_id="ORDER-RESTING", limit_price=95),
        market(),
    )
    assert open_order["order"]["status"] == "open"

    ledger.import_restrictions(
        [restriction("halt_trading", effective_until=None, restriction_id="HALT-1")]
    )
    blocked = simulator.process_market_tick("2330.TW", market(price=94))
    assert blocked["results"][0]["order"]["status"] == "open"
    assert blocked["results"][0]["trading_restrictions"]["reason"] == "trading_halted"
    assert simulator.preview(ticket(), market())["can_submit"] is False

    ledger.import_restrictions(
        [
            restriction(
                "resume_trading",
                effective_from="2026-07-28T04:30:00Z",
                effective_until=None,
                restriction_id="RESUME-1",
            )
        ]
    )
    filled = simulator.process_market_tick("2330.TW", market(price=94))
    assert filled["results"][0]["order"]["status"] == "filled"


def test_price_limits_reject_out_of_range_and_unverified_locked_market_fill(tmp_path):
    _, _, simulator = broker(tmp_path)
    outside = simulator.preview(
        ticket("limit", limit_price=111),
        market(),
    )
    locked = simulator.preview(ticket(), market(price=110))
    liquid = simulator.preview(
        ticket(order_id="ORDER-LIQUID"),
        market(price=110, buy_liquidity_confirmed=True),
    )

    assert outside["validation"]["reason"] == "limit_price_above_daily_limit"
    assert locked["validation"]["reason"] == "limit_up_liquidity_unverified"
    assert liquid["can_submit"] is True


def test_direct_oms_strategy_path_cannot_bypass_active_halt(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "direct-oms.sqlite")
    oms = PaperOMS(store=store)
    TradingRestrictionLedger(store).import_restrictions(
        [restriction("halt_trading", effective_until=None)]
    )
    result = oms.submit_and_fill(
        {
            "order_id": "DIRECT-STRATEGY",
            "symbol": "2330.TW",
            "market": "TW",
            "action": "buy",
            "entry_price": 100,
            "position_size_pct": 1,
            "risk_approved": True,
            "market_context": {"restriction_as_of": "2026-07-28T05:00:00Z"},
        }
    )
    assert result["filled"] is False
    assert result["order"]["rejection_reason"] == "trading_halted"


def test_trading_restriction_api_import_and_versioned_ui_list(tmp_path, monkeypatch):
    trade_store = TradeStore(store=SQLiteStore(db_path=tmp_path / "api.sqlite"))
    engine = SimpleNamespace(pipeline=SimpleNamespace(trade_store=trade_store))
    monkeypatch.setattr("stock_ai.main.get_runtime_engine", lambda: engine)
    client = TestClient(app)

    imported = client.post(
        "/api/official/trading-restrictions/import",
        json={"items": [restriction("attention_stock")]},
    )
    listed = client.get(
        "/api/data/ui/v1/market/2330.TW/trading-restrictions",
        params={"as_of": "2026-07-28T05:00:00Z", "active_only": "true"},
    )
    compatibility = client.get(
        "/api/market/2330.TW/trading-restrictions",
        params={"as_of": "2026-07-28T05:00:00Z"},
    )

    assert imported.status_code == 200
    assert imported.json()["created_count"] == 1
    assert listed.status_code == 200
    assert listed.json()["items"][0]["restriction_type"] == "attention_stock"
    assert compatibility.status_code == 200
