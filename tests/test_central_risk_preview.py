from __future__ import annotations

from types import SimpleNamespace

from open_stock_ai.risk.risk_engine import RiskEngine
from stock_ai import central_risk_adapter
from stock_ai.models import AssetSummary, AssetWorkspace, PositionDetail, ReadonlyWorkspaceMeta


def _workspace(*, cash: float = 100_000.0, positions: list[PositionDetail] | None = None) -> AssetWorkspace:
    positions = positions or []
    holdings = sum(item.market_value for item in positions)
    return AssetWorkspace(
        meta=ReadonlyWorkspaceMeta(
            status="read_only",
            generated_at="2026-07-15T00:00:00+00:00",
            data_sources=["open_stock_ai.paper_oms"],
        ),
        summary=AssetSummary(
            total_assets=cash + holdings,
            cash_available=cash,
            holdings_market_value=holdings,
            today_pnl=0.0,
            unrealized_pnl=sum(item.unrealized_pnl for item in positions),
            realized_pnl=0.0,
            dividend_income=0.0,
            note="paper",
        ),
        positions=positions,
    )


def test_central_risk_engine_preview_blocks_cash_and_notional():
    result = RiskEngine(max_position_size_pct=10.0).evaluate_order_preview(
        symbol="2330.TW",
        side="buy",
        quantity_shares=2_000,
        reference_price=100.0,
        industry="半導體",
        account_summary={"total_equity": 100_000.0, "cash_balance": 50_000.0, "today_pnl": 0.0, "positions": []},
    )

    gates = {item["code"]: item for item in result["gate_checks"]}
    assert result["approved"] is False
    assert gates["cash_available"]["passed"] is False
    assert gates["order_notional"]["passed"] is False
    assert result["method"] == "central_risk_engine_order_preview"


def test_central_risk_engine_preview_uses_existing_position_and_industry_exposure():
    result = RiskEngine(
        max_position_size_pct=10.0,
        max_symbol_exposure_pct=20.0,
        max_total_paper_exposure_pct=100.0,
        max_industry_exposure_pct=45.0,
    ).evaluate_order_preview(
        symbol="2330.TW",
        side="buy",
        quantity_shares=100,
        reference_price=100.0,
        industry="半導體",
        account_summary={
            "total_equity": 100_000.0,
            "cash_balance": 60_000.0,
            "today_pnl": 0.0,
            "positions": [
                {"symbol": "2330.TW", "industry": "半導體", "quantity": 100, "market_value": 15_000.0},
                {"symbol": "2454.TW", "industry": "半導體", "quantity": 100, "market_value": 25_000.0},
            ],
        },
    )

    gates = {item["code"]: item for item in result["gate_checks"]}
    assert gates["symbol_exposure"]["passed"] is False
    assert gates["industry_exposure"]["passed"] is False
    assert result["metrics"]["proposed_symbol_exposure_pct"] == 25.0
    assert result["metrics"]["proposed_industry_exposure_pct"] == 50.0


def test_ui_risk_adapter_delegates_to_runtime_risk_engine(monkeypatch):
    risk = RiskEngine(
        max_position_size_pct=10.0,
        max_daily_loss_pct=3.0,
        max_symbol_exposure_pct=20.0,
        max_total_paper_exposure_pct=100.0,
        max_industry_exposure_pct=45.0,
    )
    monkeypatch.setattr(
        central_risk_adapter,
        "get_runtime_engine",
        lambda: SimpleNamespace(pipeline=SimpleNamespace(risk=risk)),
    )
    workspace = _workspace(cash=100_000.0)

    summary = central_risk_adapter.build_risk_summary(
        asset_workspace=workspace,
        symbol="2330.TW",
        side="buy",
        quantity_lots=1,
        reference_price=5.0,
        industry="半導體",
    )

    assert summary.order_allowed is True
    assert summary.max_single_trade_risk_percent == 10.0
    assert summary.max_position_concentration_percent == 20.0
    assert summary.max_industry_exposure_percent == 45.0
    assert summary.alerts[0].code == "paper_mode"


def test_ui_risk_adapter_blocks_sell_without_paper_position(monkeypatch):
    risk = RiskEngine()
    monkeypatch.setattr(
        central_risk_adapter,
        "get_runtime_engine",
        lambda: SimpleNamespace(pipeline=SimpleNamespace(risk=risk)),
    )

    summary = central_risk_adapter.build_risk_summary(
        asset_workspace=_workspace(),
        symbol="2330.TW",
        side="sell",
        quantity_lots=1,
        reference_price=100.0,
        industry="半導體",
    )

    assert summary.order_allowed is False
    assert any(item.code == "position_available" and item.level == "block" for item in summary.alerts)
