from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai.main import app
from stock_ai.trading_anomalies import (
    TradingAnomalyError,
    TradingAnomalyStore,
    detect_trading_anomalies,
)
from open_stock_ai.storage.trade_store import TradeStore


def sample_history() -> list[dict]:
    points = [
        {
            "date": f"2026-07-{index + 1:02d}",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1_000,
            "turnover": 100_000,
        }
        for index in range(6)
    ]
    points.append(
        {
            "date": "2026-07-07",
            "open": 104,
            "high": 108,
            "low": 103,
            "close": 107,
            "volume": 3_000,
            "turnover": 321_000,
        }
    )
    points.append(
        {
            "date": "2026-07-08",
            "open": 107,
            "high": 111,
            "low": 106,
            "close": 111,
            "volume": 1_200,
            "turnover": 133_200,
        }
    )
    return points


def detected() -> list[dict]:
    return detect_trading_anomalies(
        symbol="2330.TW",
        history_points=sample_history(),
        source_ids=["twse_official_web"],
        detected_at="2026-07-28T08:00:00Z",
    )


def test_rules_detect_volume_gap_rapid_move_and_price_volume_divergence():
    events = detected()
    types = {(item["trading_date"], item["event_type"]) for item in events}

    assert ("2026-07-07", "volume_spike") in types
    assert ("2026-07-07", "gap") in types
    assert ("2026-07-07", "rapid_move") in types
    assert ("2026-07-08", "price_volume_divergence") in types
    assert all(item["source_ids"] == ["twse_official_web"] for item in events)
    assert all(item["detector_version"].endswith(".v1") for item in events)


def test_events_are_idempotent_immutable_and_scans_accumulate_observations(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "anomalies.sqlite")
    ledger = TradingAnomalyStore(store)
    events = detected()

    first = ledger.record_scan(
        symbol="2330.TW",
        events=events,
        source_ids=["twse_official_web"],
        observed_at="2026-07-28T08:00:00Z",
    )
    second = ledger.record_scan(
        symbol="2330.TW",
        events=events,
        source_ids=["twse_official_web"],
        observed_at="2026-07-28T09:00:00Z",
    )
    listed = ledger.list_events(symbol="2330.TW")

    assert first["created_count"] == len(events)
    assert second["created_count"] == 0
    assert second["existing_count"] == len(events)
    assert {item["tracking"]["observation_count"] for item in listed["items"]} == {2}
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._connect() as conn:
            conn.execute(
                "update trading_anomaly_events set title='overwritten'"
            )


def test_tracking_actions_are_append_only_and_latest_status_is_projected(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "tracking.sqlite")
    ledger = TradingAnomalyStore(store)
    event = detected()[0]
    ledger.record_scan(
        symbol="2330.TW",
        events=[event],
        source_ids=["twse_official_web"],
    )

    acknowledged = ledger.track(
        event_id=event["event_id"],
        status="acknowledged",
        note="人工檢視",
    )
    resolved = ledger.track(event_id=event["event_id"], status="resolved")
    listed = ledger.list_events(symbol="2330.TW")

    assert acknowledged["status"] == "acknowledged"
    assert resolved["status"] == "resolved"
    assert listed["items"][0]["tracking"]["status"] == "resolved"
    with store._connect() as conn:
        assert conn.execute(
            "select count(*) from trading_anomaly_tracking_actions"
        ).fetchone()[0] == 3
    with pytest.raises(TradingAnomalyError, match="status must"):
        ledger.track(event_id=event["event_id"], status="invented")


def test_anomaly_api_exposes_list_scan_and_tracking_routes(tmp_path, monkeypatch):
    trade_store = TradeStore(store=SQLiteStore(db_path=tmp_path / "api.sqlite"))
    engine = SimpleNamespace(pipeline=SimpleNamespace(trade_store=trade_store))
    monkeypatch.setattr("stock_ai.main.get_runtime_engine", lambda: engine)
    event = detected()[0]
    TradingAnomalyStore(trade_store.store).record_scan(
        symbol="2330.TW",
        events=[event],
        source_ids=["twse_official_web"],
    )

    def fake_scan(store, symbol, **options):
        assert store is trade_store.store
        return {
            "schema_version": "stock_ai.trading_anomaly_scan.v1",
            "symbol": symbol,
            "detected_count": 1,
            "created_count": 0,
            "events": [event],
        }

    monkeypatch.setattr("stock_ai.main.scan_symbol_anomalies", fake_scan)
    client = TestClient(app)
    listed = client.get("/api/data/ui/v1/market/2330.TW/anomalies")
    scanned = client.post(
        "/api/data/ui/v1/market/2330.TW/anomalies/scan",
        json={"refresh": False},
    )
    tracked = client.post(
        f"/api/data/ui/v1/market/2330.TW/anomalies/{event['event_id']}/tracking",
        json={"status": "acknowledged"},
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["event_type"] == event["event_type"]
    assert scanned.status_code == 200
    assert scanned.json()["detected_count"] == 1
    assert tracked.status_code == 200
    assert tracked.json()["status"] == "acknowledged"


def test_anomaly_ui_offers_real_scan_and_tracking_controls():
    root = Path(__file__).resolve().parents[1] / "src/stock_ai/ui/static"
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "js/features/market-chart.js").read_text(encoding="utf-8")

    assert 'id="scanTradingAnomalies"' in html
    assert 'id="tradingAnomalyList"' in html
    assert "/anomalies/scan" in script
    assert "/tracking" in script
    assert "掃描不會用假行情或模型推測補值" in script
