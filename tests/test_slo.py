from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from open_stock_ai.agent_runtime.slo import (
    SLOObservation,
    DurableSLOStore,
    SLORegistry,
    SLOTarget,
    evaluate_slo,
    verify_slo_report,
)


_NOW = datetime(2026, 8, 26, 4, 40, tzinfo=timezone.utc)


def test_slo_report_passes_latency_success_and_freshness_contract():
    target = SLOTarget("data.freshness", max_p95_latency_ms=100, min_success_ratio=0.9, max_freshness_seconds=60)
    observations = [
        SLOObservation(_NOW, 20, True, _NOW - timedelta(seconds=10)),
        SLOObservation(_NOW, 30, True, _NOW - timedelta(seconds=20)),
        SLOObservation(_NOW, 40, True, _NOW - timedelta(seconds=30)),
    ]

    report = evaluate_slo(target, observations, evaluated_at=_NOW)

    assert report.status == "pass"
    assert report.p95_latency_ms == 40
    assert report.success_ratio == 1.0
    assert report.max_freshness_seconds == 30
    assert report.verify() is True
    assert verify_slo_report(report.as_dict()) is True


def test_slo_registry_fails_closed_for_missing_and_breached_observations():
    registry = SLORegistry((SLOTarget("api.request", 50, 1.0), SLOTarget("data.freshness", 50, 1.0, 10)))
    reports = registry.evaluate(
        {"api.request": [SLOObservation(_NOW, 100, False)], "data.freshness": []},
        evaluated_at=_NOW,
    )

    assert reports["api.request"].status == "breach"
    assert reports["api.request"].breaches == ("p95_latency_slo_breached", "success_ratio_slo_breached")
    assert reports["data.freshness"].status == "no_data"
    assert reports["data.freshness"].breaches == ("slo_observations_missing",)


def test_tampered_slo_report_is_rejected():
    report = evaluate_slo(SLOTarget("agent.run", 100, 1.0), [SLOObservation(_NOW, 10, True)], evaluated_at=_NOW)
    payload = report.as_dict()
    payload["status"] = "pass" if payload["status"] != "pass" else "breach"
    assert verify_slo_report(payload) is False


def test_durable_slo_store_survives_restart_and_keeps_missing_samples_fail_closed(tmp_path):
    store = DurableSLOStore(tmp_path / "slo.sqlite")
    store.record_observation("api.request", SLOObservation(_NOW, 10, True))
    first = store.evaluate_and_record(
        SLORegistry((SLOTarget("api.request", 50, 1.0), SLOTarget("broker.feed", 50, 1.0))),
        evaluated_at=_NOW,
    )
    assert first["api.request"].status == "pass"
    assert first["broker.feed"].status == "no_data"
    assert store.dashboard()["missing_or_breached_services"] == ["broker.feed"]

    reopened = DurableSLOStore(store.path)
    assert len(reopened.observations()["api.request"]) == 1
    assert len(reopened.reports()) == 2
    assert reopened.dashboard()["dashboard_sha256"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with reopened._connect() as connection:
            connection.execute("delete from slo_reports")


def test_durable_slo_store_records_only_the_changed_service_report(tmp_path):
    store = DurableSLOStore(tmp_path / "slo-service.sqlite")
    registry = SLORegistry((
        SLOTarget("api.request", 50, 1.0),
        SLOTarget("order.lifecycle", 50, 1.0),
    ))
    store.record_observation("api.request", SLOObservation(_NOW, 10, True))

    report = store.evaluate_service_and_record(registry, "api.request", evaluated_at=_NOW)

    assert report.status == "pass"
    assert [item["service"] for item in store.reports()] == ["api.request"]
    assert registry.services() == ("api.request", "order.lifecycle")
    with pytest.raises(ValueError, match="unknown SLO service"):
        store.evaluate_service_and_record(registry, "unknown", evaluated_at=_NOW)
