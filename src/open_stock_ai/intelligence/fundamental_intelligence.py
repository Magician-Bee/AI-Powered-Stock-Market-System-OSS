from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FundamentalIntelligence:
    def analyze(self, financials: dict[str, Any], chips: dict[str, Any] | None = None) -> dict[str, Any]:
        revenue = financials.get("revenue") if isinstance(financials, dict) else {}
        margin = (chips or {}).get("margin") if isinstance(chips, dict) else {}
        revenue_yoy = self._float((revenue or {}).get("yoy_change_percent"))
        margin_balance = self._float((margin or {}).get("margin_balance"))
        score = 0.0
        if revenue_yoy is not None:
            score += max(-1.0, min(1.0, revenue_yoy / 30)) * 0.8
        if margin_balance is not None and margin_balance > 0:
            score += 0.1
        view = "positive" if score > 0.2 else "negative" if score < -0.2 else "neutral"
        return {
            "method": "fundamental_snapshot",
            "fundamental_view": view,
            "score": round(score, 4),
            "revenue_yoy_change_percent": revenue_yoy,
            "margin_balance": margin_balance,
            "risks": ["Weak fundamental score."] if score < -0.2 else [],
        }

    def _float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
