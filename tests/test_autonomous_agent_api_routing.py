"""API admission tests with fake runtime/provider; no models or live state."""
import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from stock_ai import agent_api


CAMPAIGN = (
    "請啟動台灣上市櫃全市場的自主紙上交易流水線，完成研究、決策及保存交易計畫。"
    "用 autonomy.activate 啟動同一個cycle，讓後台持續管理委託、持倉和每日有預算的Codex複查。"
)
ROWS = [{"symbol": "6538.TWO", "name": "倉和", "legal_name": "倉和股份有限公司"},
        {"symbol": "2887.TW", "name": "台新新光金", "legal_name": "台新新光金融控股股份有限公司"}]


@pytest.fixture
def security_master(monkeypatch):
    monkeypatch.setattr(agent_api, "get_market_data_platform", lambda: SimpleNamespace(securities=lambda **kwargs: ROWS))


@pytest.mark.parametrize("route", ["/runs", "/sessions/AS-test/runs"])
@pytest.mark.parametrize("objective,autonomy,expected", [
    (CAMPAIGN, "paper_execute", "paper_execute"),
    (CAMPAIGN, "advisory", "paper_execute"),
    ("請對2330.TW買進100股，完成一筆紙上模擬交易。", "advisory", "paper_execute"),
    ("只分析自主紙上交易流程的風險，不要建立紙上交易或下單。", "paper_execute", "advisory"),
    ("研究台灣市場，不進行交易。", "paper_execute", "advisory"),
    ("請說明自主紙上交易流程的風控設計。", "advisory", "advisory"),
    ("如何評估自主紙上交易流程？", "advisory", "advisory"),
    ("請說明如何啟動自主紙上交易流程。", "paper_execute", "advisory"),
])
def test_run_routes_keep_explicit_paper_campaign_and_reject_stale_authority(monkeypatch, security_master, route, objective, autonomy, expected):
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        return {"run_id": "AR-fake", "status": "queued"}
    async def submit(session_id, **kwargs):
        assert session_id == "AS-test"
        return await create(**kwargs)
    runtime = SimpleNamespace(create_run=create, submit_session_run=submit, get_session=lambda session_id: {"session_id": session_id})
    monkeypatch.setattr(agent_api, "get_agent_run_runtime", lambda: runtime)
    monkeypatch.setattr(agent_api, "_selected_driver_for_api", lambda driver: "codex")
    app = FastAPI()
    app.include_router(agent_api.router)
    response = TestClient(app).post("/api/agents" + route, json={
        "objective": objective, "autonomy": autonomy, "driver": "codex", "symbols": ["6538.TWO"],
        "context_scope": "instrument", "intent": {"category": "market_decision", "scope": "instrument"},
    })
    assert response.status_code == 202, response.text
    assert calls[0]["autonomy"] == expected
    if objective == CAMPAIGN:
        assert calls[0]["symbols"] == []
        assert calls[0]["run_metadata"]["context_scope"] == "market"
        assert "[MARKET_SCOPE]" in calls[0]["objective"]
        assert "6538.TWO" not in calls[0]["objective"]


def test_company_substring_cannot_invent_a_security_target(security_master):
    assert agent_api._literal_taiwan_security_symbols(CAMPAIGN) == []
    assert agent_api._literal_taiwan_security_symbols("請分析倉和的股票。") == ["6538.TWO"]
    assert agent_api._literal_taiwan_security_symbols("請分析台新新光金，然後做紙上交易。") == ["2887.TW"]
    assert agent_api._literal_classifier_symbols("Please assess entry conditions", ["ON"]) == []
    assert agent_api._literal_classifier_symbols("分析16538.TWO", ["6538.TWO"]) == []
    assert agent_api._literal_classifier_symbols("分析6538.TWO", ["6538.TWO"]) == ["6538.TWO"]
    assert agent_api._literal_classifier_symbols("分析6538.TWO", ["6538.TW"]) == []


@pytest.mark.parametrize("model_scope,model_symbols", [("market", []), ("instrument", ["6538.TWO"])])
def test_classifier_does_not_override_market_scope_with_name_fragment(monkeypatch, security_master, model_scope, model_symbols):
    class Provider:
        async def start_session(self, *args, **kwargs): pass
        async def close_session(self, *args): pass
        async def generate_structured(self, *args):
            return {"title": "全市場自主紙上交易", "category": "market_analysis", "scope": model_scope,
                    "symbols": model_symbols, "use_selected_symbol": False}
        def capabilities(self): return {"model": "fixture"}
    monkeypatch.setattr(agent_api, "_selected_driver_for_api", lambda driver: "codex")
    monkeypatch.setattr(agent_api, "get_agent_service", lambda: SimpleNamespace(provider_registry=SimpleNamespace(get=lambda driver: Provider())))
    result, _, _ = asyncio.run(agent_api._classify_agent_intent(agent_api.AgentIntentClassificationRequest(objective=CAMPAIGN, driver="codex")))
    assert result.scope == "market" and result.symbols == [] and not result.use_selected_symbol
    assert result.category == "market_decision"


