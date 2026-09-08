from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from open_stock_ai.research.portfolio_optimizer import CovariancePortfolioOptimizer
from open_stock_ai.research.portfolio_risk_constraints import PortfolioRiskConstraintEngine
from open_stock_ai.research.portfolio_risk_context import PortfolioRiskContextStore
from open_stock_ai.research.portfolio_risk_sizing import PortfolioRiskSizer


@dataclass
class PortfolioConstruction:
    """Build a paper-only portfolio using explicit, evidence-bound sizing receipts."""

    max_total_weight_pct: float = 100.0
    risk_sizer: PortfolioRiskSizer = PortfolioRiskSizer()
    optimizer: CovariancePortfolioOptimizer = CovariancePortfolioOptimizer()
    risk_constraints: PortfolioRiskConstraintEngine = PortfolioRiskConstraintEngine()

    def build(
        self,
        decisions: list[dict[str, Any]],
        *,
        session: str | None = None,
        source: str = "batch_session",
        portfolio_risk_context: dict[str, Any] | None = None,
        portfolio_risk_context_store: PortfolioRiskContextStore | None = None,
        portfolio_risk_context_receipt: str | None = None,
    ) -> dict[str, Any]:
        optimization_context, context_source = self._resolve_context(
            decisions,
            portfolio_risk_context=portfolio_risk_context,
            portfolio_risk_context_store=portfolio_risk_context_store,
            portfolio_risk_context_receipt=portfolio_risk_context_receipt,
        )
        optimization_context.setdefault("max_total_weight_pct", self.max_total_weight_pct)
        positions = [
            self._position(item, portfolio_risk_context=optimization_context)
            for item in decisions
            if isinstance(item, dict)
        ]
        approved_count = sum(1 for item in positions if item["risk_approved"] is True)
        executed_count = sum(1 for item in positions if item["executed"] is True)
        optimizer_receipt = self.optimizer.optimize(
            positions,
            optimization_context,
        )
        constraint_receipt = self.risk_constraints.evaluate(
            positions,
            optimizer_receipt.get("target_weights_pct") or {},
            optimization_context,
            optimizer_status=optimizer_receipt.get("status"),
        )
        portfolio_verified = not positions or (
            optimizer_receipt.get("status") == "verified"
            and constraint_receipt.get("status") == "verified"
        )
        for item in positions:
            item["target_weight_pct"] = round(
                float(
                    (
                        constraint_receipt["target_weights_pct"]
                        if portfolio_verified
                        else {}
                    ).get(str(item["symbol"] or "").upper(), 0.0)
                ),
                4,
            )
            item["portfolio_optimizer_receipt_sha256"] = optimizer_receipt["receipt_sha256"]
            item["portfolio_risk_constraints_receipt_sha256"] = constraint_receipt["receipt_sha256"]
        total_weight = round(sum(item["target_weight_pct"] for item in positions), 4)
        observed_schemas = {
            constraint_receipt.get("schema_version"),
            *{
                schema
                for item in positions
                for schema in [
                    item.get("finrl_projection_schema"),
                    item.get("qlib_projection_schema"),
                    item.get("risk_schema_version"),
                ]
                if schema
            },
        }
        observed_schemas.discard(None)
        observed_schemas = sorted(observed_schemas)
        return {
            "schema_version": "open_stock_ai.portfolio_construction.v5",
            "method": "covariance_and_factor_constrained_paper_portfolio",
            "session": session,
            "source": source,
            "portfolio_risk_context_source": context_source,
            "portfolio_risk_context_receipt": (
                optimization_context.get("risk_context_receipt_sha256")
                if context_source == "durable_store"
                else portfolio_risk_context_receipt
            ),
            "item_count": len(positions),
            "approved_count": approved_count,
            "executed_count": executed_count,
            "blocked_count": len(positions) - approved_count,
            "proposed_total_weight_pct": total_weight,
            "max_total_weight_pct": self.max_total_weight_pct,
            "allocation_basis": "explicit_portfolio_risk_sizing_receipts",
            "risk_sizing": {
                "schema_version": "open_stock_ai.portfolio_risk_sizing_receipt.v2",
                "verified_count": sum(item["risk_sizing_receipt"]["status"] == "verified" for item in positions),
                "withheld_count": sum(item["risk_sizing_receipt"]["status"] != "verified" for item in positions),
                "execution_authority": "none",
            },
            "portfolio_optimization": optimizer_receipt,
            "portfolio_risk_constraints": constraint_receipt,
            "portfolio_ready": portfolio_verified,
            "constraints": {
                "paper_only": True,
                "uses_risk_adjusted_weights": True,
                "external_runtime_import": False,
                "live_order_submission": False,
            },
            "evidence_schemas": observed_schemas,
            "positions": positions,
            "available": len(positions) == len(decisions),
            "execution_boundary": "paper_only_covariance_constrained_no_order_authority",
        }

    def _position(
        self,
        decision: dict[str, Any],
        *,
        portfolio_risk_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = decision.get("request") if isinstance(decision.get("request"), dict) else {}
        signal = decision.get("signal") if isinstance(decision.get("signal"), dict) else {}
        research = decision.get("research") if isinstance(decision.get("research"), dict) else {}
        risk = decision.get("risk") if isinstance(decision.get("risk"), dict) else {}
        execution = decision.get("execution") if isinstance(decision.get("execution"), dict) else {}
        raw = research.get("raw") if isinstance(research.get("raw"), dict) else {}
        finrl = raw.get("finrl") if isinstance(raw.get("finrl"), dict) else {}
        qlib = raw.get("qlib") if isinstance(raw.get("qlib"), dict) else {}
        finrl_projection = (
            finrl.get("backtest_projection")
            if isinstance(finrl.get("backtest_projection"), dict)
            else {}
        )
        qlib_projection = (
            qlib.get("factor_projection")
            if isinstance(qlib.get("factor_projection"), dict)
            else {}
        )
        risk_approved = risk.get("approved") is True
        action = str(signal.get("action") or "hold")
        adjusted_weight = self._number(risk.get("adjusted_position_size_pct"))
        raw_weight = adjusted_weight if risk_approved and action in {"buy", "add"} else 0.0
        sizing_inputs = (
            decision.get("portfolio_risk_sizing")
            if isinstance(decision.get("portfolio_risk_sizing"), dict)
            else raw.get("portfolio_risk_sizing")
            if isinstance(raw.get("portfolio_risk_sizing"), dict)
            else {}
        )
        sizing_inputs = dict(sizing_inputs)
        if isinstance(portfolio_risk_context, dict):
            sizing_inputs["_full_portfolio_risk_context"] = portfolio_risk_context
            sized_context = portfolio_risk_context.get("position_sizing_inputs")
            if isinstance(sized_context, dict):
                materialized = sized_context.get(str(request.get("symbol") or "").strip().upper())
                if isinstance(materialized, dict):
                    for key, value in materialized.items():
                        sizing_inputs.setdefault(key, value)
        for key in (
            "as_of",
            "dataset_manifest_hash",
            "risk_context_status",
            "risk_context_receipt_sha256",
            "account_risk_status",
            "account_risk_receipt_sha256",
        ):
            if key not in sizing_inputs and isinstance(portfolio_risk_context, dict):
                sizing_inputs[key] = portfolio_risk_context.get(key)
        sizing_receipt = self.risk_sizer.size(
            requested_weight_pct=raw_weight,
            inputs=sizing_inputs,
        )
        rule_score = self._number(signal.get("rule_score"))
        sharpe = self._number(research.get("sharpe"))
        model_score = self._number(qlib_projection.get("model_score"))
        rank_ic_proxy = self._number(qlib_projection.get("rank_ic_proxy"))
        priority_score = self._priority_score(
            rule_score=rule_score,
            sharpe=sharpe,
            model_score=model_score,
            rank_ic_proxy=rank_ic_proxy,
            risk_approved=risk_approved,
        )
        symbol = str(request.get("symbol") or "").strip().upper()
        metadata = {}
        if isinstance(portfolio_risk_context, dict):
            all_metadata = portfolio_risk_context.get("position_risk_metadata")
            if isinstance(all_metadata, dict) and isinstance(all_metadata.get(symbol), dict):
                metadata = dict(all_metadata[symbol])
        return {
            "symbol": request.get("symbol"),
            "market": request.get("market"),
            "horizon": request.get("horizon"),
            "action": action,
            "rule_score": rule_score,
            "score_range": [-1.0, 1.0],
            "calibrated": False,
            "risk_approved": risk_approved,
            "risk_reason": risk.get("reason"),
            "risk_schema_version": risk.get("schema_version"),
            "executed": execution.get("executed") is True,
            "execution_mode": execution.get("mode"),
            "raw_target_weight_pct": round(raw_weight, 4),
            "risk_sized_weight_pct": sizing_receipt["target_weight_pct"],
            "target_weight_pct": 0.0,
            "risk_sizing_receipt": sizing_receipt,
            "priority_score": priority_score,
            "finrl_projection_schema": finrl_projection.get("schema_version"),
            "finrl_sharpe": self._number(research.get("sharpe")),
            "finrl_max_drawdown_pct": self._number(research.get("max_drawdown_pct")),
            "qlib_projection_schema": qlib_projection.get("schema_version"),
            "qlib_model_score": model_score,
            "qlib_rank_ic_proxy": rank_ic_proxy,
            "portfolio_risk_metadata": metadata,
        }

    @staticmethod
    def _resolve_context(
        decisions: list[dict[str, Any]],
        *,
        portfolio_risk_context: dict[str, Any] | None,
        portfolio_risk_context_store: PortfolioRiskContextStore | None,
        portfolio_risk_context_receipt: str | None,
    ) -> tuple[dict[str, Any], str]:
        """Resolve the single context used by every portfolio risk engine.

        A receipt lookup is an explicit durable path. Missing, tampered or
        mismatched evidence returns an empty context so all downstream risk
        engines withhold targets instead of falling back to inline guesses.
        """

        if portfolio_risk_context_receipt is not None:
            if portfolio_risk_context_store is None:
                return {}, "withheld_missing_context_store"
            try:
                stored = portfolio_risk_context_store.by_receipt(portfolio_risk_context_receipt)
            except ValueError:
                return {}, "withheld_invalid_durable_receipt"
            if not isinstance(stored, dict):
                return {}, "withheld_missing_durable_receipt"
            if isinstance(portfolio_risk_context, dict):
                if stored.get("risk_context_receipt_sha256") != portfolio_risk_context.get(
                    "risk_context_receipt_sha256"
                ):
                    return {}, "withheld_context_receipt_mismatch"
            return dict(stored), "durable_store"
        if isinstance(portfolio_risk_context, dict):
            return dict(portfolio_risk_context), "inline_context"
        inferred_context = PortfolioConstruction._context_from_decisions(decisions)
        return dict(inferred_context), "decision_embedded_context" if inferred_context else "none"

    @staticmethod
    def _context_from_decisions(decisions: list[dict[str, Any]]) -> dict[str, Any]:
        contexts = [
            item.get("portfolio_risk_context")
            for item in decisions
            if isinstance(item, dict) and isinstance(item.get("portfolio_risk_context"), dict)
        ]
        if len(contexts) == 1:
            return dict(contexts[0])
        return {}

    def _priority_score(
        self,
        *,
        rule_score: float,
        sharpe: float,
        model_score: float,
        rank_ic_proxy: float,
        risk_approved: bool,
    ) -> float:
        if not risk_approved:
            return 0.0
        score = abs(rule_score) * 40.0 + min(max(sharpe, 0.0), 5.0) * 8.0 + model_score * 15.0 + rank_ic_proxy * 10.0
        return round(max(0.0, score), 4)

    def _number(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
