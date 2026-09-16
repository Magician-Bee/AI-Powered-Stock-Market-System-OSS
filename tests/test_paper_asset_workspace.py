from __future__ import annotations

from types import SimpleNamespace

from stock_ai import paper_asset_workspace


class _TradeStore:
    def __init__(self, account):
        self.account = account

    def account_summary(self):
        return self.account


def _engine(account):
    return SimpleNamespace(pipeline=SimpleNamespace(trade_store=_TradeStore(account)))


def test_empty_paper_account_does_not_seed_a_demo_position(monkeypatch):
    account = {
        "total_equity": 1_000_000.0,
        "cash_balance": 1_000_000.0,
        "holdings_market_value": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "dividend_income": 0.0,
        "positions": [],
    }
    monkeypatch.setattr(paper_asset_workspace, "get_runtime_engine", lambda: _engine(account))

    workspace = paper_asset_workspace.get_paper_asset_workspace()

    assert workspace.positions == []
    assert workspace.summary.total_assets == 1_000_000.0
    assert workspace.summary.cash_available == 1_000_000.0
    assert workspace.summary.holdings_market_value == 0.0
    assert workspace.summary.today_pnl == 0.0
    assert workspace.meta.status == "read_only"
    assert "single_symbol_preview_seed" not in str(workspace.meta.fallback)
    assert "open_stock_ai.paper_oms" in workspace.meta.data_sources
    assert "open_stock_ai.corporate_action_ledger" in workspace.meta.data_sources
    assert "unavailable_without_corporate_action_ledger" not in str(workspace.meta.fallback)


def test_paper_asset_workspace_maps_persistent_position(monkeypatch):
    account = {
        "total_equity": 1_010_000.0,
        "cash_balance": 900_000.0,
        "holdings_market_value": 110_000.0,
        "unrealized_pnl": 10_000.0,
        "realized_pnl": 2_500.0,
        "dividend_income": 800.0,
        "positions": [
            {
                "symbol": "2330.TW",
                "market": "TW",
                "quantity": 1000,
                "average_cost": 100.0,
                "last_price": 110.0,
                "market_value": 110_000.0,
                "unrealized_pnl": 10_000.0,
                "realized_pnl": 2_500.0,
            }
        ],
    }
    monkeypatch.setattr(paper_asset_workspace, "get_runtime_engine", lambda: _engine(account))

    workspace = paper_asset_workspace.get_paper_asset_workspace()

    assert len(workspace.positions) == 1
    position = workspace.positions[0]
    assert position.symbol == "2330.TW"
    assert position.quantity_shares == 1000
    assert position.average_cost == 100.0
    assert position.latest_price == 110.0
    assert position.unrealized_pnl == 10_000.0
    assert position.unrealized_pnl_percent == 10.0
    assert workspace.summary.realized_pnl == 2_500.0
    assert workspace.summary.dividend_income == 800.0
