"""Offline contracts only: no price, fill, or EV observations are being claimed."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from open_stock_ai.agent_runtime.autonomy_contract import bind_campaign_authorization, campaign_execution_authorized
from open_stock_ai.agent_runtime.completion_contract import objective_completion_contract
from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.data.execution_quote import quote_envelope
from open_stock_ai.execution.paper_decision_context import (
    build_paper_decision_context, paper_decision_policy_metadata, planning_cash_context,
)
from open_stock_ai.risk.risk_engine import RiskEngine
from stock_ai.autonomous_model_review import AutonomousModelReview
from test_agent_plan_proposal import NOW, fixture, resign_cycle


LIMITS = {"max_order_notional_pct": 5, "max_total_exposure_pct": 20}


def inputs():
    proposal, host = fixture()
    host["account_summary"].update(today_pnl=0, peak_equity=100000, open_order_reservations=[])
    market = {"symbol": proposal["symbol"], "price": 100, "source_envelope": quote_envelope(
        provider_id="twse_mis", connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
        quote_kind="last_trade", exchange_timestamp=NOW.isoformat(), received_at=NOW.isoformat(),
        max_age_seconds=90, authorized=True, realtime=True, delayed=False, official_close=False, trading_state="trading")}
    preview = {"can_submit": True, "ticket": {"symbol": proposal["symbol"], "side": "buy", "requested_quantity": 10, "limit_price": 100},
               "market": deepcopy(market), "account": {"account_id": host["account_summary"]["account_id"]},
               "estimated_costs": {"estimated_total": 1020}, "cost_evidence": {"execution_evidence_eligible": False}}
    return {**host, "proposal": proposal, "account_id": host["account_summary"]["account_id"], "mode": "paper", "authorized": True,
            "experiment_limits": dict(LIMITS), "risk": RiskEngine(), "campaign_enabled": True,
            "market": market, "broker_preview": preview}


def test_real_builder_and_order_policy_admit_paper_separately_from_ev():
    kwargs = inputs()
    before = deepcopy({k:v for k,v in kwargs.items() if k != "risk"})
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["status"] == "ready_for_host_creation"
    submission = result["submission"]
    assert submission["status"] == "risk_admitted_requires_executor_recheck"
    assert submission["risk_admission"]["approved"] is True
    assert submission["risk_admission"]["positive_ev_qualified"] is False
    assert result["positive_ev"]["status"] == "not_established"
    assert result["positive_ev"]["required_for_bounded_experiment"] is False
    assert {k:v for k,v in kwargs.items() if k != "risk"} == before


def test_closed_quote_and_other_framework_blockers_do_not_prohibit_future_draft():
    kwargs = inputs()
    cycle, proposal = kwargs["cycle"], kwargs["proposal"]
    cycle["diagnostics"] = {"qlib": {"approved": False}, "finrl": {"approved": False}, "rule_score": {"approved": False}}
    resign_cycle(proposal, kwargs)
    proposal.update(entry_condition="price_at_or_above", trigger_price=105,
                    not_before=(NOW + timedelta(days=1)).isoformat())
    kwargs["market"]["source_envelope"] = quote_envelope(
        provider_id="twse_openapi", connector_id="stock_ai.taiwan_official.official_summary_payload",
        quote_kind="official_close", exchange_timestamp=NOW.isoformat(), received_at=NOW.isoformat(),
        max_age_seconds=86400, authorized=False, realtime=False, delayed=True, official_close=True, trading_state="closed")
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["status"] == "ready_for_host_creation"
    assert result["submission"]["status"] == "waiting_for_execution_conditions"
    assert {"quote_ineligible", "waiting_time"} <= set(result["submission"]["blockers"])
    assert not any("qlib" in blocker or "finrl" in blocker for blocker in result["plan_creation"]["blockers"])


@pytest.mark.parametrize("field,value,blocker", [
    ("authorized", False, "host_paper_campaign_authorization_required"),
    ("mode", "live", "paper_broker_required"),
    ("account_id", "other-account", "account_scope_mismatch"),
])
def test_other_research_pass_cannot_override_host_mandate_or_account(field, value, blocker):
    kwargs = inputs(); kwargs[field] = value
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["status"] == "blocked"
    assert blocker in result["plan_creation"]["blockers"]


@pytest.mark.parametrize("change,blocker", [("untrusted_history", "history_identity_required"),
                                           ("missing_cost", "commission_bps"), ("stale_cycle", "requires_refresh")])
def test_draft_uses_existing_source_cost_and_cycle_checks(change, blocker):
    kwargs = inputs()
    if change == "untrusted_history":
        record = kwargs["retained_evidence"][kwargs["cycle"]["results"][0]["history_id"]]
        # Corruption is caught even before a forged provenance flag is trusted.
        record["payload"]["data_evidence"]["source_provenance_verified"] = False
        blocker = "evidence_mismatch"
    elif change == "missing_cost":
        kwargs["cycle"]["cost_assumptions"].pop("commission_bps")
        resign_cycle(kwargs["proposal"], kwargs)
    else:
        kwargs["now"] = NOW + timedelta(days=2)
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["status"] == "blocked"
    assert blocker in result["plan_creation"]["blockers"][0]


def test_five_percent_is_capital_budget_and_stop_risk_is_the_central_loss_limit():
    kwargs = inputs(); kwargs["risk"].max_daily_loss_pct = .01
    result = build_paper_decision_context(**kwargs)
    assert result["planning_cash"]["planning_cash"] == 5000
    assert result["planning_cash"]["max_order_notional_pct"] == 5
    assert result["central_limits"]["max_daily_loss_pct"] == .01
    assert result["plan_creation"]["status"] == "ready_for_host_creation"
    assert "stop_loss_exposure" in result["submission"]["blockers"]
    assert "stop_loss_budget_limit_source" in result["policy"]


def test_draft_admission_counts_explicit_exit_floor_with_unchanged_stop():
    kwargs = inputs()
    kwargs["risk"].max_daily_loss_pct = .09
    assert build_paper_decision_context(**kwargs)["submission"]["risk_admission"]["approved"] is True
    kwargs["proposal"]["exit_order_policy"] = {
        "wait_seconds": 60, "max_replacements": 2, "minimum_limit_price": 90,
    }
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["draft"]["stop_loss"] == 95
    assert result["submission"]["risk_admission"]["metrics"]["risk_exit_price"] == 90
    assert "stop_loss_exposure" in result["submission"]["blockers"]


def test_pending_plan_and_broker_reservations_reduce_actual_planning_cash():
    kwargs = inputs(); account = kwargs["account_summary"]
    account["available_cash"] = 7000
    account["open_order_reservations"] = [{"order_id": "ORDER-1", "side": "buy", "symbol": "2303.TW",
        "remaining_quantity": 10, "reservation_price": 100, "estimated_remaining_cost": 20}]
    plans = [{"account_id": kwargs["account_id"], "definition": {"cash_budget": 2000}, "state": {"status": "waiting"}},
             {"account_id": kwargs["account_id"], "definition": {"cash_budget": 5000}, "state": {"status": "entry_submitted", "entry_order_id": "ORDER-1"}},
             {"account_id": kwargs["account_id"], "definition": {"cash_budget": 5000}, "state": {"status": "closed"}}]
    result = planning_cash_context(account_summary=account, account_id=kwargs["account_id"], experiment_limits=LIMITS, active_plans=plans)
    assert result["planning_cash"] == 3980
    assert result["pending_plan_cash"] == 2000 and result["open_buy_reserved_cash"] == 1020
    plans[0]["definition"]["cash_budget"] = 7000
    result = planning_cash_context(account_summary=account, account_id=kwargs["account_id"], experiment_limits=LIMITS, active_plans=plans)
    assert result["planning_cash"] == 0 and "planning_cash_exhausted" in result["blockers"]


@pytest.mark.parametrize("change,blocker", [("cash", "invalid_account_cash_or_equity"), ("reservation", "reservations_unverified"), ("limits", "explicit_experiment_limits_required")])
def test_missing_budget_inputs_fail_closed_without_nonfinite_json(change, blocker):
    kwargs = inputs()
    if change == "cash": kwargs["account_summary"]["available_cash"] = float("nan")
    elif change == "reservation": kwargs["account_summary"]["open_order_reservations"] = [{"side": "buy"}]
    else: kwargs["experiment_limits"] = {}
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["status"] == "blocked"
    assert any(blocker in item for item in result["plan_creation"]["blockers"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("change,blocker", [("paused", "campaign_entry_paused"), ("uncertain", "account_reconciliation_required"),
                                           ("preview", "broker_preview_or_frozen_budget"), ("mismatched_preview", "broker_preview_not_bound"),
                                           ("loss_state", "account_loss_state")])
def test_submission_keeps_real_execution_guards(change, blocker):
    kwargs = inputs()
    if change == "paused": kwargs["campaign_enabled"] = False
    elif change == "uncertain": kwargs["reconciliation_pending"] = True
    elif change == "preview": kwargs["broker_preview"]["can_submit"] = False
    elif change == "mismatched_preview": kwargs["broker_preview"]["ticket"]["requested_quantity"] = 20
    else: kwargs["account_summary"].pop("today_pnl")
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["status"] == "ready_for_host_creation"
    assert any(blocker in item for item in result["submission"]["blockers"])


def test_forward_reference_is_preserved_without_becoming_qualification():
    kwargs = inputs(); kwargs["forward_validation"] = {"protocol_id": "FV-1", "policy_version": "version-1"}
    result = build_paper_decision_context(**kwargs)
    assert result["positive_ev"]["forward_validation"] == kwargs["forward_validation"]
    assert result["positive_ev"]["status"] == "not_established"
    assert paper_decision_policy_metadata() == paper_decision_policy_metadata()
    assert "run_id" not in json.dumps(paper_decision_policy_metadata())


@pytest.mark.parametrize("mode", ["verified", "unverified_flags", "different_protocol", "rejected_by_registry"])
def test_forward_qualification_requires_the_exact_host_registry_verifier(mode):
    kwargs = inputs()
    kwargs["forward_validation"] = {"protocol_id": "FV-1", "policy_version": "version-1"}
    kwargs["forward_qualification"] = {"protocol_id": "FV-1", "passed": True, "positive_ev_qualified": True,
        "receipt_sha256": "fixture-receipt", "qualification_scope": "registered_paper_scenario_only",
        "limitations": ["cash_benchmark_only_no_claim_of_market_alpha"]}
    calls = []
    def verify(receipt, *, account_id, policy_version):
        calls.append((account_id, policy_version))
        return mode == "verified"
    if mode != "unverified_flags": kwargs["qualification_verifier"] = verify
    if mode == "different_protocol": kwargs["forward_qualification"]["protocol_id"] = "FV-other"
    result = build_paper_decision_context(**kwargs)
    assert result["positive_ev"]["status"] == ("qualified_for_registered_paper_scope" if mode == "verified" else "not_established")
    assert result["submission"]["risk_admission"]["approved"] is True
    assert calls == ([(kwargs["account_id"], "version-1")] if mode in {"verified", "rejected_by_registry"} else [])
    if mode == "verified":
        assert result["positive_ev"]["live_execution_eligible"] is False
        assert result["positive_ev"]["limitations"] == ["cash_benchmark_only_no_claim_of_market_alpha"]


def test_generated_review_objective_keeps_campaign_authority_and_separate_decision_stages():
    kwargs = inputs()
    objective = AutonomousModelReview._objective(kwargs["cycle"])
    assert objective_completion_contract(objective, "market_decision")["autonomous_pipeline_requested"] is True
    context = AgentRunContext(run_id="AR-test", session_id="AS-test", symbols=(), autonomy="paper_execute", allow_paper_orders=True)
    bind_campaign_authorization(context, objective=objective, task_kind="market_decision")
    assert campaign_execution_authorized(context)
    for phrase in ("plan_creation", "submission", "positive_ev", "例如 5% 是資金配置上限", "何時再次評估", "委託與成交追蹤", "績效更新"):
        assert phrase in objective


@pytest.mark.parametrize("proxy", [False, True])
def test_quote_contract_matches_the_host_configured_paper_backend(proxy):
    kwargs = inputs()
    kwargs["market"]["source_envelope"] = quote_envelope(
        provider_id="twse_mis", connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
        quote_kind="last_trade", exchange_timestamp=NOW.isoformat(), received_at=NOW.isoformat(),
        max_age_seconds=90, authorized=False, realtime=True, delayed=False, official_close=False, trading_state="trading")
    kwargs["broker_preview"]["market"] = deepcopy(kwargs["market"])
    kwargs["paper_execution_model"] = "bounded_board_trade_odd_lot_proxy" if proxy else None
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["status"] == "ready_for_host_creation"
    assert result["submission"]["quote_gate"]["execution_eligible"] is proxy
    if proxy:
        assert result["submission"]["quote_gate"]["execution_evidence_eligible"] is False
        assert result["submission"]["status"] == "risk_admitted_requires_executor_recheck"
    else:
        assert "quote_ineligible" in result["submission"]["blockers"]
    assert kwargs["market"]["source_envelope"]["authorized"] is False


def test_missing_proposal_or_evidence_is_unevaluated_instead_of_implicitly_allowed():
    kwargs = inputs(); kwargs["proposal"] = None
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["status"] == "requires_proposal"
    assert result["submission"]["status"] == "requires_plan"
    kwargs = inputs(); kwargs["retained_evidence"] = None
    result = build_paper_decision_context(**kwargs)
    assert result["plan_creation"]["status"] == "requires_inputs"
    assert result["plan_creation"]["missing_inputs"] == ["retained_evidence"]
