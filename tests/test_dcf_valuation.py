from pathlib import Path

import pytest

from stock_ai.dcf_valuation import (
    DCFValuationError,
    build_dcf_valuation,
)


def _payload():
    return build_dcf_valuation(
        base_revenue=1_000_000,
        gross_margin_percent=50,
        fcf_conversion_percent=35,
        revenue_growth_percent=8,
        discount_rate_percent=10,
        terminal_growth_percent=2,
        net_debt=100_000,
        shares_outstanding=100_000,
        forecast_years=5,
    )


def test_dcf_discloses_every_assumption_and_returns_scenarios_not_one_target():
    payload = _payload()
    assert payload["valuation_output_policy"] == "scenario_range_not_single_target_price"
    assert [item["scenario"] for item in payload["scenarios"]] == [
        "pessimistic",
        "neutral",
        "optimistic",
    ]
    assert "target_price" not in payload
    assert payload["scenario_range_per_share_twd"]["minimum"] < payload[
        "scenario_range_per_share_twd"
    ]["maximum"]
    for item in payload["scenarios"]:
        assert len(item["forecast"]) == 5
        assert item["assumptions"]["shares_outstanding_thousand"] == 100_000
        assert item["implied_value_per_share_twd"] is not None
    assert payload["truthfulness"]["no_single_target_price"] is True


def test_dcf_sensitivity_covers_growth_discount_margin_and_terminal_value():
    matrices = _payload()["sensitivity"]
    assert [(item["row_field"], item["column_field"]) for item in matrices] == [
        ("discount_rate_percent", "revenue_growth_percent"),
        ("gross_margin_percent", "terminal_growth_percent"),
    ]
    assert all(len(item["row_values"]) == 5 for item in matrices)
    assert all(len(item["column_values"]) == 5 for item in matrices)
    assert all(len(item["cells_implied_value_per_share_twd"]) == 5 for item in matrices)
    assert all(len(row) == 5 for item in matrices for row in item["cells_implied_value_per_share_twd"])


def test_dcf_rejects_mathematically_invalid_assumptions():
    with pytest.raises(DCFValuationError, match="discount rate must exceed"):
        build_dcf_valuation(
            base_revenue=1_000_000,
            gross_margin_percent=50,
            fcf_conversion_percent=35,
            revenue_growth_percent=8,
            discount_rate_percent=2,
            terminal_growth_percent=3,
            net_debt=0,
            shares_outstanding=100_000,
        )


def test_dcf_ui_has_adjustable_assumptions_scenarios_and_matrices():
    root = Path(__file__).resolve().parents[1] / "src/stock_ai"
    markup = (root / "ui/static/index.html").read_text(encoding="utf-8")
    script = (root / "ui/static/js/features/dashboard.js").read_text(encoding="utf-8")
    for element_id in (
        "dcfRevenue",
        "dcfGrossMargin",
        "dcfConversion",
        "dcfGrowth",
        "dcfDiscount",
        "dcfTerminal",
        "dcfNetDebt",
        "dcfShares",
        "dcfYears",
        "runDcfValuationBtn",
    ):
        assert f'id="{element_id}"' in markup
    assert "不輸出單一目標價" in markup
    assert "renderDcfValuation" in script
    assert "/fundamentals/valuation/dcf" in script
