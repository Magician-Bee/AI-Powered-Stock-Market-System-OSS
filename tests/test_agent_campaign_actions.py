"""Discretionary choices reach the same durable execution path, without a model."""
import asyncio
from datetime import timedelta

import pytest

from open_stock_ai.execution.agent_campaign_actions import close_plan, propose_plan
from open_stock_ai.agent_runtime.context_broker import ContextBroker
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider
from test_autonomous_campaign import setup as campaign_setup, NOW


CONTEXT = {"run_id": "AR-offline-choice", "session_id": "AS-offline-choice", "driver_id": "codex",
           "provider_model_metadata": {"model": "gpt-5.6-sol", "reasoning_effort": "medium"}}


def setup(*args, **kwargs):
    service, quotes = campaign_setup(*args, **kwargs)
    account = service.broker.account
    async def scoped_account(*, now):
        return {**await account(now=now), "account_id": service.broker.account_id}
    service.broker.account = scoped_account
    return service, quotes


def test_model_choice_can_wait_then_cancel_without_submitting_and_retries_reuse_plan(tmp_path):
    service, _ = setup(tmp_path)
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=1)
        args = {"cycle_id": cycle["cycle_id"], "symbol": "2330.TW", "position_size_pct": 3,
                "stop_loss": 95, "target_price": 130, "rationale": "Offline independent thesis",
                "not_before": (NOW + timedelta(days=3)).isoformat()}
        plan = await propose_plan(service, args, host_context=CONTEXT, now=NOW)
        retry = await propose_plan(service, args, host_context=CONTEXT, now=NOW+timedelta(minutes=3))
        assert retry["plan_id"] == plan["plan_id"]
        assert plan["definition"]["strategy_id"] == "agent_discretionary_proposal_v1"
        assert plan["definition"]["qualification_id"] is None
        assert plan["state"]["entry_order_id"] is None
        request = close_plan(service, plan_id=plan["plan_id"], rationale="Evidence no longer supports entry", host_context=CONTEXT, now=NOW)
        repeated = close_plan(service, plan_id=plan["plan_id"], rationale="Evidence no longer supports entry", host_context=CONTEXT, now=NOW+timedelta(seconds=1))
        assert request["request_id"] == repeated["request_id"]
        # Cancel still progresses when new entries are disabled and quote is stale.
        result = await service.manage(now=NOW)
        assert not result["errors"]
        final = service.plans.get(plan["plan_id"])
        assert final["status"] == "cancelled" and final["state"]["entry_order_id"] is None
    asyncio.run(scenario())


def test_proposal_respects_combined_pending_exposure_and_owned_account(tmp_path):
    service, _ = setup(tmp_path)
    async def scenario():
        cycle = await service.research(now=NOW, deep_limit=2)
        args = {"cycle_id": cycle["cycle_id"], "symbol": "2330.TW", "position_size_pct": 50,
                "stop_loss": 95, "rationale": "Exceeds bounded experiment"}
        plan = await propose_plan(service, args, host_context=CONTEXT, now=NOW)
        # Percentage sizing may shrink to available risk budget, never exceed it.
        account = await service.broker.account(now=NOW)
        assert plan["definition"]["cash_budget"] <= account["total_equity"]*.05
        assert plan["definition"]["position_size_pct"] <= 5
        assert plan["definition"]["metadata"]["sizing"]["requested_position_size_pct"] == 50
        assert plan["definition"]["metadata"]["sizing"]["cash_allocation_pct"] <= 5
        other, _ = setup(tmp_path, shared=service.plans.store)
        other.broker.account_id = "different-account"
        with pytest.raises(ValueError, match="account_mismatch"):
            close_plan(other, plan_id=plan["plan_id"], rationale="Not owned", host_context=CONTEXT, now=NOW)
    asyncio.run(scenario())


def test_autonomous_scope_keeps_relevant_tools_while_simple_order_stays_scoped():
    tools = AutonomousTradingToolProvider().manifest() + [
        {"name": "web.research"}, {"name": "market.financials"}, {"name": "terminal.exec"}]
    broker = ContextBroker()
    ordinary = {t["name"] for t in broker.filter_capabilities(tools, task_kind="paper_execution")}
    campaign = {t["name"] for t in broker.disclose_capabilities(tools, task_kind="paper_execution", step=2, autonomous_campaign=True)}
    assert "web.research" not in ordinary
    assert {"autonomy.propose_plan", "autonomy.close_plan", "autonomy.evidence", "web.research", "market.financials"} <= campaign
    assert "terminal.exec" not in campaign
