import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, settings, strategies as st
import pytest

from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.governance.change_management import ChangeManagementRegistry
from open_stock_ai.execution.paper_execution_model import (
    HistoricalOrderBookReceiptStore,
    build_historical_order_book_receipt,
    resolve_execution,
    verify_historical_order_book_receipt,
)
from open_stock_ai.governance.content_retention import ContentAddressedRetentionLedger
from open_stock_ai.governance.durable_store import SQLiteGovernanceStore, SQLiteRetentionStore
from open_stock_ai.storage.sqlite_store import SQLiteStore


_PROPERTY_SETTINGS = settings(derandomize=True, max_examples=20, deadline=None)


def build_broker(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_ACCOUNT_ID", "broker-test")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "10000")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_COMMISSION_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SELL_TAX_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_LOT_SIZE", "1")
    store = SQLiteStore(db_path=tmp_path / "paper-broker.sqlite")
    oms = PaperOMS(store=store)
    return PaperBrokerSimulator(store=store, oms=oms)


def market(symbol="2330.TW", price=100):
    return {
        "symbol": symbol,
        "market": "TW",
        "price": price,
        "price_source": "test_exchange_last_trade",
        "source_timestamp": "2026-07-15T02:00:00+00:00",
        "is_realtime": True,
        "is_fallback": False,
        # A broker tick for an odd-lot order must represent a completed
        # periodic call auction, not a regular-board-lot last trade.
        "odd_lot_auction_matched": True,
    }


