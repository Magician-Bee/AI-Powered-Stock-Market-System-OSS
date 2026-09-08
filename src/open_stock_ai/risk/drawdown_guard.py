from __future__ import annotations


def drawdown_allowed(current_drawdown_pct: float, max_drawdown_pct: float) -> bool:
    return current_drawdown_pct <= max_drawdown_pct
