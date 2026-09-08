from __future__ import annotations

from datetime import datetime, timedelta, timezone

from open_stock_ai.risk.kill_switch import DurableRiskControlStore, LossLimitPolicy


def test_scoped_switch_survives_restart_and_only_blocks_matching_order(tmp_path) -> None:
    database = tmp_path / "risk-control.sqlite"
    store = DurableRiskControlStore(database)
    store.activate("account", "paper-001", "manual review", "test")

    reopened = DurableRiskControlStore(database)
    assert reopened.order_gate({"account": "paper-001", "symbol": "2330.TW"})["allowed"] is False
    assert reopened.order_gate({"account": "paper-002", "symbol": "2330.TW"})["allowed"] is True
    assert reopened.status()["active_switches"][0]["scope_type"] == "account"


def test_duplicate_pnl_event_is_idempotent_and_daily_limit_survives_restart(tmp_path) -> None:
    database = tmp_path / "risk-control.sqlite"
    policy = LossLimitPolicy(
        intraday_loss_pct=100,
        daily_loss_pct=1,
        weekly_loss_pct=100,
        monthly_loss_pct=100,
        consecutive_loss_count=99,
    )
    store = DurableRiskControlStore(database, policy)
    timestamp = datetime.now(timezone.utc)
    first = store.record_realized_pnl("fill-1", -1.2, timestamp, {"symbol": "2330.TW"})
    second = store.record_realized_pnl("fill-1", -1.2, timestamp, {"symbol": "2330.TW"})
    assert first["receipt_sha256"] == second["receipt_sha256"]

    receipt = store.evaluate_limits(timestamp, {"symbol": "2330.TW"})
    assert [item["limit_type"] for item in receipt["violations"]] == ["daily_loss_pct"]
    assert receipt["order_allowed"] is False
    assert DurableRiskControlStore(database).order_gate({"symbol": "2330.TW"})["allowed"] is False


def test_weekly_monthly_and_consecutive_limits_create_durable_receipts(tmp_path) -> None:
    database = tmp_path / "risk-control.sqlite"
    as_of = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    policy = LossLimitPolicy(
        intraday_loss_pct=100,
        daily_loss_pct=100,
        weekly_loss_pct=1,
        monthly_loss_pct=2,
        consecutive_loss_count=2,
    )
    store = DurableRiskControlStore(database, policy)
    store.record_realized_pnl("fill-old", -1.1, as_of - timedelta(days=3), {"strategy": "swing"})
    store.record_realized_pnl("fill-new", -1.1, as_of - timedelta(hours=2), {"strategy": "swing"})

    receipt = store.evaluate_limits(as_of, {"strategy": "swing"})
    limit_types = {item["limit_type"] for item in receipt["violations"]}
    assert {"weekly_loss_pct", "monthly_loss_pct", "consecutive_loss_count"} <= limit_types
    assert store.order_gate({"strategy": "swing"})["allowed"] is False
    assert store.order_gate({"strategy": "other"})["allowed"] is True
