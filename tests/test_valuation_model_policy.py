from pathlib import Path

import pytest

from stock_ai.valuation_model_policy import (
    ValuationModelPolicyError,
    build_valuation_model_policy,
    separate_valuation_evidence,
)


def test_model_selection_changes_with_industry_and_company_characteristics():
    financial = build_valuation_model_policy(
        industry_profile="金融業",
        profitable=True,
        positive_free_cash_flow=True,
        pays_dividend=True,
        asset_heavy=False,
        high_growth=False,
    )
    assert financial["selection"]["applicable_models"][0]["model"] == "price_to_book"
    assert any(item["model"] == "standard_dcf" for item in financial["selection"]["excluded_models"])
    growth = build_valuation_model_policy(
        industry_profile="軟體",
        profitable=False,
        positive_free_cash_flow=False,
        pays_dividend=False,
        asset_heavy=False,
        high_growth=True,
    )
    assert growth["selection"]["applicable_models"][0]["model"] == "peer_revenue_multiple"
    assert growth["selection"]["policy"] == "industry_and_company_characteristics_not_one_formula_for_all"


def test_evidence_categories_stay_separate_and_require_source_and_date():
    layers = separate_valuation_evidence([
        {"category": "reported_fact", "field": "revenue", "value": 100, "source": "MOPS", "as_of": "2026-Q1"},
        {"category": "company_guidance", "field": "growth", "value": "5-10%", "source": "issuer", "as_of": "2026-07-01"},
        {"category": "analyst_estimate", "field": "eps", "value": 12, "source": "named-provider", "as_of": "2026-07-20"},
        {"category": "model_assumption", "field": "discount", "value": 10, "source": "user"},
    ])
    assert all(len(layers[key]) == 1 for key in layers)
    assert layers["model_assumption"][0]["is_model_input"] is True
    with pytest.raises(ValuationModelPolicyError, match="requires an explicit source"):
        separate_valuation_evidence([{"category": "analyst_estimate", "field": "eps", "value": 12, "as_of": "2026-07-20"}])


def test_valuation_policy_ui_discloses_model_choice_and_evidence_layers():
    root = Path(__file__).resolve().parents[1] / "src/stock_ai"
    markup = (root / "ui/static/index.html").read_text(encoding="utf-8")
    script = (root / "ui/static/js/features/dashboard.js").read_text(encoding="utf-8")
    assert 'id="runValuationPolicyBtn"' in markup
    assert "不對所有公司套同一公式" in markup
    assert "歷史事實、公司指引、分析師預估與模型假設" in markup
    assert "renderValuationPolicy" in script
