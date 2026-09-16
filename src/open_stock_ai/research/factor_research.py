from __future__ import annotations

from dataclasses import dataclass
from statistics import pstdev
from typing import Any

from open_stock_ai.types import MarketSnapshot, StockRequest, TradingSignal


@dataclass
class FactorResearch:
    """Small Qlib-inspired factor projection using normalized local features.

    This is intentionally labelled as a projection, not a trained Qlib model.
    Agents may use it as advisory evidence, but RiskEngine must not treat it as
    empirical model validation.
    """

    def evaluate(self, request: StockRequest, signal: TradingSignal, snapshot: MarketSnapshot) -> dict[str, Any]:
        closes = [float(row.get("close")) for row in snapshot.ohlcv if row.get("close") is not None]
        if len(closes) < 5:
            return {
                "score": None,
                "model_score": None,
                "rank_ic_proxy": None,
                "rank_ic_method": "single_symbol_forward_return_proxy",
                "rank_ic_is_cross_sectional": False,
                "rank_ic_warning": "This is a single-symbol proxy, not a cross-sectional Rank IC.",
                "passed": False,
                "advisory_ready": False,
                "empirical_valid": False,
                "runtime_connected": False,
                "execution_evidence_eligible": False,
                "report_path": None,
                "note": "Insufficient OHLCV for factor projection.",
            }
        momentum = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] else 0.0
        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1]
        ]
        volatility = pstdev(returns[-20:]) if len(returns) > 1 else 0.0
        score = max(-1.0, min(1.0, momentum * 5 - volatility * 2))
        rank_ic_proxy = max(-1.0, min(1.0, momentum / max(volatility * 5, 0.01)))
        model_score = (score + 1.0) / 2.0
        aligned = (
            (signal.action in {"buy", "add"} and score > 0)
            or (signal.action in {"sell", "reduce"} and score < 0)
            or signal.action == "hold"
        )
        return {
            "score": round(score, 3),
            "model_score": round(model_score, 3),
            "rank_ic_proxy": round(rank_ic_proxy, 3),
            # Keep the established discriminator for API/report compatibility,
            # while exposing the exact statistical limitation as explicit fields.
            "rank_ic_method": "single_symbol_forward_return_proxy",
            "rank_ic_is_cross_sectional": False,
            "rank_ic_warning": "This is a single-symbol proxy, not a cross-sectional Rank IC.",
            "passed": aligned and score > -0.35,
            "advisory_ready": True,
            "empirical_valid": False,
            "runtime_connected": False,
            "execution_evidence_eligible": False,
            "approval_blockers": ["qlib_runtime_not_connected", "cross_sectional_validation_not_completed"],
            "report_path": None,
            "factors": {
                "momentum_5d": round(momentum, 4),
                "volatility_20d": round(volatility, 4),
            },
            "note": (
                "Lightweight factor projection inspired by Qlib workflow boundaries. "
                "No Qlib model was trained or inferred, and the rank-IC value is only a proxy."
            ),
        }
