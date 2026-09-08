from __future__ import annotations

from datetime import datetime, timedelta, timezone

from open_stock_ai.learning.evaluation import PolicyEvaluation
from open_stock_ai.research.regime_robustness import REQUIRED_REGIMES, evaluate_regime_robustness


def _observations(count: int = 100) -> list[dict[str, object]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    close = 100.0
    rows = []
    for index in range(count):
        bull_phase = (index // 20) % 2 == 0
        move = (1.018 if index % 3 == 0 else 1.004) if bull_phase else (0.986 if index % 3 == 0 else 0.996)
        close *= move
        timestamp = start + timedelta(days=index)
        rows.append(
            {
                "timestamp": timestamp.isoformat(),
                "close": close,
                "volume": 500 if index % 4 == 0 else 1400 + (index % 5) * 40,
                "earnings_event": (
                    {"event_type": "earnings", "available_at": timestamp.isoformat()}
                    if index % 10 == 0
                    else None
                ),
            }
        )
    return rows


def _returns(count: int = 100) -> list[float]:
    return [0.002 + (index % 7) * 0.0001 for index in range(count)]


def test_regime_receipt_is_pit_deterministic_and_stratifies_every_required_regime():
    first = evaluate_regime_robustness(_returns(), _observations())
    second = evaluate_regime_robustness(_returns(), _observations())

    assert first == second
    assert first["passed"] is True
    assert first["point_in_time_verified"] is True
    assert set(first["regimes"]) == set(REQUIRED_REGIMES)
    for regime in REQUIRED_REGIMES:
        receipt = first["regimes"][regime]
        assert receipt["sample_size"] >= first["min_samples_per_regime"]
        assert receipt["performance_metrics"]["sample_size"] == receipt["sample_size"]
        assert receipt["passed"] is True


def test_regime_receipt_fails_closed_for_future_dated_earnings_or_missing_coverage():
    observations = _observations()
    observations[20]["earnings_event"] = {
        "event_type": "earnings",
        "available_at": (datetime(2026, 3, 1, tzinfo=timezone.utc)).isoformat(),
    }
    result = evaluate_regime_robustness(_returns(), observations)

    assert result["passed"] is False
    assert result["point_in_time_verified"] is False
    assert any("available_after_decision_time" in blocker for blocker in result["blockers"])

    no_earnings = [{**row, "earnings_event": None} for row in _observations()]
    missing = evaluate_regime_robustness(_returns(), no_earnings)
    assert missing["passed"] is False
    assert missing["regimes"]["earnings"]["sample_size"] == 0
    assert "regime:earnings:insufficient_oos_regime_samples" in missing["blockers"]


def test_policy_evaluation_recomputes_regime_receipt_and_rejects_missing_or_tampered_evidence():
    returns = _returns()
    observations = _observations()
    receipt = evaluate_regime_robustness(returns, observations)
    valid = PolicyEvaluation().evaluate(
        {
            "period_returns": returns,
            "regime_observations": observations,
            "regime_robustness": receipt,
        },
        risk_review={"approved": True},
    )
    valid_gate = next(item for item in valid["gates"] if item["code"] == "regime_robustness")
    assert valid_gate["passed"] is True

    tampered = PolicyEvaluation().evaluate(
        {
            "period_returns": returns[:-1],
            "regime_observations": observations[:-1],
            "regime_robustness": receipt,
        },
        risk_review={"approved": True},
    )
    tampered_gate = next(item for item in tampered["gates"] if item["code"] == "regime_robustness")
    assert tampered_gate["passed"] is False
    assert "regime_robustness" in tampered["blockers"]
