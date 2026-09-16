from __future__ import annotations

from typing import Any


def deterministic_risk(feature: dict[str, Any]) -> dict[str, Any]:
    quality = feature.get("data_quality") or {}
    change = abs(float(feature.get("change_percent") or 0))
    trade_value = float(feature.get("trade_value") or 0)
    flags: list[str] = []
    if quality.get("status") not in {"ready", "partial"}:
        flags.append("data_quality_block")
    if trade_value < 10_000_000:
        flags.append("liquidity_block")
    if change >= 8:
        flags.append("extreme_daily_move")
    blocked = bool(flags)
    return {
        "status": "blocked" if blocked else "passed",
        "risk_level": "high" if blocked or change >= 6 else "medium" if change >= 3 else "low",
        "flags": flags,
        "method": "deterministic_home_market_gate.v1",
        "host_verified": True,
        "trade_value_threshold": 10_000_000,
    }
