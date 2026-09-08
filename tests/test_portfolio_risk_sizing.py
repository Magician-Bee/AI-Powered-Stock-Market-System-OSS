from __future__ import annotations

from open_stock_ai.research.portfolio_construction import PortfolioConstruction
from open_stock_ai.research.portfolio_optimizer import CovariancePortfolioOptimizer
from open_stock_ai.research.portfolio_risk_sizing import PortfolioRiskSizer


def _constraint_context(symbols: tuple[str, ...]) -> dict:
    return {
        "position_risk_metadata": {
            symbol: {
                "issuer_id": symbol.split(".")[0],
                "industry": "technology",
                "currency": "TWD",
                "broker_id": "paper",
                "account_alias": "paper-default",
                "adv_value": 10_000_000.0,
            }
            for symbol in symbols
        },
        "concentration_limits": {
            "issuer": 60.0,
            "industry": 100.0,
            "factor": 1.0,
            "currency": 100.0,
            "broker": 100.0,
            "account": 100.0,
        },
        "account_equity": 1_000_000.0,
        "max_participation_rate": 0.20,
        "max_days_to_liquidate": 0.20,
        "historical_returns": {symbol: [0.01, -0.005] * 10 for symbol in symbols},
        "cvar_confidence": 0.95,
        "max_cvar_pct": 5.0,
        "stress_scenarios": [
            {"scenario_id": "crash", "shocks": {symbol: -0.20 for symbol in symbols}},
        ],
        "max_stress_loss_pct": 5.0,
    }


def test_portfolio_risk_sizer_binds_volatility_loss_budget_and_fractional_kelly_caps() -> None:
    receipt = PortfolioRiskSizer().size(
        requested_weight_pct=30.0,
        inputs={
            "annualized_volatility_pct": 30.0,
            "expected_win_probability": 0.60,
            "expected_win_pct": 8.0,
            "expected_loss_pct": 4.0,
            "max_loss_pct": 5.0,
            "risk_budget_pct": 1.0,
            "as_of": "2026-08-20T09:00:00+08:00",
            "dataset_manifest_hash": "a" * 64,
            "risk_context_status": "verified",
            "risk_context_receipt_sha256": "b" * 64,
            "account_risk_status": "verified",
            "account_risk_receipt_sha256": "c" * 64,
        },
    )

    assert receipt["status"] == "verified"
    assert receipt["caps"]["volatility_target_cap_pct"] == 40.0
    assert receipt["caps"]["max_loss_budget_cap_pct"] == 20.0
    assert receipt["caps"]["fractional_kelly_cap_pct"] == 25.0
    assert receipt["target_weight_pct"] == 20.0
    assert len(receipt["receipt_sha256"]) == 64
    assert receipt["execution_authority"] == "none"


def test_portfolio_risk_sizer_withholds_missing_or_invalid_inputs_instead_of_using_score_fallback() -> None:
    receipt = PortfolioRiskSizer().size(
        requested_weight_pct=10.0,
        inputs={"annualized_volatility_pct": 0, "expected_win_probability": 1.2},
    )

    assert receipt["status"] == "withheld"
    assert receipt["target_weight_pct"] == 0.0
    assert "portfolio_risk_input_missing:annualized_volatility_pct" in receipt["blockers"]
    assert "portfolio_risk_input_missing:expected_win_probability" in receipt["blockers"]
    assert "portfolio_risk_context_not_verified" in receipt["blockers"]


def test_portfolio_risk_sizer_never_uses_default_risk_budget_without_pit_context_receipts() -> None:
    receipt = PortfolioRiskSizer().size(
        requested_weight_pct=10.0,
        inputs={
            "annualized_volatility_pct": 20.0,
            "expected_win_probability": 0.6,
            "expected_win_pct": 8.0,
            "expected_loss_pct": 4.0,
            "max_loss_pct": 5.0,
            "as_of": "2026-08-20T09:00:00+08:00",
            "dataset_manifest_hash": "a" * 64,
        },
    )

    assert receipt["status"] == "withheld"
    assert receipt["target_weight_pct"] == 0.0
    assert "portfolio_risk_input_missing:risk_budget_pct" in receipt["blockers"]
    assert "portfolio_risk_context_not_verified" in receipt["blockers"]
    assert "portfolio_account_risk_not_verified" in receipt["blockers"]


