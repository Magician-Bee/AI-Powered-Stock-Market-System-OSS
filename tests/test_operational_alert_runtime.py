from __future__ import annotations

from datetime import datetime, timezone

from open_stock_ai.agent_runtime.operational_alert_runtime import OperationalAlertRuntime
from open_stock_ai.agent_runtime.operational_alerts import OperationalAlertPolicy
from open_stock_ai.agent_runtime.chaos_recovery_runtime import ChaosRecoveryRuntime


def _storage() -> dict:
    return {
        "healthy": True,
        "database": {"total_bytes": 1024},
        "runtime_events": {"pending": 0},
    }


def test_runtime_alert_dashboard_persists_current_broker_safety_alerts(tmp_path) -> None:
    now = datetime(2026, 8, 29, 0, 2, tzinfo=timezone.utc)
    runtime = OperationalAlertRuntime(
        tmp_path / "agent-runtime.sqlite",
        storage_report=_storage,
        broker_snapshot=lambda: {
            "metrics": [{
                "broker_id": "taishin",
                "observed_at": "2026-08-29T00:00:00+00:00",
                "unknown_order_count": 2,
                "order_reconciliation_mismatch_count": 1,
                "account_reconciliation_mismatch_count": 0,
            }],
        },
        model_monitor=lambda: {"status": "enabled", "blockers": []},
        policy=OperationalAlertPolicy(feed_stale_seconds=60, database_pressure_ratio=1.0),
        clock=lambda: now,
    )

    first = runtime.dashboard()
    assert {item["code"] for item in first["current_alerts"]} == {
        "feed_stale", "reconciliation_mismatch", "unknown_order",
    }
    assert first["order_gate"]["live_submission"]["blocked"] is True
    assert first["order_gate"]["paper_sandbox"]["blocked"] is False
    assert all(item["receipt_sha256"] for item in first["new_alerts"])

    second = runtime.dashboard()
    assert second["new_alerts"] == []
    assert {item["code"] for item in second["recent_alerts"]} == {
        "feed_stale", "reconciliation_mismatch", "unknown_order",
    }


def test_runtime_marks_unconfigured_broker_scopes_without_fabricating_alerts(tmp_path) -> None:
    runtime = OperationalAlertRuntime(
        tmp_path / "agent-runtime.sqlite",
        storage_report=_storage,
        broker_snapshot=lambda: {"metrics": []},
        model_monitor=lambda: {"status": "enabled", "blockers": []},
        policy=OperationalAlertPolicy(database_pressure_ratio=1.0),
    )

    dashboard = runtime.dashboard()
    assert dashboard["current_alerts"] == []
    assert dashboard["coverage"]["feed"]["status"] == "not_configured"
    assert dashboard["coverage"]["reconciliation"]["status"] == "not_configured"
    assert dashboard["coverage"]["orders"]["status"] == "not_configured"
    assert dashboard["delivery"]["configured"] is False


def test_runtime_projects_blocked_durable_chaos_campaign_into_operational_alerts(tmp_path) -> None:
    database = tmp_path / "agent-runtime.sqlite"
    chaos = ChaosRecoveryRuntime(database)
    chaos.record_catalog(
        {
            "timeout": {
                "invariants": {
                    "durable_state_recovered": True,
                    "new_orders_blocked_until_safe": False,
                    "operator_receipt_written": True,
                }
            },
        },
        seed=10,
        campaign_id="blocked-timeout",
    )
    runtime = OperationalAlertRuntime(
        database,
        storage_report=_storage,
        broker_snapshot=lambda: {"metrics": []},
        model_monitor=lambda: {"status": "enabled", "blockers": []},
        chaos_runtime=chaos,
        policy=OperationalAlertPolicy(database_pressure_ratio=1.0),
    )

    dashboard = runtime.dashboard()

    assert dashboard["chaos_recovery"]["status"] == "blocked"
    assert dashboard["coverage"]["chaos"]["status"] == "observed"
    assert {item["code"] for item in dashboard["current_alerts"]} == {"chaos_triggered"}
