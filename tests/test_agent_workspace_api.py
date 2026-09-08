from __future__ import annotations

from fastapi.testclient import TestClient

from open_stock_ai.agent_workspace import _analysis_snapshot
from stock_ai.main import app
from stock_ai.agent_tools import _compact_workspace


client = TestClient(app)


def test_agent_tool_manifest_endpoint(monkeypatch):
    monkeypatch.setattr(
        "open_stock_ai.api.build_agent_tool_manifest",
        lambda: {
            "schema_version": "open_stock_ai.agent_tool_manifest.v1",
            "analysis_endpoints": ["/api/open-stock-ai/agent/workspace"],
            "rules": ["workspace calls never submit orders"],
        },
    )

    response = client.get("/api/open-stock-ai/agent/tool-manifest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "open_stock_ai.agent_tool_manifest.v1"
    assert "/api/open-stock-ai/agent/workspace" in payload["analysis_endpoints"]


def test_agent_workspace_endpoint_is_analysis_only(monkeypatch):
    def fake_workspace(symbol="2330.TW", market="TW", horizon="swing"):
        return {
            "schema_version": "open_stock_ai.agent_workspace.v1",
            "symbol": symbol,
            "market": market,
            "horizon": horizon,
            "recommendation_bucket": "buy_candidate",
            "execution_permission": "blocked",
            "analysis_only": True,
            "portfolio_status": {
                "source_of_truth": "open_stock_ai.trade_store",
                "demo_asset_workspace_used": False,
            },
            "execution_boundary": "analysis_only_no_order_submission",
        }

    monkeypatch.setattr("open_stock_ai.api.build_agent_workspace", fake_workspace)
    response = client.get(
        "/api/open-stock-ai/agent/workspace?symbol=2454.TW&market=TW&horizon=weekly"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "2454.TW"
    assert payload["recommendation_bucket"] == "buy_candidate"
    assert payload["execution_permission"] == "blocked"
    assert payload["analysis_only"] is True
    assert payload["portfolio_status"]["demo_asset_workspace_used"] is False
    assert payload["execution_boundary"] == "analysis_only_no_order_submission"


def test_agent_analysis_snapshot_keeps_bounded_market_evidence_and_source_context():
    snapshot = _analysis_snapshot(
        {
            "market_snapshot": {
                "price": 37.5,
                "ohlcv": [
                    {"date": "2026-08-29", "open": 37.0, "high": 38.0, "low": 36.8, "close": 37.5, "volume": 1000},
                ],
                "financials": {"revenue": {"period": "2026-07", "yoy_change_percent": 8.2}},
            },
            "intelligence": {
                "summary": "verified local summary",
                "technical_view": "uptrend",
                "fundamental_view": "neutral",
                "sentiment_score": 0.2,
                "sentiment_label": "neutral",
                "risks": ["delayed source"],
                "raw": {
                    "tradingagents": {
                        "technical_metrics": {
                            "technical_view": "uptrend",
                            "change_percent": 1.5,
                            "ma_short": 37.2,
                            "ma_long": 36.4,
                        }
                    },
                    "fingpt": {"forecast_projection": {"direction": "up", "bin_label": "positive"}},
                    "finrobot": {"report_projection": {"valuation": {"status": "limited"}}},
                },
            },
        },
        {
            "price_source": "twse",
            "exchange_timestamp": "2026-08-29T13:30:00+08:00",
            "analysis_ready": True,
            "decision_ready": False,
            "blockers": ["official_cross_source_missing"],
        },
    )

    compact = _compact_workspace({"symbol": "2887.TW", "analysis_snapshot": snapshot})

    assert compact["analysis_snapshot"]["price"] == 37.5
    assert compact["analysis_snapshot"]["latest_ohlcv"][0]["close"] == 37.5
    assert compact["analysis_snapshot"]["technical"]["technical_view"] == "uptrend"
    assert compact["analysis_snapshot"]["fundamentals"]["revenue"]["yoy_change_percent"] == 8.2
    assert compact["analysis_snapshot"]["source_context"]["decision_ready"] is False
    assert compact["analysis_snapshot"]["source_context"]["blockers"] == ["official_cross_source_missing"]
    assert compact["execution_boundary"] is None


def test_agent_watchlist_endpoint_separates_candidates_from_execution(monkeypatch):
    monkeypatch.setattr(
        "open_stock_ai.api.build_agent_watchlist",
        lambda horizon="swing", limit=20: {
            "schema_version": "open_stock_ai.agent_watchlist.v1",
            "horizon": horizon,
            "count": 2,
            "bucket_counts": {
                "buy_candidates": 1,
                "sell_candidates": 0,
                "watch": 1,
                "data_blocked": 0,
            },
            "items": [
                {
                    "symbol": "2330.TW",
                    "recommendation_bucket": "buy_candidate",
                    "execution_permission": "blocked",
                },
                {
                    "symbol": "0050.TW",
                    "recommendation_bucket": "watch",
                    "execution_permission": "blocked",
                },
            ],
            "execution_boundary": "analysis_only_no_order_submission",
        },
    )

    response = client.get("/api/open-stock-ai/agent/watchlist?horizon=swing&limit=20")

    assert response.status_code == 200
    payload = response.json()
    assert payload["bucket_counts"]["buy_candidates"] == 1
    assert payload["items"][0]["recommendation_bucket"] == "buy_candidate"
    assert payload["items"][0]["execution_permission"] == "blocked"
    assert payload["execution_boundary"] == "analysis_only_no_order_submission"


def test_agent_portfolio_endpoint_never_uses_demo_assets(monkeypatch):
    monkeypatch.setattr(
        "open_stock_ai.api.build_agent_portfolio",
        lambda: {
            "schema_version": "open_stock_ai.agent_portfolio.v1",
            "mode": "paper",
            "source_of_truth": "open_stock_ai.trade_store",
            "demo_asset_workspace_used": False,
            "exposure": {
                "schema_version": "open_stock_ai.paper_portfolio_exposure.v1",
                "total_position_size_pct": 0.0,
                "symbols": {},
            },
            "orders": [],
            "execution_boundary": "read_only_paper_ledger",
        },
    )

    response = client.get("/api/open-stock-ai/agent/portfolio")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_of_truth"] == "open_stock_ai.trade_store"
    assert payload["demo_asset_workspace_used"] is False
    assert payload["execution_boundary"] == "read_only_paper_ledger"
