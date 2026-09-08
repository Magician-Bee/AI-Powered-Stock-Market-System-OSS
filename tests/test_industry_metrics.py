from __future__ import annotations

from typing import Any

import stock_ai.industry_metrics as subject


class FakePlatform:
    def __init__(self, industry: str | None = None):
        self.industry = industry

    def resolve_entity(self, _identifier: str) -> dict[str, Any] | None:
        return {"industry": self.industry} if self.industry else None


def _install_statement_fixtures(monkeypatch) -> None:
    income = {
        "period": "2025-Q4",
        "revenue": 1_000.0,
        "gross_profit": 400.0,
        "operating_income": 250.0,
        "net_income": 200.0,
        "source_id": "mops",
        "source_url": "https://mops.example/income",
    }
    balance = {
        "period": "2025-Q4",
        "total_assets": 2_000.0,
        "total_liabilities": 800.0,
        "total_equity": 1_200.0,
        "inventory": 500.0,
        "current_assets": 900.0,
        "current_liabilities": 300.0,
        "source_id": "mops",
        "source_url": "https://mops.example/balance",
    }
    cash = {
        "period": "2025-Q4",
        "operating_cash_flow": 300.0,
        "capital_expenditure": -150.0,
        "free_cash_flow": 150.0,
        "source_id": "mops",
        "source_url": "https://mops.example/cash",
    }
    monkeypatch.setattr(
        subject,
        "query_income_statement_history",
        lambda *args, **kwargs: {"items": [income]},
    )
    monkeypatch.setattr(
        subject,
        "query_balance_sheet_history",
        lambda *args, **kwargs: {"items": [balance]},
    )
    monkeypatch.setattr(
        subject,
        "query_cash_flow_history",
        lambda *args, **kwargs: {"items": [cash]},
    )


def test_four_industries_use_different_metric_recipes(monkeypatch):
    _install_statement_fixtures(monkeypatch)
    recipes = {}
    for symbol, industry in (
        ("2330.TW", "半導體業"),
        ("2603.TW", "航運業"),
        ("2542.TW", "營建業"),
    ):
        payload = subject.query_industry_metrics(
            symbol,
            period="2025-Q4",
            industry=industry,
            platform=FakePlatform(),
        )
        recipes[payload["profile"]] = [
            metric["metric_id"] for metric in payload["metrics"]
        ]

    finance_rows = {
        subject.TWSE_FINANCIAL_HOLDING_INCOME_URL: [{
            "年度": "115",
            "季別": "1",
            "公司代號": "2882",
            "利息淨收益": "76415488",
            "呆帳費用、承諾及保證責任準備提存": "13366691",
            "本期稅後淨利（淨損）": "31655932",
            "基本每股盈餘（元）": "2.15",
        }],
        subject.TWSE_FINANCIAL_HOLDING_BALANCE_URL: [{
            "年度": "115",
            "季別": "1",
            "公司代號": "2882",
            "資產總計": "14450034484",
            "權益總計": "817026831",
            "保險合約負債及再保險合約負債": "7135948491",
        }],
    }
    finance = subject.query_industry_metrics(
        "2882.TW",
        industry="金融業",
        platform=FakePlatform(),
        fetch_json=lambda url: finance_rows[url],
    )
    recipes["financial"] = [
        metric["metric_id"] for metric in finance["metrics"]
    ]

    assert len({tuple(value) for value in recipes.values()}) == 4
    assert "capital_intensity" in recipes["semiconductor"]
    assert "asset_turnover" in recipes["shipping"]
    assert "inventory_intensity" in recipes["construction"]
    assert "net_interest_income" in recipes["financial"]
    assert finance["period"] == "2026-Q1"
    assert finance["sources"][0]["url"].startswith("https://openapi.twse.com.tw/")


def test_missing_official_field_is_unavailable_not_zero(monkeypatch):
    _install_statement_fixtures(monkeypatch)
    monkeypatch.setattr(
        subject,
        "query_cash_flow_history",
        lambda *args, **kwargs: {"items": [{"period": "2025-Q4"}]},
    )
    payload = subject.query_industry_metrics(
        "2330.TW",
        period="2025-Q4",
        industry="半導體業",
        platform=FakePlatform(),
    )
    capital = next(
        item for item in payload["metrics"]
        if item["metric_id"] == "capital_intensity"
    )
    assert capital["value"] is None
    assert capital["available"] is False
    assert capital["unavailable_reason"] == "required_official_fields_missing"


def test_unknown_industry_does_not_fall_back_to_generic_template():
    payload = subject.query_industry_metrics(
        "9999.TW",
        industry="其他業",
        platform=FakePlatform(),
    )
    assert payload["supported"] is False
    assert payload["metrics"] == []
    assert payload["template_policy"] == "No generic template is substituted."


def test_security_master_can_select_profile(monkeypatch):
    _install_statement_fixtures(monkeypatch)
    payload = subject.query_industry_metrics(
        "2454.TW",
        period="2025-Q4",
        platform=FakePlatform("半導體業"),
    )
    assert payload["profile"] == "semiconductor"
    assert payload["classification_basis"] == "security_master"


def test_ui_exposes_industry_specific_controls_and_non_generic_copy():
    html = (
        subject.__file__.replace(
            "industry_metrics.py", "ui/static/index.html"
        )
    )
    javascript = (
        subject.__file__.replace(
            "industry_metrics.py", "ui/static/js/features/dashboard.js"
        )
    )
    with open(html, encoding="utf-8") as handle:
        markup = handle.read()
    with open(javascript, encoding="utf-8") as handle:
        script = handle.read()
    assert 'id="industryMetricsProfile"' in markup
    assert "金融 · 半導體 · 航運 · 營建" in markup
    assert "不同產業不共用同一指標清單" in script
    assert "loadIndustryMetrics" in script
