from __future__ import annotations


def cap_position_size(size_pct: float, max_pct: float) -> float:
    return min(max(size_pct, 0.0), max_pct)
