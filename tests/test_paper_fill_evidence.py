from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3

import pytest

from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.storage.sqlite_store import SQLiteStore


NOW = datetime(2026, 9, 11, 2, tzinfo=timezone.utc)


def market(price=100, seconds=0, **kwargs):
    timestamp = (NOW + timedelta(seconds=seconds)).isoformat()
    return {"symbol": "2330.TW", "market": "TW", "price": price, "source_timestamp": timestamp,
            "price_source": "explicit_test_fixture", "is_realtime": True, "is_fallback": False,
            "odd_lot_auction_matched": True,
            "source_envelope": {"provider_id": "fixture", "exchange_timestamp": timestamp,
                                "received_at": (NOW + timedelta(seconds=seconds, milliseconds=100)).isoformat(),
                                "realtime": True}, **kwargs}


def ticket(**kwargs):
    return {"order_id": "FILL-EVIDENCE-ORDER", "symbol": "2330.TW", "market": "TW", "side": "buy",
            "order_type": "limit", "limit_price": 100, "quantity_shares": 10,
            "time_in_force": "rod", "lot_type": "odd_lot", "session": "regular", **kwargs}


def broker_fixture(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "paper-fill-evidence.sqlite")
    clock = {"now": NOW}
    monkeypatch.setattr(PaperOMS, "_now", lambda self: clock["now"].isoformat())
    monkeypatch.setattr(PaperBrokerSimulator, "_now", lambda self: clock["now"].isoformat())
    oms = PaperOMS(store, account_id="fill-evidence-test", initial_cash=10_000, read_environment=False)
    context = {"deployment_receipt": {"schema_version": "open_stock_ai.autonomous_deployment_receipt.v1", "fixture": True},
               "execution_model": {"untrusted_override": "must_not_replace_actual_broker_model"}}
    broker = PaperBrokerSimulator(store, oms, host_execution_context=context)
    return store, clock, broker


def fills(broker):
    return asyncio.run(PaperBrokerPort(broker).fills([ticket()["order_id"]]))


def metadata_rows(store):
    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "select * from cash_ledger where order_id=? order by id", (ticket()["order_id"],)).fetchall()]


@pytest.mark.parametrize("second_quantity", [3, 6])
def test_partial_fill_evidence_survives_oms_commit_before_broker_event_crash(tmp_path, monkeypatch, second_quantity):
    store, clock, broker = broker_fixture(tmp_path, monkeypatch)
    first_market, second_market = market(100, available_quantity=4), market(99, 1, available_quantity=second_quantity)
    broker.submit(ticket(), first_market)
    first_evidence = fills(broker)[0]["fill_evidence"]
    original_event = broker._event

    def crash_before_event(conn, **kwargs):
        if kwargs["event_type"] in {"filled", "partially_filled"}:
            raise RuntimeError("crash_after_oms_commit_before_broker_event")
        return original_event(conn, **kwargs)

    monkeypatch.setattr(broker, "_event", crash_before_event)
    clock["now"] = NOW + timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="crash_after_oms_commit"):
        broker.process_market_tick("2330.TW", second_market)
    persisted = fills(broker)
    assert len(persisted) == 2
    assert persisted[0]["fill_evidence"] == first_evidence
    for index, (fill, quote) in enumerate(zip(persisted, (first_market, second_market)), 1):
        evidence = fill["fill_evidence"]
        assert evidence["market_context"] == quote
        assert evidence["fill"]["fill_id"] == fill["fill_id"]
        assert evidence["execution_context"]["execution_model"]["fill_sequence"] == index
        assert "untrusted_override" not in evidence["execution_context"]["execution_model"]
        assert fill["fill_evidence_verification"]["integrity_status"] == "verified"
        assert fill["fill_evidence_verification"]["source_status"] == "unknown"
    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_broker_order_events where event_type in ('filled','partially_filled')").fetchone()[0] == 1
    restarted = PaperBrokerSimulator(store, PaperOMS(store, account_id=broker.oms.account_id, read_environment=False))
    recovered = restarted.get_order(ticket()["order_id"])
    assert recovered["order"]["filled_quantity"] == 4 + second_quantity
    assert recovered["order"]["status"] == ("filled" if second_quantity == 6 else "partially_filled")
    if second_quantity == 6:
        assert recovered["order"]["filled_at"] == (NOW + timedelta(seconds=1)).isoformat()
    restarted.get_order(ticket()["order_id"])
    assert len([event for event in restarted.get_order(ticket()["order_id"])["events"] if event["event_type"] == "fills_reconciled"]) == 1
    restarted.process_market_tick("2330.TW", market(98, 2, available_quantity=0))
    assert fills(restarted) == persisted
    assert restarted.oms.portfolio_summary()["cash_balance"] == 10_000 - 400 - 99 * second_quantity


