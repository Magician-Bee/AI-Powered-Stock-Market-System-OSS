from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from open_stock_ai.runtime import get_runtime_engine

from .models import AssetSummary, AssetWorkspace, PositionDetail, ReadonlyWorkspaceMeta


def get_paper_asset_workspace(limit: int = 4) -> AssetWorkspace:
    """Return the UI asset read model from the same Paper OMS used by RiskEngine.

    No position is invented when the ledger is empty. Prices are the latest paper
    fill prices until a mark-to-market service is connected, and that limitation is
    surfaced in metadata rather than hidden behind demonstration values.
    """
    engine = get_runtime_engine()
    trade_store = engine.pipeline.trade_store
    account = trade_store.account_summary() if trade_store is not None else _empty_account()
    positions = [
        _position_detail(item, total_equity=float(account.get("total_equity") or 0.0))
        for item in (account.get("positions") or [])[: max(1, min(limit, 100))]
        if isinstance(item, dict)
    ]
    summary = AssetSummary(
        total_assets=round(float(account.get("total_equity") or 0.0), 2),
        cash_available=round(float(account.get("cash_balance") or 0.0), 2),
        holdings_market_value=round(float(account.get("holdings_market_value") or 0.0), 2),
        today_pnl=0.0,
        unrealized_pnl=round(float(account.get("unrealized_pnl") or 0.0), 2),
        realized_pnl=round(float(account.get("realized_pnl") or 0.0), 2),
        dividend_income=round(float(account.get("dividend_income") or 0.0), 2),
        note=(
            "本機 Paper OMS 模擬帳戶。現金、委託、成交、部位與損益由持久化帳本重建；"
            "今日損益尚未接入逐筆市價重估；股利收入由已套用的官方公司行動帳本加總。"
        ),
    )
    meta = ReadonlyWorkspaceMeta(
        status="read_only",
        broker_api_connected=False,
        order_submission_enabled=False,
        generated_at=datetime.now(timezone.utc).isoformat(),
        data_sources=[
            "open_stock_ai.paper_oms",
            "open_stock_ai.cash_ledger",
            "open_stock_ai.paper_fills",
            "open_stock_ai.corporate_action_ledger",
        ],
        limitations=[
            "這是模擬帳戶，不是券商真實資產。",
            "持倉估值使用最新紙上成交價，尚未接入持續 mark-to-market。",
            "手續費、交易稅與滑價由 .env 的 Paper OMS 模擬參數決定。",
            "未成交時不會自動產生任何示範持倉。",
        ],
        fallback={
            "valuation": "latest_paper_fill_price",
            "today_pnl": "unavailable_without_mark_to_market_history",
        },
    )
    return AssetWorkspace(meta=meta, summary=summary, positions=positions)


def _position_detail(item: dict[str, Any], *, total_equity: float) -> PositionDetail:
    quantity = float(item.get("quantity") or 0.0)
    average_cost = float(item.get("average_cost") or 0.0)
    latest_price = float(item.get("last_price") or 0.0)
    market_value = float(item.get("market_value") or 0.0)
    unrealized = float(item.get("unrealized_pnl") or 0.0)
    cost_basis = average_cost * quantity
    unrealized_pct = unrealized / cost_basis * 100.0 if cost_basis else 0.0
    weight = market_value / total_equity * 100.0 if total_equity else 0.0
    symbol = str(item.get("symbol") or "")
    return PositionDetail(
        symbol=symbol,
        name=symbol,
        industry=None,
        quantity_shares=max(0.0, round(quantity, 8)),
        average_cost=round(average_cost, 4),
        latest_price=round(latest_price, 4),
        market_value=round(market_value, 2),
        unrealized_pnl=round(unrealized, 2),
        unrealized_pnl_percent=round(unrealized_pct, 2),
        weight_percent=round(weight, 2),
        data_sources=["open_stock_ai.paper_oms", "latest_paper_fill_price"],
    )


def _empty_account() -> dict[str, Any]:
    return {
        "schema_version": "open_stock_ai.paper_account.v1",
        "mode": "paper",
        "is_simulated": True,
        "initial_cash": 0.0,
        "cash_balance": 0.0,
        "holdings_market_value": 0.0,
        "total_equity": 0.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "dividend_income": 0.0,
        "positions": [],
    }
