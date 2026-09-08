from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RSIMACDStrategy:
    rsi_window: int = 14

    def evaluate(self, ohlcv: list[dict[str, Any]]) -> dict[str, Any]:
        closes = [float(row.get("close")) for row in ohlcv if row.get("close") is not None]
        if len(closes) < 3:
            return {"method": "rsi_macd", "signal": "hold", "score": 0.0, "reason": "Insufficient prices."}
        rsi = self._rsi(closes)
        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)
        macd = ema12 - ema26
        if rsi < 35 and macd > 0:
            signal = "buy"
        elif rsi > 70 and macd < 0:
            signal = "sell"
        else:
            signal = "hold"
        rsi_score = (50 - rsi) / 50
        macd_score = macd / closes[-1] * 20 if closes[-1] else 0.0
        return {
            "method": "rsi_macd",
            "signal": signal,
            "score": max(-1.0, min(1.0, rsi_score + macd_score)),
            "rsi": round(rsi, 4),
            "macd": round(macd, 4),
        }

    def _rsi(self, closes: list[float]) -> float:
        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        recent = changes[-self.rsi_window :]
        gains = [value for value in recent if value > 0]
        losses = [-value for value in recent if value < 0]
        avg_gain = sum(gains) / self.rsi_window
        avg_loss = sum(losses) / self.rsi_window
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _ema(self, closes: list[float], window: int) -> float:
        values = closes[-window:]
        alpha = 2 / (len(values) + 1)
        ema = values[0]
        for value in values[1:]:
            ema = value * alpha + ema * (1 - alpha)
        return ema
