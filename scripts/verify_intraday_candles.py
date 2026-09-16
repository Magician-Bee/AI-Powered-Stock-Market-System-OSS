#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from stock_ai import intraday_candles
from stock_ai import main as main_module
from stock_ai.intraday_candles import (
    FUGLE_SOURCE,
    IntradayCandleStore,
)


ROOT = Path(__file__).resolve().parents[1]
TAIPEI = ZoneInfo("Asia/Taipei")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def full_session(trading_date: str) -> list[dict]:
    start = datetime.fromisoformat(
        f"{trading_date}T09:00:00"
    ).replace(tzinfo=TAIPEI)
    rows: list[dict] = []
    for minute in range(270):
        bucket_start = start + timedelta(minutes=minute)
        opening = 100 + minute / 100
        rows.append(
            {
                "symbol": "2330.TW",
                "bucket_start": bucket_start.isoformat(timespec="seconds"),
                "open": opening,
                "high": opening + 0.02,
                "low": opening - 0.02,
                "close": opening + 0.01,
                "volume": minute + 1,
                "turnover": (minute + 1) * (opening + 0.01),
                "is_final": True,
                "raw": {
                    "minute": minute,
                    "fixture": "STOCK-002",
                },
            }
        )
    return rows


def main() -> int:
    with TemporaryDirectory(prefix="stock-ai-intraday-candles-") as temp:
        database = Path(temp) / "intraday-candles.sqlite"
        store = IntradayCandleStore(database)
        store.record_candles(
            full_session("2026-07-24"),
            source=FUGLE_SOURCE,
            is_complete=True,
            requested_at=datetime(
                2026, 7, 24, 6, 0, tzinfo=TAIPEI
            ),
            completed_at=datetime(
                2026, 7, 24, 6, 1, tzinfo=TAIPEI
            ),
            metadata={"fixture": "STOCK-002 full session"},
        )
        store.record_candles(
            full_session("2026-07-27"),
            source=FUGLE_SOURCE,
            is_complete=True,
            metadata={"fixture": "STOCK-002 second trading day"},
        )

        expected_counts = {1: 270, 5: 54, 15: 18, 30: 9, 60: 5}
        rebuilt = {
            timeframe: store.reconstruct(
                "2330.TW",
                "2026-07-24",
                timeframe=timeframe,
            )
            for timeframe in expected_counts
        }
        for timeframe, expected in expected_counts.items():
            payload = rebuilt[timeframe]
            require(
                payload["candle_count"] == expected,
                f"{timeframe}m rebuilt {payload['candle_count']} candles, "
                f"expected {expected}",
            )
            require(
                payload["source_one_minute_count"] == 270,
                f"{timeframe}m did not use all persisted one-minute candles",
            )
            require(
                payload["reconstruction_status"] == "complete",
                f"{timeframe}m was not marked complete",
            )
            require(
                payload["missing_session_minute_count"] == 0,
                f"{timeframe}m invented or lost regular-session minutes",
            )

        first_five = rebuilt[5]["points"][0]
        source_rows = full_session("2026-07-24")[:5]
        require(first_five["open"] == source_rows[0]["open"], "5m open mismatch")
        require(first_five["close"] == source_rows[-1]["close"], "5m close mismatch")
        require(
            first_five["high"] == max(row["high"] for row in source_rows),
            "5m high mismatch",
        )
        require(
            first_five["low"] == min(row["low"] for row in source_rows),
            "5m low mismatch",
        )
        require(
            first_five["volume_lots"]
            == sum(row["volume"] for row in source_rows),
            "5m volume mismatch",
        )
        require(
            rebuilt[60]["points"][-1]["expected_one_minute_count"] == 30,
            "13:00–13:30 partial 60m session bucket is incorrect",
        )

        dates = store.available_dates("2330")
        require(
            [item["trading_date"] for item in dates["dates"]]
            == ["2026-07-27", "2026-07-24"],
            "persisted trading dates are not independently reconstructable",
        )

        original_store = intraday_candles.get_intraday_candle_store
        intraday_candles.get_intraday_candle_store = lambda: store
        try:
            client = TestClient(main_module.app)
            paths = (
                "/api/data/ui/v1/intraday/candles/2330.TW"
                "?date=2026-07-24&timeframe=60",
                "/api/intraday/candles/2330.TW"
                "?date=2026-07-24&timeframe=60",
                "/api/data/ui/v1/intraday/candles/2330.TW/dates",
            )
            responses = [client.get(path) for path in paths]
        finally:
            intraday_candles.get_intraday_candle_store = original_store
        require(
            all(response.status_code == 200 for response in responses),
            "unified or compatibility intraday API failed",
        )
        require(
            responses[0].json()["points"] == responses[1].json()["points"],
            "unified and compatibility APIs disagree",
        )

        market_chart = (
            ROOT / "src/stock_ai/ui/static/js/features/market-chart.js"
        ).read_text(encoding="utf-8")
        index_html = (
            ROOT / "src/stock_ai/ui/static/index.html"
        ).read_text(encoding="utf-8")
        for required in (
            "initIntradayCandleControls",
            "loadIntradayCandles",
            "'candle'",
            "指定交易日重建",
        ):
            require(required in market_chart, f"UI is missing {required}")
        require(
            "20260728-intraday-candles-v2" in index_html,
            "intraday chart cache version was not advanced",
        )

        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.stock_002_verification.v1",
                    "status": "passed",
                    "database": str(database),
                    "base_timeframe_minutes": 1,
                    "trading_dates": [
                        item["trading_date"] for item in dates["dates"]
                    ],
                    "reconstructed_counts": expected_counts,
                    "regular_session_minutes": 270,
                    "api_statuses": [
                        response.status_code for response in responses
                    ],
                    "source": FUGLE_SOURCE.source_id,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
