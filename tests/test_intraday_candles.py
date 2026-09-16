from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from stock_ai.intraday_candles import (
    FUGLE_SOURCE,
    INTRADAY_CANDLES_SCHEMA_VERSION,
    IntradayCandleStore,
    InvalidIntradayTimeframe,
    TWSE_QUOTE_SOURCE,
    YAHOO_SOURCE,
    normalize_fugle_candles,
)


def _day(
    trading_date: str,
    *,
    symbol: str = "2330.TW",
    base_price: float = 100,
) -> list[dict]:
    start = datetime.fromisoformat(f"{trading_date}T09:00:00+08:00")
    rows = []
    for index in range(270):
        timestamp = start + timedelta(minutes=index)
        open_price = base_price + index / 100
        close_price = open_price + (0.05 if index % 2 == 0 else -0.03)
        rows.append(
            {
                "symbol": symbol,
                "bucket_start": timestamp.isoformat(),
                "open": open_price,
                "high": max(open_price, close_price) + 0.02,
                "low": min(open_price, close_price) - 0.02,
                "close": close_price,
                "volume": index + 1,
                "turnover": (index + 1) * close_price * 1000,
                "average": (open_price + close_price) / 2,
                "is_final": True,
                "raw": {
                    "date": timestamp.isoformat(),
                    "serial": index,
                },
            }
        )
    return rows


def test_all_required_timeframes_rebuild_from_same_one_minute_day(
    tmp_path,
) -> None:
    store = IntradayCandleStore(tmp_path / "candles.sqlite")
    source_rows = _day("2026-07-24")
    receipt = store.record_candles(
        source_rows,
        source=FUGLE_SOURCE,
        is_complete=True,
        response_payload={"data": source_rows},
    )

    assert receipt["inserted_revision_count"] == 270
    expected_counts = {1: 270, 5: 54, 15: 18, 30: 9, 60: 5}
    for timeframe, expected_count in expected_counts.items():
        rebuilt = store.reconstruct(
            "2330.TW", "2026-07-24", timeframe=timeframe
        )
        assert rebuilt["schema_version"] == INTRADAY_CANDLES_SCHEMA_VERSION
        assert rebuilt["reconstructable"] is True
        assert rebuilt["reconstruction_status"] == "complete"
        assert rebuilt["source_one_minute_count"] == 270
        assert rebuilt["candle_count"] == expected_count
        assert len(rebuilt["points"]) == expected_count
        assert all(
            point["timeframe_minutes"] == timeframe
            for point in rebuilt["points"]
        )

    five = store.reconstruct(
        "2330.TW", "2026-07-24", timeframe=5
    )["points"][0]
    assert five["open"] == source_rows[0]["open"]
    assert five["close"] == source_rows[4]["close"]
    assert five["high"] == max(row["high"] for row in source_rows[:5])
    assert five["low"] == min(row["low"] for row in source_rows[:5])
    assert five["volume_lots"] == sum(
        row["volume"] for row in source_rows[:5]
    )
    assert five["one_minute_count"] == 5
    assert len(five["revision_ids"]) == 5

    sixty = store.reconstruct(
        "2330.TW", "2026-07-24", timeframe=60
    )["points"]
    assert sixty[-1]["bucket_start"].endswith("13:00:00+08:00")
    assert sixty[-1]["bucket_end"].endswith("13:30:00+08:00")
    assert sixty[-1]["one_minute_count"] == 30
    assert sixty[-1]["expected_one_minute_count"] == 30


def test_any_persisted_trading_day_can_be_selected_and_rebuilt(
    tmp_path,
) -> None:
    store = IntradayCandleStore(tmp_path / "candles.sqlite")
    for trading_date, price in (
        ("2026-07-23", 90),
        ("2026-07-24", 100),
    ):
        store.record_candles(
            _day(trading_date, base_price=price),
            source=YAHOO_SOURCE,
            is_complete=True,
        )

    dates = store.available_dates("2330")
    assert [item["trading_date"] for item in dates["dates"]] == [
        "2026-07-24",
        "2026-07-23",
    ]
    for trading_date in dates["dates"]:
        for timeframe in (1, 5, 15, 30, 60):
            rebuilt = store.reconstruct(
                "2330",
                trading_date["trading_date"],
                timeframe=timeframe,
            )
            assert rebuilt["reconstructable"] is True
            assert rebuilt["candle_count"] > 0


