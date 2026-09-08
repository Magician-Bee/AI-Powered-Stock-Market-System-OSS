from __future__ import annotations

from dataclasses import dataclass
import sqlite3

import pytest

from stock_ai.brokers import (
    BrokerReconciliationService,
    ReconciliationReceiptStore,
    ReconciliationSchedule,
)


@dataclass
class _FakeOMS:
    results: list[dict]

    def reconcile_snapshot(self, snapshot, *, raw_event):
        self.results.append({"snapshot": snapshot, "raw_event": raw_event})
        return {
            "schema_version": "stock_ai.broker_order_reconciliation_result.v1",
            "trading_allowed": True,
            "applied_report_count": 1,
            "marked_unknown_intent_ids": [],
        }


def test_service_exposes_startup_periodic_and_event_driven_triggers_with_receipts() -> None:
    oms = _FakeOMS([])
    loaded: list[tuple[str, str]] = []

    def loader(broker_id: str, account_alias: str):
        loaded.append((broker_id, account_alias))
        return object(), object()

    service = BrokerReconciliationService(
        oms,
        loader,
        clock=lambda: "2026-08-26T00:00:00+00:00",
    )

    startup = service.run_startup([("fubon", "paper-01")])
    periodic = service.run_periodic([("fubon", "paper-01")])
    event = service.handle_event("broker.order.report", "EV-1", [("fubon", "paper-01")])
    duplicate = service.handle_event("broker.order.report", "EV-1", [("fubon", "paper-01")])

    assert service.triggers()["startup"] is True
    assert service.triggers()["periodic_seconds"] == 60
    assert startup[0].trigger == "startup"
    assert periodic[0].trigger == "periodic"
    assert event[0].trigger == "event:broker.order.report"
    assert duplicate == []
    assert all(receipt.status == "completed" and receipt.verify() for receipt in service.receipts())
    assert loaded == [("fubon", "paper-01")] * 3


def test_unknown_event_is_ignored_and_invalid_event_id_is_rejected() -> None:
    service = BrokerReconciliationService(_FakeOMS([]), lambda *_: (object(), object()))

    assert service.handle_event("unrelated.event", "EV-ignored", [("fubon", "paper-01")]) == []
    with pytest.raises(ValueError, match="event ID"):
        service.handle_event("broker.order.report", "", [("fubon", "paper-01")])


def test_schedule_rejects_an_interval_that_could_leave_open_orders_stale() -> None:
    with pytest.raises(ValueError, match="at least 60"):
        ReconciliationSchedule(periodic_seconds=59)


def test_reconciliation_receipts_and_event_claims_survive_service_restart(tmp_path) -> None:
    store = ReconciliationReceiptStore(tmp_path / "reconciliation.sqlite")
    calls: list[tuple[str, str]] = []

    def loader(broker_id: str, account_alias: str):
        calls.append((broker_id, account_alias))
        return object(), object()

    first = BrokerReconciliationService(
        _FakeOMS([]),
        loader,
        receipt_store=store,
        clock=lambda: "2026-08-26T00:00:00+00:00",
    )
    first.run_startup([("fubon", "paper-01")])
    first.handle_event("broker.order.report", "EV-DURABLE-1", [("fubon", "paper-01")])

    restarted = BrokerReconciliationService(
        _FakeOMS([]),
        loader,
        receipt_store=ReconciliationReceiptStore(store.path),
        clock=lambda: "2026-08-26T00:01:00+00:00",
    )
    assert len(restarted.receipts()) == 2
    assert restarted.handle_event(
        "broker.order.report", "EV-DURABLE-1", [("fubon", "paper-01")]
    ) == []
    assert len(restarted.run_periodic([("fubon", "paper-01")])) == 1
    assert all(receipt.verify() for receipt in ReconciliationReceiptStore(store.path).receipts())

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._connect() as connection:
            connection.execute("delete from broker_reconciliation_execution_receipts")
