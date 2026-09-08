from __future__ import annotations

from datetime import datetime, timezone

import pytest

from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.risk.kill_switch import DurableRiskControlStore, LossLimitPolicy
from open_stock_ai.risk.settlement_pnl_feed import (
    SettlementPnLFeed,
    build_source_receipt,
    verify_source_receipt,
)
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.storage.trade_store import TradeStore


def _source(event_id: str) -> dict:
    return build_source_receipt(
        {
            "source": "broker_reconciliation",
            "source_event_id": event_id,
            "settlement_id": "SETTLE-1",
        }
    )


def test_feed_requires_and_verifies_content_addressed_source_receipt(tmp_path) -> None:
    control = DurableRiskControlStore(tmp_path / "risk.sqlite")
    feed = SettlementPnLFeed(control)
    settled_at = datetime(2026, 8, 26, 8, tzinfo=timezone.utc).isoformat()

    result = feed.ingest(
        {
            "event_id": "broker-realized-1",
            "settled_at": settled_at,
            "realized_pnl_amount": -1200.0,
            "realized_pnl_pct": -1.2,
            "scopes": {"account": "broker-001", "strategy": "swing"},
            "source_receipt": _source("broker-realized-1"),
        }
    )

    assert result["order_allowed"] is True
    assert result["idempotent"] is False
    assert verify_source_receipt(result["event"]["source_receipt"])

    duplicate = feed.ingest(
        {
            "event_id": "broker-realized-1",
            "settled_at": settled_at,
            "realized_pnl_amount": -1200.0,
            "realized_pnl_pct": -1.2,
            "scopes": {"account": "broker-001", "strategy": "swing"},
            "source_receipt": _source("broker-realized-1"),
        }
    )
    assert duplicate["idempotent"] is True
    assert duplicate["risk_event"]["receipt_sha256"] == result["risk_event"]["receipt_sha256"]

    with pytest.raises(ValueError, match="event_conflict"):
        feed.ingest(
            {
                "event_id": "broker-realized-1",
                "settled_at": settled_at,
                "realized_pnl_amount": -1200.0,
                "realized_pnl_pct": -9.9,
                "scopes": {"account": "broker-001", "strategy": "swing"},
                "source_receipt": _source("broker-realized-1"),
            }
        )


def test_feed_activates_account_switch_from_paper_oms_realized_loss_and_survives_restart(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "100000")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_COMMISSION_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SELL_TAX_BPS", "0")
    database = tmp_path / "paper.sqlite"
    paper_store = SQLiteStore(db_path=database)
    oms = PaperOMS(store=paper_store, account_id="paper-001")
    oms.submit_and_fill(
        {
            "order_id": "BUY-1",
            "symbol": "2330.TW",
            "market": "TW",
            "action": "buy",
            "entry_price": 100.0,
            "position_size_pct": 10.0,
            "risk_approved": True,
        }
    )
    sell = oms.submit_and_fill(
        {
            "order_id": "SELL-1",
            "symbol": "2330.TW",
            "market": "TW",
            "action": "sell",
            "entry_price": 90.0,
            "position_size_pct": 100.0,
            "risk_approved": True,
        }
    )
    assert sell["portfolio"]["realized_pnl"] == -1000.0

    control = DurableRiskControlStore(
        database,
        LossLimitPolicy(
            intraday_loss_pct=0.5,
            daily_loss_pct=100,
            weekly_loss_pct=100,
            monthly_loss_pct=100,
            consecutive_loss_count=99,
        ),
    )
    batch = SettlementPnLFeed(control).ingest_paper_oms(oms)

    assert batch["ingested_count"] == 1
    assert batch["ingested"][0]["event"]["realized_pnl_pct"] == -1.0
    assert batch["ingested"][0]["order_allowed"] is False
    assert DurableRiskControlStore(database).order_gate({"account": "paper-001"})["allowed"] is False


def test_paper_oms_feed_does_not_evaluate_unsettled_t_plus_2_realized_loss(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "100000")
    database = tmp_path / "paper-t2.sqlite"
    oms = PaperOMS(store=SQLiteStore(db_path=database), account_id="paper-t2")
    market = {
        "settlement_rules_enforced": True,
        "source_timestamp": "2026-08-24T10:00:00+08:00",
        "currency": "TWD",
    }
    oms.submit_and_fill(
        {
            "order_id": "BUY-T2",
            "symbol": "2330.TW",
            "market": "TW",
            "action": "buy",
            "entry_price": 100.0,
            "position_size_pct": 10.0,
            "risk_approved": True,
            "market_context": market,
        }
    )
    oms.submit_and_fill(
        {
            "order_id": "SELL-T2",
            "symbol": "2330.TW",
            "market": "TW",
            "action": "sell",
            "entry_price": 90.0,
            "position_size_pct": 100.0,
            "risk_approved": True,
            "market_context": market,
        }
    )
    control = DurableRiskControlStore(database)
    batch = SettlementPnLFeed(control).ingest_paper_oms(oms)

    assert batch["ingested_count"] == 0
    assert {item["reason"] for item in batch["skipped"]} == {"no_realized_pnl", "settlement_pending"}
    assert control.order_gate({"account": "paper-t2"})["allowed"] is True


def test_tampered_source_receipt_is_rejected(tmp_path) -> None:
    control = DurableRiskControlStore(tmp_path / "risk.sqlite")
    feed = SettlementPnLFeed(control)
    source = _source("bad-1")
    source["settlement_id"] = "tampered"

    with pytest.raises(ValueError, match="hash_mismatch"):
        feed.ingest(
            {
                "event_id": "bad-1",
                "settled_at": "2026-08-26T08:00:00+00:00",
                "realized_pnl_pct": -1.0,
                "scopes": {"account": "broker-001"},
                "source_receipt": source,
            }
        )


def test_trade_store_wires_settlement_feed_into_runtime_path(tmp_path) -> None:
    database = tmp_path / "runtime.sqlite"
    control = DurableRiskControlStore(database)
    trades = TradeStore(store=SQLiteStore(db_path=database), risk_control=control)

    assert trades.settlement_pnl_feed is not None
    result = trades.sync_settlement_risk()

    assert result["connected"] is True
    assert result["ingested_count"] == 0
    assert result["pnl_denominator"]["basis"] == "paper_account_initial_cash"