def test_portfolio_construction_only_proposes_a_weight_when_each_position_has_a_verified_receipt() -> None:
    decision = {
        "request": {"symbol": "2330.TW", "market": "TW", "horizon": "swing"},
        "signal": {"action": "buy", "rule_score": 0.8},
        "research": {"sharpe": 1.2, "raw": {}},
        "risk": {"approved": True, "adjusted_position_size_pct": 25.0},
        "execution": {"executed": False, "mode": "paper"},
        "portfolio_risk_sizing": {
            "annualized_volatility_pct": 40.0,
            "expected_win_probability": 0.55,
            "expected_win_pct": 6.0,
            "expected_loss_pct": 4.0,
            "max_loss_pct": 10.0,
            "risk_budget_pct": 1.0,
        },
    }
    withheld = {**decision, "portfolio_risk_sizing": {}}

    context = {
        "as_of": "2026-08-20T09:00:00+08:00",
        "dataset_manifest_hash": "b" * 64,
        "covariance_matrix": {
            "2330.TW": {"2330.TW": 0.16, "2317.TW": 0.03},
            "2317.TW": {"2330.TW": 0.03, "2317.TW": 0.09},
        },
        "factor_exposures": {
            "2330.TW": {"market": 1.1, "semiconductor": 1.0},
            "2317.TW": {"market": 0.8, "semiconductor": 0.2},
        },
        "factor_limits": {"market": 0.3, "semiconductor": 0.2},
        "risk_context_status": "verified",
        "risk_context_receipt_sha256": "d" * 64,
        "account_risk_status": "verified",
        "account_risk_receipt_sha256": "e" * 64,
    }
    context.update(_constraint_context(("2330.TW", "2317.TW")))
    withheld["request"] = {"symbol": "2317.TW", "market": "TW", "horizon": "swing"}
    projection = PortfolioConstruction().build([decision, withheld], portfolio_risk_context=context)

    assert projection["schema_version"] == "open_stock_ai.portfolio_construction.v5"
    assert projection["risk_sizing"] == {
        "schema_version": "open_stock_ai.portfolio_risk_sizing_receipt.v2",
        "verified_count": 1,
        "withheld_count": 1,
        "execution_authority": "none",
    }
    assert projection["positions"][0]["target_weight_pct"] == 10.0
    assert projection["positions"][1]["target_weight_pct"] == 0.0
    assert projection["positions"][1]["risk_sizing_receipt"]["status"] == "withheld"
    assert projection["portfolio_optimization"]["status"] == "verified"
    assert projection["positions"][0]["target_weight_pct"] <= 10.0
    assert projection["execution_boundary"] == "paper_only_covariance_constrained_no_order_authority"


def test_covariance_optimizer_scales_portfolio_volatility_and_factor_exposure_without_breaching_caps() -> None:
    positions = [
        {"symbol": "AAA.TW", "risk_sized_weight_pct": 20.0},
        {"symbol": "BBB.TW", "risk_sized_weight_pct": 20.0},
    ]
    context = {
        "as_of": "2026-08-20T09:00:00+08:00",
        "dataset_manifest_hash": "c" * 64,
        "target_portfolio_volatility_pct": 10.0,
        "max_symbol_weight_pct": 20.0,
        "max_total_weight_pct": 15.0,
        "covariance_matrix": {
            "AAA.TW": {"AAA.TW": 0.25, "BBB.TW": 0.10},
            "BBB.TW": {"AAA.TW": 0.10, "BBB.TW": 0.16},
        },
        "factor_exposures": {
            "AAA.TW": {"market": 1.0},
            "BBB.TW": {"market": 0.8},
        },
        "factor_limits": {"market": 0.15},
        "risk_context_status": "verified",
        "risk_context_receipt_sha256": "f" * 64,
        "account_risk_status": "verified",
        "account_risk_receipt_sha256": "0" * 64,
    }
    context.update(_constraint_context(("AAA.TW", "BBB.TW")))

    receipt = CovariancePortfolioOptimizer().optimize(positions, context)

    assert receipt["status"] == "verified"
    assert receipt["metrics"]["portfolio_volatility_pct"] <= 10.0
    assert abs(receipt["metrics"]["factor_exposures"]["market"]) <= 0.15
    assert all(weight <= 20.0 for weight in receipt["target_weights_pct"].values())
    assert receipt["metrics"]["total_weight_pct"] <= 15.0
    assert len(receipt["receipt_sha256"]) == 64


def test_covariance_optimizer_withholds_on_missing_or_non_symmetric_covariance_evidence() -> None:
    receipt = CovariancePortfolioOptimizer().optimize(
        [{"symbol": "AAA.TW", "risk_sized_weight_pct": 10.0}],
        {
            "as_of": "2026-08-20T09:00:00+08:00",
            "dataset_manifest_hash": "d" * 64,
            "covariance_matrix": {"AAA.TW": {"AAA.TW": 0.04}},
            "factor_exposures": {"AAA.TW": {"market": 1.0}},
            "factor_limits": {},
        },
    )

    assert receipt["status"] == "withheld"
    assert receipt["target_weights_pct"] == {"AAA.TW": 0.0}
    assert "portfolio_factor_limits_missing" in receipt["blockers"]
