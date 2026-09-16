"""Offline contracts, not forward market evidence or a model profitability test.

The end-to-end case uses production paper orders/accounting with synthetic
quotes, model identity and calendar. It explicitly cannot receive native EV
qualification. Pure statistical positive controls do not create market proof.
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import socket

import pytest

from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_odd_lot import OddLotBoardProxyModel
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.paper_training import PaperTrainingLab
from open_stock_ai.execution.trading_plan import TradingPlan, content_hash
from open_stock_ai.research.agent_forward_validation import AgentForwardValidation, _bootstrap, freeze_agent_policy
from open_stock_ai.storage.sqlite_store import SQLiteStore
from test_autonomous_campaign import NOW, history_fixture, setup as campaign_setup
from test_autonomous_trading_plans import market as executable_market
from test_paper_odd_lot_proxy import _market as proxy_market
from product_admission_fixtures import admitted_product


COSTS = {"commission_bps": 14.25, "minimum_commission": 20, "sell_tax_bps": 30,
         "slippage_bps": 5, "market_impact_bps": 20}
MODEL = {"driver_id": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium"}
CONTEXT = {"run_id": "AR-forward-fixture", "session_id": "AS-forward-fixture", "driver_id": "codex",
           "provider_model_metadata": {"model": MODEL["model"], "reasoning_effort": "medium"}}


def policy(**kwargs):
    return freeze_agent_policy(**{"model_receipt": MODEL, "prompt_template": "Decide using retained evidence.",
        "decision_policy": {"paper_budget_pct": 5}, "tool_manifest": [{"name": "market.research"}],
        "source_hashes": {group: {group + ".py": letter*64} for group, letter in (("tools", "a"), ("execution", "b"), ("config", "c"))},
        "required_evidence": ["price_history"], **kwargs})


@pytest.fixture
def host(tmp_path, monkeypatch, request):
    def no_network(*args, **kwargs):
        raise AssertionError("forward_contract_tests_must_not_open_network")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    clock = {"now": NOW, "price": 100}
    for cls in (PaperOMS, PaperBrokerSimulator, PaperTrainingLab):
        monkeypatch.setattr(cls, "_now", lambda _self: clock["now"].isoformat())
    store = SQLiteStore(tmp_path / "forward.sqlite")
    board = getattr(request, "param", None) == "board"
    oms = PaperOMS(store, account_id="forward-fixture", initial_cash=3_000_000 if board else 100_000,
                   commission_bps=14.25, minimum_commission=20, sell_tax_bps=30, slippage_bps=5, read_environment=False)
    broker = PaperBrokerPort(PaperBrokerSimulator(store, oms, odd_lot_execution_model=OddLotBoardProxyModel()),
                             public_board_quote_simulation=board)
    campaign, _ = campaign_setup(tmp_path, broker=broker, shared=store, symbols=("2330.TW",))
    campaign.costs = deepcopy(COSTS)
    async def quote(symbol):
        packet = proxy_market(at=clock["now"], price=clock["price"], size=100000 if board else 1000)
        packet["source_envelope"] = executable_market(clock["price"], clock["now"])["source_envelope"]
        packet.update(limit_up=120, limit_down=80)
        return packet
    campaign.quote_loader = quote
    registry = AgentForwardValidation(store, clock=lambda: clock["now"])
    frozen = registry.register_policy(policy())
    start = (NOW+timedelta(days=3)).replace(hour=1, minute=0)
    day, slots = start, []
    while len(slots) < 120:
        if day.weekday() < 5:
            slots.append(day.replace(hour=6, minute=35))
        day += timedelta(days=1)
    spec = {"account_id": broker.account_id, "policy_version": frozen["policy_version"], "symbols": ["2330.TW"],
            "costs": COSTS, "starts_at": start, "ends_at": slots[-1], "evaluate_at": slots[-1]+timedelta(hours=5),
            "observation_times": [s.isoformat() for s in slots], "resamples": 1000, "fixture": True}
    protocol = registry.register_protocol(**spec)
    history = campaign._retain("price_history", history_fixture("2330.TW"))
    return {"registry": registry, "campaign": campaign, "clock": clock, "policy": frozen, "protocol": protocol,
            "spec": spec, "slots": slots, "history": history, "store": store}


def hold(host, *, decision_id="hold", action="hold", **kwargs):
    protocol = host["protocol"]
    return host["registry"].record_decision(**{"protocol_id": protocol["protocol_id"], "account_id": protocol["account_id"],
        "decision_id": decision_id, "policy_version": protocol["policy_version"], "action": action,
        "model_receipt": MODEL, "evidence_ids": [host["history"]], "rationale": "Fixture decision", **kwargs})


def make_plan(host, *, key="plan", metadata=None, quantity=10):
    p, clock, campaign = host["protocol"], host["clock"], host["campaign"]
    definition = TradingPlan(symbol="2330.TW", strategy_id="agent_discretionary_proposal_v1",
        strategy_version=content_hash({"key": key}), evidence_ids=(host["history"],), reference_price=100,
        position_size_pct=quantity*100/p["initial_equity"]*100, stop_loss=95, target_price=110,
        quantity_shares=quantity, cash_budget=quantity*105,
        not_before=max(clock["now"], host["spec"]["starts_at"]).isoformat(), max_holding_seconds=86400,
        rationale="Offline prospective policy fixture", metadata={"product_admission": admitted_product("2330.TW", now=clock["now"]),
            **(metadata or {"agent_context": CONTEXT, "cost_assumptions": COSTS,
                "forward_validation": {"protocol_id": p["protocol_id"], "policy_version": p["policy_version"]}})})
    return campaign.plans.create(account_id=p["account_id"], plan=definition, idempotency_key=key, now=clock["now"])


def observe(host, slot):
    registry, campaign, protocol = host["registry"], host["campaign"], host["protocol"]
    host["clock"]["now"] = slot
    snapshot = asyncio.run(campaign.broker.account(now=slot))
    evidence = campaign._retain("forward_daily_mark", {"schema_version": "open_stock_ai.forward_daily_mark.v1",
        "account_id": protocol["account_id"], "scheduled_at": slot.isoformat(), "observed_at": slot.isoformat(),
        "snapshot": snapshot, "quotes": [], "market_evidence_ids": [host["history"]],
        "fixture_only_not_market_or_ev_proof": True})
    return registry.record_daily_observation(protocol_id=protocol["protocol_id"], account_id=protocol["account_id"],
                                              scheduled_at=slot, evidence_ids=[evidence])


def evaluate(host):
    host["clock"]["now"] = host["spec"]["evaluate_at"]
    return host["registry"].evaluate(protocol_id=host["protocol"]["protocol_id"], account_id=host["protocol"]["account_id"])


def test_policy_stable_across_runtime_ids_and_binds_every_material_input():
    base = policy()
    assert policy(model_receipt={**MODEL, "run_id": "other", "session_id": "other", "thread_id": "transient"}) == base
    for change in ({"model_receipt": {**MODEL, "model": "gpt-6-astra"}}, {"model_receipt": {**MODEL, "reasoning_effort": "ultra"}},
                   {"prompt_template": "Changed decision prompt"}, {"decision_policy": {"paper_budget_pct": 2}},
                   {"tool_manifest": [{"name": "market.history"}]}, {"required_evidence": ["events"]}):
        assert policy(**change)["policy_version"] != base["policy_version"]
    changed = deepcopy(base["source_hashes"])
    changed["execution"]["execution.py"] = "d"*64
    assert policy(source_hashes=changed)["policy_version"] != base["policy_version"]
    with pytest.raises(ValueError, match="credentials"):
        policy(model_receipt={**MODEL, "api_key": "secret"})


def test_protocol_preregistered_immutable_scoped_and_no_overlapping_reruns(host):
    r, p = host["registry"], host["protocol"]
    assert r.register_protocol(**host["spec"]) == p
    with pytest.raises(ValueError, match="overlapping"):
        r.register_protocol(**{**host["spec"], "block_length": 6})
    with pytest.raises(ValueError, match="account_mismatch"):
        r.protocol(p["protocol_id"], account_id="other")
    host["clock"]["now"] = host["spec"]["starts_at"]
    with pytest.raises(ValueError, match="precede_future"):
        r.register_protocol(**{**host["spec"], "block_length": 6})
    assert r.list_protocols(account_id="other") == []
    assert r.list_protocols(account_id=p["account_id"])[0]["status"] == "collecting"


def test_prestart_decisions_bind_future_plan_and_duplicate_or_versions_cannot_change(host):
    first = hold(host)
    assert first["recorded_at"] < host["protocol"]["starts_at"]
    assert hold(host) == first
    with pytest.raises(ValueError, match="immutable"):
        hold(host, rationale="Select a different answer afterward")
    with pytest.raises(ValueError, match="model_or_policy"):
        hold(host, model_receipt={**MODEL, "reasoning_effort": "ultra"})
    plan = make_plan(host)
    registered = hold(host, decision_id="propose", action="propose", symbol="2330.TW", plan_id=plan["plan_id"])
    assert registered["plan_definition_hash"] == plan["definition_hash"]
    with pytest.raises(ValueError, match="already_bound"):
        hold(host, decision_id="another", action="propose", symbol="2330.TW", plan_id=plan["plan_id"])


def test_clock_backfill_daily_gaps_and_premature_evaluation_are_rejected(host):
    hold(host)
    host["clock"]["now"] -= timedelta(seconds=1)
    with pytest.raises(ValueError, match="outside_registered|clock_regressed"):
        hold(host, decision_id="backdated")
    host["clock"]["now"] = host["slots"][0] + timedelta(hours=5)
    with pytest.raises(ValueError, match="timely"):
        host["registry"].record_daily_observation(protocol_id=host["protocol"]["protocol_id"], account_id=host["protocol"]["account_id"],
                                                  scheduled_at=host["slots"][0], evidence_ids=[host["history"]])
    with pytest.raises(ValueError, match="evaluation_time"):
        host["registry"].evaluate(protocol_id=host["protocol"]["protocol_id"], account_id=host["protocol"]["account_id"])
    result = evaluate(host)
    assert not result["passed"] and "missing_registered_observations_no_cherry_picking" in result["reasons"]


def test_deviation_is_immutable_nonqualifying_and_abandonment_spends_alpha(host):
    r, p = host["registry"], host["protocol"]
    deviation = r.record_deviation(protocol_id=p["protocol_id"], account_id=p["account_id"], reason="Actual model changed")
    assert r.record_deviation(protocol_id=p["protocol_id"], account_id=p["account_id"], reason="Actual model changed") == deviation
    abandoned = r.abandon_protocol(protocol_id=p["protocol_id"], account_id=p["account_id"], reason="Freeze replacement policy")
    assert abandoned["status"] == "abandoned" and not abandoned["positive_ev_qualified"]
    replacement = r.register_protocol(**{**host["spec"], "block_length": 6})
    assert replacement["family_sequence"] == 2 and replacement["one_sided_alpha"] < p["one_sided_alpha"]
    with pytest.raises(ValueError, match="already_sealed"):
        hold(host, decision_id="late")
    assert r.list_protocols(account_id=p["account_id"])[0]["status"] == "abandoned"


def test_active_plan_cannot_be_abandoned_to_select_a_new_winner(host):
    make_plan(host)
    with pytest.raises(ValueError, match="flat_account"):
        host["registry"].abandon_protocol(protocol_id=host["protocol"]["protocol_id"], account_id=host["protocol"]["account_id"], reason="hide loss")


@pytest.mark.parametrize("registered", [True, False])
def test_prestart_plans_cannot_escape_terminal_experiment_checks(host, registered):
    plan = make_plan(host)
    assert plan["created_at"] < host["protocol"]["starts_at"]
    if registered:
        hold(host, decision_id="prestart-propose", action="propose", symbol="2330.TW", plan_id=plan["plan_id"])
    result = evaluate(host)
    expected = "unresolved_plan_or_open_position" if registered else "unregistered_plan_in_experiment_account"
    assert expected in result["reasons"]


@pytest.mark.parametrize("change", [{"minimum_closed_trades": 2}, {"minimum_observation_days": 5},
    {"benchmark": {"kind": "choose_winner_later"}}, {"costs": {**COSTS, "market_impact_bps": 0}}])
def test_protocol_cannot_shorten_samples_change_benchmark_or_remove_cost_floor(host, change):
    with pytest.raises(ValueError):
        host["registry"].register_protocol(**{**host["spec"], **change})


def test_unknown_evidence_and_unbound_outcomes_are_not_trusted(host):
    with pytest.raises(ValueError, match="owned_retained"):
        hold(host, evidence_ids=["AE-" + "d"*64])
    with pytest.raises(ValueError, match="preregistered_owned"):
        host["registry"].seal_outcome(protocol_id=host["protocol"]["protocol_id"], account_id=host["protocol"]["account_id"], plan_id="unregistered")


def test_plan_cannot_be_attached_after_broker_submission(host):
    r, c, p = host["registry"], host["campaign"], host["protocol"]
    host["clock"]["now"] = host["slots"][0].replace(hour=2, minute=0)
    plan = make_plan(host)
    c.configure(enabled=True)
    assert not asyncio.run(c.manage(now=host["clock"]["now"]))["errors"]
    assert c.plans.get(plan["plan_id"])["state"]["entry_order_id"]
    with pytest.raises(ValueError, match="after_submission"):
        hold(host, decision_id="late", action="propose", symbol="2330.TW", plan_id=plan["plan_id"])
    host["clock"]["now"] += timedelta(seconds=5)
    host["clock"]["price"] = 99
    assert not asyncio.run(c.manage(now=host["clock"]["now"]))["errors"]
    assert c.plans.get(plan["plan_id"])["state"]["remaining_quantity"] == 10
    result = evaluate(host)
    assert "unregistered_plan_in_experiment_account" in result["reasons"]
    assert "account_fills_not_fully_attributed_to_registered_outcomes" in result["reasons"]


def test_daily_nav_without_matching_retained_mark_is_observed_but_not_qualified(host):
    p, r = host["protocol"], host["registry"]
    host["clock"]["now"] = host["slots"][0]
    value = r.record_daily_observation(protocol_id=p["protocol_id"], account_id=p["account_id"],
                                       scheduled_at=host["slots"][0], evidence_ids=[host["history"]])
    assert value["valuation_verified"] is False
    assert "daily_account_valuation_not_source_bound" in evaluate(host)["reasons"]


def test_nested_daily_quote_fixture_cannot_lose_its_non_market_marker(host):
    evidence_id = host["campaign"]._retain("forward_daily_mark", {
        "source_kind": "exchange_official", "source_provenance_verified": True,
        "quotes": [{"symbol": "2330.TW", "fixture_only_not_market_or_ev_proof": True}]})
    recorded = hold(host, evidence_ids=[evidence_id])
    assert recorded["evidence"][0]["fixture"] is True


def test_deviations_and_forged_qualification_cannot_be_promoted(host):
    r, p = host["registry"], host["protocol"]
    r.record_deviation(protocol_id=p["protocol_id"], account_id=p["account_id"], reason="Different model used")
    result = evaluate(host)
    assert "protocol_deviation_requires_new_prospective_trial" in result["reasons"]
    forged = {**result, "passed": True, "positive_ev_qualified": True, "reasons": []}
    forged["receipt_sha256"] = content_hash({k: v for k, v in forged.items() if k != "receipt_sha256"})
    assert not r.verify_qualification(forged, account_id=p["account_id"], policy_version=p["policy_version"])
    assert not r.verify_qualification(result, account_id="other", policy_version=p["policy_version"])
    with pytest.raises(ValueError, match="already_sealed"):
        hold(host, decision_id="after-evaluation")


@pytest.mark.parametrize("change", ["same", "model", "unknown_policy"])
def test_explicit_close_has_separate_owned_policy_record_and_drift_cannot_qualify(host, change):
    r, p = host["registry"], host["protocol"]
    plan = make_plan(host)
    hold(host, decision_id="propose", action="propose", symbol="2330.TW", plan_id=plan["plan_id"])
    arguments = {"protocol_id": p["protocol_id"], "account_id": p["account_id"], "decision_id": "close",
        "plan_id": plan["plan_id"], "policy_version": "continuity_unverified" if change == "unknown_policy" else p["policy_version"],
        "model_receipt": {**MODEL, "reasoning_effort": "ultra"} if change == "model" else MODEL,
        "evidence_ids": [host["history"]], "rationale": "Explicit model exit decision fixture"}
    closed = r.record_exit_decision(**arguments)
    assert closed["action"] == "close" and closed["continuity_verified"] is (change == "same")
    assert r.record_exit_decision(**arguments) == closed
    records = r.records(protocol_id=p["protocol_id"], account_id=p["account_id"])
    assert len(records["exit_decision"]) == len(records["decision"]) == 1
    assert host["campaign"].plans.get(plan["plan_id"])["status"] == plan["status"]
    result = evaluate(host)
    assert result["explicit_close_decision_count"] == 1
    assert ("exit_decision_policy_or_model_continuity_not_verified" in result["reasons"]) is (change != "same")


@pytest.mark.parametrize("host", ["board"], indirect=True)
def test_board_trade_receipts_restore_original_quote_for_registered_cost_validation(host):
    r, campaign, p, clock = host["registry"], host["campaign"], host["protocol"], host["clock"]
    clock["now"] = host["slots"][0].replace(hour=2, minute=0)
    plan = make_plan(host, quantity=1000)
    hold(host, decision_id="board-propose", action="propose", symbol="2330.TW", plan_id=plan["plan_id"])
    campaign.configure(enabled=True)
    for price in (100, 99, 110, 111):
        clock["price"] = price
        managed = asyncio.run(campaign.manage(now=clock["now"]))
        assert managed["errors"] == [], managed
        clock["now"] += timedelta(seconds=5)
    final = campaign.plans.get(plan["plan_id"])
    assert final["status"] == "closed", final["state"]
    sealed = r.seal_outcome(protocol_id=p["protocol_id"], account_id=p["account_id"], plan_id=plan["plan_id"])
    assert len(sealed["outcome"]["fills"]) == 2
    assert sealed["cost_scenario_verified"] is True
    with host["store"]._connect() as conn:
        receipts = [json.loads(row[0]) for row in conn.execute("select receipt_json from paper_board_trade_allocations where quantity>0")]
    assert len(receipts) == 2 and all(receipt["execution_evidence_eligible"] is False for receipt in receipts)
    # The remaining OMS reference alone shows only its 5bps. The registry must
    # verify the owned allocation to recover the actual 20+5bps scenario price.
    assert all(abs(fill["fill_price"]/fill["reference_price"]-1)*10000 < 6 for fill in sealed["outcome"]["fills"])


def test_positive_daily_control_is_statistically_reachable_but_losing_or_zero_is_not():
    args = {"alpha": .025, "block_length": 5, "resamples": 1000, "seed": 3}
    assert _bootstrap([.1, .05, .2, 0]*30, **args)["lower_confidence_bound_pct"] > 0
    assert _bootstrap([-.1, .05, -.2, 0]*30, **args)["lower_confidence_bound_pct"] < 0
    assert _bootstrap([0]*120, **args)["lower_confidence_bound_pct"] == 0


def test_real_paper_outcomes_join_frozen_policy_and_fixture_gains_never_become_native_ev(host):
    r, campaign, p, clock = host["registry"], host["campaign"], host["protocol"], host["clock"]
    hold(host, decision_id="initial-hold")
    hold(host, decision_id="initial-reject", action="reject", rationale="Insufficient entry data")
    campaign.configure(enabled=True)

    async def trade(index, slot):
        clock["now"] = slot.replace(hour=2, minute=0)
        plan = make_plan(host, key=f"plan-{index}")
        decision = hold(host, decision_id=f"decision-{index}", action="propose", symbol="2330.TW", plan_id=plan["plan_id"])
        assert decision["policy_version"] == p["policy_version"]
        for price in (100, 99, 110, 111):
            clock["price"] = price
            result = await campaign.manage(now=clock["now"])
            assert result["errors"] == [], result
            clock["now"] += timedelta(seconds=5)
        final = campaign.plans.get(plan["plan_id"])
        assert final["status"] == "closed", final["state"]
        sealed = r.seal_outcome(protocol_id=p["protocol_id"], account_id=p["account_id"], plan_id=plan["plan_id"])
        assert sealed["cost_scenario_verified"]
        assert sealed["outcome"]["net_pnl"] > 0
        assert r.seal_outcome(protocol_id=p["protocol_id"], account_id=p["account_id"], plan_id=plan["plan_id"]) == sealed

    for index, slot in enumerate(host["slots"]):
        if index % 4 == 0:
            asyncio.run(trade(index, slot))
        observed = observe(host, slot)
        assert observed["valuation_verified"]
    result = evaluate(host)
    assert result["observation_count"] == 120 and result["closed_trade_count"] == 30
    assert result["daily_net_expectancy"]["lower_confidence_bound_pct"] > 0
    assert result["decision_counts"] == {"hold": 1, "reject": 1, "propose": 30}
    assert result["reasons"] == ["fixture_observations_not_native_market_evidence"]
    assert result["positive_ev_qualified"] is False and result["live_execution_eligible"] is False
    assert not r.verify_qualification(result, account_id=p["account_id"], policy_version=p["policy_version"])
    restarted = AgentForwardValidation(host["store"], clock=lambda: clock["now"])
    assert restarted.evaluate(protocol_id=p["protocol_id"], account_id=p["account_id"]) == result
    assert len(restarted.records(protocol_id=p["protocol_id"], account_id=p["account_id"], kind="outcome")) == 30
    # Real fee/tax accounting leaves fractional cents internally. A subsequent
    # flat experiment accepts the public cent-rounded account snapshot.
    shift = timedelta(days=200)
    replacement = restarted.register_protocol(**{**host["spec"],
        "starts_at": host["spec"]["starts_at"]+shift, "ends_at": host["spec"]["ends_at"]+shift,
        "evaluate_at": host["spec"]["evaluate_at"]+shift,
        "observation_times": [(slot+shift).isoformat() for slot in host["slots"]]})
    assert replacement["family_sequence"] == 2
    assert replacement["initial_equity"] > p["initial_equity"]
