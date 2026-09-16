"""Offline discretionary drafting contracts; fixture bars are not EV evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.execution.agent_plan_proposal import AGENT_PLAN_PROPOSAL_SCHEMA, STRATEGY_ID, build_agent_plan_proposal
from open_stock_ai.execution.trading_plan import content_hash
from product_admission_fixtures import product_feature, product_snapshot


NOW = datetime(2026, 9, 11, 7, tzinfo=timezone.utc)


def fixture():
    account = {"account_id": "proposal-offline-account", "cash_balance": 100000, "available_cash": 90000,
               "total_equity": 100000, "positions": []}
    evidence = {}

    def retain(kind, payload):
        key = "AE-" + content_hash({"account_id": account["account_id"], "kind": kind, "payload": payload})
        evidence[key] = {"kind": kind, "payload": payload}
        return key

    # No 61-bar rule requirement for a discretionary proposal; its only
    # history dependency here is an attributable completed reference close.
    rows = [{"timestamp": (NOW - timedelta(days=1)).isoformat(), "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1000}]
    history_id = retain("price_history", {"rows": rows, "data_evidence": {
        "symbol": "2330.TW", "source_provenance_verified": True,
        "source_id": "offline_contract_fixture", "data_sha256": content_hash(rows)}})
    feature = {**product_feature("2330.TW", now=NOW), "close": 100}
    bulk_id = retain("market_screen", {"features": [feature]})
    overlay_id = retain("event_research", {"symbol": "2330.TW", "items": [{"source_id": "offline_document", "text": "Fixture overlay"}]})
    cycle = {"schema_version": "open_stock_ai.autonomous_research_cycle.v1", "account_id": account["account_id"],
             "created_at": NOW.isoformat(), "bulk_evidence_id": bulk_id,
             "results": [{"symbol": "2330.TW", "feature": feature, "history_id": history_id, "last_bar": rows[-1]["timestamp"],
                          "candidates": [{"signal": {"action": "hold"}, "positive_ev_qualified": False,
                                          "research_paper_candidate_eligible": False}]}],
             "cost_assumptions": {"commission_bps": 14.25, "minimum_commission": 20, "slippage_bps": 5}}
    cycle["cycle_id"] = "AC-" + content_hash(cycle)
    context = {"run_id": "AR-fixture", "session_id": "AS-fixture", "driver_id": "codex", "parent_run_id": None,
               "provider_model_metadata": {"model": "gpt-5.6-sol", "reasoning_effort": "medium", "thread_id": "thread-fixture",
                                           "selected_model": "gpt-6-astra", "resolution_source": "sdk_thread_start"}}
    proposal = {"cycle_id": cycle["cycle_id"], "symbol": "2330.TW", "quantity_shares": 10,
                "stop_loss": 95, "target_price": 115, "rationale": "使用已保存行情與事件資料提出小額試驗。", "evidence_ids": [overlay_id]}
    return proposal, {"cycle": cycle, "retained_evidence": evidence, "host_context": context, "account_summary": account, "now": NOW,
                      "product_snapshot": product_snapshot("2330.TW", now=NOW)}


def resign_cycle(proposal, host):
    cycle = host["cycle"]
    cycle["cycle_id"] = "AC-" + content_hash({k: v for k, v in cycle.items() if k != "cycle_id"})
    proposal["cycle_id"] = cycle["cycle_id"]


def test_source_manifest_is_stable_when_package_is_imported_through_symlink(tmp_path, monkeypatch):
    from pathlib import Path
    from open_stock_ai.execution import agent_plan_proposal as builder

    real_file = Path(builder.__file__).resolve()
    expected = builder.proposal_source_manifest()
    alias = tmp_path / "package-alias"
    alias.symlink_to(real_file.parents[1], target_is_directory=True)
    monkeypatch.setattr(builder, "__file__", str(alias / "execution" / real_file.name))
    assert builder.proposal_source_manifest() == expected


def test_agent_can_propose_despite_all_fixed_rules_holding_and_unqualified():
    args, host = fixture()
    original = deepcopy((args, host))
    plan = build_agent_plan_proposal(args, **host)
    assert plan.strategy_id == STRATEGY_ID and plan.qualification_id is None
    assert plan.quantity_shares == 10 and plan.cash_budget == 1025
    assert plan.metadata["eligibility"] == "bounded_experiment"
    assert plan.metadata["positive_ev_qualified"] is False
    assert plan.metadata["submission_risk_status"] == "requires_central_risk_recheck"
    assert "research_paper_candidate_eligible" not in plan.metadata
    assert plan.metadata["agent_context"] == host["host_context"]
    assert list(plan.evidence_ids) == [host["cycle"]["results"][0]["history_id"], host["cycle"]["bulk_evidence_id"], *args["evidence_ids"]]
    assert (args, host) == original
    host["host_context"]["provider_model_metadata"]["model"] = "changed"
    assert plan.metadata["agent_context"]["provider_model_metadata"]["model"] == "gpt-5.6-sol"


def test_percentage_uses_equity_cost_budget_and_available_host_planning_cash():
    args, host = fixture()
    args.pop("quantity_shares")
    args["position_size_pct"] = 5
    plan = build_agent_plan_proposal(args, **host, planning_cash=4000)
    assert plan.quantity_shares == 39 and plan.cash_budget == 4000
    assert plan.quantity_shares * plan.reference_price + 20 <= plan.cash_budget


def test_mixed_lot_quantity_only_rounds_down_and_discloses_original_and_residual():
    args, host = fixture()
    host["account_summary"].update(total_equity=1000000, cash_balance=500000, available_cash=500000)
    args["quantity_shares"] = 1225
    plan = build_agent_plan_proposal(args, **host)
    assert plan.quantity_shares == 1000
    assert plan.metadata["sizing"]["original_quantity_shares"] == 1225
    assert plan.metadata["sizing"]["unsubmitted_quantity"] == 225
    assert plan.metadata["sizing"]["lot_type"] == "board_lot"


def test_timed_breakout_budget_is_reserved_at_trigger_and_holding_is_preserved():
    args, host = fixture()
    start = NOW + timedelta(days=3)
    args.update(entry_condition="price_at_or_above", trigger_price=105, not_before=start.isoformat(),
                expires_at=(start + timedelta(hours=3)).isoformat(), max_holding_seconds=7200,
                exit_not_after=(start + timedelta(days=2)).isoformat())
    plan = build_agent_plan_proposal(args, **host)
    assert plan.reference_price == 105 and plan.cash_budget == 1075
    assert plan.metadata["proposal_spec"]["observed_history_close"] == 100
    assert plan.entry_status(price=106, now=NOW) == "waiting_time"
    assert plan.entry_status(price=104, now=start) == "waiting_price"
    assert plan.entry_status(price=105, now=start) == "triggered"
    assert plan.time_exit_reason(entered_at=start.isoformat(), now=start + timedelta(seconds=7200)) == "holding_period_expired"


def test_pullback_freezes_lower_trigger_without_enlarging_requested_shares():
    args, host = fixture()
    args.update(entry_condition="price_at_or_below", trigger_price=98)
    plan = build_agent_plan_proposal(args, **host)
    assert plan.quantity_shares == 10 and plan.reference_price == 98 and plan.cash_budget == 1001
    assert plan.entry_status(price=100, now=NOW) == "waiting_price"
    assert plan.entry_status(price=98, now=NOW) == "triggered"


def test_explicit_exit_policy_is_frozen_into_model_decision_and_version():
    args, host = fixture()
    legacy = build_agent_plan_proposal(args, **host)
    args["exit_order_policy"] = {"wait_seconds": 60, "max_replacements": 2, "minimum_limit_price": 90}
    plan = build_agent_plan_proposal(args, **host)
    assert plan.stop_loss == 95
    assert plan.exit_order_policy.minimum_limit_price == 90
    assert plan.to_dict()["exit_order_policy"] == args["exit_order_policy"]
    assert plan.metadata["proposal_spec"]["exit_order_policy"] == args["exit_order_policy"]
    assert plan.strategy_version != legacy.strategy_version
    assert "exit_order_policy" not in legacy.to_dict()
    args["exit_order_policy"]["minimum_limit_price"] = 89
    assert plan.exit_order_policy.minimum_limit_price == 90
    assert plan.metadata["proposal_spec"]["exit_order_policy"]["minimum_limit_price"] == 90
    assert build_agent_plan_proposal(args, **host).strategy_version != plan.strategy_version


@pytest.mark.parametrize("policy", [None, [], {},
    {"wait_seconds": 60, "max_replacements": 2},
    {"wait_seconds": 60, "max_replacements": 2, "minimum_limit_price": 90, "market_order": True},
    {"wait_seconds": True, "max_replacements": 2, "minimum_limit_price": 90},
    {"wait_seconds": 60, "max_replacements": 2, "minimum_limit_price": 96},
])
def test_incomplete_or_unbounded_exit_policy_is_rejected(policy):
    args, host = fixture()
    args["exit_order_policy"] = policy
    with pytest.raises(ValueError, match="exit_order"):
        build_agent_plan_proposal(args, **host)


@pytest.mark.parametrize("condition,trigger,stop,target,gap,gap_status", [
    ("price_at_or_above", 110, 105, 120, 125, "opportunity_passed"),
    ("price_at_or_below", 90, 85, 95, 80, "thesis_invalidated"),
])
def test_future_bracket_waits_for_entry_but_rejects_gap_past_bracket(
    condition, trigger, stop, target, gap, gap_status,
):
    args, host = fixture()
    due = NOW + timedelta(days=3)
    expiry = due + timedelta(hours=2)
    args.update(entry_condition=condition, trigger_price=trigger, stop_loss=stop,
                target_price=target, not_before=due.isoformat(), expires_at=expiry.isoformat())
    plan = build_agent_plan_proposal(args, **host)
    assert plan.reference_price == trigger
    assert plan.metadata["proposal_spec"]["observed_history_close"] == 100
    assert plan.entry_status(price=trigger, now=NOW) == "waiting_time"
    assert plan.entry_status(price=100, now=due) == "waiting_price"
    assert plan.entry_status(price=trigger, now=due) == "triggered"
    assert plan.entry_status(price=gap, now=due) == gap_status
    assert plan.entry_status(price=100, now=expiry) == "expired"
    # After a fill the same bracket still protects the position immediately.
    assert plan.exit_reason(price=stop, entered_at=due.isoformat(), now=due) == "stop_loss"
    assert plan.exit_reason(price=target, entered_at=due.isoformat(), now=due) == "take_profit"


def test_same_input_is_reproducible_and_spec_or_rule_changes_change_strategy_hash(monkeypatch):
    import open_stock_ai.execution.agent_plan_proposal as module
    args, host = fixture()
    first = build_agent_plan_proposal(args, **host)
    assert first.to_dict() == build_agent_plan_proposal(args, **host).to_dict()
    args["stop_loss"] = 94
    changed = build_agent_plan_proposal(args, **host)
    assert first.strategy_version != changed.strategy_version
    assert first.metadata["proposal_spec_sha256"] != changed.metadata["proposal_spec_sha256"]
    monkeypatch.setattr(module, "proposal_source_manifest", lambda: {"strategy/execution_policy.py": "changed-source"})
    assert changed.strategy_version != build_agent_plan_proposal(args, **host).strategy_version


@pytest.mark.parametrize("key,value", [("passed", True), ("positive_ev_qualified", True), ("qualification_id", "AE-forged"),
    ("strategy_id", "tw_candle_breakout_20_60_v1"), ("strategy_version", "forged"), ("reference_price", 1),
    ("source_receipts", {"source_provenance_verified": True}), ("host_context", {"model": "forged"}),
    ("account_summary", {"cash_balance": 1e9})])
def test_model_cannot_supply_host_authority_fields(key, value):
    args, host = fixture()
    args[key] = value
    with pytest.raises(ValueError, match="fields_not_allowed"):
        build_agent_plan_proposal(args, **host)


@pytest.mark.parametrize("patch,error", [
    ({"quantity_shares": True}, "positive_integer"), ({"quantity_shares": 1.5}, "positive_integer"),
    ({"position_size_pct": 5}, "exactly_one"), ({"stop_loss": float("nan")}, "finite_json"),
    ({"stop_loss": True}, "invalid_stop_loss"), ({"target_price": True}, "invalid_target_price"),
    ({"stop_loss": 100}, "invalid_proposal_bracket"), ({"target_price": 99}, "invalid_proposal_bracket"),
    ({"entry_condition": "price_at_or_above", "trigger_price": 115}, "inside_bracket"),
    ({"entry_condition": "price_at_or_below", "trigger_price": 95}, "inside_bracket"),
    ({"trigger_price": 102}, "immediate_entry"), ({"rationale": " "}, "rationale_required"),
    ({"max_holding_seconds": True}, "invalid_holding"), ({"not_before": "2026-09-12T09:05:00"}, "timezone"),
    ({"expires_at": NOW.isoformat()}, "expiry_must_be_future"),
    ({"exit_not_after": NOW.isoformat()}, "deadline_must_follow"),
    ({"symbol": "AAPL"}, "canonical_taiwan"), ({"symbol": "2317.TW"}, "retained_symbol"),
    ({"evidence_ids": ["AE-forged"]}, "must_be_host_retained"),
])
def test_invalid_or_unexecutable_agent_choices_rejected(patch, error):
    args, host = fixture()
    args.update(patch)
    with pytest.raises(ValueError, match=error):
        build_agent_plan_proposal(args, **host)


@pytest.mark.parametrize("cash", [1019, 0])
def test_minimum_commission_cannot_be_omitted_to_squeeze_an_unaffordable_order(cash):
    args, host = fixture()
    with pytest.raises(ValueError, match="planning_cash"):
        build_agent_plan_proposal(args, **host, planning_cash=cash)


@pytest.mark.parametrize("field,value,error", [
    ("run_id", "", "run_id_required"), ("session_id", "", "session_id_required"),
    ("provider_model_metadata", {"selected_model": "preferred-is-not-actual"}, "actual_host_model"),
    ("provider_model_metadata", {"model": "codex", "nested": {"api_key": "secret"}}, "credentials_forbidden"),
    ("provider_model_metadata", {"model": "codex", "auth": [{"access_token": "secret"}]}, "credentials_forbidden"),
    ("provider_model_metadata", {"model": "codex", "note": "Bearer secret"}, "credentials_forbidden"),
    ("api_key", "secret", "fields_not_allowed"),
])
def test_context_requires_actual_model_identity_and_never_retains_credentials(field, value, error):
    args, host = fixture()
    host["host_context"][field] = value
    with pytest.raises(ValueError, match=error):
        build_agent_plan_proposal(args, **host)


def test_cross_account_and_corrupted_cycle_or_evidence_are_rejected():
    args, host = fixture()
    host["account_summary"]["account_id"] = "other-account"
    with pytest.raises(ValueError, match="account_scope"):
        build_agent_plan_proposal(args, **host)
    args, host = fixture()
    host["cycle"]["created_at"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="cycle_mismatch"):
        build_agent_plan_proposal(args, **host)
    resign_cycle(args, host)
    with pytest.raises(ValueError, match="requires_refresh"):
        build_agent_plan_proposal(args, **host)
    args, host = fixture()
    history = host["retained_evidence"][host["cycle"]["results"][0]["history_id"]]
    history["payload"]["rows"][0]["close"] = 1
    with pytest.raises(ValueError, match="evidence_mismatch"):
        build_agent_plan_proposal(args, **host)


def test_stale_cycle_requires_refresh_and_proposal_cannot_borrow_unrelated_source():
    args, host = fixture()
    host["now"] = NOW + timedelta(days=2)
    with pytest.raises(ValueError, match="requires_refresh"):
        build_agent_plan_proposal(args, **host)
    args, host = fixture()
    history_id = host["cycle"]["results"][0]["history_id"]
    record = host["retained_evidence"].pop(history_id)
    record["payload"]["data_evidence"]["symbol"] = "2317.TW"
    new_id = "AE-" + content_hash({"account_id": host["account_summary"]["account_id"], **record})
    host["retained_evidence"][new_id] = record
    host["cycle"]["results"][0]["history_id"] = new_id
    resign_cycle(args, host)
    with pytest.raises(ValueError, match="history_identity"):
        build_agent_plan_proposal(args, **host)


def test_schema_can_be_reused_by_tool_without_exposing_trusted_records():
    assert AGENT_PLAN_PROPOSAL_SCHEMA["additionalProperties"] is False
    assert set(AGENT_PLAN_PROPOSAL_SCHEMA["required"]) == {"cycle_id", "symbol", "stop_loss", "rationale"}
    assert not {"passed", "strategy_id", "qualification", "account_summary", "host_context", "retained_evidence"} & set(AGENT_PLAN_PROPOSAL_SCHEMA["properties"])


def test_host_proxy_reserve_and_minimum_commission_plus_exchange_fee_are_affordable():
    args, host = fixture()
    host["cycle"]["cost_assumptions"].update(market_impact_bps=20)
    resign_cycle(args, host)
    plan = build_agent_plan_proposal(args, **host)
    assert plan.metadata["sizing"]["slippage_and_impact_reserve_bps"] == 25
    assert plan.cash_budget == 1025  # shared equity tick makes 100.25 -> 100.5
    args.pop("quantity_shares")
    args["position_size_pct"] = 1.025
    host["cycle"]["cost_assumptions"].update(slippage_bps=0, market_impact_bps=0, exchange_fee_bps=100)
    resign_cycle(args, host)
    plan = build_agent_plan_proposal(args, **host)
    assert plan.quantity_shares == 9
    assert 9*100 + 20 + 9 <= plan.cash_budget
