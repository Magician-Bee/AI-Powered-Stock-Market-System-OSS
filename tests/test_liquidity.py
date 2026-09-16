from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stock_ai.main import app
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.storage.trade_store import TradeStore
from stock_ai.liquidity import (
    LiquidityAssessmentError,
    OfficialShareLedger,
    _persist_assessment,
    build_liquidity_assessment,
)


def history(count: int = 20) -> list[dict]:
    return [
        {
            "date": f"2026-07-{index + 1:02d}",
            "open": 99,
            "high": 101,
            "low": 98,
            "close": 100,
            "volume": 2_000_000,
            "turnover": 200_000_000,
        }
        for index in range(count)
    ]


def quote() -> dict:
    return {
        "provider": "twse_mis",
        "received_at": "2026-07-28T05:00:00Z",
        "total_volume_shares": 1_500_000,
        "bids": [{"price": 100.0, "size": 50}],
        "asks": [{"price": 100.1, "size": 40}],
    }


def official_shares(store: SQLiteStore) -> dict:
    return OfficialShareLedger(store).import_record(
        symbol="2330.TW",
        issued_common_shares=10_000_000_000,
        effective_date="2026-07-27",
        source_id="twse_openapi",
        source_url="https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        source_payload={
            "出表日期": "1150727",
            "公司代號": "2330",
            "已發行普通股數或TDR原股發行股數": "10000000000",
        },
        acquired_at="2026-07-28T04:00:00Z",
    )


