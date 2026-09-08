from __future__ import annotations

from dataclasses import dataclass

from open_stock_ai.types import IntelligenceResult, MarketSnapshot, StockRequest, TradingSignal

from open_stock_ai.research.model_calibration import calibrate_rule_score
from .structured_decision import action_to_signal_action, build_structured_decision
from .strategy_registry import StrategyArtifactRegistry


@dataclass
class StrategyEngine:
    artifact_registry: StrategyArtifactRegistry | None = None

    def generate_signal(
        self,
        request: StockRequest,
        market_snapshot: MarketSnapshot,
        intelligence: IntelligenceResult,
    ) -> TradingSignal:
        action = "hold"
        reason = intelligence.summary or "No strong signal."
        score = 0.0
        if intelligence.sentiment_score is not None:
            score += float(intelligence.sentiment_score) * 0.35
        fingpt = intelligence.raw.get("fingpt") if isinstance(intelligence.raw, dict) else {}
        forecast_projection = (
            fingpt.get("forecast_projection")
            if isinstance(fingpt, dict) and isinstance(fingpt.get("forecast_projection"), dict)
            else {}
        )
        forecast_score = self._number(forecast_projection.get("forecast_score")) if forecast_projection else None
        if forecast_score is not None:
            score += forecast_score * 0.10
        if intelligence.fundamental_view == "positive":
            score += 0.25
        elif intelligence.fundamental_view == "negative":
            score -= 0.25
        if intelligence.technical_view == "uptrend":
            score += 0.20
        elif intelligence.technical_view == "downtrend":
            score -= 0.20
        if score >= 0.35:
            action = "buy"
        elif score <= -0.35:
            action = "sell"
        rule_score = max(-1.0, min(1.0, score))
        data_contract = (
            market_snapshot.raw.get("data_contract")
            if isinstance(market_snapshot.raw, dict)
            and isinstance(market_snapshot.raw.get("data_contract"), dict)
            else {}
        )
        data_ready = not data_contract or data_contract.get("decision_ready") is True
        calibration_receipt = (
            market_snapshot.raw.get("score_calibration")
            if isinstance(market_snapshot.raw, dict) and isinstance(market_snapshot.raw.get("score_calibration"), dict)
            else {}
        )
        calibrated_score = calibrate_rule_score(rule_score if data_ready else None, calibration_receipt)
        structured_decision = build_structured_decision(
            score=score,
            snapshot=market_snapshot,
            intelligence=intelligence,
            horizon=request.horizon,
        )
        strategy_artifact = (self.artifact_registry or StrategyArtifactRegistry()).baseline_artifact()
        tradingagents = intelligence.raw.get("tradingagents") if isinstance(intelligence.raw, dict) else {}
        external_risk_evidence = (
            tradingagents.get("risk_debate_summary")
            if isinstance(tradingagents, dict)
            and isinstance(tradingagents.get("risk_debate_summary"), dict)
            else {}
        )
        finrobot = intelligence.raw.get("finrobot") if isinstance(intelligence.raw, dict) else {}
        external_report_evidence = (
            finrobot.get("report_projection")
            if isinstance(finrobot, dict) and isinstance(finrobot.get("report_projection"), dict)
            else {}
        )
        action = action_to_signal_action(structured_decision.action, structured_decision.rating)
        if not data_ready:
            action = "hold"
        return TradingSignal(
            symbol=request.symbol,
            market=request.market,
            action=action,
            # Legacy field retained for API compatibility, but explicitly typed as
            # an uncalibrated rule-score magnitude rather than a probability.
            confidence=(
                calibrated_score.get("probability")
                if calibrated_score.get("status") == "calibrated"
                else round(abs(rule_score), 3) if data_ready else None
            ),
            horizon=request.horizon,
            reason=(
                f"{reason}\n\nUnified strategy score: {score:.3f}.\n"
                f"FinGPT forecast: {forecast_projection.get('direction', 'unknown')} "
                f"({forecast_projection.get('bin_label', 'n/a')}).\n"
                f"Portfolio rating: {structured_decision.rating.value}; "
                f"Trader action: {structured_decision.action.value}."
            ),
            entry_price=structured_decision.entry_price if data_ready else None,
            target_price=structured_decision.target_price if data_ready else None,
            stop_loss=structured_decision.stop_loss if data_ready else None,
            position_size_pct=structured_decision.position_size_pct,
            rule_score=round(rule_score, 3) if data_ready else None,
            confidence_type=(
                "calibrated_probability" if calibrated_score.get("status") == "calibrated"
                else "rule_score" if data_ready else "none"
            ),
            confidence_calibrated=calibrated_score.get("status") == "calibrated",
            rule_set_id="open_stock_ai.strategy_v2",
            price_method_id=structured_decision.price_method_id if data_ready else None,
            decision_status="ready" if data_ready else "insufficient_data",
            evidence=intelligence.evidence,
            source_modules=list(intelligence.raw.keys()),
            decision_schema={
                **structured_decision.to_dict(),
                "strategy_artifact": strategy_artifact,
                "score_calibration": calibrated_score,
            },
            external_risk_evidence=external_risk_evidence,
            external_report_evidence=external_report_evidence,
        )

    def _number(self, value: object) -> float | None:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
