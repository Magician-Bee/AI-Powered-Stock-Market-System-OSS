from __future__ import annotations

from typing import Any

from stock_ai.paper_asset_workspace import get_paper_asset_workspace


def current_paper_positions() -> dict[str, dict[str, Any]]:
    workspace = get_paper_asset_workspace(limit=100)
    return {
        item.symbol.upper(): item.model_dump(mode="json")
        for item in workspace.positions
        if float(item.quantity_shares or 0) > 0
    }


def portfolio_action(
    feature: dict[str, Any],
    position: dict[str, Any],
) -> tuple[str, str, str]:
    change = float(feature.get("change_percent") or 0)
    average_cost = float(position.get("average_cost") or 0)
    close = float(feature.get("close") or position.get("latest_price") or 0)
    pnl_percent = (close - average_cost) / average_cost * 100 if average_cost else 0.0
    weight = float(position.get("weight_percent") or 0)
    if change <= -6 or pnl_percent <= -12:
        return "exit", "退出", "價格或部位損益已觸發高風險退出檢查"
    if change <= -3 or weight > 20 or pnl_percent >= 25:
        return "reduce", "減碼", "部位波動、集中度或既有獲利需要減碼檢查"
    if (
        change >= 1
        and float(feature.get("range_position") or 0) >= 0.65
        and weight <= 10
        and pnl_percent >= -5
    ):
        return "add", "加碼", "趨勢、收盤位置與既有部位權重均符合加碼檢查；仍須遵守單一標的風險上限"
    return "hold", "持有", "尚未觸發退出或減碼條件，持續依失效條件監控"