def test_market_buy_and_partial_sell_use_one_cash_and_position_ledger(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    buy = broker.submit(
        {
            "order_id": "PB-BUY-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "actor": "user",
        },
        market(price=100),
    )
    assert buy["filled"] is True
    assert buy["order"]["status"] == "filled"
    assert buy["portfolio"]["cash_balance"] == 9000
    assert buy["portfolio"]["positions"][0]["quantity"] == 10

    sell = broker.submit(
        {
            "order_id": "PB-SELL-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "sell",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 5,
            "actor": "user",
        },
        market(price=110),
    )
    assert sell["filled"] is True
    assert sell["portfolio"]["cash_balance"] == 9550
    assert sell["portfolio"]["positions"][0]["quantity"] == 5
    assert sell["portfolio"]["realized_pnl"] == 50


def test_paper_preview_labels_local_costs_as_non_execution_evidence(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)

    preview = broker.preview(
        {
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 1,
            "actor": "user",
        },
        market(price=100),
    )

    evidence = preview["cost_evidence"]
    assert evidence["cost_source"] == "local_paper_configuration"
    assert evidence["is_simulated"] is True
    assert evidence["execution_evidence_eligible"] is False
    assert evidence["assumptions"] == {
        "commission_bps": 0.0,
        "sell_tax_bps": 0.0,
        "slippage_bps": 0.0,
    }
    assert "broker_account_cost_schedule_receipt_missing" in evidence["blockers"]
    assert "local_paper_cost_assumption_zero_or_unconfigured" in evidence["blockers"]


def test_paper_broker_propagates_the_immutable_change_set_to_paper_oms(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_ACCOUNT_ID", "broker-change-binding")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "10000")
    database = tmp_path / "paper-broker-change-binding.sqlite"
    store = SQLiteStore(db_path=database)
    registry = ChangeManagementRegistry(SQLiteGovernanceStore(database))
    change = registry.pin_change(
        "paper-broker-change-1",
        code_sha256="a" * 64,
        model_sha256="b" * 64,
        data_sha256="c" * 64,
        risk_policy_sha256="d" * 64,
        approved_by="owner",
        approved_at="2026-08-26T10:05:00+00:00",
    )
    broker = PaperBrokerSimulator(
        store=store,
        oms=PaperOMS(store=store, change_management=registry, require_change_binding=True),
    )

    result = broker.submit(
        {
            "order_id": "PB-WITH-CHANGE",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "actor": "user",
            "change_id": change.change_id,
        },
        market(price=100),
    )

    assert result["filled"] is True
    assert result["oms"]["order"]["change_binding"]["change_id"] == change.change_id


def test_limit_order_rests_then_fills_when_market_crosses_limit(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    submitted = broker.submit(
        {
            "order_id": "PB-LIMIT-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "limit_price": 95,
            "actor": "codex",
        },
        market(price=100),
    )
    assert submitted["pending"] is True
    assert submitted["order"]["status"] == "open"
    assert broker.open_symbols() == ["2330.TW"]

    tick = broker.process_market_tick("2330.TW", market(price=94))
    assert tick["processed_count"] == 1
    assert tick["results"][0]["filled"] is True
    assert tick["results"][0]["order"]["status"] == "filled"
    assert broker.recent_orders(status="open") == []


def test_marketable_order_persists_multiple_partial_fills_across_broker_restart(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    first = broker.submit(
        {
            "order_id": "PB-PARTIAL-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "actor": "user",
        },
        {**market(price=100), "available_quantity": 4},
    )

    assert first["filled"] is False
    assert first["pending"] is True
    assert first["order"]["status"] == "partially_filled"
    assert first["order"]["filled_quantity"] == 4
    assert first["order"]["remaining_quantity"] == 6
    assert first["portfolio"]["cash_balance"] == 9600

    restarted = PaperBrokerSimulator(store=broker.store, oms=PaperOMS(store=broker.store))
    second = restarted.process_market_tick(
        "2330.TW", {**market(price=100), "available_quantity": 6},
    )["results"][0]

    assert second["filled"] is True
    assert second["pending"] is False
    assert second["order"]["status"] == "filled"
    assert second["order"]["filled_quantity"] == 10
    assert second["order"]["remaining_quantity"] == 0
    assert second["portfolio"]["cash_balance"] == 9000
    assert second["portfolio"]["positions"][0]["quantity"] == 10
    assert len(restarted.recent_fills()) == 2


def test_execution_model_replays_queue_consumption_and_resumes_after_queue_is_empty():
    first = resolve_execution(
        order_id="PB-QUEUE-1",
        fill_sequence=1,
        remaining_quantity=10,
        market={
            "available_quantity": 10,
            "matched_quantity": 10,
            "queue_ahead_quantity": 6,
        },
    )
    assert first["fill_quantity"] == 4
    assert first["queue_consumed"] == 6
    assert first["queue_ahead_remaining"] == 0
    assert first["blockers"] == []

    second = resolve_execution(
        order_id="PB-QUEUE-1",
        fill_sequence=2,
        remaining_quantity=6,
        market={"available_quantity": 6},
        prior_state={"queue_ahead_remaining": first["queue_ahead_remaining"]},
    )
    assert second["fill_quantity"] == 6
    assert second["blockers"] == []


def test_execution_model_probability_and_latency_are_explicit_fail_closed_inputs():
    blocked = resolve_execution(
        order_id="PB-MODEL-BLOCK-1",
        fill_sequence=1,
        remaining_quantity=10,
        market={
            "available_quantity": 10,
            "fill_probability": 0,
            "latency_ms": 250,
            "max_latency_ms": 100,
        },
    )
    assert blocked["fill_quantity"] == 0
    assert blocked["execution_allowed"] is False
    assert blocked["blockers"] == ["fill_probability_not_met", "execution_latency_exceeded"]


def test_execution_model_marks_scenario_inputs_ineligible_without_historical_or_calibration_receipts():
    result = resolve_execution(
        order_id="PB-EVIDENCE-1",
        fill_sequence=1,
        remaining_quantity=4,
        market={"available_quantity": 4},
    )

    assert result["execution_allowed"] is True
    assert result["execution_evidence_eligible"] is False
    assert result["evidence_blockers"] == [
        "historical_order_book_receipt_unavailable",
        "empirical_impact_calibration_unavailable",
    ]


def test_historical_order_book_receipt_binds_queue_inputs_and_rejects_tampering():
    receipt = build_historical_order_book_receipt(
        {
            "snapshot_id": "TWSE-2330-20260826-010000",
            "symbol": "2330.TW",
            "venue": "TWSE",
            "captured_at": "2026-08-26T01:00:00+00:00",
            "available_at": "2026-08-26T01:00:01+00:00",
            "source": "twse_order_book_archive",
            "source_revision": "2026-08-26-r1",
            "provider_verified": True,
            "levels": [{"price": 100.0, "size": 6.0}, {"price": 100.1, "size": 4.0}],
            "available_quantity": 10.0,
            "matched_quantity": 10.0,
            "queue_ahead_quantity": 6.0,
        }
    )
    assert verify_historical_order_book_receipt(receipt)
    result = resolve_execution(
        order_id="PB-EVIDENCE-2",
        fill_sequence=1,
        remaining_quantity=4,
        market={"historical_order_book_receipt": receipt},
    )
    assert result["fill_quantity"] == 4
    assert result["historical_order_book_replay_verified"] is True
    assert result["execution_evidence_eligible"] is False
    assert "empirical_impact_calibration_unavailable" in result["evidence_blockers"]

    tampered = {**receipt, "available_quantity": 11.0}
    with pytest.raises(ValueError, match="hash_mismatch"):
        verify_historical_order_book_receipt(tampered)


def test_historical_order_book_receipt_store_replays_by_id_after_restart(tmp_path):
    receipt = build_historical_order_book_receipt(
        {
            "snapshot_id": "TWSE-2330-20260826-010100",
            "symbol": "2330.TW",
            "venue": "TWSE",
            "captured_at": "2026-08-26T01:01:00+00:00",
            "available_at": "2026-08-26T01:01:01+00:00",
            "source": "twse_order_book_archive",
            "source_revision": "2026-08-26-r2",
            "provider_verified": True,
            "levels": [{"price": 100.0, "size": 6.0}, {"price": 100.1, "size": 4.0}],
            "available_quantity": 10.0,
            "matched_quantity": 10.0,
            "queue_ahead_quantity": 6.0,
        }
    )
    store = HistoricalOrderBookReceiptStore(tmp_path / "order-book.sqlite")
    assert store.save(receipt) == receipt
    reopened = HistoricalOrderBookReceiptStore(store.path)
    assert reopened.by_receipt(receipt["receipt_sha256"]) == receipt
    assert reopened.receipts(symbol="2330.TW") == [receipt]

    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.DatabaseError, match="immutable"):
        connection.execute(
            "delete from historical_order_book_receipts where receipt_sha256=?",
            (receipt["receipt_sha256"],),
        )


def test_execution_model_rejects_order_book_id_without_durable_receipt_resolution():
    result = resolve_execution(
        order_id="PB-EVIDENCE-ID-1",
        fill_sequence=1,
        remaining_quantity=4,
        market={
            "historical_order_book_receipt_id": "a" * 64,
            "available_quantity": 4,
        },
    )

    assert result["fill_quantity"] == 0
    assert result["execution_allowed"] is False
    assert "historical_order_book_receipt_lookup_failed" in result["blockers"]


def test_paper_broker_resolves_historical_order_book_id_from_store(tmp_path):
    receipt = build_historical_order_book_receipt(
        {
            "snapshot_id": "TWSE-2330-20260826-010200",
            "symbol": "2330.TW",
            "venue": "TWSE",
            "captured_at": "2026-08-26T01:02:00+00:00",
            "available_at": "2026-08-26T01:02:01+00:00",
            "source": "twse_order_book_archive",
            "source_revision": "2026-08-26-r3",
            "provider_verified": True,
            "levels": [{"price": 100.0, "size": 6.0}, {"price": 100.1, "size": 4.0}],
            "available_quantity": 10.0,
            "matched_quantity": 10.0,
            "queue_ahead_quantity": 6.0,
        }
    )
    receipt_store = HistoricalOrderBookReceiptStore(tmp_path / "order-book.sqlite")
    receipt_store.save(receipt)
    store = SQLiteStore(db_path=tmp_path / "paper-broker.sqlite")
    oms = PaperOMS(store=store)
    broker = PaperBrokerSimulator(
        store=store,
        oms=oms,
        historical_order_book_store=receipt_store,
    )

    result = broker.submit(
        {
            "order_id": "PB-HISTORICAL-ID-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 4,
            "actor": "user",
        },
        {**market(), "historical_order_book_receipt_id": receipt["receipt_sha256"]},
    )

    assert result["filled"] is True
    assert result["order"]["filled_quantity"] == 4
    execution = json.loads(result["events"][-1]["payload_json"])["execution_model"]
    assert execution["historical_order_book_replay_verified"] is True


def test_execution_model_enforces_explicit_participation_and_volume_caps():
    capped = resolve_execution(
        order_id="PB-LIQUIDITY-CAP-1",
        fill_sequence=1,
        remaining_quantity=100,
        market={
            "available_quantity": 100,
            "market_volume": 50,
            "participation_rate": 0.2,
            "volume_cap": 8,
        },
    )
    assert capped["participation_model_enabled"] is True
    assert capped["participation_quantity_cap"] == 10
    assert capped["liquidity_quantity_cap"] == 8
    assert capped["fill_quantity"] == 8
    assert capped["blockers"] == []

    participation_only = resolve_execution(
        order_id="PB-LIQUIDITY-CAP-2",
        fill_sequence=1,
        remaining_quantity=100,
        market={
            "available_quantity": 100,
            "market_volume": 50,
            "participation_rate": 0.2,
        },
    )
    assert participation_only["fill_quantity"] == 10


@pytest.mark.parametrize(
    ("market", "message"),
    [
        ({"market_volume": 100}, "participation_model_requires_participation_rate"),
        ({"participation_rate": 0.2}, "participation_model_requires_market_volume"),
        ({"market_volume": 100, "participation_rate": 1.1}, "participation_rate_must_be_between_zero_and_one"),
        ({"volume_cap": -1}, "volume_cap_must_be_finite_non_negative"),
    ],
)
def test_execution_model_rejects_incomplete_or_invalid_liquidity_contracts(market, message):
    if "requires_" in message:
        blocked = resolve_execution(
            order_id="PB-LIQUIDITY-BLOCK-1",
            fill_sequence=1,
            remaining_quantity=10,
            market=market,
        )
        assert blocked["fill_quantity"] == 0
        assert message in blocked["blockers"]
    else:
        with pytest.raises(ValueError, match=message):
            resolve_execution(
                order_id="PB-LIQUIDITY-BLOCK-1",
                fill_sequence=1,
                remaining_quantity=10,
                market=market,
            )


def test_broker_persists_participation_cap_in_fill_receipt(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    result = broker.submit(
        {
            "order_id": "PB-LIQUIDITY-RECEIPT-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "actor": "simulator",
        },
        {
            **market(price=100),
            "available_quantity": 10,
            "market_volume": 10,
            "participation_rate": 0.4,
        },
    )
    assert result["order"]["filled_quantity"] == 4
    assert result["order"]["remaining_quantity"] == 6
    payload = result["events"][-1]["payload_json"]
    assert '"participation_rate": 0.4' in payload
    assert '"participation_quantity_cap": 4.0' in payload


def test_broker_persists_execution_model_receipt_and_market_impact(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    result = broker.submit(
        {
            "order_id": "PB-MODEL-RECEIPT-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "actor": "simulator",
        },
        {
            **market(price=100),
            "market_impact_bps": 10,
            "latency_ms": 50,
            "latency_impact_bps_per_100ms": 2,
        },
    )
    assert result["filled"] is True
    assert round(result["order"]["fill"]["fill_price"], 8) == 100.11
    execution = result["events"][-1]["payload_json"]
    assert '"total_impact_bps": 11.0' in execution
    assert '"execution_allowed": true' in execution

    with broker.store._connect() as conn:
        payload = conn.execute(
            "select payload_json from paper_broker_orders where order_id = ?",
            ("PB-MODEL-RECEIPT-1",),
        ).fetchone()[0]
    assert '"execution_state"' in payload


def test_ioc_keeps_executed_partial_fill_and_cancels_only_remainder(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    result = broker.submit(
        {
            "order_id": "PB-IOC-PARTIAL-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "ioc",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "actor": "user",
        },
        {**market(price=100), "available_quantity": 4},
    )

    assert result["filled"] is False
    assert result["pending"] is False
    assert result["order"]["status"] == "canceled"
    assert result["order"]["filled_quantity"] == 4
    assert result["order"]["remaining_quantity"] == 6
    assert result["portfolio"]["positions"][0]["quantity"] == 4


def test_fok_rejects_insufficient_tick_liquidity_without_creating_a_fill(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    result = broker.submit(
        {
            "order_id": "PB-FOK-NO-PARTIAL-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "fok",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "actor": "user",
        },
        {**market(price=100), "available_quantity": 4},
    )

    assert result["order"]["status"] == "canceled"
    assert result["portfolio"]["position_count"] == 0
    assert broker.recent_fills() == []


def test_stop_sell_waits_for_trigger_and_then_exits_position(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    broker.submit(
        {
            "order_id": "PB-BUY-FOR-STOP",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "actor": "user",
        },
        market(price=100),
    )
    stop = broker.submit(
        {
            "order_id": "PB-STOP-SELL",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "sell",
            "order_type": "stop",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "stop_price": 95,
            "actor": "agent",
        },
        market(price=100),
    )
    assert stop["order"]["status"] == "open"

    triggered = broker.process_market_tick("2330.TW", market(price=94))
    result = triggered["results"][0]
    assert result["filled"] is True
    assert result["order"]["status"] == "filled"
    assert result["portfolio"]["position_count"] == 0
    assert result["portfolio"]["realized_pnl"] == -60


def test_pending_order_can_be_canceled_and_will_not_fill_later(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    broker.submit(
        {
            "order_id": "PB-CANCEL-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 5,
            "limit_price": 90,
            "actor": "user",
        },
        market(price=100),
    )
    canceled = broker.cancel("PB-CANCEL-1")
    assert canceled["order"]["status"] == "canceled"
    assert canceled["order"]["can_cancel"] is False
    tick = broker.process_market_tick("2330.TW", market(price=80))
    assert tick["processed_count"] == 0
    assert broker.oms.portfolio_summary()["position_count"] == 0


def test_ioc_order_cancels_when_not_marketable(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    result = broker.submit(
        {
            "order_id": "PB-IOC-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "ioc",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "limit_price": 90,
            "actor": "user",
        },
        market(price=100),
    )
    assert result["filled"] is False
    assert result["pending"] is False
    assert result["order"]["status"] == "canceled"


def test_acknowledged_partial_order_replaces_remaining_quantity_and_preserves_audit_chain(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    first = broker.submit(
        {
            "order_id": "PB-REPLACE-ORIGINAL",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "limit_price": 100,
            "actor": "user",
        },
        {**market(price=100), "available_quantity": 4},
    )

    assert first["order"]["status"] == "partially_filled"
    assert [event["event_type"] for event in first["events"]] == [
        "submitted",
        "acknowledged",
        "partially_filled",
    ]

    replacement = broker.replace(
        "PB-REPLACE-ORIGINAL",
        {"order_id": "PB-REPLACE-NEW", "limit_price": 95, "actor": "user"},
        market(price=100),
    )
    original = broker.get_order("PB-REPLACE-ORIGINAL")
    assert original["order"]["status"] == "replaced"
    assert original["order"]["filled_quantity"] == 4
    assert original["order"]["replaced_by_order_id"] == "PB-REPLACE-NEW"
    assert original["events"][-1]["event_type"] == "replaced"
    assert replacement["replaced_order_id"] == "PB-REPLACE-ORIGINAL"
    assert replacement["order"]["replaces_order_id"] == "PB-REPLACE-ORIGINAL"
    assert replacement["order"]["requested_quantity"] == 6
    assert replacement["order"]["status"] == "open"
    assert [event["event_type"] for event in replacement["events"]] == [
        "submitted",
        "acknowledged",
        "resting",
    ]

    completed = broker.process_market_tick(
        "2330.TW", {**market(price=94), "available_quantity": 6}
    )["results"][0]
    assert completed["order"]["status"] == "filled"
    assert completed["portfolio"]["positions"][0]["quantity"] == 10
    assert len(broker.recent_fills()) == 2


def test_explicit_expiry_and_rejection_are_durable_terminal_lifecycle_events(tmp_path, monkeypatch):
    broker = build_broker(tmp_path, monkeypatch)
    expiring = broker.submit(
        {
            "order_id": "PB-EXPIRE-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "limit_price": 90,
            "expires_at": "2026-07-15T03:00:01+00:00",
            "actor": "user",
        },
        market(price=100),
    )
    assert expiring["order"]["status"] == "open"
    expired = broker.expire_due(as_of="2026-07-15T03:00:01+00:00")
    assert expired[0]["order"]["status"] == "expired"
    assert expired[0]["events"][-1]["event_type"] == "expired"
    assert expired[0]["order"]["can_cancel"] is False

    rejected = broker.submit(
        {
            "order_id": "PB-REJECT-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "market",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 1_000,
            "actor": "user",
        },
        market(price=100),
    )
    assert rejected["order"]["status"] == "rejected"
    assert [event["event_type"] for event in rejected["events"]] == ["submitted", "rejected"]
    assert broker.recent_orders(status="rejected")[0]["order_id"] == "PB-REJECT-1"


def test_paper_broker_retains_every_lifecycle_projection_across_restart(tmp_path, monkeypatch):
    store = SQLiteStore(db_path=tmp_path / "paper-broker.sqlite")
    oms = PaperOMS(
        store=store,
        account_id="broker-retention",
        initial_cash=10_000,
        commission_bps=0,
        sell_tax_bps=0,
        slippage_bps=0,
        lot_size=1,
    )
    retention = ContentAddressedRetentionLedger(
        store=SQLiteRetentionStore(store.path),
    )
    broker = PaperBrokerSimulator(store=store, oms=oms, retention_ledger=retention)
    submitted = broker.submit(
        {
            "order_id": "PB-RETENTION-1",
            "symbol": "2330.TW",
            "market": "TW",
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "rod",
            "lot_type": "odd_lot",
            "session": "regular",
            "quantity_shares": 10,
            "limit_price": 95,
            "actor": "user",
        },
        market(price=100),
    )
    assert submitted["order"]["status"] == "open"
    assert submitted["retention"]["critical"] is True
    filled = broker.process_market_tick(
        "2330.TW", {**market(price=94), "available_quantity": 10}
    )["results"][0]
    assert filled["order"]["status"] == "filled"
    assert filled["retention"]["kind"] == "execution_broker_state"

    reopened_retention = ContentAddressedRetentionLedger(
        store=SQLiteRetentionStore(store.path),
    )
    records = [
        record for record in reopened_retention._records.values()
        if record["kind"] == "execution_broker_state"
    ]
    assert len(records) == 2
    assert {record["payload"]["result"]["order"]["status"] for record in records} == {
        "open", "filled",
    }
    restarted = PaperBrokerSimulator(
        store=store,
        oms=PaperOMS(store=store, account_id="broker-retention"),
        retention_ledger=reopened_retention,
    )
    replayed = restarted.get_order("PB-RETENTION-1")
    assert replayed["order"]["status"] == "filled"
    assert reopened_retention.retained_counts()["critical"] == 2


def test_paper_broker_rejects_retention_store_on_a_different_database(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "paper-broker.sqlite")
    other = ContentAddressedRetentionLedger(
        store=SQLiteRetentionStore(tmp_path / "other.sqlite"),
    )
    with pytest.raises(ValueError, match="paper_broker_retention_must_share_runtime_database"):
        PaperBrokerSimulator(
            store=store,
            oms=PaperOMS(store=store, account_id="broker-retention-mismatch"),
            retention_ledger=other,
        )


@_PROPERTY_SETTINGS
@given(available_quantity=st.integers(min_value=0, max_value=10))
def test_generated_liquidity_paths_never_label_a_zero_fill_as_partial(available_quantity: int) -> None:
    """The durable lifecycle has one truthful state for every fill quantity."""

    with TemporaryDirectory(prefix="open-stock-ai-paper-broker-") as directory:
        store = SQLiteStore(db_path=Path(directory) / "broker.sqlite")
        oms = PaperOMS(
            store=store,
            account_id="broker-property",
            initial_cash=10_000.0,
            commission_bps=0.0,
            sell_tax_bps=0.0,
            slippage_bps=0.0,
            lot_size=1.0,
        )
        broker = PaperBrokerSimulator(store=store, oms=oms)
        order = broker.submit(
            {
                "order_id": "PB-PROPERTY-1",
                "symbol": "2330.TW",
                "market": "TW",
                "side": "buy",
                "order_type": "market",
                "time_in_force": "rod",
                "lot_type": "odd_lot",
                "session": "regular",
                "quantity_shares": 10,
                "actor": "property-test",
            },
            {**market(price=100), "available_quantity": available_quantity},
        )
        if available_quantity == 0:
            assert order["order"]["status"] == "open"
            assert order["order"]["filled_quantity"] == 0
        elif available_quantity < 10:
            assert order["order"]["status"] == "partially_filled"
            assert order["order"]["filled_quantity"] == available_quantity
        else:
            assert order["order"]["status"] == "filled"
            assert order["order"]["filled_quantity"] == 10
