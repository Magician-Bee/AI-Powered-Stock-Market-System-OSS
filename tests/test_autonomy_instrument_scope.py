"""Host instrument limits cannot silently activate the whole-account service."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.agent_runtime.autonomy_contract import bind_campaign_authorization
from open_stock_ai.execution.autonomous_campaign import AutonomousCampaign
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider

HOST_AUTHORIZED_AT = "2026-09-11T00:00:00+00:00"


def context(symbols=("6538.TWO",)):
    value = AgentRunContext(run_id="offline-scoped", session_id="offline-session", driver_id="codex",
        autonomy="paper_execute", symbols=symbols, allow_paper_orders=True)
    objective = "僅為6538.TWO啟動自主紙上交易流程。" if symbols else "請啟動全市場自主紙上交易流水線。"
    bind_campaign_authorization(value, objective=objective, task_kind="market_decision")
    assert value.state["explicit_autonomous_campaign_authorized"]
    return value


@pytest.mark.parametrize("name,args,reason", [
    ("autonomy.propose_plan", {"symbol": "2330.TW", "cycle_id": "c", "symbols": ["6538.TWO"]}, "symbol_outside_host"),
    ("autonomy.activate", {"cycle_id": "c", "use_candidate_plans": False}, "whole_account_campaign"),
    ("autonomy.activate", {"cycle_id": "c", "use_candidate_plans": True}, "whole_account_campaign"),
])
def test_scoped_mutations_are_rejected_before_service_initialization(monkeypatch, name, args, reason):
    def forbidden_service():
        pytest.fail("Denied requests must not initialize or enable the campaign/model review")
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", forbidden_service)
    with pytest.raises(PermissionError, match=reason):
        asyncio.run(AutonomousTradingToolProvider().execute(name, args, context()))


def fake_service(monkeypatch):
    calls = []
    plan = {"plan_id": "plan", "account_id": "isolated", "symbol": "6538.TWO"}
    async def manage(**kwargs):
        calls.append(("manage", kwargs))
        return {"account_id": "isolated", "results": [], "errors": []}
    async def create_plans(**kwargs):
        calls.append(("create_plans", kwargs))
        return {"plans": [], "skipped": []}
    service = SimpleNamespace(broker=SimpleNamespace(account_id="isolated"), manage=manage, create_plans=create_plans,
        cycle=lambda cycle: {"cycle_id": cycle, "created_at": datetime.now(timezone.utc).isoformat()},
        status=lambda: {"enabled": True, "plans": []}, plans=SimpleNamespace(get=lambda _id: plan),
        assert_activation_authorized=lambda *, authorized_at: None,
        configure=lambda **kwargs: calls.append(("configure", kwargs)), _retain=lambda *_args: "AE-" + "a"*64)
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", lambda: service)
    review = SimpleNamespace(runtime=SimpleNamespace(store=SimpleNamespace(
        get_run=lambda _id: {"created_at": HOST_AUTHORIZED_AT})))
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_model_review", lambda _service: review)
    return service, calls


@pytest.mark.parametrize("symbols,proposal_symbol", [(("6538.TWO",), "6538.TWO"), ((), "2330.TW")])
def test_matching_proposals_and_whole_market_choices_reach_the_existing_builder(monkeypatch, symbols, proposal_symbol):
    service, calls = fake_service(monkeypatch)
    async def propose(actual, proposal, **kwargs):
        assert actual is service
        calls.append(("propose", proposal["symbol"]))
        return {"plan_id": "plan", "symbol": proposal["symbol"], "account_id": "isolated"}
    monkeypatch.setattr("open_stock_ai.execution.agent_campaign_actions.propose_plan", propose)
    receipt = asyncio.run(AutonomousTradingToolProvider().execute("autonomy.propose_plan",
        {"cycle_id": "c", "symbol": proposal_symbol}, context(symbols)))
    assert calls == [("propose", proposal_symbol)]
    assert receipt["plan"]["symbol"] == proposal_symbol


@pytest.mark.parametrize("tool", ["autonomy.manage", "autonomy.close_plan"])
def test_scoped_management_and_close_forward_host_scope_without_blocking_owned_reductions(monkeypatch, tool):
    service, calls = fake_service(monkeypatch)
    def close(actual, **kwargs):
        assert actual is service
        calls.append(("close", kwargs["plan_id"]))
        return {"plan_id": kwargs["plan_id"], "request_id": "exit", "evidence_id": "AE-" + "b"*64}
    monkeypatch.setattr("open_stock_ai.execution.agent_campaign_actions.close_plan", close)
    asyncio.run(AutonomousTradingToolProvider().execute(tool, {"plan_id": "existing-other-symbol", "rationale": "reduce"}, context()))
    assert ("manage", {"entry_symbols": frozenset({"6538.TWO"})}) in calls
    assert not any(name == "configure" for name, _ in calls)
    if tool == "autonomy.close_plan":
        assert ("close", "existing-other-symbol") in calls


def test_whole_market_activation_keeps_original_persistent_enablement(monkeypatch):
    _, calls = fake_service(monkeypatch)
    review = SimpleNamespace(runtime=SimpleNamespace(store=SimpleNamespace(get_run=lambda _id: {"created_at": HOST_AUTHORIZED_AT})),
        register_current_review=lambda **kwargs: calls.append(("register", kwargs)),
        configure=lambda **kwargs: calls.append(("review_configure", kwargs)), status=lambda: {"enabled": True})
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_model_review", lambda _service: review)
    receipt = asyncio.run(AutonomousTradingToolProvider().execute("autonomy.activate", {"cycle_id": "c"}, context(())))
    assert receipt["action"] == "autonomy.activate"
    control = {"enabled": True, "authorized_at": HOST_AUTHORIZED_AT}
    assert ("configure", control) in calls and ("review_configure", control) in calls
    assert calls.index(("configure", control)) < calls.index(("review_configure", control))
    assert ("manage", {}) in calls


@pytest.mark.parametrize("entry_symbols,expected", [
    (frozenset({"6538.TWO"}), {"target", "position", "submitted", "cancel"}),
    (frozenset(), {"position", "submitted", "cancel"}),
    (None, {"target", "outside", "position", "submitted", "cancel"}),
])
def test_manage_limits_unsubmitted_entries_but_keeps_existing_commitments_and_cancellation(entry_symbols, expected):
    # This uses the production campaign loop with offline ports; no model, DB or account is opened.
    records = [{"plan_id": identity, "symbol": symbol, "state": {"entry_order_id": order_id},
                "definition": {"strategy_version": "v"}}
               for identity, symbol, order_id in [
                   ("target", "6538.TWO", None), ("outside", "2330.TW", None),
                   ("position", "2317.TW", "filled-order"), ("submitted", "2454.TW", "open-order"),
                   ("cancel", "1101.TW", None)]]
    campaign = object.__new__(AutonomousCampaign)
    campaign.quote_timeout_seconds = 10
    campaign.status = lambda: {"enabled": True}
    campaign.broker = SimpleNamespace(account_id="isolated")
    campaign.plans = SimpleNamespace(list=lambda **kwargs: records,
        exit_request=lambda plan_id, **kwargs: {"reason": "agent_reassessment"} if plan_id == "cancel" else None)
    seen = []
    async def quote(symbol):
        return {"symbol": symbol}
    async def tick(plan_id, **kwargs):
        seen.append(plan_id)
        return {"plan_id": plan_id}
    campaign.quote_loader = quote
    campaign.executor = SimpleNamespace(tick=tick)
    result = asyncio.run(campaign.manage(entry_symbols=entry_symbols))
    assert set(seen) == expected and result["errors"] == []
    assert campaign.status()["enabled"] is True
    if entry_symbols is not None:
        assert {row["plan_id"] for row in result["skipped_entries"]} == {"target", "outside"} - expected
