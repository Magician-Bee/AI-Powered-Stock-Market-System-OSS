from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MAStrategy:
    short_window: int = 5
    long_window: int = 20

    def evaluate(self, ohlcv: list[dict[str, Any]]) -> dict[str, Any]:
        closes = [float(row.get("close")) for row in ohlcv if row.get("close") is not None]
        if not closes:
            return {"method": "moving_average_cross", "signal": "hold", "score": 0.0, "reason": "No close prices."}
        short_values = closes[-self.short_window :]
        long_values = closes[-self.long_window :]
        ma_short = sum(short_values) / len(short_values)
        ma_long = sum(long_values) / len(long_values)
        spread_pct = ((ma_short - ma_long) / ma_long * 100) if ma_long else 0.0
        if spread_pct > 1:
            signal = "buy"
        elif spread_pct < -1:
            signal = "sell"
        else:
            signal = "hold"
        return {
            "method": "moving_average_cross",
            "signal": signal,
            "score": max(-1.0, min(1.0, spread_pct / 5)),
            "ma_short": round(ma_short, 4),
            "ma_long": round(ma_long, 4),
            "spread_pct": round(spread_pct, 4),
        }
