"""Offline bridge integration with real paper accounting; never EV evidence."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import socket
from types import SimpleNamespace

import pytest

from open_stock_ai.execution.agent_campaign_actions import close_plan, propose_plan
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_odd_lot import OddLotBoardProxyModel
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.paper_training import PaperTrainingLab
from open_stock_ai.execution.trading_plan import content_hash, utc_time
from open_stock_ai.research.agent_forward_validation import AgentForwardValidation
from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai import autonomous_forward_monitor as bridge
from test_agent_forward_validation import CONTEXT, COSTS
from test_autonomous_campaign import NOW, setup as campaign_setup
from product_admission_fixtures import product_feature


@pytest.fixture
def host(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("offline_forward_monitor_must_not_open_network")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    clock = {"now": NOW, "source": "a"}
    for cls in (PaperOMS, PaperBrokerSimulator, PaperTrainingLab):
        monkeypatch.setattr(cls, "_now", lambda self: clock["now"].isoformat())
    monkeypatch.setattr(bridge, "host_policy_sources", lambda configuration: {
        "tools": {"manifest.py": clock["source"] * 64}, "execution": {"executor.py": "b" * 64},
        "config": {"host": content_hash(configuration)}})
    store = SQLiteStore(tmp_path / "forward-bridge.sqlite")
    oms = PaperOMS(store, account_id="forward-bridge-fixture", initial_cash=100_000,
                   commission_bps=14.25, minimum_commission=20, sell_tax_bps=30,
                   slippage_bps=5, read_environment=False)
    broker = PaperBrokerPort(PaperBrokerSimulator(store, oms, odd_lot_execution_model=OddLotBoardProxyModel()))
    campaign, _ = campaign_setup(tmp_path, broker=broker, shared=store, symbols=("2330.TW",))
    campaign.calendar.snapshot_sha256 = "c" * 64
    campaign.calendar.source = "offline_calendar_fixture_not_market_evidence"
    campaign.costs = deepcopy(COSTS)
    cycle = asyncio.run(campaign.research(now=clock["now"], deep_limit=1))
    registry = AgentForwardValidation(store, clock=lambda: clock["now"])
    monitor = bridge.AutonomousForwardMonitor(campaign, registry=registry)
    campaign.forward_monitor = monitor
    run = {"run_id": CONTEXT["run_id"], "session_id": CONTEXT["session_id"], "status": "running",
           "request": {"max_steps": 12, "objective": "Frozen autonomous paper policy"}}
    return {"campaign": campaign, "monitor": monitor, "registry": registry, "store": store,
            "clock": clock, "run": run, "cycle": cycle}


def prepare(host, *, run=None, cycle=None, model=None):
    return host["monitor"].prepare(cycle_id=(cycle or host["cycle"])["cycle_id"], run=run or host["run"],
        model_receipt=model or CONTEXT, tool_manifest=[{"name": "autonomy.propose_plan"}],
        prompt_template="Scan, research, decide and manage bounded paper positions.")


def decisions(host, binding):
    return host["registry"].records(protocol_id=binding["protocol_id"],
                                     account_id=host["campaign"].broker.account_id, kind="decision")


def proposal(host):
    return {"cycle_id": host["cycle"]["cycle_id"], "symbol": "2330.TW", "quantity_shares": 10,
            "entry_condition": "price_at_or_above", "trigger_price": 110, "stop_loss": 105,
            "target_price": 120, "rationale": "Offline conditional decision; no profitability claim."}


def test_policy_and_protocol_stay_stable_across_runs_cycles_and_restart(host):
    first = prepare(host)
    host["clock"]["now"] += timedelta(seconds=1)
    cycle = asyncio.run(host["campaign"].research(now=host["clock"]["now"], deep_limit=1))
    other_run = {**host["run"], "run_id": "AR-other", "session_id": "AS-other"}
    other_model = {**CONTEXT, "run_id": "AR-other", "session_id": "AS-other",
                   "provider_model_metadata": {**CONTEXT["provider_model_metadata"], "thread_id": "transient"}}
    second = prepare(host, run=other_run, cycle=cycle, model=other_model)
    assert first["cycle_id"] != second["cycle_id"]
    assert first["policy_version"] == second["policy_version"]
    assert first["protocol_id"] == second["protocol_id"]
    assert first["binding_eligible"] and first["entry_cutoff"] == first["exit_deadline"]
    assert utc_time(first["exit_deadline"]) == utc_time(first["ends_at"]) - timedelta(minutes=70)
    policy = host["registry"].policy(first["policy_version"])
    assert policy["decision_policy"]["host_configuration"]["protocol_terminal_liquidation_at_last_observation_minus70minutes"]
    assert policy["decision_policy"]["host_configuration"]["calendar_snapshot_sha256"] == "c" * 64
    host["monitor"] = bridge.AutonomousForwardMonitor(host["campaign"], registry=AgentForwardValidation(
        host["store"], clock=lambda: host["clock"]["now"]))
    assert prepare(host) == first
    assert len(host["monitor"].status()["protocols"]) == 1
    assert not host["monitor"].status()["live_execution_eligible"]


def test_preregister_timed_plan_cost_evidence_and_duplicate_proposal(host):
    binding = prepare(host)
    args = proposal(host)
    plan = asyncio.run(propose_plan(host["campaign"], args, host_context=CONTEXT,
                                    now=host["clock"]["now"], forward_binding=binding))
    assert utc_time(plan["created_at"]) < utc_time(binding["starts_at"])
    assert utc_time(plan["definition"]["not_before"]) >= utc_time(binding["starts_at"])
    assert utc_time(plan["definition"]["exit_not_after"]) <= utc_time(binding["exit_deadline"])
    assert plan["definition"]["metadata"]["forward_validation"] == {
        key: binding[key] for key in ("protocol_id", "policy_version")}
    assert plan["definition"]["metadata"]["positive_ev_qualified"] is False
    first = decisions(host, binding)
    assert len(first) == 1 and first[0]["action"] == "propose"
    assert {"price_history", "cost_model"} <= {e["kind"] for e in first[0]["evidence"]}
    host["clock"]["now"] += timedelta(seconds=10)
    assert asyncio.run(propose_plan(host["campaign"], args, host_context=CONTEXT,
                                   now=host["clock"]["now"], forward_binding=binding)) == plan
    assert decisions(host, binding) == first
    assert host["campaign"].broker.broker.recent_fills() == []


@pytest.mark.parametrize("status", ["failed", "cancelled", "partially_completed", "max_steps_reached", "interrupted"])
def test_terminal_rejection_then_completed_recovery_are_immutable_and_idempotent(host, status):
    binding = prepare(host)
    run = {**host["run"], "status": status, "completed_at": host["clock"]["now"].isoformat(),
           "result": {"summary": "Retained opaque model result."}}
    failed = host["monitor"].capture_review(cycle_id=host["cycle"]["cycle_id"], run=run, model_receipt=CONTEXT)
    assert failed["action"] == "reject"
    assert host["monitor"].capture_review(cycle_id=host["cycle"]["cycle_id"], run=run, model_receipt=CONTEXT) == failed
    assert host["monitor"].capture_review(cycle_id=host["cycle"]["cycle_id"], run=run,
                                          model_receipt=CONTEXT["provider_model_metadata"]) == failed
    host["clock"]["now"] += timedelta(seconds=1)
    completed = {**run, "status": "completed", "completed_at": host["clock"]["now"].isoformat(),
                 "result": {"summary": "Recovered; wait for a future trigger."}}
    held = host["monitor"].capture_review(cycle_id=host["cycle"]["cycle_id"], run=completed, model_receipt=CONTEXT)
    assert held["action"] == "hold" and held["decision_id"] != failed["decision_id"]
    assert host["monitor"].capture_review(cycle_id=host["cycle"]["cycle_id"], run=completed, model_receipt=CONTEXT) == held
    assert decisions(host, binding) == [failed, held]
    records = host["registry"].records(protocol_id=binding["protocol_id"], account_id=host["campaign"].broker.account_id)
    assert not records["deviation"]
    assert host["monitor"].status()["protocols"][0]["decisions"] == 2


def test_same_run_two_cycles_terminal_receipts_do_not_conflict(host):
    first = prepare(host)
    host["clock"]["now"] += timedelta(seconds=1)
    second_cycle = asyncio.run(host["campaign"].research(now=host["clock"]["now"], deep_limit=1))
    second = prepare(host, cycle=second_cycle)
    run = {**host["run"], "status": "completed", "result": {"summary": "Wait."}}
    for cycle in (host["cycle"], second_cycle):
        result = host["monitor"].capture_review(cycle_id=cycle["cycle_id"], run=run, model_receipt=CONTEXT)
        assert host["monitor"].capture_review(cycle_id=cycle["cycle_id"], run=run, model_receipt=CONTEXT) == result
    assert len(decisions(host, first)) == 2 and first["protocol_id"] == second["protocol_id"]


def test_terminal_receipt_keeps_decision_and_hash_without_copying_durable_transcript(host):
    binding = prepare(host)
    result = {"status": "completed", "summary": "Wait for a verified opening quote.",
              "decision": {"action": "hold"}, "tool_trace": [{"result": "large evidence" * 20000}],
              "plan": {"nodes": [{"details": "large plan" * 20000}]},
              "completion_validation": {"passed": True, "checks": [
                  {"name": "completion_evaluator", "passed": True, "details": "large trace" * 20000}]}}
    original = deepcopy(result)
    row = host["monitor"].capture_review(cycle_id=host["cycle"]["cycle_id"],
        run={**host["run"], "status": "completed", "result": result}, model_receipt=CONTEXT)
    evidence = next(e for e in row["evidence"] if e["kind"] == "agent_forward_review")
    saved = host["campaign"]._evidence(evidence["evidence_id"])
    assert saved["result_sha256"] == content_hash(result)
    assert saved["result"]["summary"] == result["summary"] and saved["result"]["decision"] == {"action": "hold"}
    assert saved["result"]["completion_validation"]["checks"] == [{"name": "completion_evaluator", "passed": True}]
    assert len(json.dumps(saved)) < 3000 and result == original
    assert len(decisions(host, binding)) == 1


def test_generated_review_hook_freezes_constant_prompt_across_different_market_cycles(host):
    from stock_ai.autonomous_model_review import AutonomousModelReview
    runtime = SimpleNamespace(_service_provider=lambda: SimpleNamespace(
        tools=SimpleNamespace(manifest=lambda: [{"name": "autonomy.propose_plan"}])))
    review = AutonomousModelReview(campaign=host["campaign"], runtime=runtime)
    first_cycle = host["cycle"]
    first_run = {**host["run"], "request": {"max_steps": 12, "objective": review._objective(first_cycle)}}
    first = review.prepare_forward_review(cycle_id=first_cycle["cycle_id"], run=first_run, model_receipt=CONTEXT)
    host["clock"]["now"] += timedelta(seconds=1)
    second_cycle = asyncio.run(host["campaign"].research(now=host["clock"]["now"], deep_limit=1))
    second_run = {**first_run, "run_id": "AR-next-day", "request": {"max_steps": 12,
        "objective": review._objective(second_cycle)}}
    second = review.prepare_forward_review(cycle_id=second_cycle["cycle_id"], run=second_run, model_receipt=CONTEXT)
    assert first_run["request"]["objective"] != second_run["request"]["objective"]
    assert first["policy_version"] == second["policy_version"] and first["protocol_id"] == second["protocol_id"]
    policy = host["registry"].policy(first["policy_version"])
    assert first_cycle["cycle_id"] not in policy["prompt_template"]
    assert policy["decision_policy"]["eligibility"] == "bounded_experiment"


@pytest.mark.parametrize("change", ["model", "code"])
def test_changed_policy_cannot_pool_runs_or_reenable_prior_binding(host, change):
    first = prepare(host)
    model = deepcopy(CONTEXT)
    if change == "model":
        model["provider_model_metadata"]["model"] = "gpt-6-astra"
    else:
        host["clock"]["source"] = "d"
    mismatch = prepare(host, model=model)
    assert not mismatch["binding_eligible"] and mismatch["status"] == "policy_changed_during_review"
    assert host["monitor"]._binding(host["run"]["run_id"], host["cycle"]["cycle_id"]) == mismatch
    assert host["monitor"].capture_review(cycle_id=host["cycle"]["cycle_id"],
        run={**host["run"], "status": "completed"}, model_receipt=model) is None
    later = prepare(host, run={**host["run"], "run_id": "AR-new-policy"}, model=model)
    assert later["binding_eligible"] and later["policy_version"] != first["policy_version"]
    assert later["protocol_id"] != first["protocol_id"]
    rows = host["monitor"].status()["protocols"]
    assert len(rows) == 2 and rows[0]["status"] == "abandoned"
    assert not rows[0]["evaluation"]["positive_ev_qualified"]


def test_registration_diagnostic_does_not_block_safe_paper_draft(host):
    host["campaign"].costs["market_impact_bps"] = 1
    binding = prepare(host)
    assert binding["status"] == "registration_requires_inputs" and not binding["binding_eligible"]
    plan = asyncio.run(propose_plan(host["campaign"], proposal(host), host_context=CONTEXT,
                                    now=host["clock"]["now"], forward_binding=binding))
    assert not plan["definition"]["metadata"].get("forward_validation")
    assert plan["definition"]["metadata"]["eligibility"] == "bounded_experiment"
    assert not host["monitor"].status()["protocols"]
    assert host["campaign"].broker.broker.recent_fills() == []


def _cycle_with_features(host, features):
    campaign = host["campaign"]
    bulk = campaign._evidence(host["cycle"]["bulk_evidence_id"], "market_screen")
    bulk = {**bulk, "features": features}
    cycle = {**host["cycle"], "bulk_evidence_id": campaign._retain("market_screen", bulk)}
    cycle["cycle_id"] = "AC-" + content_hash({key: value for key, value in cycle.items() if key != "cycle_id"})
    with host["store"]._connect() as conn:
        conn.execute("insert into autonomous_research_cycles values (?,?,?,?)", (
            cycle["cycle_id"], campaign.broker.account_id, cycle["created_at"], json.dumps(cycle)))
        conn.commit()
    return cycle


def test_native_mixed_universe_registers_numeric_instruments_and_retains_exclusions(host):
    features = [product_feature(symbol, now=NOW) for symbol in ("3231.TW", "6538.TWO", "1101B.TW", "2887Z1.TW", "01001T.TW")]
    features += [{**product_feature("0050.TW", now=NOW), "is_etf": True}, product_feature("3231.TW", now=NOW),
                 {"symbol": "2330.tw"}, {"symbol": "２３３０.TW"}]
    cycle = _cycle_with_features(host, features)
    binding = prepare(host, cycle=cycle)
    assert binding["binding_eligible"] and binding["status"] == "registered"
    protocol = host["registry"].protocol(binding["protocol_id"], account_id=host["campaign"].broker.account_id)
    assert protocol["symbols"] == ["3231.TW", "6538.TWO"]
    selection = binding["universe_selection"]
    assert selection["source_count"] == 9 and selection["accepted_count"] == 2 and selection["excluded_count"] == 6
    assert selection["evidence_id"] in binding["evidence_ids"]
    retained = host["campaign"]._evidence(selection["evidence_id"], "forward_universe_selection")
    excluded = {item["symbol"]: item["reasons"] for item in retained["excluded"]}
    assert "product_type_not_supported" in excluded["0050.TW"]
    assert "product_symbol_not_supported" in excluded["1101B.TW"]
    assert retained["source_evidence_id"] == cycle["bulk_evidence_id"]
    assert prepare(host, cycle=cycle) == binding


def test_empty_supported_universe_remains_registration_error(host):
    cycle = _cycle_with_features(host, [{"symbol": "1101B.TW"}, {"symbol": "0050.TW", "is_etf": True}])
    binding = prepare(host, cycle=cycle)
    assert not binding["binding_eligible"] and binding["protocol_id"] is None
    assert binding["error"] == "frozen_canonical_taiwan_universe_required"
    assert binding["universe_selection"]["excluded_count"] == 2
    assert not host["registry"].list_protocols(account_id=host["campaign"].broker.account_id)


@pytest.mark.parametrize("change", ["model", "code"])
def test_failed_registration_can_prepare_corrected_policy_before_any_protocol(host, change):
    host["campaign"].costs["market_impact_bps"] = 1
    failed = prepare(host)
    assert failed["protocol_id"] is None and not failed["binding_eligible"]
    original_policy = host["registry"].policy(failed["policy_version"])
    host["campaign"].costs = deepcopy(COSTS)
    model = deepcopy(CONTEXT)
    if change == "model":
        model["provider_model_metadata"]["model"] = "gpt-6-astra"
    else:
        host["clock"]["source"] = "d"
    corrected = prepare(host, model=model)
    assert corrected["binding_eligible"] and corrected["status"] == "registered"
    assert corrected["policy_version"] != failed["policy_version"]
    assert corrected["previous_registration_attempts"][0]["error"] == failed["error"]
    assert host["registry"].policy(failed["policy_version"]) == original_policy
    assert len(host["registry"].list_protocols(account_id=host["campaign"].broker.account_id)) == 1
    assert prepare(host, model=model) == corrected


def test_corrected_unregistered_policy_still_cannot_preregister_an_existing_plan(host):
    host["campaign"].costs["market_impact_bps"] = 1
    failed = prepare(host)
    asyncio.run(propose_plan(host["campaign"], proposal(host), host_context=CONTEXT,
                             now=host["clock"]["now"], forward_binding=failed))
    host["campaign"].costs = deepcopy(COSTS)
    corrected = prepare(host)
    assert corrected["protocol_id"] is None and not corrected["binding_eligible"]
    assert corrected["error"] == "protocol_requires_flat_account_without_active_plans"
    assert not host["registry"].list_protocols(account_id=host["campaign"].broker.account_id)


def test_native_failed_universe_registration_recovers_after_host_code_change(host, monkeypatch):
    cycle = _cycle_with_features(host, [product_feature("3231.TW", now=NOW), {"symbol": "1101B.TW"}])
    fixed_selector = bridge._forward_universe
    def old_selector(features, *, now=None):
        symbols = [row["symbol"] for row in features]
        return symbols, {"source_count": len(features), "accepted_count": len(symbols),
                         "excluded_count": 0, "excluded": [], "selection_policy": "old_flags_only_fixture"}
    monkeypatch.setattr(bridge, "_forward_universe", old_selector)
    failed = prepare(host, cycle=cycle)
    assert failed["error"] == "frozen_canonical_taiwan_universe_required" and failed["protocol_id"] is None
    old_policy = host["registry"].policy(failed["policy_version"])
    monkeypatch.setattr(bridge, "_forward_universe", fixed_selector)
    host["clock"]["source"] = "d"
    corrected = prepare(host, cycle=cycle)
    assert corrected["binding_eligible"] and corrected["protocol_id"]
    assert corrected["previous_registration_attempts"][0]["error"] == failed["error"]
    assert host["registry"].policy(failed["policy_version"]) == old_policy
    assert host["registry"].protocol(corrected["protocol_id"], account_id=host["campaign"].broker.account_id)["symbols"] == ["3231.TW"]


def test_rejected_proposal_retries_retain_one_decision_and_original_reason(host):
    binding = prepare(host)
    args = {**proposal(host), "stop_loss": 115}
    for _ in range(2):
        with pytest.raises(ValueError):
            asyncio.run(propose_plan(host["campaign"], args, host_context=CONTEXT,
                                    now=host["clock"]["now"], forward_binding=binding))
    rows = decisions(host, binding)
    assert len(rows) == 1 and rows[0]["action"] == "reject"
    retained = next(e for e in rows[0]["evidence"] if e["kind"] == "agent_proposal_rejection")
    original = host["campaign"]._evidence(retained["evidence_id"])
    assert original["proposal"] == args and original["reason"] == rows[0]["rationale"]
    assert not host["campaign"].plans.list(account_id=host["campaign"].broker.account_id)


def test_new_policy_registration_can_retry_after_old_pending_plan_resolves(host):
    first = prepare(host)
    plan = asyncio.run(propose_plan(host["campaign"], proposal(host), host_context=CONTEXT,
                                   now=host["clock"]["now"], forward_binding=first))
    host["clock"]["source"] = "d"
    run = {**host["run"], "run_id": "AR-new-policy"}
    blocked = prepare(host, run=run)
    assert blocked["status"] == "previous_policy_has_unresolved_positions"
    assert not blocked["binding_eligible"]
    plans, now = host["campaign"].plans, host["clock"]["now"]
    with plans.lease(plan["plan_id"], now=now) as owner:
        plans.save_state(plan["plan_id"], state={**plan["state"], "status": "cancelled"},
                         revision=plan["revision"], event_type="plan.cancelled", now=now, lease_owner=owner)
    retry = prepare(host, run=run)
    assert retry["binding_eligible"] and retry["protocol_id"] != first["protocol_id"]
    assert prepare(host, run=run) == retry


def test_cutoff_is_diagnostic_and_same_policy_does_not_auto_restart_after_evaluation(host):
    first = prepare(host)
    host["clock"]["now"] = utc_time(first["entry_cutoff"])
    late = prepare(host, run={**host["run"], "run_id": "AR-late"})
    assert late["protocol_id"] == first["protocol_id"] and not late["binding_eligible"]
    assert late["status"] == "registration_window_closed"
    assert not prepare(host)["binding_eligible"]
    # New entries have closed, but an already running review may still finish
    # and retain its decision before the observation window ends.
    terminal = host["monitor"].capture_review(cycle_id=host["cycle"]["cycle_id"],
        run={**host["run"], "status": "completed", "result": {"summary": "Remain in cash."}}, model_receipt=CONTEXT)
    assert terminal["action"] == "hold"
    protocol = host["registry"].protocol(first["protocol_id"], account_id=host["campaign"].broker.account_id)
    host["clock"]["now"] = utc_time(protocol["evaluate_at"])
    assert asyncio.run(host["monitor"].sync()) == {"errors": [], "model_calls": 0}
    later = prepare(host, run={**host["run"], "run_id": "AR-after-evaluation"})
    assert later["protocol_id"] == first["protocol_id"] and not later["binding_eligible"]
    assert len(host["monitor"].status()["protocols"]) == 1
    assert not host["monitor"].status()["protocols"][0]["evaluation"]["positive_ev_qualified"]


def test_daily_clock_dispatch_requires_ready_and_deduplicates_after_restart(host, monkeypatch):
    from open_stock_ai.execution import forward_daily_mark
    binding = prepare(host)
    protocol = host["registry"].protocol(binding["protocol_id"], account_id=host["campaign"].broker.account_id)
    calls, state = [], {"ready": False}
    async def retained_mark(campaign, *, scheduled_at, now):
        calls.append((scheduled_at, now))
        if not state["ready"]:
            return {"status": "retry", "evidence_ids": [], "blockers": ["same_day_close_not_available"]}
        snapshot = await campaign.broker.account(now=now)
        evidence = campaign._retain("forward_daily_mark", {
            "schema_version": "open_stock_ai.forward_daily_mark.v1", "account_id": campaign.broker.account_id,
            "scheduled_at": scheduled_at, "observed_at": now.isoformat(), "snapshot": snapshot,
            "quotes": [], "market_evidence_ids": [host["cycle"]["bulk_evidence_id"]],
            "fixture_only_not_market_or_ev_proof": True})
        return {"status": "ready", "evidence_ids": [evidence], "blockers": [], "snapshot": snapshot}
    monkeypatch.setattr(forward_daily_mark, "retain_forward_daily_mark", retained_mark)
    assert asyncio.run(host["monitor"].sync()) == {"errors": [], "model_calls": 0}
    assert not calls
    host["clock"]["now"] = utc_time(protocol["observation_times"][0])
    failed = asyncio.run(host["monitor"].sync())
    assert failed["errors"][0]["status"] == "retry"
    assert host["monitor"].status()["protocols"][0]["observations"] == 0
    state["ready"] = True
    assert asyncio.run(host["monitor"].sync()) == {"errors": [], "model_calls": 0}
    assert host["monitor"].status()["protocols"][0]["observations"] == 1
    restarted = bridge.AutonomousForwardMonitor(host["campaign"], registry=AgentForwardValidation(
        host["store"], clock=lambda: host["clock"]["now"]))
    assert asyncio.run(restarted.sync()) == {"errors": [], "model_calls": 0}
    assert len(calls) == 2 and host["campaign"].broker.broker.recent_fills() == []


def test_bound_native_failure_before_activation_reconciles_without_reservation_or_backfill(host, tmp_path, monkeypatch):
    from stock_ai import agent_run_store
    from stock_ai.autonomous_model_review import AutonomousModelReview
    monkeypatch.setattr(agent_run_store, "_now", lambda: host["clock"]["now"].isoformat())
    store = agent_run_store.AgentRunStore(tmp_path / "agent-runs.sqlite")
    request = {"objective": "Frozen paper policy", "session_id": CONTEXT["session_id"], "driver_id": "codex"}
    store.create_run(host["run"]["run_id"], request)
    store.create_run("AR-historic-unbound", request)
    store.complete_run("AR-historic-unbound", {"status": "completed", "summary": "Historical, never preregistered."})
    prepare(host)
    model = CONTEXT["provider_model_metadata"]
    calls = []
    original_get = store.get_run
    def get_run(run_id):
        calls.append(run_id)
        return original_get(run_id)
    monkeypatch.setattr(store, "get_run", get_run)
    review = AutonomousModelReview(campaign=host["campaign"], runtime=SimpleNamespace(store=store))
    assert not review._rows()  # Never reached activate/register_current_review.
    assert review.reconcile_forward_reviews() == {"captured": 0, "errors": [], "model_calls": 0}
    assert not calls
    store.fail_run(host["run"]["run_id"], {"message": "Failed before activation."})
    calls.clear()
    assert review.reconcile_forward_reviews() == {"captured": 1, "errors": [], "model_calls": 0}
    assert calls == [host["run"]["run_id"]]
    assert review.reconcile_forward_reviews() == {"captured": 0, "errors": [], "model_calls": 0}
    assert len(calls) == 1 and not review._rows()
    binding = prepare(host)
    assert [r["action"] for r in decisions(host, binding)] == ["reject"]
    restarted = bridge.AutonomousForwardMonitor(host["campaign"], registry=host["registry"])
    assert restarted.reconcile_reviews(store)["captured"] == 1
    assert len(decisions(host, binding)) == 1
    host["clock"]["now"] += timedelta(seconds=1)
    store.complete_run(host["run"]["run_id"], {"status": "completed", "summary": "Recovered and waiting.",
                                              "provider_model_metadata": model})
    review.status(now=host["clock"]["now"])
    assert [r["action"] for r in decisions(host, binding)] == ["reject", "hold"]
    assert "AR-historic-unbound" not in calls and not review._rows()


@pytest.mark.parametrize("change,verified", [(None, True), ("model", False), ("source", False), ("unbound_run", False)])
def test_close_records_original_protocol_and_current_policy_continuity(host, change, verified):
    binding = prepare(host)
    plan = asyncio.run(propose_plan(host["campaign"], proposal(host), host_context=CONTEXT,
                                    now=host["clock"]["now"], forward_binding=binding))
    context = deepcopy(CONTEXT)
    if change == "model":
        context["provider_model_metadata"]["model"] = "gpt-6-astra"
    elif change == "source":
        host["clock"]["source"] = "e"
    elif change == "unbound_run":
        context["run_id"] = "AR-no-preregistered-policy"
    result = close_plan(host["campaign"], plan_id=plan["plan_id"], rationale="Reassessed; close the owned plan.",
                        host_context=context, now=host["clock"]["now"])
    assert result["request_id"] and result["plan_id"] == plan["plan_id"]
    rows = host["registry"].records(protocol_id=binding["protocol_id"],
                                    account_id=host["campaign"].broker.account_id, kind="exit_decision")
    assert len(rows) == 1 and rows[0]["continuity_verified"] is verified
    again = close_plan(host["campaign"], plan_id=plan["plan_id"], rationale="Reassessed; close the owned plan.",
                       host_context=context, now=host["clock"]["now"])
    assert again == result
    assert host["campaign"].plans.get(plan["plan_id"])["definition_hash"] == plan["definition_hash"]
    assert host["campaign"].broker.broker.recent_fills() == []


def test_exit_bookkeeping_failure_is_diagnostic_and_does_not_block_safe_close(host, monkeypatch):
    binding = prepare(host)
    plan = asyncio.run(propose_plan(host["campaign"], proposal(host), host_context=CONTEXT,
                                    now=host["clock"]["now"], forward_binding=binding))
    def failure(**kwargs):
        raise ValueError("qualification_ledger_unavailable")
    monkeypatch.setattr(host["monitor"], "record_exit", failure)
    result = close_plan(host["campaign"], plan_id=plan["plan_id"], rationale="Reduce existing risk.",
                        host_context=CONTEXT, now=host["clock"]["now"])
    assert result["request_id"] and result["forward_validation"]["status"] == "exit_validation_requires_reconciliation"
    assert host["campaign"].plans.exit_request(plan["plan_id"], strategy_version=plan["definition"]["strategy_version"])


@pytest.mark.parametrize("change", ["source", "risk"])
def test_background_management_marks_changed_policy_without_a_model_decision(host, change):
    binding = prepare(host)
    assert asyncio.run(host["monitor"].sync())["errors"] == []
    if change == "source":
        host["clock"]["source"] = "d"
    else:
        host["campaign"].risk.max_daily_loss_pct += 1
    result = asyncio.run(host["campaign"].manage(now=host["clock"]["now"]))
    assert result["errors"] == [] and result["forward_validation"]["errors"] == []
    rows = host["registry"].records(protocol_id=binding["protocol_id"],
                                    account_id=host["campaign"].broker.account_id, kind="deviation")
    assert len(rows) == 1 and rows[0]["reason"] == "host_execution_policy_changed_during_forward_window"
    state = host["monitor"].status()["protocols"][0]
    assert state["status"] == "abandoned" and state["deviations"] == 1
    assert state["qualification_continuity_intact"] is False
    assert asyncio.run(host["monitor"].sync())["errors"] == []
    assert host["registry"].records(protocol_id=binding["protocol_id"],
        account_id=host["campaign"].broker.account_id, kind="deviation") == rows


def test_changed_policy_stops_observations_but_waits_for_active_plan_to_flatten(host):
    binding = prepare(host)
    asyncio.run(propose_plan(host["campaign"], proposal(host), host_context=CONTEXT,
                             now=host["clock"]["now"], forward_binding=binding))
    host["clock"]["source"] = "d"
    host["clock"]["now"] = utc_time(host["registry"].protocol(
        binding["protocol_id"], account_id=host["campaign"].broker.account_id)["observation_times"][0])

    result = asyncio.run(host["monitor"].sync())
    state = host["monitor"].status()["protocols"][0]
    records = host["registry"].records(protocol_id=binding["protocol_id"],
                                       account_id=host["campaign"].broker.account_id)
    assert result["errors"] == [] and state["status"] == "collecting"
    assert state["qualification_continuity_intact"] is False
    assert len(records["deviation"]) == 1 and records["observation"] == []