def test_official_share_ledger_is_revisioned_idempotent_and_immutable(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "liquidity.sqlite")
    ledger = OfficialShareLedger(store)
    first = official_shares(store)
    same = official_shares(store)
    revised = ledger.import_record(
        symbol="2330.TW",
        issued_common_shares=10_100_000_000,
        effective_date="2026-07-28",
        source_id="twse_openapi",
        source_url="https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        source_payload={
            "出表日期": "1150728",
            "公司代號": "2330",
            "已發行普通股數或TDR原股發行股數": "10100000000",
        },
    )

    assert same["revision_id"] == first["revision_id"]
    assert revised["revision"] == 2
    assert revised["supersedes_revision_id"] == first["revision_id"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store._connect() as conn:
            conn.execute(
                "update official_share_revisions set issued_common_shares=1"
            )


def test_complete_inputs_compute_turnover_spread_participation_and_slippage(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "assessment.sqlite")
    shares = official_shares(store)
    result = build_liquidity_assessment(
        symbol="2330.TW",
        history_points=history(),
        history_source_ids=["twse_official_web"],
        quote=quote(),
        share_revision=shares,
        window_sessions=20,
        order_quantity_shares=10_000,
        assessed_at="2026-07-28T05:00:00Z",
    )
    _persist_assessment(store, result)

    assert result["metrics"]["average_daily_volume_shares"] == 2_000_000
    assert result["metrics"]["average_daily_turnover_twd"] == 200_000_000
    assert result["metrics"]["turnover_rate_percent"] == pytest.approx(0.015)
    assert result["metrics"]["bid_ask_spread_bps"] == pytest.approx(9.995, rel=1e-3)
    assert result["order_assessment"]["average_volume_participation_percent"] == 0.5
    assert result["order_assessment"]["estimated_slippage_bps"] > 0
    impact = result["order_assessment"]["market_impact_receipt"]
    assert impact["adv_volume_shares"] == 2_000_000
    assert impact["bar_volume_shares"] == 1_500_000
    # Liquidity inputs are complete, but this view has no effective-dated
    # venue/product/side impact calibration receipt.  It remains advisory
    # instead of promoting a generic square-root model to execution evidence.
    assert impact["execution_evidence_eligible"] is False
    assert result["metrics"]["realized_volatility_bps"] == 0
    assert result["tradability"]["status"] == "highly_tradeable"
    assert result["assessment_id"]

    with store._connect() as conn:
        row = conn.execute(
            "select tradability_status, share_revision_id from liquidity_assessments"
        ).fetchone()
    assert row == ("highly_tradeable", shares["revision_id"])


def test_missing_quote_shares_and_order_size_are_explicitly_unavailable():
    result = build_liquidity_assessment(
        symbol="2330.TW",
        history_points=history(),
        history_source_ids=["twse_official_web"],
        quote=None,
        share_revision=None,
    )

    assert result["metrics"]["turnover_rate_percent"] is None
    assert result["metrics"]["bid_ask_spread_bps"] is None
    assert result["order_assessment"]["estimated_slippage_bps"] is None
    assert result["tradability"]["status"] == "insufficient_data"
    assert "live_bid_ask_spread_unavailable" in result["tradability"]["blockers"]
    assert "order_quantity_required_for_slippage" in result["tradability"]["blockers"]


def test_liquidity_slippage_increases_for_larger_orders_and_lower_adv(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "impact.sqlite")
    shares = official_shares(store)
    liquid = build_liquidity_assessment(
        symbol="2330.TW",
        history_points=history(),
        history_source_ids=["twse_official_web"],
        quote=quote(),
        share_revision=shares,
        order_quantity_shares=10_000,
    )
    larger_order = build_liquidity_assessment(
        symbol="2330.TW",
        history_points=history(),
        history_source_ids=["twse_official_web"],
        quote=quote(),
        share_revision=shares,
        order_quantity_shares=100_000,
    )
    illiquid_history = [
        {**item, "volume": 200_000, "turnover": 20_000_000}
        for item in history()
    ]
    illiquid = build_liquidity_assessment(
        symbol="2330.TW",
        history_points=illiquid_history,
        history_source_ids=["twse_official_web"],
        quote=quote(),
        share_revision=shares,
        order_quantity_shares=10_000,
    )

    assert larger_order["order_assessment"]["estimated_slippage_bps"] > liquid["order_assessment"]["estimated_slippage_bps"]
    assert illiquid["order_assessment"]["estimated_slippage_bps"] > liquid["order_assessment"]["estimated_slippage_bps"]


def test_invalid_window_quantity_and_crossed_book_fail_closed(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "invalid.sqlite")
    with pytest.raises(LiquidityAssessmentError, match="window_sessions"):
        build_liquidity_assessment(
            symbol="2330.TW",
            history_points=history(),
            history_source_ids=[],
            quote=quote(),
            share_revision=official_shares(store),
            window_sessions=4,
        )
    crossed = quote()
    crossed["asks"][0]["price"] = 99
    result = build_liquidity_assessment(
        symbol="2330.TW",
        history_points=history(),
        history_source_ids=["twse_official_web"],
        quote=crossed,
        share_revision=official_shares(store),
        order_quantity_shares=1,
    )
    assert result["metrics"]["bid_ask_spread_bps"] is None
    assert result["tradability"]["status"] == "insufficient_data"


def test_liquidity_api_uses_unified_and_compatibility_routes(tmp_path, monkeypatch):
    trade_store = TradeStore(store=SQLiteStore(db_path=tmp_path / "api.sqlite"))
    engine = SimpleNamespace(pipeline=SimpleNamespace(trade_store=trade_store))
    monkeypatch.setattr("stock_ai.main.get_runtime_engine", lambda: engine)
    calls: list[dict] = []

    def fake_assessment(store, symbol, **options):
        calls.append({"store": store, "symbol": symbol, **options})
        return {
            "schema_version": "stock_ai.liquidity_assessment.v1",
            "symbol": symbol,
            "tradability": {"status": "tradeable"},
        }

    monkeypatch.setattr("stock_ai.main.assess_symbol_liquidity", fake_assessment)
    client = TestClient(app)
    unified = client.get(
        "/api/data/ui/v1/market/2330.TW/liquidity",
        params={"window_sessions": 20, "order_quantity_shares": 1000},
    )
    compatibility = client.get("/api/market/2330.TW/liquidity")

    assert unified.status_code == 200
    assert compatibility.status_code == 200
    assert unified.json()["tradability"]["status"] == "tradeable"
    assert calls[0]["order_quantity_shares"] == 1000
    assert calls[0]["store"] is trade_store.store


def test_liquidity_ui_does_not_render_null_metrics_as_zero():
    script = (
        Path(__file__).resolve().parents[1]
        / "src/stock_ai/ui/static/js/features/market-chart.js"
    ).read_text(encoding="utf-8")
    function = script.split("function liquidityNumber", 1)[1].split(
        "function renderLiquidityAssessment", 1
    )[0]

    assert "value === null" in function
    assert "value === undefined" in function
    assert "return '無資料'" in function
