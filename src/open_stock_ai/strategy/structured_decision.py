from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from open_stock_ai.types import Action, Horizon, IntelligenceResult, MarketSnapshot

from .target_stop_calibration import PRICE_METHOD_ID, calibrate_target_stop


class PortfolioRating(str, Enum):
    """Internal 5-tier portfolio scale aligned with TradingAgents schemas."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class TraderAction(str, Enum):
    """Internal 3-tier transaction direction aligned with TradingAgents trader output."""

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


@dataclass
class StructuredDecision:
    rating: PortfolioRating
    action: TraderAction
    rationale: str
    entry_price: float | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    position_size_pct: float = 0.0
    time_horizon: Horizon = "swing"
    source_ratings: dict[str, Any] = field(default_factory=dict)
    price_method_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rating"] = self.rating.value
        payload["action"] = self.action.value
        return payload


def action_to_signal_action(action: TraderAction, rating: PortfolioRating) -> Action:
    if action == TraderAction.BUY:
        return "buy" if rating == PortfolioRating.BUY else "add"
    if action == TraderAction.SELL:
        return "sell" if rating == PortfolioRating.SELL else "reduce"
    return "hold"


def build_structured_decision(
    *,
    score: float,
    snapshot: MarketSnapshot,
    intelligence: IntelligenceResult,
    horizon: Horizon,
) -> StructuredDecision:
    rating = _rating_from_score(score)
    action = _action_from_rating(rating)
    entry_price = _round_price(snapshot.price)
    direction = "long" if action == TraderAction.BUY else "short" if action == TraderAction.SELL else "hold"
    calibration = calibrate_target_stop(
        snapshot=snapshot,
        intelligence=intelligence,
        direction=direction,
        horizon=horizon,
    )
    position_size_pct = 0.0
    rationale = _rationale(score, intelligence)
    return StructuredDecision(
        rating=rating,
        action=action,
        rationale=rationale,
        entry_price=entry_price,
        target_price=calibration.target_price,
        stop_loss=calibration.stop_loss,
        position_size_pct=position_size_pct,
        time_horizon=horizon,
        price_method_id=PRICE_METHOD_ID if calibration.calibrated else None,
        source_ratings={
            "sentiment": {
                "label": intelligence.sentiment_label,
                "score": intelligence.sentiment_score,
            },
            "fundamental": intelligence.fundamental_view,
            "technical": intelligence.technical_view,
            "unified_score": round(score, 3),
            "rule_score": round(score, 3),
            "score_type": "uncalibrated_rule_score",
            "calibrated": False,
            "target_stop_calibration": calibration.receipt,
        },
    )


def _rating_from_score(score: float) -> PortfolioRating:
    if score >= 0.55:
        return PortfolioRating.BUY
    if score >= 0.35:
        return PortfolioRating.OVERWEIGHT
    if score <= -0.55:
        return PortfolioRating.SELL
    if score <= -0.35:
        return PortfolioRating.UNDERWEIGHT
    return PortfolioRating.HOLD


def _action_from_rating(rating: PortfolioRating) -> TraderAction:
    if rating in {PortfolioRating.BUY, PortfolioRating.OVERWEIGHT}:
        return TraderAction.BUY
    if rating in {PortfolioRating.SELL, PortfolioRating.UNDERWEIGHT}:
        return TraderAction.SELL
    return TraderAction.HOLD


def _rationale(score: float, intelligence: IntelligenceResult) -> str:
    signals = [
        f"sentiment={intelligence.sentiment_label or 'unknown'}",
        f"fundamental={intelligence.fundamental_view or 'unknown'}",
        f"technical={intelligence.technical_view or 'unknown'}",
    ]
    return (
        "Structured decision uses uncalibrated Open Stock AI rule score "
        f"{score:.3f}; " + ", ".join(signals) + "."
    )


def _round_price(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)
