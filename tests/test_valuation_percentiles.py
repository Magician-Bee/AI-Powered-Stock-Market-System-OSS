from datetime import date

from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai.valuation_percentiles import (
    VALUATION_PERCENTILE_SCHEMA_VERSION,
    ValuationHistoryStore,
    _month_targets,
    build_valuation_percentiles,
    parse_tpex_day,
    parse_twse_month,
    percentile_rank,
)


def test_exchange_parsers_preserve_historical_values_and_source_rows():
    twse = parse_twse_month(
        {
            "fields": ["日期", "殖利率(%)", "股利年度", "本益比", "股價淨值比", "財報年/季"],
            "data": [
                ["115年07月01日", "1.20", 114, "28.00", "9.00", "115/1"],
                ["115年07月29日", "1.00", 114, "29.58", "9.68", "115/1"],
            ],
        },
        symbol="2330.TW",
        source_url="https://www.twse.com.tw/example",
    )
    assert twse is not None
    assert (twse["date"], twse["pe"], twse["pb"], twse["dividend_yield"]) == (
        "2026-07-29",
        29.58,
        9.68,
        1.0,
    )
    assert twse["raw"]["row"][0] == "115年07月29日"

    tpex = parse_tpex_day(
        {
            "tables": [{
                "date": "114/07/29",
                "fields": ["股票代號", "公司名稱", "本益比", "每股股利", "股利年度", "殖利率(%)", "股價淨值比", "財報年/季"],
                "data": [["6488", "環球晶", "21.17", "11", 113, "3.20", "1.79", "114Q1"]],
            }]
        },
        symbol="6488.TWO",
        source_url="https://www.tpex.org.tw/example",
    )
    assert tpex is not None
    assert (tpex["date"], tpex["pe"], tpex["pb"], tpex["dividend_yield"]) == (
        "2025-07-29",
        21.17,
        1.79,
        3.2,
    )


def test_percentiles_use_midrank_and_exact_1_3_5_10_year_month_windows():
    targets = _month_targets(date(2026, 7, 29), 120)
    samples = [
        {
            "date": target.isoformat(),
            "symbol": "2330.TW",
            "source_id": "twse_official_web",
            "pe": float(index + 1),
            "pb": float(index + 1) / 10,
            "dividend_yield": float(index + 1) / 20,
            "raw_hash": f"hash-{index}",
        }
        for index, target in enumerate(targets)
    ]
    result = build_valuation_percentiles(symbol="2330.TW", samples=samples)
    assert result["schema_version"] == VALUATION_PERCENTILE_SCHEMA_VERSION
    assert result["status"] == "complete"
    assert result["coverage"]["sample_count"] == 120
    pe = next(item for item in result["metrics"] if item["code"] == "pe")
    assert [window["sample_count"] for window in pe["windows"]] == [12, 36, 60, 120]
    assert pe["windows"][-1]["percentile"] == 99.58
    assert pe["windows"][-1]["classification"] == "relative_high"
    assert percentile_rank([1, 2, 2, 3], 2) == 50.0
    assert result["truthfulness"]["unsupported_historical_metrics"] == [
        "ps",
        "ev_to_ebitda",
        "fcf_yield",
    ]


def test_history_store_keeps_raw_hash_and_does_not_duplicate_month(tmp_path):
    store = ValuationHistoryStore(SQLiteStore(tmp_path / "valuation.sqlite"))
    sample = {
        "symbol": "2330.TW",
        "date": "2026-07-29",
        "source_id": "twse_official_web",
        "source_url": "https://www.twse.com.tw/example",
        "pe": 29.58,
        "pb": 9.68,
        "dividend_yield": 1.0,
        "fiscal_period": "115/1",
        "raw": {"row": ["115年07月29日", "1.00", "29.58", "9.68"]},
    }
    store.import_sample(sample)
    store.import_sample(sample)
    rows = store.query("2330.TW")
    assert len(rows) == 1
    assert len(rows[0]["raw_hash"]) == 64


def test_incomplete_ten_year_window_is_not_reported_complete():
    samples = [
        {
            "date": f"2026-{month:02d}-01",
            "symbol": "2330.TW",
            "source_id": "twse_official_web",
            "pe": float(month),
            "pb": float(month),
            "dividend_yield": float(month),
            "raw_hash": f"hash-{month}",
        }
        for month in range(1, 13)
    ]
    result = build_valuation_percentiles(symbol="2330.TW", samples=samples)
    assert result["status"] == "partial"
    pe = next(item for item in result["metrics"] if item["code"] == "pe")
    assert pe["windows"][0]["status"] == "complete"
    assert pe["windows"][-1]["status"] == "partial"