def test_explicit_ticker_or_name_is_preserved_for_campaign_instrument_scope(security_master):
    for objective, expected in (("請為6538.TWO啟動自主紙上交易流程。", "6538.TWO"),
                                ("請為倉和股票啟動自主紙上交易流程。", "6538.TWO")):
        _, symbols, scope = agent_api._run_context(agent_api.AgentRunRequest(
            objective=objective, symbols=["6538.TWO", "9999.TW"], context_scope="instrument", autonomy="paper_execute"))
        assert symbols == [expected] and scope == "instrument"


@pytest.mark.parametrize("supplied", [[], ["6538.TWO"], ["6538.TW"]])
@pytest.mark.parametrize("stale_scope", ["market", "instrument", "neutral"])
def test_explicit_single_security_mandate_wins_over_stale_classifier(security_master, supplied, stale_scope):
    objective, symbols, scope = agent_api._run_context(agent_api.AgentRunRequest(
        objective="僅為6538.TWO啟動自主紙上交易流程。", symbols=supplied, context_scope=stale_scope))
    assert symbols == ["6538.TWO"] and scope == "instrument"
    assert "[MARKET_SCOPE]" not in objective


@pytest.mark.parametrize("objective", [
    CAMPAIGN + "例如2330.TW只是參考案例，請自主選擇。",
    "請啟動全市場自主紙上交易流程，以6538.TWO作示範，其他股票亦須納入。",
])
def test_market_mandate_with_an_example_remains_market_wide(security_master, objective):
    _, symbols, scope = agent_api._run_context(agent_api.AgentRunRequest(
        objective=objective, symbols=["6538.TWO", "2330.TW"], context_scope="instrument"))
    assert symbols == [] and scope == "market"


@pytest.mark.parametrize("objective", [
    "全市場研究後，僅為6538.TWO啟動自主紙上交易流程。",
    "全市場研究後，只針對倉和股票啟動自主紙上交易流程。",
])
def test_market_research_does_not_expand_explicit_single_security_execution(security_master, objective):
    _, symbols, scope = agent_api._run_context(agent_api.AgentRunRequest(objective=objective, context_scope="market"))
    assert symbols == ["6538.TWO"] and scope == "instrument"


@pytest.mark.parametrize("objective", [
    "請說明自主紙上交易流程的風控設計。", "如何評估自主紙上交易流程？",
    "請說明如何啟動自主紙上交易流程。", "設計自主紙上交易流程的啟用介面。",
    "我想了解自主紙上交易流程如何啟動。", "請確認自主紙上交易流程是否已啟用。",
    "Explain how to activate an autonomous paper trade campaign.",
    "自主紙上交易流程應該如何啟動？",
    "自主紙上交易流程的啟動條件是什麼？",
    "請說明自主紙上交易流程。然後執行單元測試。",
    "請建立自主紙上交易流程的單元測試。",
])
@pytest.mark.parametrize("mode", ["paper_execute", "full_execute"])
def test_central_contract_never_restores_campaign_authority_for_explanation(objective, mode):
    from open_stock_ai.agent_runtime.autonomy_contract import bind_campaign_authorization
    from open_stock_ai.agent_runtime.completion_contract import objective_completion_contract
    from open_stock_ai.agent_runtime.contracts import AgentRunContext
    contract = objective_completion_contract(objective, "market_information")
    assert not contract["paper_order_requested"] and not contract.get("autonomous_pipeline_requested")
    context = AgentRunContext(run_id="fake", autonomy=mode, symbols=(), allow_paper_orders=True,
                              state={"explicit_autonomous_campaign_authorized": True})
    bind_campaign_authorization(context, objective=objective, task_kind="market_information")
    assert not context.state["explicit_autonomous_campaign_authorized"]


def test_adapter_execution_objective_retains_explicit_campaign_authority():
    from stock_ai.autonomous_model_review import AutonomousModelReview
    from open_stock_ai.agent_runtime.completion_contract import objective_completion_contract
    objective = AutonomousModelReview._objective({"cycle_id": "AC-proof", "results": []})
    assert objective_completion_contract(objective, "market_decision")["autonomous_pipeline_requested"] is True


def test_execution_after_explanation_requires_clear_campaign_reference():
    from open_stock_ai.agent_runtime.completion_contract import objective_completion_contract
    objective = "請先說明自主紙上交易流程的風險。然後請啟動該流程。"
    assert objective_completion_contract(objective, "market_decision")["autonomous_pipeline_requested"] is True
