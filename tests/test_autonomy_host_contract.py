from __future__ import annotations

from copy import deepcopy
import asyncio
import pytest

from open_stock_ai.agent_runtime.autonomy_contract import bind_campaign_authorization
from open_stock_ai.agent_runtime.completion_contract import evaluate_objective_completion, objective_completion_contract
from open_stock_ai.agent_runtime.context_broker import ContextBroker
from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.agent_runtime.paper_protocol import _explicit_paper_order_from_objective
from open_stock_ai.agent_runtime.policy_engine import PolicyEngine
from open_stock_ai.agent_runtime.validators import ValidatorEngine
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider


OBJECTIVE = "用紙上交易啟動全市場自主進出場流水線；等待時間和價格條件並管理持倉。"


def receipt():
    return {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "action": "autonomy.activate",
            "account_id": "isolated", "mode": "paper", "cycle_id": "AC-real-cycle",
            "campaign_receipt_id": "AE-" + "a"*64, "plans": [],
            "skipped": [{"symbol": "2330.TW", "reason": "no_frozen_candidate_entry"}],
            "management": {"account_id": "isolated", "enabled": True, "results": [], "errors": []}}


def observations():
    return [{"tool": "autonomy.research", "ok": True,
             "result": {"schema_version": "open_stock_ai.autonomous_research_cycle.v1", "cycle_id": "AC-real-cycle", "deep_success_count": 2}},
            {"tool": "autonomy.activate", "ok": True, "result": receipt()}]


def test_continuing_pipeline_does_not_invent_an_immediate_one_share_buy():
    contract = objective_completion_contract(OBJECTIVE, "market_decision")
    assert contract["autonomous_pipeline_requested"] and not contract["paper_order_requested"]
    assert _explicit_paper_order_from_objective(OBJECTIVE, symbols=("2330.TW",)) is None
    plain = objective_completion_contract("紙上買進2330.TW一股", "market_decision")
    assert plain["paper_order_requested"]


def test_an_analysis_only_request_never_activates_an_autonomous_campaign():
    contract = objective_completion_contract("只做分析：全市場自主流水線如何用紙上交易驗證？不要下單", "market_information")
    assert not contract.get("autonomous_pipeline_requested") and not contract["paper_order_requested"]


def test_campaign_tool_exposure_includes_research_and_permission_gated_activation():
    tools = AutonomousTradingToolProvider().manifest()
    broker = ContextBroker()
    names = {t["name"] for t in broker.filter_capabilities(tools, task_kind="paper_execution")}
    assert names == {"autonomy.status", "autonomy.coverage", "autonomy.evidence", "autonomy.research", "autonomy.activate", "autonomy.manage",
                     "autonomy.propose_plan", "autonomy.close_plan"}
    research_names = {t["name"] for t in broker.filter_capabilities(tools, task_kind="paper_execution", phase="research")}
    assert {"autonomy.coverage", "autonomy.research", "autonomy.evidence"}.issubset(research_names)
    assert not {"autonomy.activate", "autonomy.manage", "autonomy.propose_plan", "autonomy.close_plan"} & research_names


def test_paper_mode_authorizes_bounded_campaign_but_advisory_and_live_do_not():
    tool = next(t for t in AutonomousTradingToolProvider().manifest() if t["name"] == "autonomy.activate")
    engine = PolicyEngine()
    paper = AgentRunContext(run_id="r", autonomy="paper_execute", symbols=(), allow_paper_orders=True)
    bind_campaign_authorization(paper, objective=OBJECTIVE, task_kind="market_decision")
    assert engine.evaluate(tool=tool, arguments={"cycle_id": "c"}, context=paper).action == "allow"
    paper.allow_paper_orders = False
    assert engine.evaluate(tool=tool, arguments={"cycle_id": "c"}, context=paper).action == "deny"
    assert engine.evaluate(tool={"name": "live.place", "risk_class": "financial_real_action"}, arguments={}, context=paper).action == "deny"


@pytest.mark.parametrize("name", ["autonomy.activate", "autonomy.manage", "autonomy.propose_plan", "autonomy.close_plan"])
def test_one_share_order_does_not_authorize_a_whole_market_campaign(name):
    tool = next(t for t in AutonomousTradingToolProvider().manifest() if t["name"] == name)
    context = AgentRunContext(run_id="single-order", autonomy="paper_execute", symbols=("2330.TW",), allow_paper_orders=True,
                              state={"explicit_paper_order_authorized": True, "explicit_autonomous_campaign_authorized": True})
    # Restoration cannot carry broader permissions into a narrower objective.
    bind_campaign_authorization(context, objective="只紙上買進2330.TW一股", task_kind="market_decision")
    assert context.state["explicit_autonomous_campaign_authorized"] is False
    decision = PolicyEngine().evaluate(tool=tool, arguments={"cycle_id": "cycle", "explicit_autonomous_campaign_authorized": True}, context=context)
    assert decision.action == "deny"


@pytest.mark.parametrize("objective,mode,allowed", [(OBJECTIVE, "advisory", False),
    ("只做分析：全市場自主流水線如何用紙上交易驗證？不要下單", "paper_execute", False),
    (OBJECTIVE, "paper_execute", True), (OBJECTIVE, "full_execute", True)])
