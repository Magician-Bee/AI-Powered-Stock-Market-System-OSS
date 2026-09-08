from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BreakoutStrategy:
    lookback: int = 20

    def evaluate(self, ohlcv: list[dict[str, Any]]) -> dict[str, Any]:
        closes = [float(row.get("close")) for row in ohlcv if row.get("close") is not None]
        if len(closes) < 2:
            return {"method": "breakout_range", "signal": "hold", "score": 0.0, "reason": "Insufficient prices."}
        prior = closes[-self.lookback - 1 : -1] or closes[:-1]
        high = max(prior)
        low = min(prior)
        latest = closes[-1]
        range_width = high - low
        if latest > high:
            signal = "buy"
            score = (latest - high) / high if high else 0.0
        elif latest < low:
            signal = "sell"
            score = -((low - latest) / low) if low else 0.0
        else:
            signal = "hold"
            midpoint = low + range_width / 2
            score = ((latest - midpoint) / range_width) if range_width else 0.0
        return {
            "method": "breakout_range",
            "signal": signal,
            "score": max(-1.0, min(1.0, score * 10)),
            "range_high": round(high, 4),
            "range_low": round(low, 4),
            "latest_close": round(latest, 4),
        }
