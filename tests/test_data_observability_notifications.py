from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from stock_ai.data_platform import api as data_api
from stock_ai.data_platform.observability_notifications import ObservabilityNotificationLedger
from stock_ai.data_platform.service import MarketDataPlatform


def _alert(alert_id: str = "DSO-alert-1") -> dict:
    return {
        "alert_id": alert_id,
        "source_id": "twse_openapi",
        "dataset": "prices_daily",
        "severity": "error",
        "code": "missing_partition",
        "details": {"partition_key": "2026-08-20"},
    }


def test_missing_operator_sender_is_an_explicit_durable_receipt_and_retryable(tmp_path):
    ledger = ObservabilityNotificationLedger(tmp_path / "market.sqlite3")

    first = ledger.dispatch([_alert()], now="2026-08-26T01:00:00+00:00")
    assert first[0]["status"] == "not_configured"
    assert first[0]["reason"] == "operator_notification_sender_not_configured"
    assert len(ledger.list(alert_id="DSO-alert-1")) == 1

    delivered = ledger.dispatch(
        [_alert()],
        sender=lambda payload: {"accepted": True, "message_id": "ops-1", "payload_hash": hash(json.dumps(payload, sort_keys=True))},
        now="2026-08-26T01:01:00+00:00",
    )
    assert delivered[0]["status"] == "delivered"
    assert delivered[0]["attempt"] == 2
    assert len(ledger.list(alert_id="DSO-alert-1")) == 2

    again = ledger.dispatch(
        [_alert()],
        sender=lambda _payload: {"accepted": True, "message_id": "must-not-send"},
        now="2026-08-26T01:02:00+00:00",
    )
    assert again[0]["notification_id"] == delivered[0]["notification_id"]
    assert again[0]["attempt"] == 2


def test_notification_receipts_are_immutable_and_failed_provider_is_recorded(tmp_path):
    ledger = ObservabilityNotificationLedger(tmp_path / "market.sqlite3")
    failed = ledger.dispatch(
        [_alert("DSO-alert-2")],
        sender=lambda _payload: {"accepted": False, "error": "provider unavailable"},
        now="2026-08-26T01:00:00+00:00",
    )
    assert failed[0]["status"] == "failed"
    assert failed[0]["reason"] == "provider unavailable"

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with ledger._connect() as conn:
            conn.execute(
                "update data_observability_notifications set status='delivered' where notification_id=?",
                (failed[0]["notification_id"],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with ledger._connect() as conn:
            conn.execute(
                "delete from data_observability_notifications where notification_id=?",
                (failed[0]["notification_id"],),
            )


def test_market_data_platform_notify_endpoint_contract_keeps_external_gap_explicit(tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "platform.sqlite3")
    result = platform.notify_source_observability(as_of="2026-08-26T01:00:00+00:00")

    assert result["schema_version"] == "stock_ai.source_observability_dashboard.v1"
    assert result["notification_delivery_configured"] is False
    assert result["notification_attempt_count"] >= 1
    assert set(result["notification_status_counts"]) == {"not_configured"}
    assert all(item["receipt_sha256"] for item in result["notification_receipts"])


def test_market_data_platform_wires_configured_operator_sender_and_preserves_receipts(tmp_path):
    deliveries: list[dict] = []

    def sender(payload):
        deliveries.append(dict(payload))
        return {
            "accepted": True,
            "sink": "macos_notification_center",
            "message_sha256": "a" * 64,
            "acknowledgement": "os_request_accepted_only",
        }

    platform = MarketDataPlatform(
        database_path=tmp_path / "platform.sqlite3",
        observability_notification_sender=sender,
    )
    result = platform.notify_source_observability(as_of="2026-08-26T01:00:00+00:00")

    assert result["notification_delivery_configured"] is True
    assert result["notification_attempt_count"] == len(deliveries)
    assert result["notification_status_counts"] == {"delivered": len(deliveries)}
    assert all(item["status"] == "delivered" for item in result["notification_receipts"])
    assert all(
        item["provider_receipt"]["acknowledgement"] == "os_request_accepted_only"
        for item in result["notification_receipts"]
    )


def test_source_freshness_slo_uses_oldest_real_success_time_not_dashboard_time():
    dashboard = {
        "generated_at": "2026-08-28T12:00:00+00:00",
        "status": "passed",
        "sources": [
            {"source_id": "twse", "dataset": "prices", "lag_status": "passed", "latest_success_at": "2026-08-28T11:59:00+00:00"},
            {"source_id": "mops", "dataset": "financials", "lag_status": "passed", "latest_success_at": "2026-08-28T11:45:00+00:00"},
        ],
    }
    calls: list[dict] = []
    runtime = SimpleNamespace(record_slo_observation=lambda *args, **kwargs: calls.append({"args": args, **kwargs}) or {"status": "pass", "report_sha256": "verified"})

    result = data_api._record_source_freshness_slo(dashboard, runtime=runtime, latency_ms=12.5)

    assert result["status"] == "recorded"
    assert result["evidence"]["data_at"] == "2026-08-28T11:45:00+00:00"
    assert calls == [{"args": ("data.freshness",), "latency_ms": 12.5, "success": True, "data_at": datetime(2026, 8, 28, 11, 45, tzinfo=timezone.utc)}]


def test_source_freshness_slo_fails_closed_when_any_source_is_missing_or_stale():
    dashboard = {
        "status": "passed",
        "sources": [
            {"source_id": "twse", "dataset": "prices", "lag_status": "passed", "latest_success_at": "2026-08-28T11:59:00+00:00"},
            {"source_id": "mops", "dataset": "financials", "lag_status": "missing", "latest_success_at": None},
        ],
    }
    calls: list[dict] = []
    runtime = SimpleNamespace(record_slo_observation=lambda *args, **kwargs: calls.append({"args": args, **kwargs}) or {"status": "breach", "report_sha256": "verified"})

    result = data_api._record_source_freshness_slo(dashboard, runtime=runtime, latency_ms=5)

    assert result["evidence"]["unavailable_sources"] == ["mops:financials"]
    assert result["evidence"]["data_at"] is None
    assert calls[0]["success"] is False
    assert calls[0]["data_at"] is None
