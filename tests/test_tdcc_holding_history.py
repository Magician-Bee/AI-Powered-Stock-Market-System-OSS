from copy import deepcopy

import stock_ai.tdcc_holding_history as tdcc_module
from stock_ai.tdcc_holding_history import (
    TDCCHoldingHistoryStore,
    parse_tdcc_openapi_distribution,
    query_tdcc_holding_history,
)


def _official_rows(
    *,
    code: str = "2330",
    report_date: str = "20260724",
) -> list[dict[str, str]]:
    ratios = {
        1: 1.1,
        2: 2.2,
        3: 3.3,
        12: 4.0,
        13: 5.0,
        14: 6.0,
        15: 70.0,
        17: 100.0,
    }
    return [
        {
            "\ufeff資料日期": report_date,
            "證券代號": f"{code}  ",
            "持股分級": str(grade),
            "人數": str(100 * grade if grade != 17 else 3041119),
            "股數": str(1000 * grade if grade != 17 else 25932370067),
            "占集保庫存數比例%": str(ratios.get(grade, 0)),
        }
        for grade in range(1, 18)
    ]


def _record(
    report_date: str,
    *,
    symbol: str = "2330.TW",
    holders: int = 1000,
    major_ratio: float = 70.0,
) -> dict:
    rows = _official_rows(
        code=symbol.split(".", 1)[0],
        report_date=report_date.replace("-", ""),
    )
    result = parse_tdcc_openapi_distribution(rows, symbol)
    result["total_holders"] = holders
    result["major_holder_1000_lot_ratio"] = major_ratio
    return result


def test_parser_normalizes_official_rows_and_preserves_tpex_suffix():
    parsed = parse_tdcc_openapi_distribution(
        _official_rows(code="6488"),
        "6488.TWO",
    )

    assert parsed["report_date"] == "2026-07-24"
    assert parsed["symbol"] == "6488.TWO"
    assert parsed["total_holders"] == 3041119
    assert parsed["total_shares"] == 25932370067
    assert parsed["small_shareholder_count"] == 600
    assert parsed["small_shareholder_ratio"] == 6.6
    assert parsed["major_holder_1000_lot_ratio"] == 70.0
    assert parsed["holder_400_lot_ratio"] == 85.0
    assert parsed["concentration_score"] == 85.0
    assert len(parsed["distribution"]) == 17
    assert parsed["distribution"][14]["range"] == "1,000,001 股以上"
    assert len(parsed["raw_hash"]) == 64


def test_parser_rejects_missing_symbol_and_missing_total_grade():
    try:
        parse_tdcc_openapi_distribution(_official_rows(), "9999.TW")
    except ValueError as exc:
        assert "沒有 9999" in str(exc)
    else:
        raise AssertionError("missing symbol must fail")

    rows = [row for row in _official_rows() if row["持股分級"] != "17"]
    try:
        parse_tdcc_openapi_distribution(rows, "2330.TW")
    except ValueError as exc:
        assert "第 17 級" in str(exc)
    else:
        raise AssertionError("missing total grade must fail")


def test_history_query_preserves_weekly_snapshots_and_computes_changes(tmp_path):
    db_path = tmp_path / "tdcc.sqlite"
    store = TDCCHoldingHistoryStore(db_path)
    store.save(_record("2026-07-17", holders=1000, major_ratio=69.5))
    store.save(_record("2026-07-24", holders=1100, major_ratio=70.0))

    payload = query_tdcc_holding_history(
        "2330.TW",
        weeks=52,
        db_path=db_path,
    )

    assert payload["status"] == "operational_latest_only"
    assert payload["coverage"] == {
        "requested_weeks": 52,
        "count": 2,
        "complete_count": 2,
        "pit_certified_count": 0,
        "revision_count": 2,
        "first_report_date": "2026-07-17",
        "last_report_date": "2026-07-24",
    }
    assert payload["items"][-1]["week_over_week"]["total_holders"] == 100
    assert (
        payload["items"][-1]["week_over_week"][
            "major_holder_1000_lot_ratio"
        ]
        == 0.5
    )
    assert payload["summary"]["major_holder_1000_lot_trend"] == {
        "direction": "increase",
        "streak_weeks": 1,
    }
    assert payload["truthfulness"]["openapi_latest_snapshot_only"] is True
    assert payload["truthfulness"]["raw_rows_hash_preserved"] is True
    assert payload["truthfulness"]["historical_pit_eligible"] is False
    assert payload["truthfulness"]["revision_history_append_only"] is True
    assert payload["coverage"]["revision_count"] == 2


def test_refresh_stores_official_snapshot_and_populates_compatibility_cache(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "tdcc.sqlite"
    latest = _record("2026-07-24", holders=1234, major_ratio=71.25)
    monkeypatch.setattr(
        tdcc_module,
        "fetch_tdcc_holding_distribution",
        lambda _symbol: deepcopy(latest),
    )

    payload = query_tdcc_holding_history(
        "2330.TW",
        refresh=True,
        db_path=db_path,
    )

    assert payload["sync"]["status"] == "refreshed"
    assert payload["sync"]["official_grade_count"] == 17
    assert payload["summary"]["latest"]["total_holders"] == 1234
    assert payload["summary"]["latest"]["major_holder_1000_lot_ratio"] == 71.25

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        compatible = conn.execute(
            """
            select total_holders, major_holder_1000_lot_ratio
            from tdcc_holding_distribution
            where symbol='2330.TW'
            """
        ).fetchone()
    assert compatible == (1234, 71.25)


def test_history_store_keeps_distinct_same_week_revisions_append_only(tmp_path):
    db_path = tmp_path / "tdcc.sqlite"
    store = TDCCHoldingHistoryStore(db_path)
    first = _record("2026-07-24", holders=1000, major_ratio=70.0)
    second = _record("2026-07-24", holders=1200, major_ratio=71.0)
    # A changed normalized observation represents a different immutable
    # revision even if a fixture retained the same raw source hash.
    second["raw_hash"] = "b" * 64

    assert store.save(first)["created"] is True
    assert store.save(second)["created"] is True
    assert store.save(second)["created"] is False

    payload = query_tdcc_holding_history("2330.TW", db_path=db_path)
    assert payload["coverage"]["count"] == 1
    assert payload["coverage"]["revision_count"] == 2
    assert payload["items"][0]["total_holders"] == 1200
    assert payload["items"][0]["revision_sha256"]


def test_refresh_failure_serves_existing_history_without_inventing_rows(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "tdcc.sqlite"
    TDCCHoldingHistoryStore(db_path).save(_record("2026-07-24"))

    def fail(_symbol):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(tdcc_module, "fetch_tdcc_holding_distribution", fail)
    payload = query_tdcc_holding_history(
        "2330.TW",
        refresh=True,
        db_path=db_path,
    )

    assert payload["status"] == "stale_cache"
    assert payload["sync"] == {
        "status": "failed",
        "error_type": "RuntimeError",
        "message": "upstream unavailable",
    }
    assert payload["coverage"]["count"] == 1
    assert payload["truthfulness"]["missing_weeks_not_zero_filled"] is True
