from __future__ import annotations

"""A small, frozen research candidate family with explicit input dependencies.

These are economically motivated hypotheses, not promoted profitable models.
No parameters are selected from validation/holdout performance. The same signal
API and execution policy are used by historical replay and the trading host.
"""

from dataclasses import asdict, dataclass
from math import isfinite
from statistics import mean
from typing import Any

from open_stock_ai.strategy.execution_policy import POLICY_ID
from open_stock_ai.strategy.provenance import content_hash, strategy_source_manifest
from open_stock_ai.types import IntelligenceResult, MarketSnapshot, StockRequest, TradingSignal


_FROZEN_PARAMETERS = {"fast_window":20,"slow_window":60,"atr_window":14,"stop_atr":2.0,
                      "target_atr":3.0,"position_size_pct":5.0,"maximum_holding_bars":20}


@dataclass(frozen=True)
class CandleCandidate:
    candidate_id: str = "tw_candle_breakout_20_60_v1"
    fast_window: int = 20
    slow_window: int = 60
    atr_window: int = 14
    stop_atr: float = 2.0
    target_atr: float = 3.0
    position_size_pct: float = 5.0
    maximum_holding_bars: int = 20

    def __post_init__(self) -> None:
        if self.candidate_id not in {"tw_candle_breakout_20_60_v1", "tw_candle_pullback_20_60_v1"}:
            raise ValueError("unknown_frozen_candle_candidate")
        if not 1 < self.atr_window <= self.fast_window < self.slow_window:
            raise ValueError("invalid_candle_windows")
        if not 0 < self.position_size_pct <= 100 or self.stop_atr <= 0 or self.target_atr <= 0:
            raise ValueError("invalid_candle_risk_configuration")
        if not all(isfinite(v) for v in (self.stop_atr,self.target_atr)) or self.maximum_holding_bars < 1:
            raise ValueError("invalid_candle_risk_configuration")

    def policy_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.candle_candidate.v1",
            "candidate_id": self.candidate_id, "configuration": asdict(self),
            "registered_frozen_configuration": all(getattr(self,k)==v for k,v in _FROZEN_PARAMETERS.items()),
            "execution_policy_id": POLICY_ID,
            "required_inputs": ["completed_ohlcv_bars", "bar_timestamps", "source_provenance",
                                "current_position_and_equity", "venue_product_cost_assumptions"],
            "optional_inputs": [], "required_external_models": [],
            "hypothesis": ("medium_term_trend_continuation_after_prior_high_breakout"
                           if "breakout" in self.candidate_id
                           else "trend_continuation_after_pullback_and_mean_reclaim"),
            "candidate_count": 2, "selection_method": "fixed_before_evaluation_no_parameter_search",
            "entry_trigger": "next_bar_after_completed_candle_signal",
            "exit_rules": ["per_entry_atr_stop", "per_entry_atr_target", "close_below_slow_mean", "holding_expiry"],
            "research_candidate": True, "positive_expectancy_proven": False,
            "full_market_universe_claim": False,
        }

    def strategy_version_hash(self) -> str:
        return content_hash(strategy_source_manifest(self))

    def generate_signal(
        self, request: StockRequest, market_snapshot: MarketSnapshot,
        intelligence: IntelligenceResult | None = None,
    ) -> TradingSignal:
        rows = market_snapshot.ohlcv
        action = "hold"
        reason = "Insufficient completed candles for the frozen candidate."
        entry = stop = target = None
        size = 0.0
        required = self.slow_window + 1
        if len(rows) >= required:
            bars = rows[-required:]
            try:
                closes = [float(r["close"]) for r in bars]
                highs = [float(r["high"]) for r in bars]
                lows = [float(r["low"]) for r in bars]
                volumes = [float(r["volume"]) for r in bars]
                if any(not isfinite(v) or v <= 0 for v in closes + highs + lows):
                    raise ValueError("invalid_candles")
                if any(not isfinite(v) or v < 0 for v in volumes):
                    raise ValueError("invalid_volume")
                price = closes[-1]
                slow = mean(closes[-self.slow_window:])
                fast = mean(closes[-self.fast_window:])
                previous_fast = mean(closes[-self.fast_window-1:-1])
                true_ranges = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
                                   abs(lows[i]-closes[i-1])) for i in range(1, len(bars))]
                atr = mean(true_ranges[-self.atr_window:])
                position = float(market_snapshot.raw.get("position_quantity") or 0)
                holding_bars = int(market_snapshot.raw.get("holding_bars") or 0)
                if position > 0:
                    if price < slow or holding_bars >= self.maximum_holding_bars:
                        action, size = "sell", 100.0
                        reason = "Frozen trend invalidation or holding-period expiry; close the owned position."
                    else:
                        reason = "Keep the existing position and its original protective bracket."
                else:
                    if "breakout" in self.candidate_id:
                        enter = (price > max(highs[-self.fast_window-1:-1]) and price > slow
                                 and volumes[-1] >= mean(volumes[-self.fast_window-1:-1]))
                    else:
                        enter = price > slow and closes[-2] < previous_fast and price >= fast
                    if enter and atr > 0 and price - self.stop_atr * atr > 0:
                        action, size = "buy", self.position_size_pct
                        entry, stop, target = price, price-self.stop_atr*atr, price+self.target_atr*atr
                        reason = "Frozen candle hypothesis triggered; ATR levels are rule assumptions, not calibrated forecasts."
                    else:
                        reason = "The completed candles do not trigger this frozen candidate."
            except (KeyError, TypeError, ValueError, OverflowError):
                reason = "Candle input is invalid; no executable signal."
        return TradingSignal(
            symbol=request.symbol, market=request.market, horizon=request.horizon,
            action=action, confidence=None, confidence_type="none", confidence_calibrated=False,
            reason=reason, entry_price=entry, stop_loss=stop, target_price=target,
            position_size_pct=size, rule_set_id=self.candidate_id,
            price_method_id="frozen_atr_rule_research.v1" if entry is not None else None,
            decision_schema={"candidate_id": self.candidate_id, "candidate_count": 2,
                             "execution_policy_id": POLICY_ID,
                             "qualification_required_for_positive_ev_claim": True},
        )


def frozen_candidates() -> tuple[CandleCandidate, CandleCandidate]:
    return (CandleCandidate(), CandleCandidate(candidate_id="tw_candle_pullback_20_60_v1"))
