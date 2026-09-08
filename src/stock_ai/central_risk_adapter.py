from __future__ import annotations

from typing import Any

from open_stock_ai.runtime import get_runtime_engine

from .models import AssetWorkspace, RiskAlert, RiskSummary
from .realtime_data import normalize_symbol


_ALERT_COPY = {
    "paper_mode": ("info", "紙上交易模式", "此結果來自中央 RiskEngine，只能用於本機 Paper OMS，不會送往券商。"),
    "cash_available": ("block", "可用現金不足", "預估買入金額高於 Paper OMS 可用現金。"),
    "position_available": ("block", "可賣部位不足", "Paper OMS 目前沒有足夠持倉可供賣出或減碼。"),
    "order_notional": ("block", "單筆部位超限", "本次委託占總資產比重超過中央 RiskEngine 上限。"),
    "symbol_exposure": ("block", "單一標的曝險超限", "交易後單一標的曝險將超過中央 RiskEngine 上限。"),
    "total_exposure": ("block", "總曝險超限", "交易後 Paper OMS 總曝險將超過中央 RiskEngine 上限。"),
    "industry_exposure": ("warning", "同產業曝險偏高", "交易後同產業曝險將超過中央 RiskEngine 提醒線。"),
    "daily_loss": ("block", "單日損失超限", "目前可觀測單日損失已超過中央 RiskEngine 上限。"),
}


def build_risk_summary(
    asset_workspace: AssetWorkspace,
    symbol: str,
    side: str,
    quantity_lots: int,
    reference_price: float,
    industry: str | None,
) -> RiskSummary:
    engine = get_runtime_engine()
    risk_engine = engine.pipeline.risk
    normalized = normalize_symbol(symbol)
    account_summary = {
        "total_equity": asset_workspace.summary.total_assets,
        "cash_balance": asset_workspace.summary.cash_available,
        "today_pnl": asset_workspace.summary.today_pnl,
        "positions": [
            {
                "symbol": normalize_symbol(item.symbol),
                "industry": item.industry,
                "market_value": item.market_value,
                "quantity": item.quantity_shares,
                "position_size_pct": item.weight_percent,
            }
            for item in asset_workspace.positions
        ],
    }
    preview = risk_engine.evaluate_order_preview(
        symbol=normalized,
        side=side,
        quantity_shares=max(1, quantity_lots) * 1000,
        reference_price=reference_price,
        industry=industry,
        account_summary=account_summary,
    )
    metrics = preview["metrics"]
    limits = preview["limits"]
    alerts: list[RiskAlert] = []
    for gate in preview["gate_checks"]:
        if gate.get("code") == "paper_mode" or gate.get("passed") is not True:
            level, title, message = _ALERT_COPY.get(
                str(gate.get("code")),
                ("warning", "中央風控提醒", str(gate.get("message") or "風控條件需要人工確認。")),
            )
            alerts.append(
                RiskAlert(
                    code=str(gate.get("code") or "central_risk"),
                    level=level,  # type: ignore[arg-type]
                    title=title,
                    message=message,
                    metric_value=_number(gate.get("observed")),
                    threshold_value=_number(gate.get("limit")),
                )
            )
    return RiskSummary(
        single_trade_risk_percent=metrics["order_notional_pct"],
        daily_risk_percent=metrics["current_daily_loss_pct"],
        position_concentration_percent=metrics["proposed_symbol_exposure_pct"],
        industry_exposure_percent=metrics["proposed_industry_exposure_pct"],
        max_single_trade_risk_percent=limits["max_position_size_pct"],
        max_daily_risk_percent=limits["max_daily_loss_pct"],
        max_position_concentration_percent=limits["max_symbol_exposure_pct"],
        max_industry_exposure_percent=limits["max_industry_exposure_pct"],
        order_allowed=preview["approved"] is True,
        alerts=alerts,
    )


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None