def test_source_priority_and_revision_history_are_not_silent_overwrites(
    tmp_path,
) -> None:
    store = IntradayCandleStore(tmp_path / "candles.sqlite")
    bucket = "2026-07-24T09:00:00+08:00"

    for source, close in (
        (TWSE_QUOTE_SOURCE, 99.5),
        (YAHOO_SOURCE, 100.0),
        (FUGLE_SOURCE, 100.5),
    ):
        store.record_candles(
            [
                {
                    "symbol": "2330.TW",
                    "bucket_start": bucket,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1,
                    "is_final": True,
                    "raw": {"source": source.source_id, "close": close},
                }
            ],
            source=source,
            is_complete=True,
        )

    rebuilt = store.reconstruct(
        "2330.TW", "2026-07-24", timeframe=1
    )
    assert rebuilt["points"][0]["close"] == 100.5
    assert rebuilt["points"][0]["source_ids"] == ["fugle_marketdata"]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "select count(*) from intraday_candle_revisions"
        ).fetchone()[0] == 3
        revision_id = connection.execute(
            "select revision_id from intraday_candle_revisions limit 1"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "update intraday_candle_revisions set close=0 "
                "where revision_id=?",
                (revision_id,),
            )


def test_point_in_time_rebuild_uses_revision_known_at_cutoff(
    tmp_path,
) -> None:
    store = IntradayCandleStore(tmp_path / "candles.sqlite")
    bucket = "2026-07-24T09:00:00+08:00"
    first_ingest = datetime(2026, 7, 24, 1, 2, tzinfo=timezone.utc)
    second_ingest = datetime(2026, 7, 24, 1, 4, tzinfo=timezone.utc)
    for close, completed in ((100.0, first_ingest), (101.0, second_ingest)):
        store.record_candles(
            [
                {
                    "symbol": "2330.TW",
                    "bucket_start": bucket,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1,
                    "is_final": True,
                    "raw": {"close": close},
                }
            ],
            source=FUGLE_SOURCE,
            is_complete=True,
            requested_at=completed,
            completed_at=completed,
        )

    historical = store.reconstruct(
        "2330.TW",
        "2026-07-24",
        timeframe=1,
        as_of=datetime(2026, 7, 24, 1, 3, tzinfo=timezone.utc),
    )
    current = store.reconstruct(
        "2330.TW", "2026-07-24", timeframe=1
    )
    assert historical["points"][0]["close"] == 100
    assert current["points"][0]["close"] == 101
    assert historical["latest_import_receipt"]["completed_at"] == (
        first_ingest.isoformat(timespec="microseconds")
    )
    assert current["latest_import_receipt"]["completed_at"] == (
        second_ingest.isoformat(timespec="microseconds")
    )


def test_fugle_period_end_timestamp_becomes_canonical_minute_start(
    tmp_path,
) -> None:
    payload = {
        "symbol": "2330",
        "exchange": "TWSE",
        "timeframe": "1",
        "data": [
            {
                "date": "2026-07-24T09:01:00.000+08:00",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 25,
                "average": 100.2,
            }
        ],
    }
    normalized = normalize_fugle_candles(payload)
    store = IntradayCandleStore(tmp_path / "candles.sqlite")
    store.record_candles(
        normalized,
        source=FUGLE_SOURCE,
        is_complete=True,
        response_payload=payload,
    )

    rebuilt = store.reconstruct(
        "2330.TW", "2026-07-24", timeframe=1
    )
    assert rebuilt["points"][0]["bucket_start"].endswith(
        "09:00:00+08:00"
    )
    assert rebuilt["points"][0]["bucket_end"].endswith(
        "09:01:00+08:00"
    )


def test_quote_samples_require_actual_trade_and_never_use_midpoint(
    tmp_path,
) -> None:
    store = IntradayCandleStore(tmp_path / "candles.sqlite")
    no_trade = {
        "schema_version": "stock_ai.realtime_quote.v1",
        "provider": "twse_mis",
        "symbol": "2330",
        "exchange": "tse",
        "exchange_timestamp": "2026-07-24T09:01:30+08:00",
        "last_price": None,
        "best_bid": {"price": 100, "size": 1},
        "best_ask": {"price": 101, "size": 1},
    }
    assert store.record_quote(no_trade) is None
    assert store.available_dates("2330")["count"] == 0

    trade = {
        **no_trade,
        "last_price": 100.5,
        "last_trade_size_lots": 2,
        "total_volume_lots": 120,
        "sequence": 1,
    }
    store.record_quote(trade)
    rebuilt = store.reconstruct(
        "2330.TW", "2026-07-24", timeframe=1
    )
    assert rebuilt["points"][0]["close"] == 100.5
    assert rebuilt["points"][0]["volume_lots"] == 2
    assert rebuilt["reconstruction_status"] == "partial"


def test_invalid_timeframe_fails_closed(tmp_path) -> None:
    store = IntradayCandleStore(tmp_path / "candles.sqlite")
    with pytest.raises(InvalidIntradayTimeframe):
        store.reconstruct("2330", "2026-07-24", timeframe=10)
