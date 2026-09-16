from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stock_ai.financial_ratios import (
    FINANCIAL_RATIO_SCHEMA_VERSION,
    query_financial_ratio_history,
)
from stock_ai.main import app


client = TestClient(app)


def _income_item(period: str = "2025-Q2") -> dict:
    return {
        "period": period,
        "fiscal_year": 2025,
        "quarter": int(period[-1]),
        "symbol": "2330.TW",
        "statement_scope": "合併綜合損益表",
        "revenue": 1_000.0,
        "gross_profit": 500.0,
        "operating_income": 300.0,
        "net_income": 200.0,
        "source_id": "mops_archive",
        "source_url": f"https://mops.example/income/{period}",
    }


def _balance_item(
    period: str,
    *,
    assets: float,
    liabilities: float,
    equity: float,
) -> dict:
    return {
        "period": period,
        "fiscal_year": int(period[:4]),
        "quarter": int(period[-1]),
        "symbol": "2330.TW",
        "statement_scope": "合併資產負債表",
        "total_assets": assets,
        "total_liabilities": liabilities,
        "total_equity": equity,
        "source_id": "mops_archive",
        "source_url": f"https://mops.example/balance/{period}",
    }


def test_ratios_use_consistent_formulas_and_expose_both_sources(
    monkeypatch,
) -> None:
    income = _income_item()
    balances = [
        _balance_item(
            "2025-Q2",
            assets=2_000,
            liabilities=800,
            equity=1_200,
        ),
        _balance_item(
            "2025-Q1",
            assets=1_800,
            liabilities=800,
            equity=1_000,
        ),
    ]
    monkeypatch.setattr(
        "stock_ai.financial_ratios.query_income_statement_history",
        lambda *args, **kwargs: {
            "start_period": "2025-Q1",
            "end_period": "2025-Q2",
            "items": [income],
            "coverage": {"requested_period_count": 2},
        },
    )
    monkeypatch.setattr(
        "stock_ai.financial_ratios.query_balance_sheet_history",
        lambda *args, **kwargs: {
            "items": balances,
            "coverage": {"requested_period_count": 2},
        },
    )
    result = query_financial_ratio_history(
        "2330.TW",
        start_period="2025-Q1",
        end_period="2025-Q2",
    )
    item = result["items"][0]
    assert item["gross_margin_percent"] == 50
    assert item["operating_margin_percent"] == 30
    assert item["net_margin_percent"] == 20
    assert item["roe_percent"] == 400 / 1_100 * 100
    assert item["roa_percent"] == 400 / 1_900 * 100
    assert item["debt_ratio_percent"] == 40
    assert item["calculation_contract"]["return_annualization_factor"] == 2
    assert item["calculation_contract"]["roe_denominator_basis"] == (
        "consecutive_quarter_average"
    )
    sources = item["source_comparison"]
    assert sources["periods_match"] is True
    assert sources["income_statement"]["inputs"]["net_income"] == 200
    assert sources["balance_sheet"]["inputs"]["total_assets"] == 2_000
    assert sources["previous_balance_sheet"]["period"] == "2025-Q1"
    assert sources["income_statement"]["source_url"].endswith("2025-Q2")
    assert sources["balance_sheet"]["source_url"].endswith("2025-Q2")


def test_missing_and_zero_inputs_remain_null(monkeypatch) -> None:
    income = {
        **_income_item("2025-Q1"),
        "revenue": 0,
        "gross_profit": None,
        "operating_income": None,
        "net_income": None,
    }
    balance = _balance_item(
        "2025-Q1",
        assets=0,
        liabilities=100,
        equity=0,
    )
    monkeypatch.setattr(
        "stock_ai.financial_ratios.query_income_statement_history",
        lambda *args, **kwargs: {
            "start_period": "2025-Q1",
            "end_period": "2025-Q1",
            "items": [income],
            "coverage": {"requested_period_count": 1},
        },
    )
    monkeypatch.setattr(
        "stock_ai.financial_ratios.query_balance_sheet_history",
        lambda *args, **kwargs: {"items": [balance]},
    )
    item = query_financial_ratio_history(
        "2330.TW",
        start_period="2025-Q1",
        end_period="2025-Q1",
    )["items"][0]
    for field in (
        "gross_margin_percent",
        "operating_margin_percent",
        "net_margin_percent",
        "roe_percent",
        "roa_percent",
        "debt_ratio_percent",
    ):
        assert item[field] is None
    assert item["calculation_status"] == "partial"
    assert len(item["missing_metrics"]) == 6


def test_versioned_api_and_ui_source_comparison(monkeypatch) -> None:
    expected = {
        "schema_version": FINANCIAL_RATIO_SCHEMA_VERSION,
        "symbol": "2330.TW",
        "items": [],
        "coverage": {"is_complete": False},
    }
    monkeypatch.setattr(
        "stock_ai.main.query_financial_ratio_history",
        lambda *args, **kwargs: expected,
    )
    unified = client.get(
        "/api/data/ui/v1/fundamentals/ratios/history"
        "?symbol=2330.TW&start_period=2025-Q1&end_period=2025-Q4"
    )
    compatibility = client.get(
        "/api/fundamentals/ratios/history"
        "?symbol=2330.TW&start_period=2025-Q1&end_period=2025-Q4"
    )
    assert unified.status_code == compatibility.status_code == 200
    assert unified.json() == compatibility.json() == expected

    root = Path(__file__).resolve().parents[1]
    html = (root / "src/stock_ai/ui/static/index.html").read_text()
    javascript = (
        root / "src/stock_ai/ui/static/js/features/dashboard.js"
    ).read_text()
    for identifier in (
        "financialRatioSymbol",
        "financialRatioStart",
        "financialRatioEnd",
        "loadFinancialRatioHistoryBtn",
        "financialRatioHistoryStatus",
        "financialRatioHistoryTable",
    ):
        assert f'id="{identifier}"' in html
    assert "/fundamentals/ratios/history" in javascript
    assert "source_comparison" in javascript
