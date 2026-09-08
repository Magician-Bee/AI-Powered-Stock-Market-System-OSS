import asyncio

from fastapi.testclient import TestClient

from open_stock_ai.runtime import clear_runtime_engine_cache
from stock_ai.main import app


def test_agent_reset_and_legacy_account_endpoint_share_one_sqlite_account(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_SQLITE_PATH", str(tmp_path / "shared-agent-account.sqlite"))
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_ACCOUNT_ID", "shared-agent-account-test")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "1000000")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_COMMISSION_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SELL_TAX_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SLIPPAGE_BPS", "0")
    clear_runtime_engine_cache()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/open-stock-ai/agent/trading/account/reset",
            json={"initial_cash": 10_000_000},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        account = payload["account"]
        assert payload["verified"] is True
        assert account["initial_cash"] == 10_000_000
        assert account["cash_balance"] == 10_000_000
        assert account["total_equity"] == 10_000_000
        assert account["account_revision"].startswith("PA-")
        assert account["source_of_truth"]["account_id"] == "shared-agent-account-test"
        assert account["source_of_truth"]["sqlite_path"].endswith("shared-agent-account.sqlite")

        legacy = client.get("/api/open-stock-ai/agent/paper-training/account")
        assert legacy.status_code == 200, legacy.text
        legacy_account = legacy.json()
        assert legacy_account["initial_cash"] == 10_000_000
        assert legacy_account["cash_balance"] == 10_000_000
        assert legacy_account["total_equity"] == 10_000_000
    finally:
        clear_runtime_engine_cache()


def test_agent_trading_routes_are_exposed():
    paths = set(app.openapi()["paths"])
    assert "/api/open-stock-ai/agent/trading/session" in paths
    assert "/api/open-stock-ai/agent/trading/account/reset" in paths
    assert "/api/open-stock-ai/agent/trading/analyze" in paths
    assert "/api/open-stock-ai/agent/trading/control" in paths

    analysis_schema = app.openapi()["components"]["schemas"]["AgentTradingAnalysisRequest"]
    assert {"symbol", "prompt"}.issubset(analysis_schema["required"])
    assert "default" not in analysis_schema["properties"]["symbol"]
    assert "default" not in analysis_schema["properties"]["prompt"]


def test_agent_trading_session_without_symbol_syncs_account_only(monkeypatch):
    from stock_ai import agent_trading_api

    monkeypatch.setattr(
        agent_trading_api,
        "account_snapshot",
        lambda refresh_prices=False: {"account_id": "default-paper", "total_equity": 1_000_000},
    )

    async def account_status(refresh=False):
        return {"authenticated": True}

    monkeypatch.setattr(agent_trading_api.codex_runtime, "account_status", account_status)
    payload = asyncio.run(agent_trading_api.agent_trading_session(symbol=None))

    assert payload["symbol"] is None
    assert payload["research"] == {}
    assert payload["research_error"] is None
    assert payload["account"]["total_equity"] == 1_000_000


def test_agent_trading_session_keeps_account_when_research_price_is_unavailable(monkeypatch):
    from stock_ai import agent_trading_api

    monkeypatch.setattr(
        agent_trading_api,
        "account_snapshot",
        lambda refresh_prices=False: {"account_id": "default-paper", "total_equity": 1_000_000},
    )
    monkeypatch.setattr(
        agent_trading_api,
        "_compact_research",
        lambda symbol, horizon: (_ for _ in ()).throw(ValueError("temporary quote gap")),
    )

    async def account_status(refresh=False):
        return {"authenticated": True}

    monkeypatch.setattr(agent_trading_api.codex_runtime, "account_status", account_status)
    payload = asyncio.run(agent_trading_api.agent_trading_session(symbol="2330.TW"))

    assert payload["symbol"] == "2330.TW"
    assert payload["research"] == {}
    assert payload["research_error"] == "temporary quote gap"
    assert payload["account"]["total_equity"] == 1_000_000


def test_agent_trading_model_result_has_typed_confidence_and_invocation_receipt(monkeypatch):
    from stock_ai import agent_trading_api

    async def require_account():
        return {"authenticated": True}

    async def run_structured(_prompt, schema):
        assert "confidence_type" in schema["required"]
        return {
            "summary": "observe",
            "action": "watch",
            "confidence": 61,
            "confidence_type": "model_self_reported",
            "confidence_calibrated": False,
            "order_draft": None,
            "evidence": ["quote"],
            "risks": ["uncertain"],
            "memory_used": [],
            "next_checks": ["next session"],
        }

    monkeypatch.setattr(agent_trading_api.codex_runtime, "require_account", require_account)
    monkeypatch.setattr(agent_trading_api.codex_runtime, "run_structured", run_structured)
    monkeypatch.setattr(
        agent_trading_api,
        "account_snapshot",
        lambda refresh_prices=False: {"account_id": "paper", "total_equity": 1_000_000},
    )
    monkeypatch.setattr(
        agent_trading_api,
        "_compact_research",
        lambda symbol, horizon: {"symbol": symbol, "data_ready": True},
    )

    payload = asyncio.run(
        agent_trading_api.agent_trading_analyze(
            agent_trading_api.AgentTradingAnalysisRequest(
                symbol="AAA",
                prompt="分析這檔股票",
            )
        )
    )

    assert payload["decision"]["confidence_type"] == "model_self_reported"
    assert payload["decision"]["confidence_calibrated"] is False
    assert payload["model_invocation"]["status"] == "succeeded"
    assert payload["model_invocation"]["provider"] == "codex"
    assert payload["provenance"]["model_call_id"] == payload["model_invocation"]["call_id"]