def test_campaign_permission_is_derived_only_from_host_objective_and_mode(objective, mode, allowed):
    context = AgentRunContext(run_id="authorization", autonomy=mode, symbols=(), allow_paper_orders=True)
    bind_campaign_authorization(context, objective=objective, task_kind="market_decision")
    assert context.state["explicit_autonomous_campaign_authorized"] is allowed


@pytest.mark.parametrize("name", ["autonomy.activate", "autonomy.manage", "autonomy.propose_plan", "autonomy.close_plan"])
def test_direct_provider_call_cannot_bypass_campaign_authorization(name, monkeypatch):
    def unexpected_service():
        pytest.fail("Unauthorized provider call must not initialize or mutate a campaign")
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", unexpected_service)
    context = AgentRunContext(run_id="direct", autonomy="paper_execute", symbols=("2330.TW",), allow_paper_orders=True)
    with pytest.raises(PermissionError, match="explicit_host_authorization"):
        asyncio.run(AutonomousTradingToolProvider().execute(name, {"cycle_id": "cycle"}, context))


def test_waiting_campaign_is_valid_activation_without_claiming_fills_or_profit():
    result = evaluate_objective_completion(objective=OBJECTIVE, task_kind="market_decision", observations=observations(), decision=None)
    assert result["passed"] and result["order_receipts"] == []
    assert "fills_and_profitability_require_separate_receipts" in result["campaign"]["outcome_scope"]


def test_missing_or_failed_research_cannot_be_covered_by_a_fake_activation():
    for mutate in (lambda rows: rows.pop(0),
                   lambda rows: rows[0]["result"].update(deep_success_count=0),
                   lambda rows: rows[1]["result"].update(cycle_id="different"),
                   lambda rows: rows[1]["result"]["management"].update(errors=[{"error": "failed"}]),
                   lambda rows: rows[1]["result"].pop("campaign_receipt_id")):
        rows = deepcopy(observations())
        mutate(rows)
        result = evaluate_objective_completion(objective=OBJECTIVE, task_kind="market_decision", observations=rows, decision=None)
        assert not result["passed"]


def test_host_tool_validator_accepts_persisted_waiting_receipt_without_an_order():
    tool = next(t for t in AutonomousTradingToolProvider().manifest() if t["name"] == "autonomy.activate")
    result = ValidatorEngine().validate_tool_result(tool=tool, arguments={"cycle_id": "AC-real-cycle"}, result=receipt())
    assert result.passed, result.to_dict()


def _plan_receipt(name):
    return {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "action": name,
            "account_id": "isolated", "mode": "paper", "cycle_id": "cycle", "campaign_receipt_id": "AE-" + "a"*64,
            "plan": {"plan_id": "plan", "account_id": "isolated", "symbol": "2330.TW",
                     "definition": {"strategy_id": "candle", "metadata": {"cycle_id": "cycle"}},
                     "state": {"status": "waiting_entry", "filled_quantity": 0}, "status": "waiting_entry"}}


def _validate_plan_receipt(name, result):
    return ValidatorEngine().validate_tool_result(tool={"name": name, "mutating": True},
        arguments={"cycle_id": "cycle", "plan_id": "plan", "symbol": "2330.TW"}, result=result)


def test_proposal_receipt_binds_cycle_and_account_without_requiring_a_fill_or_manage():
    value = _plan_receipt("autonomy.propose_plan")
    assert _validate_plan_receipt("autonomy.propose_plan", value).passed
    for mutation in (
        lambda r: r.update(cycle_id="other"),
        lambda r: r["plan"].update(account_id="other"),
        lambda r: r["plan"].update(symbol="2317.TW"),
        lambda r: r["plan"]["definition"]["metadata"].update(cycle_id="other"),
        lambda r: r["plan"]["state"].update(status="closed"),
    ):
        wrong = deepcopy(value)
        mutation(wrong)
        assert not _validate_plan_receipt("autonomy.propose_plan", wrong).passed
        assert not ValidatorEngine().validate_tool_result(
            tool={"name": "autonomy.propose_plan", "mutating": True}, arguments={"cycle_id": "cycle", "symbol": "2330.TW"},
            result=wrong, before={"hash": "old"}, after={"hash": "new"},
        ).passed


def test_close_request_receipt_requires_bound_plan_exit_evidence_and_management():
    value = _plan_receipt("autonomy.close_plan")
    value["exit_request"] = {"request_id": "exit", "plan_id": "plan", "evidence_id": "AE-" + "b"*64, "reason": "agent_reassessment"}
    value["management"] = {"account_id": "isolated", "enabled": True, "results": [], "errors": []}
    assert _validate_plan_receipt("autonomy.close_plan", value).passed
    for mutation in (
        lambda r: r["plan"].update(plan_id="other"),
        lambda r: r["exit_request"].update(plan_id="other"),
        lambda r: r["exit_request"].pop("evidence_id"),
        lambda r: r["exit_request"].update(reason="unreviewed"),
        lambda r: r["management"].update(errors=[{"error": "failed"}]),
        lambda r: r["management"].update(account_id="other"),
    ):
        wrong = deepcopy(value)
        mutation(wrong)
        assert not _validate_plan_receipt("autonomy.close_plan", wrong).passed
