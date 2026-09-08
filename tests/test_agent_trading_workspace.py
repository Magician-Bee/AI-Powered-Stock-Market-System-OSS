from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "stock_ai" / "ui" / "static"


def test_agent_trading_workspace_assets_are_loaded_by_paper_ui():
    script = (STATIC / "paper-training.js").read_text(encoding="utf-8")
    assert "loadAgentTradingWorkspace" in script
    assert "/static/agent-trading-workspace.js" in script
    assert "/static/agent-trading-workspace.css" in script


def test_agent_workspace_routes_paper_actions_through_the_durable_stock_ai_runtime():
    script = (STATIC / "agent-trading-workspace.js").read_text(encoding="utf-8")
    assert "Agent 交易中樞" in script
    assert "Agent 自主模擬一筆" in script
    assert "stock-ai-paper-account-updated" in script
    assert "window.AgentDockController.submit" in script
    assert "paper_execute" in script
    assert "renderDecision" not in script
    assert "renderControlAnswer" not in script


def test_paper_account_reset_and_summary_have_one_frontend_owner():
    agent_script = (STATIC / "agent-trading-workspace.js").read_text(encoding="utf-8")
    paper_script = (STATIC / "paper-training.js").read_text(encoding="utf-8")

    assert "resetSharedAccount" not in agent_script
    assert "document.addEventListener('click', resetSharedAccount" not in agent_script
    assert "byId('paperTrainingSummary')" not in agent_script
    assert "byId('paperTrainingInitialCash')" not in agent_script
    assert "sessionRequestVersion" in agent_script
    assert "acceptPaperAccountEvent" in agent_script

    assert "bind('paperTrainingReset', resetAccount)" in paper_script
    assert "function renderAccount(" in paper_script
    assert "stock-ai-paper-account-updated" in paper_script


def test_agent_workspace_has_card_spacing_and_no_stuck_surfaces():
    css = (STATIC / "agent-trading-workspace.css").read_text(encoding="utf-8")
    assert ".paper-broker-layout>div" in css
    assert "gap:22px!important" in css
    assert ".paper-risk-summary+.paper-position-table" in css


def test_agent_trading_router_is_mounted():
    api = (ROOT / "src" / "open_stock_ai" / "api.py").read_text(encoding="utf-8")
    backend = (ROOT / "src" / "stock_ai" / "agent_trading_api.py").read_text(encoding="utf-8")
    codex = (ROOT / "src" / "stock_ai" / "codex_api.py").read_text(encoding="utf-8")
    assert "router.include_router(agent_trading_router)" in api
    assert '@router.post("/account/reset")' in backend
    assert '@router.post("/analyze")' in backend
    assert '@router.post("/control")' in backend
    assert "account_revision" in backend
    assert "source_of_truth" in backend
    assert "不得暗中建立委託" in codex
