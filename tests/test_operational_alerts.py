from __future__ import annotations

import sqlite3

import pytest

from open_stock_ai.agent_runtime import (
    DurableOperationalAlertStore,
    OperationalAlertDispatcher,
    OperationalAlertRouter,
)


def test_operational_alerts_cover_safety_signals_and_are_hash_verified() -> None:
    router = OperationalAlertRouter(clock=lambda: "2026-08-26T00:00:00+00:00")
    alerts = router.evaluate(
        {
            "feed": {"freshness_seconds": 120},
            "reconciliation": {"mismatch_count": 1},
            "orders": {"unknown_count": 2},
            "database": {"pressure_ratio": 0.91},
            "model": {"status": "disabled", "blockers": ["feature_drift"]},
            "chaos": {"triggered": True, "fault": "network_partition"},
        }
    )

    assert {item.code for item in alerts} == {
        "feed_stale", "reconciliation_mismatch", "unknown_order",
        "database_pressure", "model_drift", "chaos_triggered",
    }
    assert all(item.verify() for item in alerts)
    assert all(item.stop_new_orders for item in alerts if item.code != "database_pressure" and item.code != "chaos_triggered")
    assert router.evaluate({
        "feed": {"freshness_seconds": 120},
        "reconciliation": {"mismatch_count": 1},
        "orders": {"unknown_count": 2},
        "database": {"pressure_ratio": 0.91},
        "model": {"status": "disabled", "blockers": ["feature_drift"]},
        "chaos": {"triggered": True, "fault": "network_partition"},
    }) == []


def test_missing_safety_signals_fail_closed_and_healthy_signals_are_quiet() -> None:
    router = OperationalAlertRouter(clock=lambda: "2026-08-26T00:00:00+00:00")
    missing = router.evaluate({})
    assert {item.code for item in missing} == {
        "feed_stale", "reconciliation_mismatch", "unknown_order", "database_pressure",
    }

    healthy = OperationalAlertRouter().evaluate(
        {
            "feed": {"freshness_seconds": 1},
            "reconciliation": {"mismatch_count": 0},
            "orders": {"unknown_count": 0},
            "database": {"pressure_ratio": 0.1},
            "model": {"status": "enabled", "blockers": []},
        }
    )
    assert healthy == []


def test_operational_alert_ledger_survives_restart_and_deduplicates(tmp_path) -> None:
    path = tmp_path / "operational-alerts.sqlite"
    first_store = DurableOperationalAlertStore(path)
    first_router = OperationalAlertRouter(
        clock=lambda: "2026-08-26T00:00:00+00:00", store=first_store
    )
    signals = {
        "feed": {"freshness_seconds": 120},
        "reconciliation": {"mismatch_count": 1},
        "orders": {"unknown_count": 0},
        "database": {"pressure_ratio": 0.1},
        "model": {"status": "enabled", "blockers": []},
    }
    first = first_router.evaluate(signals)
    assert [item.code for item in first] == ["feed_stale", "reconciliation_mismatch"]

    reopened_store = DurableOperationalAlertStore(path)
    reopened_router = OperationalAlertRouter(
        clock=lambda: "2026-08-26T00:01:00+00:00", store=reopened_store
    )
    assert reopened_router.evaluate(signals) == []
    persisted = reopened_store.alerts()
    assert {item.code for item in persisted} == {"feed_stale", "reconciliation_mismatch"}
    assert all(item.verify() for item in persisted)


def test_operational_alert_dispatch_persists_delivery_and_failures(tmp_path) -> None:
    store = DurableOperationalAlertStore(tmp_path / "operational-alerts.sqlite")
    router = OperationalAlertRouter(
        clock=lambda: "2026-08-26T00:00:00+00:00", store=store
    )
    alert = router.evaluate({"feed": {"freshness_seconds": 120}})[0]
    calls: list[dict] = []

    def sink(payload):
        calls.append(payload)
        return {"accepted": True, "incident_id": "INC-1"}

    dispatcher = OperationalAlertDispatcher(
        store,
        {"on_call": sink},
        clock=lambda: "2026-08-26T00:00:01+00:00",
    )
    deliveries = dispatcher.dispatch([alert])
    assert deliveries[0].status == "delivered"
    assert deliveries[0].verify()
    assert calls[0]["alert_id"] == alert.alert_id
    assert store.deliveries(alert_id=alert.alert_id)[0].provider_receipt["incident_id"] == "INC-1"

    # A delivered alert is not sent twice after a dispatcher restart.
    assert OperationalAlertDispatcher(store, {"on_call": sink}).dispatch([alert]) == []


def test_operational_alert_ledger_is_append_only(tmp_path) -> None:
    store = DurableOperationalAlertStore(tmp_path / "operational-alerts.sqlite")
    alert = OperationalAlertRouter(
        clock=lambda: "2026-08-26T00:00:00+00:00", store=store
    ).evaluate({"feed": {"freshness_seconds": 120}})[0]
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "delete from operational_alert_receipts where alert_id=?", (alert.alert_id,)
            )
