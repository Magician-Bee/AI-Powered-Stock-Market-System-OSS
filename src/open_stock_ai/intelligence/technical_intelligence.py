from __future__ import annotations

from dataclasses import dataclass
from statistics import pstdev
from typing import Any


@dataclass
class TechnicalIntelligence:
    def analyze(self, ohlcv: list[dict[str, Any]]) -> dict[str, Any]:
        closes = [float(row.get("close")) for row in ohlcv if row.get("close") is not None]
        if len(closes) < 2:
            return {
                "technical_view": "neutral",
                "change_percent": 0.0,
                "momentum_score": 0.0,
                "volatility_percent": 0.0,
                "evidence": ohlcv[-3:],
                "risks": ["Insufficient OHLCV for technical analysis."],
            }
        change_percent = round((closes[-1] - closes[0]) / closes[0] * 100, 3) if closes[0] else 0.0
        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1]
        ]
        volatility_percent = round(pstdev(returns) * 100, 3) if len(returns) > 1 else 0.0
        ma_short = sum(closes[-5:]) / min(len(closes), 5)
        ma_long = sum(closes[-20:]) / min(len(closes), 20)
        momentum_score = max(-1.0, min(1.0, change_percent / 10))
        if ma_short > ma_long and change_percent > 0:
            view = "uptrend"
        elif ma_short < ma_long and change_percent < 0:
            view = "downtrend"
        else:
            view = "neutral"
        return {
            "technical_view": view,
            "change_percent": change_percent,
            "momentum_score": round(momentum_score, 3),
            "volatility_percent": volatility_percent,
            "ma_short": round(ma_short, 3),
            "ma_long": round(ma_long, 3),
            "evidence": ohlcv[-3:],
            "risks": ["High short-term volatility."] if volatility_percent > 3 else [],
        }
