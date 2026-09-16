from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stock_ai.growth_metrics import (
    GROWTH_HISTORY_SCHEMA_VERSION,
    query_growth_history,
)
from stock_ai.main import app


client = TestClient(app)


def _quarter(period: str, revenue: float, standalone: float | None) -> dict:
    year = int(period[:4])
    quarter = int(period[-1])
    return {
        "period": period,
        "fiscal_year": year,
        "quarter": quarter,
        "symbol": "2330.TW",
        "revenue": revenue,
        "current_quarter_revenue": standalone,
        "source_id": "mops_archive",
        "source_url": f"https://mops.example/income/{period}",
    }


def _mock_sources(monkeypatch) -> None:
    monthly_items = [
        {
            "period": "2025-02",
            "current_revenue": 200,
            "mom_change_percent": 10,
            "yoy_change_percent": 20,
            "ytd_change_percent": 18,
            "growth_source": "official_disclosed",
            "source_id": "mops_archive",
            "source_url": "https://mops.example/monthly/2025-02",
        },
        {
            "period": "2025-01",
            "current_revenue": 180,
            "mom_change_percent": -5,
            "yoy_change_percent": 15,
            "ytd_change_percent": 15,
            "growth_source": "official_disclosed",
            "source_id": "mops_archive",
            "source_url": "https://mops.example/monthly/2025-01",
        },
    ]
    annual = [
        _quarter(f"{year}-Q4", 100 * (1.1 ** (year - 2015)), None)
        for year in range(2015, 2026)
    ]
    quarterly_items = [
        _quarter("2025-Q3", 330, 120),
        _quarter("2025-Q2", 210, 110),
        _quarter("2025-Q1", 100, 100),
        *annual,
    ]
    monkeypatch.setattr(
        "stock_ai.growth_metrics.query_monthly_revenue_history",
        lambda *args, **kwargs: {
            "start_period": "2025-01",
            "end_period": "2025-02",
            "items": monthly_items,
            "coverage": {"is_complete": True},
        },
    )
    monkeypatch.setattr(
        "stock_ai.growth_metrics.query_income_statement_history",
        lambda *args, **kwargs: {
            "start_period": "2015-Q1",
            "end_period": "2025-Q4",
            "items": quarterly_items,
            "coverage": {"is_complete": True},
        },
    )


def test_growth_contract_separates_month_quarter_year_and_cagr(
    monkeypatch,
) -> None:
    _mock_sources(monkeypatch)
    result = query_growth_history(
        "2330.TW",
        monthly_start_period="2025-01",
        monthly_end_period="2025-02",
        quarterly_start_period="2015-Q1",
        quarterly_end_period="2025-Q4",
    )
    assert result["schema_version"] == GROWTH_HISTORY_SCHEMA_VERSION
    latest_month = result["monthly"]["items"][0]
    assert latest_month["mom_percent"] == 10
    assert latest_month["yoy_percent"] == 20
    assert latest_month["growth_source"] == "official_disclosed"
    quarters = {
        item["period"]: item for item in result["quarterly"]["items"]
    }
    assert quarters["2025-Q2"]["qoq_percent"] == 10
    assert quarters["2025-Q3"]["qoq_percent"] == (
        (120 - 110) / 110 * 100
    )
    assert quarters["2025-Q4"]["qoq_percent"] is None
    assert quarters["2025-Q4"]["comparison_status"] == (
        "official_single_quarter_value_missing"
    )
    latest_year = result["annual"]["items"][0]
    assert latest_year["year"] == 2025
    assert round(latest_year["yoy_percent"], 8) == 10
    assert round(latest_year["cagr_3y_percent"], 8) == 10
    assert round(latest_year["cagr_5y_percent"], 8) == 10
    assert round(latest_year["cagr_10y_percent"], 8) == 10
    available = result["annual"]["available_range_cagr"]
    assert available["years"] == 10
    assert round(available["percent"], 8) == 10


def test_cagr_and_growth_do_not_invent_invalid_endpoints(monkeypatch) -> None:
    _mock_sources(monkeypatch)
    result = query_growth_history(
        "2330.TW",
        monthly_start_period="2025-01",
        monthly_end_period="2025-02",
        quarterly_start_period="2015-Q1",
        quarterly_end_period="2025-Q4",
    )
    q1 = next(
        item
        for item in result["quarterly"]["items"]
        if item["period"] == "2025-Q1"
    )
    assert q1["qoq_percent"] is None
    assert q1["comparison_status"] == "official_single_quarter_value_missing"
    first_year = result["annual"]["items"][-1]
    assert first_year["yoy_percent"] is None
    assert first_year["cagr_3y_percent"] is None


def test_versioned_api_and_ui_controls(monkeypatch) -> None:
    expected = {
        "schema_version": GROWTH_HISTORY_SCHEMA_VERSION,
        "symbol": "2330.TW",
        "monthly": {"items": []},
        "quarterly": {"items": []},
        "annual": {"items": []},
    }
    monkeypatch.setattr(
        "stock_ai.main.query_growth_history",
        lambda *args, **kwargs: expected,
    )
    unified = client.get(
        "/api/data/ui/v1/fundamentals/growth/history"
        "?symbol=2330.TW&monthly_start_period=2025-01"
        "&monthly_end_period=2025-02&quarterly_start_period=2015-Q1"
        "&quarterly_end_period=2025-Q4"
    )
    compatibility = client.get(
        "/api/fundamentals/growth/history"
        "?symbol=2330.TW&monthly_start_period=2025-01"
        "&monthly_end_period=2025-02&quarterly_start_period=2015-Q1"
        "&quarterly_end_period=2025-Q4"
    )
    assert unified.status_code == compatibility.status_code == 200
    assert unified.json() == compatibility.json() == expected

    root = Path(__file__).resolve().parents[1]
    html = (root / "src/stock_ai/ui/static/index.html").read_text()
    javascript = (
        root / "src/stock_ai/ui/static/js/features/dashboard.js"
    ).read_text()
    for identifier in (
        "growthSymbol",
        "growthMonthlyStart",
        "growthMonthlyEnd",
        "growthQuarterlyStart",
        "growthQuarterlyEnd",
        "loadGrowthHistoryBtn",
        "growthHistoryStatus",
        "growthHistoryTable",
    ):
        assert f'id="{identifier}"' in html
    assert "/fundamentals/growth/history" in javascript
    assert "available_range_cagr" in javascript
