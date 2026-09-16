"""Isolated Host/SDK event contracts; no model, network or production ledger."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from open_stock_ai.agent_runtime.contracts import build_runtime_event
from open_stock_ai.execution.agent_plan_proposal import build_agent_plan_proposal
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider
from stock_ai.tool_providers.autonomy_model_context import retain_model_invocation_context
from test_agent_plan_proposal import fixture as draft_fixture
from test_autonomy_evidence_resolution import host


MODEL = {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium",
         "thread_id": "thread-contract-fixture", "resolution_source": "sdk_thread_start"}


def event(h, event_type, *, step=1, **payload):
    h["sequence"] += 1
    h["store"].append_event(h["context"].run_id, build_runtime_event(
        event_type, sequence=h["sequence"], run_id=h["context"].run_id,
        session_id=h["context"].session_id, payload={"step": step, **payload}))


def lifecycle(h, *, step=1, call="call-one", node="node-one", sdk=True,
              sdk_status="completed", resumed=False, tool="autonomy.propose_plan"):
    h["context"].state.update(provider_model_metadata=dict(MODEL), current_plan_node_id=node)
    model = {"driver": "codex", "model": MODEL["model"], "reasoning_effort": MODEL["reasoning_effort"],
             "model_call_id": f"{h['context'].run_id}:turn:{step}"}
    if not resumed:
        event(h, "model.turn.started", step=step, **model)
        event(h, "model.session.configured", step=step, **MODEL)
        if sdk:
            for kind, status in (("started", "inProgress"), ("completed", sdk_status)):
                event(h, "model.sdk.turn." + kind, step=step, source="codex_app_server",
                      model=MODEL["model"], reasoning_effort=MODEL["reasoning_effort"],
                      turn={"id": f"sdk-turn-{step}", "status": status}, raw_prompt="DO-NOT-RETAIN-PROMPT")
    else:
        event(h, "approval.resumed_tool_dispatch", step=step)
    # The existing Host also emits completion after resumed dispatch; that
    # event alone cannot become a newly invoked model.
    event(h, "model.turn.completed", step=step, **model)
    event(h, "tool.started", step=step, call_id=call, node_id=node, tool=tool,
          arguments={"rationale": "DO-NOT-RETAIN-ARGUMENTS"})


def capture(h, name="autonomy.propose_plan"):
    result = retain_model_invocation_context(h["campaign"], name=name, context=h["context"],
                                             run_store_loader=lambda: h["store"])
    return result, h["campaign"]._evidence(result["context_receipt_id"], "model_invocation_context")


def test_exact_host_sdk_turn_and_account_owned_context_are_retained_without_raw_bodies(host):
    lifecycle(host)
    host["context"].state["provider_model_metadata"]["api_key"] = "DO-NOT-RETAIN-CREDENTIAL"
    host["campaign"].execution_context = {"deployment_receipt_id": "AE-deployment",
        "raw_arguments": "DO-NOT-RETAIN-DEPLOYMENT", "deployment_receipt": {
            "account_id": host["campaign"].broker.account_id, "instance_id": "instance-fixture",
            "database_bindings": {"trading_database_path_sha256": "a" * 64, "raw_path": "DO-NOT-RETAIN-PATH"},
            "secret": "DO-NOT-RETAIN-SECRET"}}
    result, payload = capture(host)
    assert result["status"] == "host_sdk_turn_correlated" and result["reasons"] == []
    assert payload["account_id"] == host["campaign"].broker.account_id
    assert payload["tool"]["call_id"] == "call-one" and payload["tool"]["node_id"] == "node-one"
    assert payload["model_turn_events"][0]["model_call_id"].endswith(":turn:1")
    assert payload["sdk_turn_events"][-1]["turn"] == {"id": "sdk-turn-1", "status": "completed"}
    assert payload["runtime_database_path_sha256"] == hashlib.sha256(str(host["store"].path.resolve()).encode()).hexdigest()
    assert "DO-NOT-RETAIN" not in json.dumps(payload)
    assert payload["execution_context"]["deployment_receipt"]["database_bindings"] == {"trading_database_path_sha256": "a" * 64}
    assert "positive_ev_qualified" not in result and "m1_verified" not in result


@pytest.mark.parametrize("sdk,status,reason", [(False, "completed", "completed_sdk_turn_pair_missing_or_ambiguous"),
                                               (True, "failed", "completed_sdk_turn_pair_missing_or_ambiguous")])
def test_host_model_completion_without_successful_sdk_turn_remains_unknown(host, sdk, status, reason):
    lifecycle(host, sdk=sdk, sdk_status=status)
    result, _ = capture(host)
    assert result["status"] == "unknown" and reason in result["reasons"]


def test_two_calls_in_same_run_bind_only_their_own_model_steps(host):
    lifecycle(host)
    first, one = capture(host)
    lifecycle(host, step=2, call="call-two", node="node-two")
    second, two = capture(host)
    assert first["status"] == second["status"] == "host_sdk_turn_correlated"
    assert first["context_receipt_id"] != second["context_receipt_id"]
    assert one["tool"]["call_id"] == "call-one" and two["tool"]["call_id"] == "call-two"
    assert {e["step"] for e in two["model_turn_events"] + two["sdk_turn_events"]} == {2}
    host["context"].state["current_plan_node_id"] = "node-one"
    assert capture(host)[0] == first


def test_ambiguous_running_calls_and_wrong_run_identity_do_not_correlate(host):
    lifecycle(host)
    event(host, "tool.started", call_id="duplicate", node_id="node-one", tool="autonomy.propose_plan")
    result, _ = capture(host)
    assert result["status"] == "unknown" and "running_tool_call_ambiguous" in result["reasons"]
    host["context"].session_id = "wrong-session"
    result, _ = capture(host)
    assert "runtime_run_identity_mismatch_or_missing" in result["reasons"]


def test_host_resumed_dispatch_never_creates_a_new_model_invocation(host):
    lifecycle(host, resumed=True)
    result, payload = capture(host)
    assert result["status"] == "unknown" and payload["host_resumed_dispatch"]
    assert "host_resumed_dispatch_is_not_a_new_model_turn" in result["reasons"]


def test_configured_selection_without_sdk_resolved_session_is_unknown(host):
    lifecycle(host)
    host["context"].state["provider_model_metadata"].update(resolution_source="host_configured_selection", thread_id="")
    result, _ = capture(host)
    assert result["status"] == "unknown" and "sdk_resolved_model_session_missing" in result["reasons"]


def test_legacy_direct_context_skips_runtime_loading_and_keeps_unknown_receipt(host):
    def forbidden():
        raise AssertionError("A direct context must not initialize or scan a runtime")
    result = retain_model_invocation_context(host["campaign"], name="autonomy.manage", context=host["context"],
                                             run_store_loader=forbidden)
    assert result["status"] == "unknown" and result["reasons"] == ["host_plan_node_id_missing"]


def test_failed_provenance_reads_and_retention_do_not_block_management(host, monkeypatch):
    lifecycle(host, tool="autonomy.manage")
    from stock_ai import autonomous_trading_service as wiring
    monkeypatch.setattr(wiring, "get_autonomous_model_review", lambda service=None: SimpleNamespace(runtime=SimpleNamespace(store=object())))
    calls = []
    async def manage():
        calls.append("managed")
        return {"results": [], "errors": []}
    host["campaign"].manage = manage
    original = host["campaign"]._retain
    def retain(kind, payload):
        if kind == "model_invocation_context":
            raise OSError("DO-NOT-EXPOSE-ERROR")
        return original(kind, payload)
    host["campaign"]._retain = retain
    receipt = asyncio.run(AutonomousTradingToolProvider().execute("autonomy.manage", {}, host["context"]))
    assert calls == ["managed"] and receipt["model_invocation_context"]["status"] == "unknown"
    assert receipt["model_invocation_context"]["retention_status"] == "unavailable"
    assert "DO-NOT-EXPOSE" not in json.dumps(receipt)


def test_proposal_retry_with_different_context_keeps_original_definition_and_model_origin(host):
    lifecycle(host)
    args = {"cycle_id": host["cycle"]["cycle_id"], "symbol": "2330.TW", "quantity_shares": 10,
            "stop_loss": 95, "target_price": 130, "rationale": "Offline provenance contract."}
    provider = AutonomousTradingToolProvider()
    first = asyncio.run(provider.execute("autonomy.propose_plan", args, host["context"]))
    original = deepcopy(first["plan"])
    lifecycle(host, step=2, call="retry-call", node="retry-node")
    second = asyncio.run(provider.execute("autonomy.propose_plan", args, host["context"]))
    assert first["model_invocation_context"]["context_receipt_id"] != second["model_invocation_context"]["context_receipt_id"]
    assert second["plan"] == original
    assert second["plan"]["definition"]["metadata"]["agent_context"]["tool_call_id"] == "call-one"
    assert len(host["campaign"].plans.list(account_id=host["campaign"].broker.account_id)) == 1
    assert host["campaign"].broker.submissions == []


def test_missing_runtime_receipts_do_not_strand_owned_plan_close(host, monkeypatch):
    lifecycle(host)
    args = {"cycle_id": host["cycle"]["cycle_id"], "symbol": "2330.TW", "quantity_shares": 10,
            "stop_loss": 95, "target_price": 130, "rationale": "Offline close contract."}
    provider = AutonomousTradingToolProvider()
    plan = asyncio.run(provider.execute("autonomy.propose_plan", args, host["context"]))["plan"]
    from stock_ai import autonomous_trading_service as wiring
    monkeypatch.setattr(wiring, "get_autonomous_model_review", lambda service=None: SimpleNamespace(runtime=SimpleNamespace(store=object())))
    result = asyncio.run(provider.execute("autonomy.close_plan", {"plan_id": plan["plan_id"],
                                             "rationale": "Withdraw the unfilled plan."}, host["context"]))
    assert result["plan"]["status"] == "cancelled" and host["campaign"].broker.submissions == []
    context = result["model_invocation_context"]
    assert context["status"] == "unknown" and context["context_receipt_id"].startswith("AE-")
    assert host["campaign"]._evidence(result["exit_request"]["evidence_id"])["host_context"]["context_receipt_id"] == context["context_receipt_id"]


def test_runtime_reads_are_bounded_to_current_run_node_and_step(host, monkeypatch):
    lifecycle(host)
    queries = []
    original = host["store"]._connect
    def connect():
        conn = original()
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr(host["store"], "_connect", connect)
    assert capture(host)[0]["status"] == "host_sdk_turn_correlated"
    reads = [q.lower() for q in queries if q.lstrip().lower().startswith("select")]
    assert len(reads) == 3 and all("where run_id=" in q for q in reads)
    assert any("node_id=" in q and "limit 2" in q for q in reads)
    assert any("'$.step'" in q and "limit 33" in q for q in reads)
    assert all("select *" not in q and "arguments_json" not in q and "checkpoint" not in q for q in reads)


def test_context_receipt_changes_evidence_binding_without_changing_investment_policy():
    args, kwargs = draft_fixture()
    first = build_agent_plan_proposal(args, **kwargs)
    kwargs["host_context"].update(context_receipt_id="AE-new-context", tool_call_id="new-call")
    second = build_agent_plan_proposal(args, **kwargs)
    assert second.strategy_version == first.strategy_version
    assert second.metadata["proposal_spec"] == first.metadata["proposal_spec"]
    assert second.metadata["agent_context_sha256"] != first.metadata["agent_context_sha256"]
