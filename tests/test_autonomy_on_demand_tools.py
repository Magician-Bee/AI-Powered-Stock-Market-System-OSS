"""On-demand research uses the existing provider and plan store; no live model/orders."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from open_stock_ai.agent_runtime.contracts import AgentRunContext
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider
from test_agent_campaign_actions import setup
from test_autonomous_campaign import history_fixture


def context(symbols=()):
    value = AgentRunContext(run_id="AR-offline-on-demand", session_id="AS-offline-on-demand", driver_id="codex",
                            autonomy="paper_execute", symbols=symbols, allow_paper_orders=True)
    value.state.update(explicit_autonomous_campaign_authorized=True,
                       provider_model_metadata={"model": "gpt-5.6-sol", "reasoning_effort": "medium"})
    return value


@pytest.mark.parametrize("arguments,reason", [
    ({"symbols": []}, "nonempty_list"),
    ({"symbols": None}, "nonempty_list"),
    ({"symbols": "2330.TW"}, "nonempty_list"),
    ({"symbols": ["2330.TW", "2330.TW"]}, "duplicate"),
    ({"symbols": ["2330.TW", "2317.TW"], "deep_limit": 1}, "exceed_deep_limit"),
    ({"symbols": ["2330.tw"]}, "canonical"),
    ({"symbols": [" 2330.TW"]}, "canonical"),
    ({"symbols": ["AAPL"]}, "canonical"),
    ({"symbols": [2330]}, "canonical"),
    ({"symbols": ["2330.TW"], "cycle_id": "AC-retained"}, "cannot_combine"),
    ({"symbols": None, "cycle_id": "AC-retained"}, "cannot_combine"),
    ({"cycle_id": ""}, "cycle_id_required"),
    ({"cycle_id": 1}, "cycle_id_required"),
    ({"deep_limit": True}, "deep_limit_between"),
    ({"deep_limit": "20"}, "deep_limit_between"),
    ({"deep_limit": 21}, "deep_limit_between"),
])
def test_invalid_inputs_fail_before_service_or_network(monkeypatch, arguments, reason):
    def forbidden():
        pytest.fail("Invalid research must not initialize the production campaign")
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", forbidden)
    with pytest.raises(ValueError, match=reason):
        asyncio.run(AutonomousTradingToolProvider().execute("autonomy.research", arguments, context()))


def test_explicit_research_respects_host_scope_without_mutating_it(monkeypatch):
    def forbidden():
        pytest.fail("Out-of-scope research must fail before service initialization")
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", forbidden)
    scoped = context(("2330.TW",))
    with pytest.raises(PermissionError, match="research_symbol_outside_host_instrument_scope"):
        asyncio.run(AutonomousTradingToolProvider().execute("autonomy.research", {"symbols": ["8299.TWO"]}, scoped))
    assert scoped.symbols == ("2330.TW",)


def test_scoped_request_and_retained_read_keep_exact_arguments_and_no_new_run(monkeypatch):
    calls = []
    async def research(**kwargs):
        calls.append(kwargs)
        return {"cycle_id": "AC-new"}
    service = SimpleNamespace(research=research, cycle=lambda value: {"cycle_id": value})
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", lambda: service)
    def no_review(*_args):
        pytest.fail("Research/readback must not launch or configure model review")
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_model_review", no_review)
    provider = AutonomousTradingToolProvider()
    scoped = context(("8299.TWO",))
    assert asyncio.run(provider.execute("autonomy.research", {"symbols": ["8299.TWO"], "deep_limit": 1}, scoped)) == {"cycle_id": "AC-new"}
    assert asyncio.run(provider.execute("autonomy.research", {"cycle_id": "AC-old"}, scoped)) == {"cycle_id": "AC-old"}
    assert calls == [{"deep_limit": 1, "symbols": ["8299.TWO"]}]
    assert scoped.symbols == ("8299.TWO",)


def test_coverage_tool_reads_persistent_ledger_without_model_or_research(monkeypatch):
    calls = []
    ledger = SimpleNamespace(query=lambda **kwargs: calls.append(kwargs) or {
        "schema_version": "open_stock_ai.security_research_coverage_query.v1",
        "items": [{"symbol": "2330.TW", "deep_research_status": "never_researched"}],
    })
    service = SimpleNamespace(coverage_ledger=ledger)
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", lambda: service)
    result = asyncio.run(AutonomousTradingToolProvider().execute("autonomy.coverage", {
        "deep_status": "never_researched", "domain": "price_history", "needs_update": True,
        "new_entry_eligible": True, "limit": 20,
    }, context()))
    assert result["items"][0]["symbol"] == "2330.TW"
    assert calls == [{"symbols": None, "deep_status": "never_researched", "domain": "price_history",
                      "needs_update": True, "new_entry_eligible": True, "after": None, "limit": 20}]


def test_new_candidate_cycle_reaches_existing_evidence_proposal_and_activation(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    campaign, _ = setup(tmp_path, symbols=tuple(f"{2300+index}.TW" for index in range(25)),
                        history=lambda symbol, instant: history_fixture(symbol, now=instant-timedelta(days=1)))
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", lambda: campaign)
    registrations = []
    review = SimpleNamespace(runtime=SimpleNamespace(store=SimpleNamespace(get_run=lambda _: {"created_at": now.isoformat()})),
        register_current_review=lambda **kwargs: registrations.append(kwargs), configure=lambda **kwargs: None,
        status=lambda: {"enabled": True})
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_model_review", lambda _service: review)
    provider, ctx = AutonomousTradingToolProvider(), context()

    async def scenario():
        original = await provider.execute("autonomy.research", {"deep_limit": 1}, ctx)
        assert original["results"][0]["symbol"] == "2300.TW"
        requested = await provider.execute("autonomy.research", {"symbols": ["2324.TW"], "deep_limit": 1}, ctx)
        assert requested["cycle_id"] != original["cycle_id"]
        assert requested["results"][0]["symbol"] == "2324.TW"
        assert requested["selection"]["mode"] == "on_demand"
        evidence = await provider.execute("autonomy.evidence", {
            "evidence_id": requested["results"][0]["history_id"], "view": "summary"}, ctx)
        assert evidence["summary"]["symbol"] == "2324.TW"
        arguments = {"cycle_id": requested["cycle_id"], "symbol": "2324.TW", "quantity_shares": 10,
            "stop_loss": 95, "target_price": 130, "rationale": "Offline independent on-demand thesis",
            "not_before": (now+timedelta(days=3)).isoformat()}
        result = await provider.execute("autonomy.propose_plan", arguments, ctx)
        retry = await provider.execute("autonomy.propose_plan", arguments, ctx)
        plan = result["plan"]
        assert retry["plan"]["plan_id"] == plan["plan_id"]
        assert plan["definition"]["strategy_id"] == "agent_discretionary_proposal_v1"
        assert plan["definition"]["metadata"]["cycle_id"] == requested["cycle_id"]
        assert plan["definition"]["qualification_id"] is None
        assert result["model_invocation_context"]["status"] == "unknown"
        receipt = await provider.execute("autonomy.activate", {
            "cycle_id": requested["cycle_id"], "use_candidate_plans": False}, ctx)
        assert [item["plan_id"] for item in receipt["plans"]] == [plan["plan_id"]]
        assert registrations == [{"cycle_id": requested["cycle_id"], "run_id": ctx.run_id,
                                  "provider_model_metadata": ctx.state["provider_model_metadata"]}]
        assert not campaign.broker.submissions
        assert len(campaign.plans.list(account_id=campaign.broker.account_id)) == 1
        assert campaign.status()["positive_ev_qualified"] is False
        assert ctx.symbols == ()
    asyncio.run(scenario())
