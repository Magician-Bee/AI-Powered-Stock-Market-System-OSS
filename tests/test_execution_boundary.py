from __future__ import annotations

from fastapi.testclient import TestClient

from open_stock_ai.data.market_data_hub import MarketDataHub
from open_stock_ai.types import MarketSnapshot
from stock_ai.main import app


client = TestClient(app)


def test_advisory_analysis_does_not_create_paper_order_or_exposure(tmp_path, monkeypatch):
    db_path = tmp_path / "blocked-analysis.sqlite"
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text(
        f"""
system:
  mode: paper
  live_trading_enabled: false
risk:
  min_rule_score_threshold: 0.70
  require_backtest_passed: true
  max_position_size_pct: 10
  max_daily_loss_pct: 3
  max_total_drawdown_pct: 15
  max_symbol_exposure_pct: 20
  max_total_paper_exposure_pct: 100
storage:
  sqlite_path: "{db_path.as_posix()}"
external_projects:
  tradingagents: "external/TradingAgents"
  fingpt: "external/FinGPT"
  finrobot: "external/FinRobot"
  finrl_trading: "external/FinRL-Trading"
  finrl: "external/FinRL"
  qlib: "external/qlib"
  ai_trader: "external/AI-Trader"
watchlist:
  - symbol: "2330.TW"
    market: "TW"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPEN_STOCK_AI_CONFIG", str(config_path))

    def fake_load(self, request):
        return MarketSnapshot(
            symbol=request.symbol,
            market=request.market,
            price=140.0,
            ohlcv=[
                {"date": f"2026-06-{day:02d}", "close": float(100 + day)}
                for day in range(1, 31)
            ],
            news=[
                {"title": "Company reports strong growth and record profit"},
                {"title": "Analysts praise upbeat demand and expansion"},
            ],
            financials={"revenue": {"yoy_change_percent": 24.0}},
            raw={
                "data_contract": {
                    "schema_version": "open_stock_ai.market_data_contract.v2",
                    "decision_ready": True,
                    "execution_eligible": True,
                    "is_realtime": True,
                    "is_fallback": False,
                    "is_simulated": False,
                    "blockers": [],
                }
            },
        )

    monkeypatch.setattr(MarketDataHub, "load", fake_load)

    analysis = client.get("/api/open-stock-ai/analyze?symbol=2330.TW&market=TW&horizon=swing")
    orders = client.get("/api/open-stock-ai/paper-orders?limit=10")
    exposure = client.get("/api/open-stock-ai/paper-exposure")

    assert analysis.status_code == 200
    decision = analysis.json()
    assert decision["signal"]["action"] == "buy"
    assert decision["research"]["passed"] is False
    assert decision["research"]["raw"]["validation_status"]["execution_evidence_eligible"] is False
    assert "point_in_time_dataset_missing" in decision["research"]["raw"]["validation_status"]["blockers"]
    assert decision["risk"]["approved"] is False
    assert decision["execution"]["executed"] is False

    assert orders.status_code == 200
    assert orders.json()["count"] == 0
    assert orders.json()["items"] == []

    assert exposure.status_code == 200
    assert exposure.json()["total_position_size_pct"] == 0.0
    assert exposure.json()["symbols"] == {}
