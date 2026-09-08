#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai.trading_anomalies import (
    TradingAnomalyStore,
    detect_trading_anomalies,
)


def main() -> None:
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
    points.extend(
        [
            {
                "date": "2026-07-07",
                "open": 104,
                "high": 108,
                "low": 103,
                "close": 107,
                "volume": 3_000,
                "turnover": 321_000,
            },
            {
                "date": "2026-07-08",
                "open": 107,
                "high": 111,
                "low": 106,
                "close": 111,
                "volume": 1_200,
                "turnover": 133_200,
            },
        ]
    )
    events = detect_trading_anomalies(
        symbol="2330.TW",
        history_points=points,
        source_ids=["twse_official_web"],
        detected_at="2026-07-28T08:00:00Z",
    )
    types = {item["event_type"] for item in events}
    assert {
        "volume_spike",
        "gap",
        "rapid_move",
        "price_volume_divergence",
    }.issubset(types)

    with tempfile.TemporaryDirectory(prefix="stock008-") as directory:
        store = SQLiteStore(db_path=Path(directory) / "verify.sqlite")
        ledger = TradingAnomalyStore(store)
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
        event_id = events[0]["event_id"]
        ledger.track(event_id=event_id, status="acknowledged")
        ledger.track(event_id=event_id, status="resolved")
        listed = ledger.list_events(symbol="2330.TW")

        assert first["created_count"] == len(events)
        assert second["created_count"] == 0
        assert all(
            item["tracking"]["observation_count"] == 2
            for item in listed["items"]
        )
        assert next(
            item for item in listed["items"] if item["event_id"] == event_id
        )["tracking"]["status"] == "resolved"
        with store._connect() as conn:
            table_counts = {
                table: conn.execute(f"select count(*) from {table}").fetchone()[0]
                for table in (
                    "trading_anomaly_events",
                    "trading_anomaly_observations",
                    "trading_anomaly_tracking_actions",
                )
            }
        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.stock008_verification.v1",
                    "detected_types": sorted(types),
                    "event_count": len(events),
                    "repeat_scan_created_count": second["created_count"],
                    "observation_count_per_event": 2,
                    "tracked_status": "resolved",
                    "table_counts": table_counts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
