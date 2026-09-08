from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FinancialReportIntelligence:
    def analyze(self, financials: dict[str, Any]) -> dict[str, Any]:
        revenue = financials.get("revenue") if isinstance(financials, dict) else {}
        eps = financials.get("eps") if isinstance(financials, dict) else {}
        revenue_yoy = self._float((revenue or {}).get("yoy_change_percent"))
        eps_value = self._float((eps or {}).get("value"))
        if revenue_yoy is None and eps_value is None:
            view = "neutral"
            score = 0.0
        else:
            score = 0.0
            if revenue_yoy is not None:
                score += max(-1.0, min(1.0, revenue_yoy / 30)) * 0.7
            if eps_value is not None:
                score += (0.3 if eps_value > 0 else -0.3)
            view = "positive" if score > 0.2 else "negative" if score < -0.2 else "neutral"
        return {
            "method": "financial_report_snapshot",
            "financial_view": view,
            "score": round(score, 4),
            "revenue_yoy_change_percent": revenue_yoy,
            "eps": eps_value,
            "risks": ["Revenue growth is negative."] if revenue_yoy is not None and revenue_yoy < 0 else [],
        }

    def _float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
