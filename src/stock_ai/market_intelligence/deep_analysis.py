from __future__ import annotations

from typing import Any


# The Agent Market Radar request contract accepts at most twenty symbols.  Keep
# the deterministic funnel and every overlay transition bounded by the same
# value so a successful market scan can never create an invalid Agent request.
MAX_DEEP_ANALYSIS_SYMBOLS = 20


def select_deep_analysis_symbols(
    snapshot: dict[str, Any],
    limit: int = MAX_DEEP_ANALYSIS_SYMBOLS,
) -> list[str]:
    """Limit expensive model analysis to the deterministic candidate funnel."""

    rankings = snapshot.get("rankings") or {}
    ordered: list[str] = []
    for category in (
        "actionable_now",
        "near_actionable",
        "wait_for_pullback",
        "wait_for_breakout",
        "high_risk",
    ):
        for symbol in rankings.get(category) or []:
            if symbol not in ordered:
                ordered.append(symbol)
            if len(ordered) >= max(1, min(limit, MAX_DEEP_ANALYSIS_SYMBOLS)):
                return ordered
    return ordered