def test_ioc_partial_commit_crash_recovers_cancellation_without_filling_remainder(tmp_path, monkeypatch):
    store, clock, broker = broker_fixture(tmp_path, monkeypatch)
    original_event = broker._event

    def crash(conn, **kwargs):
        if kwargs["event_type"] == "partially_filled_then_canceled":
            raise RuntimeError("crash_after_ioc_fill")
        return original_event(conn, **kwargs)

    monkeypatch.setattr(broker, "_event", crash)
    with pytest.raises(RuntimeError, match="crash_after_ioc_fill"):
        broker.submit(ticket(time_in_force="ioc", order_type="market", lot_type="board_lot"), market(100, available_quantity=4))
    restarted = PaperBrokerSimulator(store, PaperOMS(store, account_id=broker.oms.account_id, read_environment=False))
    result = restarted.get_order(ticket()["order_id"])
    assert result["order"]["status"] == "canceled"
    assert result["order"]["filled_quantity"] == 4
    restarted.process_market_tick("2330.TW", market(99, 1, available_quantity=10))
    assert len(fills(restarted)) == 1
    assert restarted.oms.portfolio_summary()["cash_balance"] == 9600


def test_duplicate_fill_does_not_replace_committed_evidence(tmp_path, monkeypatch):
    store, _, broker = broker_fixture(tmp_path, monkeypatch)
    order = {"order_id": ticket()["order_id"], "symbol": "2330.TW", "market": "TW", "action": "buy",
             "entry_price": 100, "position_size_pct": 10, "risk_approved": True, "market_context": market(100)}
    broker.oms.submit_partial_fill(order, fill_quantity=4, fill_id="EXPLICIT-FILL-1", execution_context={"execution_model": {"fixture": 1}})
    original = metadata_rows(store)
    duplicate = broker.oms.submit_partial_fill({**order, "market_context": market(99, 1)}, fill_quantity=4,
        fill_id="EXPLICIT-FILL-1", execution_context={"execution_model": {"fixture": "changed"}})
    assert duplicate["idempotent"] is True
    assert metadata_rows(store) == original
    assert len(fills(broker)) == 1


def test_direct_oms_preserves_supported_native_context_but_does_not_trust_ticket_claims(tmp_path, monkeypatch):
    _, _, broker = broker_fixture(tmp_path, monkeypatch)
    context = {**market(100), "source_time": NOW, "session_date": date(2026, 9, 11), "exact_decimal": Decimal("0.1234567890123456789")}
    broker.oms.submit_and_fill({"order_id": ticket()["order_id"], "symbol": "2330.TW", "market": "TW", "action": "buy",
        "entry_price": 100, "position_size_pct": 10, "risk_approved": True, "market_context": context,
        "execution_context": {"execution_model": {"claimed_real": True}, "deployment_receipt": {"claimed_verified": True}}})
    retained = fills(broker)[0]
    assert retained["fill_evidence"]["market_context"]["source_time"] == NOW.isoformat()
    assert retained["fill_evidence"]["market_context"]["session_date"] == "2026-09-11"
    assert retained["fill_evidence"]["market_context"]["exact_decimal"] == "0.1234567890123456789"
    assert retained["fill_evidence"]["execution_context"] == {}
    assert retained["fill_evidence_verification"]["source_status"] == "unknown"
    assert retained["fill_evidence_verification"]["execution_model_status"] == "unknown"
    assert retained["fill_evidence_verification"]["deployment_status"] == "unknown"
    broker.oms.submit_and_fill({"order_id": "NATIVE-CONTEXT-SELL", "symbol": "2330.TW", "market": "TW", "action": "sell",
        "entry_price": 100, "position_size_pct": 100, "risk_approved": True, "market_context": context})
    sold = asyncio.run(PaperBrokerPort(broker).fills(["NATIVE-CONTEXT-SELL"]))[0]
    assert sold["side"] == "sell"
    assert sold["fill_evidence"]["market_context"] == retained["fill_evidence"]["market_context"]
    assert sold["fill_evidence_verification"]["integrity_status"] == "verified"


