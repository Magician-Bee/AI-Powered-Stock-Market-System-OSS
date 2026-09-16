from __future__ import annotations

from typing import Any

from open_stock_ai.research.statistical_significance import evaluate_statistical_significance
from open_stock_ai.research.regime_robustness import evaluate_regime_robustness
from open_stock_ai.research.benchmark_metrics import evaluate_benchmark_metrics
from open_stock_ai.research.monte_carlo import simulate_tail_risk


class PolicyEvaluation:
    REQUIRED = {
        "strategy_replay_exact": True,
        "empirical_valid": True,
        "lookahead_safe": True,
        "transaction_costs_included": True,
        "slippage_included": True,
    }

    def evaluate(self, evidence: dict[str, Any], *, risk_review: dict[str, Any]) -> dict[str, Any]:
        significance = evaluate_statistical_significance(
            evidence.get("period_returns") or [],
            candidate_count=max(1, int(evidence.get("candidate_count") or 1)),
        )
        regime_robustness = evaluate_regime_robustness(
            evidence.get("period_returns") or [],
            evidence.get("regime_observations") or [],
            position_weights=evidence.get("regime_position_weights"),
        )
        benchmark_metrics = evaluate_benchmark_metrics(
            evidence.get("period_returns") or [],
            evidence.get("benchmark_observations") if isinstance(evidence.get("benchmark_observations"), list) else None,
            evidence.get("risk_free_observations") if isinstance(evidence.get("risk_free_observations"), list) else None,
            evaluation_timestamps=evidence.get("return_timestamps") if isinstance(evidence.get("return_timestamps"), list) else None,
        )
        monte_carlo_tail_risk = simulate_tail_risk(evidence.get("period_returns") or [])
        reported_regime_robustness = evidence.get("regime_robustness") if isinstance(evidence.get("regime_robustness"), dict) else {}
        reported_benchmark_metrics = evidence.get("benchmark_metrics") if isinstance(evidence.get("benchmark_metrics"), dict) else {}
        reported_monte_carlo = evidence.get("monte_carlo_tail_risk") if isinstance(evidence.get("monte_carlo_tail_risk"), dict) else {}
        historical_universe = evidence.get("historical_universe") if isinstance(evidence.get("historical_universe"), dict) else {}
        universe_rows = evidence.get("universe_membership_rows") if isinstance(evidence.get("universe_membership_rows"), list) else []
        gates = [
            {"code": key, "passed": evidence.get(key) is expected, "observed": evidence.get(key), "required": expected}
            for key, expected in self.REQUIRED.items()
        ]
        gates.extend(
            [
                self._nested("walk_forward", evidence, "passed"),
                self._nested("out_of_sample", evidence, "passed"),
                self._nested("purged_cross_validation", evidence, "passed"),
                {
                    "code": "statistical_significance",
                    "passed": significance.get("passed") is True,
                    "observed": significance,
                    "required": "positive bootstrap CI, DSR >= 0.95, Holm-adjusted p <= 0.05",
                },
                {
                    "code": "regime_robustness",
                    "passed": (
                        regime_robustness.get("passed") is True
                        and reported_regime_robustness.get("passed") is True
                        and reported_regime_robustness.get("period_returns_hash") == regime_robustness.get("period_returns_hash")
                    ),
                    "observed": regime_robustness,
                    "required": "point-in-time OOS bull/bear/high-volatility/low-liquidity/earnings metrics",
                },
                {
                    "code": "benchmark_alignment",
                    "passed": (
                        benchmark_metrics.get("passed") is True
                        and reported_benchmark_metrics.get("passed") is True
                        and reported_benchmark_metrics.get("alignment_sha256") == benchmark_metrics.get("alignment_sha256")
                    ),
                    "observed": benchmark_metrics,
                    "required": "independent PIT benchmark and cash-rate observations aligned to every strategy return",
                },
                {
                    "code": "monte_carlo_tail_risk",
                    "passed": (
                        monte_carlo_tail_risk.get("passed") is True
                        and reported_monte_carlo.get("passed") is True
                        and reported_monte_carlo.get("input_sha256") == monte_carlo_tail_risk.get("input_sha256")
                    ),
                    "observed": monte_carlo_tail_risk,
                    "required": "deterministic block-bootstrap VaR/CVaR stability receipt",
                },
                {
                    "code": "historical_universe",
                    "passed": (
                        historical_universe.get("passed") is True
                        and historical_universe.get("point_in_time_verified") is True
                        and historical_universe.get("universe_coverage_complete") is True
                        and bool(historical_universe.get("universe_scope_ids"))
                        and bool(universe_rows)
                        and all(
                            isinstance(item, dict)
                            and item.get("passed") is True
                            and item.get("point_in_time_verified") is True
                            and item.get("required_entity_in_universe") is True
                            and item.get("universe_coverage_complete") is True
                            and bool(item.get("universe_scope_id"))
                            and len(str(item.get("manifest_hash") or "")) == 64
                            for item in universe_rows
                        )
                    ),
                    "observed": historical_universe,
                    "required": "known-at historical universe membership for every replay decision",
                },
                {
                    "code": "ledger_accounting_identity",
                    "passed": bool(evidence.get("accounting_identity"))
                    and all(abs(float(item.get("residual") or 0.0)) <= 1e-6 for item in evidence.get("accounting_identity") or []),
                    "observed": evidence.get("accounting_identity"),
                    "required": "non-empty reconciled cash/position/equity ledger",
                },
                {
                    "code": "timestamp_lifecycle",
                    "passed": all(
                        item.get("feature_available_time") <= item.get("decision_time") <= item.get("order_time")
                        <= item.get("broker_receive_time") <= item.get("fill_time")
                        for item in evidence.get("fill_ledger") or []
                    ) and bool(evidence.get("fill_ledger")),
                    "observed": evidence.get("fill_ledger"),
                    "required": "feature_available → decision → order → broker_receive → fill",
                },
                {
                    "code": "live_execution_boundary",
                    "passed": evidence.get("live_execution_evidence_eligible") is False,
                    "observed": evidence.get("live_execution_evidence_eligible"),
                    "required": False,
                },
                {
                    "code": "execution_latency",
                    "passed": int(evidence.get("execution_latency_bars") or 0) >= 1,
                    "observed": evidence.get("execution_latency_bars"),
                    "required": ">=1 bar",
                },
                {
                    "code": "strategy_version_hash",
                    "passed": len(str(evidence.get("strategy_version_hash") or "")) == 64,
                    "observed": evidence.get("strategy_version_hash"),
                    "required": "sha256",
                },
                {
                    "code": "data_version_hash",
                    "passed": len(str(evidence.get("data_version_hash") or "")) == 64,
                    "observed": evidence.get("data_version_hash"),
                    "required": "sha256",
                },
                {
                    "code": "risk_review",
                    "passed": risk_review.get("approved") is True,
                    "observed": risk_review,
                    "required": "approved",
                },
            ]
        )
        passed = all(item["passed"] for item in gates)
        return {
            "schema_version": "open_stock_ai.policy_evaluation.v1",
            "passed": passed,
            "gates": gates,
            "blockers": [item["code"] for item in gates if not item["passed"]],
            "strategy_version_hash": evidence.get("strategy_version_hash"),
            "data_version_hash": evidence.get("data_version_hash"),
            "statistical_significance": significance,
            "regime_robustness": regime_robustness,
            "benchmark_metrics": benchmark_metrics,
            "monte_carlo_tail_risk": monte_carlo_tail_risk,
            "historical_universe": historical_universe,
            "boundary": "evaluation_does_not_activate_policy",
        }

    def _nested(self, code: str, evidence: dict[str, Any], key: str) -> dict[str, Any]:
        observed = evidence.get(code) if isinstance(evidence.get(code), dict) else {}
        return {"code": code, "passed": observed.get(key) is True, "observed": observed, "required": {key: True}}
