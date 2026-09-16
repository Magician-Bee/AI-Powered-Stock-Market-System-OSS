"""Read-only Host contract separating paper drafts, dispatch, and EV evidence.

Callers supply retained evidence and authoritative account/broker inputs. This
projection never grants authorization, creates a plan, reserves cash, or trades.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Callable

from open_stock_ai.data.execution_quote import plan_quote_eligibility
from open_stock_ai.risk.order_policy import _number, _reservations, order_risk_evidence_receipt
from open_stock_ai.strategy.execution_policy import policy_metadata
from .agent_plan_proposal import AGENT_PLAN_PROPOSAL_SCHEMA, build_agent_plan_proposal
from .trading_plan import utc_time


def paper_decision_policy_metadata() -> dict[str, Any]:
    """Stable semantics for a forward protocol; no run/cycle/account observations."""
    return {
        "schema_version": "open_stock_ai.paper_decision_policy.v1",
        "eligibility": "bounded_experiment",
        "execution_policy": policy_metadata(),
        "plan_builder": "build_agent_plan_proposal",
        "submission_risk": "RiskEngine.evaluate_order_intent",
        "required_evidence": ["price_history", "cost_model"],
        "plan_budget_basis": "order_cash_allocation_pct_of_decision_equity_including_cost_reserve",
        "stop_loss_budget_basis": "stop_or_lower_explicit_exit_floor_distance_times_quantity_plus_entry_cost_over_equity",
        "stop_loss_budget_limit_source": "RiskEngine.max_daily_loss_pct",
        "closed_quote_effect": "wait_for_submission; does_not_disqualify_a_retained_history_draft",
        "other_framework_flags": "diagnostic_unless_declared_as_this_strategy_evidence_dependencies",
        "qualification_scope": "matching_verified_strategy_or_forward_policy_receipt; separate_from_paper_admission",
        "fill_authority": "broker_order_and_fill_receipts",
        "live_execution_eligible": False,
    }


def planning_cash_context(*, account_summary: dict[str, Any], account_id: str,
                          experiment_limits: dict[str, Any], active_plans=()) -> dict[str, Any]:
    """Expose the actual uncommitted plan budget, using the order reservation parser."""
    blockers = []
    equity = _number(account_summary.get("total_equity"))
    cash = _number(account_summary.get("available_cash", account_summary.get("cash_balance")))
    positions = account_summary.get("positions")
    limits = {key: _number(experiment_limits.get(key)) for key in ("max_order_notional_pct", "max_total_exposure_pct")}
    if not account_id or account_summary.get("account_id") != account_id:
        blockers.append("account_scope_mismatch")
    if equity is None or equity <= 0 or cash is None or cash < 0:
        blockers.append("invalid_account_cash_or_equity")
    if not isinstance(positions, list) or any(not isinstance(p, dict) or not p.get("symbol")
            or _number(p.get("quantity")) is None or _number(p.get("market_value")) is None for p in positions):
        blockers.append("invalid_account_positions")
    if any(value is None or not 0 < value <= 100 for value in limits.values()):
        blockers.append("explicit_experiment_limits_required")
    reservations = _reservations(account_summary, {})
    if not reservations["valid"]:
        blockers.append("account_open_order_reservations_unverified")
    pending = 0.0
    for plan in active_plans:
        if plan.get("account_id") != account_id:
            blockers.append("active_plan_account_scope_mismatch")
            continue
        state = plan.get("state") or {}
        if state.get("status", plan.get("status")) in {"closed", "cancelled", "expired", "invalidated", "rejected"}:
            continue
        if not state.get("entry_order_id"):
            budget = _number((plan.get("definition") or {}).get("cash_budget"))
            if budget is None or budget < 0:
                blockers.append("invalid_pending_plan_budget")
            else:
                pending += budget
    exposure = sum(max(0.0, float(p["market_value"])) for p in positions) if "invalid_account_positions" not in blockers else None
    if not isfinite(pending) or exposure is not None and not isfinite(exposure):
        blockers.append("nonfinite_aggregate_exposure")
        pending, exposure = None, None
    capacity = None if blockers else max(0.0, min(
        cash - reservations["buy_cash"] - pending,
        equity * limits["max_total_exposure_pct"] / 100 - exposure - reservations["buy_cash"] - pending,
        equity * limits["max_order_notional_pct"] / 100,
    ))
    if capacity == 0:
        blockers.append("planning_cash_exhausted")
    return {"account_id": account_id, "planning_cash": capacity, "blockers": list(dict.fromkeys(blockers)),
            "available_cash": cash, "equity": equity, "position_exposure": exposure,
            "open_buy_reserved_cash": reservations["buy_cash"] if isfinite(reservations["buy_cash"]) else None, "pending_plan_cash": pending,
            **limits, "budget_basis": paper_decision_policy_metadata()["plan_budget_basis"]}


def build_paper_decision_context(*, risk, account_summary: dict[str, Any], account_id: str,
        mode: str, authorized: bool, experiment_limits: dict[str, Any], active_plans=(),
        cycle: dict[str, Any] | None = None, retained_evidence: dict[str, Any] | None = None,
        proposal: dict[str, Any] | None = None, host_context: dict[str, Any] | None = None,
        market: dict[str, Any] | None = None, broker_preview: dict[str, Any] | None = None,
        campaign_enabled: bool = False, reconciliation_pending: bool = False,
        paper_execution_model: str | None = None,
        forward_validation: dict[str, Any] | None = None, forward_qualification: dict[str, Any] | None = None,
        qualification_verifier: Callable[..., bool] | None = None, now: datetime | None = None,
        product_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate a proposed draft with the real builder and a current dispatch separately.

    Missing proposal/preview inputs stay unevaluated. A valid draft can coexist
    with an ineligible current quote; the executor will recheck its future trigger.
    ``authorized`` must be computed by Host from the actual run mandate and pause
    epoch, never accepted from model arguments. Forward references are not proof.
    """
    instant = utc_time(now or datetime.now(timezone.utc))
    active_plans = tuple(active_plans)
    cash = planning_cash_context(account_summary=account_summary, account_id=account_id,
                                 experiment_limits=experiment_limits, active_plans=active_plans)
    blockers = list(cash["blockers"])
    if authorized is not True:
        blockers.append("host_paper_campaign_authorization_required")
    if mode != "paper":
        blockers.append("paper_broker_required")
    if (_number(experiment_limits.get("max_order_notional_pct")) or float("inf")) > risk.max_position_size_pct:
        blockers.append("experiment_order_limit_exceeds_central_policy")
    if (_number(experiment_limits.get("max_total_exposure_pct")) or float("inf")) > risk.max_total_paper_exposure_pct:
        blockers.append("experiment_total_limit_exceeds_central_policy")
    creation = {"status": "blocked" if blockers else "requires_proposal", "blockers": blockers,
                "required_inputs": ["host_authorization_and_account", "fresh_retained_cycle", "verified_chronological_history",
                                    "declared_cost_assumptions", "uncommitted_planning_cash", "schema_valid_proposal",
                                    "latest_verified_ordinary_stock_identity"],
                "proposal_required_fields": list(AGENT_PLAN_PROPOSAL_SCHEMA["required"]),
                "sizing_requirement": "exactly_one_of_quantity_shares_or_position_size_pct"}
    submission = {"status": "requires_plan", "blockers": [],
                  "required_inputs": ["persisted_plan", "campaign_enabled", "fresh_executable_quote", "time_and_price_trigger",
                                      "reconciled_account", "bound_broker_preview", "shared_order_risk_approval"]}
    result = {"schema_version": "open_stock_ai.paper_decision_context.v1", "account_id": account_id,
              "observed_at": instant.isoformat(), "mode": mode, "policy": paper_decision_policy_metadata(),
              "planning_cash": cash, "plan_creation": creation, "submission": submission,
              "central_limits": {key: getattr(risk, key) for key in ("max_position_size_pct", "max_daily_loss_pct",
                    "max_total_drawdown_pct", "max_symbol_exposure_pct", "max_total_paper_exposure_pct", "max_industry_exposure_pct")},
              "positive_ev": {"status": "not_established", "required_for_bounded_experiment": False,
                    "required_inputs": ["matching_frozen_strategy_or_policy_version", "verified_qualification_receipt",
                                        "declared_costs_and_evaluation_protocol"], "forward_validation": deepcopy(forward_validation)},
              "execution_boundary": "read_only_decision_context; only_plan_and_broker_receipts_confirm_actions"}
    reference = forward_validation or {}
    if forward_qualification is not None and qualification_verifier is not None:
        # A model/client JSON 'passed' flag cannot stand in for the exact
        # account/policy evaluation retained by the Host's forward registry.
        try:
            qualified = bool(reference.get("policy_version") and reference.get("protocol_id")
                and forward_qualification.get("protocol_id") == reference["protocol_id"]
                and qualification_verifier(forward_qualification, account_id=account_id,
                                           policy_version=reference["policy_version"]) is True)
        except (TypeError, ValueError, KeyError):
            qualified = False
        if qualified:
            result["positive_ev"].update(status="qualified_for_registered_paper_scope",
                qualification_receipt_sha256=forward_qualification.get("receipt_sha256"),
                qualification_scope=forward_qualification.get("qualification_scope"), live_execution_eligible=False,
                limitations=deepcopy(forward_qualification.get("limitations") or []))
    if proposal is None or blockers:
        return result
    missing = [name for name, value in (("cycle", cycle), ("retained_evidence", retained_evidence), ("host_context", host_context)) if value is None]
    if missing:
        creation.update(status="requires_inputs", missing_inputs=missing)
        return result
    symbol = str(proposal.get("symbol") or "").upper()
    if any(p["symbol"] == symbol and float(p["quantity"]) != 0 for p in account_summary["positions"]):
        creation.update(status="blocked", blockers=["existing_position_requires_adoption"])
        return result
    try:
        plan = build_agent_plan_proposal(proposal, cycle=cycle, retained_evidence=retained_evidence,
            host_context=host_context, account_summary=account_summary, planning_cash=cash["planning_cash"], now=instant,
            product_snapshot=product_snapshot)
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        creation.update(status="blocked", blockers=[str(exc)])
        return result
    creation.update(status="ready_for_host_creation", blockers=[], product_admission=plan.metadata["product_admission"],
        draft={key: plan.to_dict()[key] for key in (
        "symbol", "quantity_shares", "cash_budget", "reference_price", "stop_loss", "target_price", "entry_condition",
        "trigger_price", "not_before", "expires_at", "exit_not_after", "strategy_id", "strategy_version", "evidence_ids")})
    if plan.exit_order_policy:
        creation["draft"]["exit_order_policy"] = plan.to_dict()["exit_order_policy"]
    submission["status"] = "requires_execution_inputs"
    waits = submission["blockers"]
    if campaign_enabled is not True:
        waits.append("campaign_entry_paused")
    if reconciliation_pending:
        waits.append("account_reconciliation_required")
    if not market:
        waits.append("current_execution_quote_required")
        return result
    if market.get("symbol") != plan.symbol:
        waits.append("plan_quote_symbol_mismatch")
        return result
    try:
        quote = plan_quote_eligibility(dict(market.get("source_envelope") or {}), mode=mode,
            paper_execution_model=paper_execution_model, quantity_shares=plan.quantity_shares, now=instant)
    except (TypeError, ValueError, OverflowError):
        quote = {"execution_eligible": False, "blockers": ["invalid_source_envelope"]}
    submission["quote_gate"] = quote
    price = _number(market.get("price"))
    if quote.get("execution_eligible") is not True or price is None or price <= 0:
        waits.append("quote_ineligible")
    trigger = plan.entry_status(price=price if price is not None else float("nan"), now=instant)
    submission["entry_status"] = trigger
    if trigger != "triggered":
        waits.append(trigger)
    if not broker_preview:
        waits.append("current_broker_preview_required")
    total = _number((broker_preview or {}).get("estimated_costs", {}).get("estimated_total"))
    if broker_preview and ((broker_preview.get("can_submit") is not True) or total is None or total > plan.cash_budget + .01):
        waits.append("broker_preview_or_frozen_budget")
    if broker_preview:
        ticket, preview_market = broker_preview.get("ticket") or {}, broker_preview.get("market") or {}
        if (ticket.get("symbol") != plan.symbol or ticket.get("side") != "buy"
            or _number(ticket.get("requested_quantity")) != plan.quantity_shares
            or _number(ticket.get("limit_price")) != price
            or (broker_preview.get("account") or {}).get("account_id") != account_id
            or preview_market.get("source_envelope") != market.get("source_envelope")
            or preview_market.get("symbol") != plan.symbol or _number(preview_market.get("price")) != price):
            waits.append("broker_preview_not_bound_to_current_plan_quote")
    if waits:
        submission["status"] = "waiting_for_execution_conditions"
        return result
    intent = {"symbol": plan.symbol, "side": "buy", "quantity_shares": plan.quantity_shares,
              "reference_price": price, "stop_loss": plan.stop_loss, "account_id": account_id,
              "strategy_id": plan.strategy_id, "strategy_version_hash": plan.strategy_version,
              **({"exit_minimum_limit_price": plan.exit_order_policy.minimum_limit_price}
                 if plan.exit_order_policy else {})}
    source = retained_evidence[plan.evidence_ids[0]]["payload"]["data_evidence"]
    evidence = {"required_evidence": ["price_history", "cost_model"], "experiment_limits": experiment_limits,
        "receipts": {
            "price_history": order_risk_evidence_receipt(kind="price_history", source=str(source.get("source_id")), passed=True,
                payload={"symbol": plan.symbol, "evidence_id": plan.evidence_ids[0]}),
            "cost_model": order_risk_evidence_receipt(kind="cost_model", source="broker.preview", passed=True,
                payload={**(broker_preview.get("cost_evidence") or {}), **intent, "estimated_total_cost": abs(total - plan.quantity_shares * price)}),
        }}
    admission = risk.evaluate_order_intent(intent=intent, account_summary=account_summary, evidence=evidence,
                                          mode=mode, eligibility="bounded_experiment", now=instant)
    submission.update(status="risk_admitted_requires_executor_recheck" if admission["approved"] else "blocked_by_order_risk",
                      blockers=admission["blockers"], risk_admission=admission)
    return result