def test_fill_projection_uses_one_read_snapshot_during_concurrent_ledger_commit(tmp_path, monkeypatch):
    store, _, broker = broker_fixture(tmp_path, monkeypatch)
    broker.submit(ticket(), market())
    original_connect = store._connect
    changed = False

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            nonlocal changed
            rows = self.cursor.fetchall()
            if not changed:
                changed = True
                with original_connect() as writer:
                    writer.execute("update cash_ledger set amount=amount+1 where order_id=?", (ticket()["order_id"],))
            return rows

    class Connection:
        def __init__(self):
            self.conn = original_connect()

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        @property
        def row_factory(self):
            return self.conn.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.conn.row_factory = value

        def execute(self, sql, *args):
            cursor = self.conn.execute(sql, *args)
            return Cursor(cursor) if sql.startswith("select f.* from paper_fills") else cursor

    monkeypatch.setattr(store, "_connect", Connection)
    assert fills(broker)[0]["fill_evidence_verification"]["integrity_status"] == "verified"
    assert changed
    assert fills(broker)[0]["fill_evidence_verification"]["integrity_status"] == "invalid"


@pytest.mark.parametrize("damage,expected", [
    ("market", "invalid"), ("identity", "invalid"), ("missing_receipt", "missing"),
    ("invalid_json", "invalid"), ("cash_amount", "invalid"), ("cash_time", "invalid"),
    ("duplicate_cash", "invalid"), ("missing_cash", "missing"),
])
def test_missing_or_damaged_evidence_never_borrows_order_submission_market(tmp_path, monkeypatch, damage, expected):
    store, _, broker = broker_fixture(tmp_path, monkeypatch)
    broker.submit(ticket(), market())
    entry = metadata_rows(store)[0]
    metadata = json.loads(entry["metadata_json"])
    if damage == "market":
        metadata["fill_evidence"]["market_context"]["price"] = 999
    elif damage == "identity":
        metadata["fill_evidence"]["fill"]["account_id"] = "another-account"
    elif damage == "missing_receipt":
        metadata.pop("fill_evidence")
    with store._connect() as conn:
        if damage == "cash_amount":
            conn.execute("update cash_ledger set amount=amount+1 where id=?", (entry["id"],))
        elif damage == "cash_time":
            conn.execute("update cash_ledger set created_at=? where id=?", ("2020-01-01T00:00:00+00:00", entry["id"]))
        elif damage == "duplicate_cash":
            conn.execute("insert into cash_ledger(account_id,order_id,created_at,entry_type,amount,balance_after,currency,metadata_json) select account_id,order_id,created_at,entry_type,amount,balance_after,currency,metadata_json from cash_ledger where id=?", (entry["id"],))
        elif damage == "missing_cash":
            conn.execute("delete from cash_ledger where id=?", (entry["id"],))
        else:
            conn.execute("update cash_ledger set metadata_json=? where id=?", ("{invalid" if damage == "invalid_json" else json.dumps(metadata), entry["id"]))
    inspected = fills(broker)[0]
    assert inspected["fill_evidence"] is None
    assert inspected["fill_evidence_verification"]["integrity_status"] == expected
    assert inspected["fill_evidence_verification"]["source_status"] == "unknown"


def test_failed_evidence_serialization_rolls_back_fill_and_cash_together(tmp_path, monkeypatch):
    store, _, broker = broker_fixture(tmp_path, monkeypatch)
    with pytest.raises(TypeError, match="unsupported_paper_fill_evidence_value"):
        broker.oms.submit_partial_fill({"order_id": ticket()["order_id"], "symbol": "2330.TW", "action": "buy",
            "entry_price": 100, "position_size_pct": 10, "risk_approved": True}, fill_quantity=4,
            execution_context={"unsupported": object()})
    assert fills(broker) == []
    assert metadata_rows(store) == []
    assert broker.oms.portfolio_summary()["cash_balance"] == 10_000


@pytest.mark.parametrize("field,value", [("account_id", "unrelated-account"), ("symbol", "2317.TW"), ("requested_quantity", 20)])
def test_reconciliation_rejects_mismatched_oms_contract_without_new_fills(tmp_path, monkeypatch, field, value):
    store, _, broker = broker_fixture(tmp_path, monkeypatch)
    broker.submit(ticket(), market(available_quantity=4))
    if field == "account_id":
        PaperOMS(store, account_id=value, read_environment=False)
    with store._connect() as conn:
        # This isolated corruption fixture never touches a running account.
        assert field in {"account_id", "symbol", "requested_quantity"}
        conn.execute(f"update paper_orders set {field}=? where order_id=?", (value, ticket()["order_id"]))
    with pytest.raises(ValueError, match="paper_broker_oms_contract_mismatch"):
        broker.get_order(ticket()["order_id"])
    assert len(fills(broker)) == 1
