from __future__ import annotations

from open_stock_ai.research.portfolio_risk_constraints import PortfolioRiskConstraintEngine


def _context(symbols: tuple[str, ...] = ("AAA.TW", "BBB.TW")) -> dict:
    return {
        "as_of": "2026-08-20T09:00:00+08:00",
        "dataset_manifest_hash": "a" * 64,
        "risk_context_status": "verified",
        "risk_context_receipt_sha256": "b" * 64,
        "account_risk_status": "verified",
        "account_risk_receipt_sha256": "c" * 64,
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
            "industry": 80.0,
            "factor": 0.30,
            "currency": 100.0,
            "broker": 100.0,
            "account": 100.0,
        },
        "account_equity": 1_000_000.0,
        "max_participation_rate": 0.20,
        "max_days_to_liquidate": 0.20,
        "factor_exposures": {symbol: {"market": 1.0} for symbol in symbols},
        "factor_limits": {"market": 0.30},
        "historical_returns": {
            symbol: [0.01, -0.005] * 10 for symbol in symbols
        },
        "cvar_confidence": 0.95,
        "max_cvar_pct": 5.0,
        "stress_scenarios": [
            {"scenario_id": "crash", "shocks": {symbol: -0.20 for symbol in symbols}},
            {"scenario_id": "gap", "shocks": {symbol: -0.10 for symbol in symbols}},
        ],
        "max_stress_loss_pct": 5.0,
    }


def test_portfolio_constraints_verify_concentration_liquidity_cvar_and_stress_receipt() -> None:
    receipt = PortfolioRiskConstraintEngine().evaluate(
        [{"symbol": "AAA.TW"}, {"symbol": "BBB.TW"}],
        {"AAA.TW": 5.0, "BBB.TW": 5.0},
        _context(),
        optimizer_status="verified",
    )

    assert receipt["status"] == "verified"
    assert receipt["blockers"] == []
    assert receipt["metrics"]["liquidity"]["symbols"][0]["participation_rate"] == 0.005
    assert receipt["metrics"]["cvar"]["observation_count"] == 20
    assert receipt["metrics"]["stress"]["worst_loss_pct"] == 2.0
    assert len(receipt["receipt_sha256"]) == 64
    assert receipt["execution_authority"] == "none"


def test_portfolio_constraints_fail_closed_when_pit_risk_context_is_incomplete() -> None:
    receipt = PortfolioRiskConstraintEngine().evaluate(
        [{"symbol": "AAA.TW"}],
        {"AAA.TW": 5.0},
        {"as_of": "2026-08-20T09:00:00+08:00"},
        optimizer_status="verified",
    )

    assert receipt["status"] == "withheld"
    assert receipt["target_weights_pct"] == {"AAA.TW": 0.0}
    assert "portfolio_position_risk_metadata_missing" in receipt["blockers"]
    assert "portfolio_concentration_limits_missing" in receipt["blockers"]
    assert "portfolio_historical_returns_missing" in receipt["blockers"]


def test_portfolio_constraints_withhold_concentration_liquidity_tail_and_stress_breaches() -> None:
    context = _context()
    context["concentration_limits"]["industry"] = 5.0
    context["concentration_limits"]["factor"] = 0.05
    context["position_risk_metadata"]["AAA.TW"]["adv_value"] = 1_000.0
    context["max_cvar_pct"] = 0.5
    context["max_stress_loss_pct"] = 5.0
    context["historical_returns"] = {
        "AAA.TW": [-0.10] * 20,
        "BBB.TW": [-0.10] * 20,
    }
    context["stress_scenarios"] = [
        {"scenario_id": "crash", "shocks": {"AAA.TW": -0.80, "BBB.TW": -0.80}},
    ]

    receipt = PortfolioRiskConstraintEngine().evaluate(
        [{"symbol": "AAA.TW"}, {"symbol": "BBB.TW"}],
        {"AAA.TW": 5.0, "BBB.TW": 5.0},
        context,
        optimizer_status="verified",
    )

    assert receipt["status"] == "withheld"
    assert receipt["target_weights_pct"] == {"AAA.TW": 0.0, "BBB.TW": 0.0}
    assert "portfolio_industry_concentration_limit_breached:technology" in receipt["blockers"]
    assert "portfolio_factor_concentration_limit_breached:market" in receipt["blockers"]
    assert "portfolio_participation_limit_breached:AAA.TW" in receipt["blockers"]
    assert "portfolio_historical_cvar_limit_breached" in receipt["blockers"]
    assert "portfolio_stress_loss_limit_breached" in receipt["blockers"]
