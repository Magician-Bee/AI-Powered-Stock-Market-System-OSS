"""Host-bound discretionary planning; model inputs never become broker receipts."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .agent_plan_proposal import build_agent_plan_proposal
from .paper_decision_context import planning_cash_context
from .trading_plan import content_hash, utc_time


def retained_records(campaign, evidence_ids):
    records = {}
    for evidence_id in evidence_ids:
        payload = campaign._evidence(evidence_id)
        with campaign.plans.store._connect() as conn:
            row = conn.execute("select kind from autonomous_evidence where account_id=? and evidence_id=?",
                               (campaign.broker.account_id, evidence_id)).fetchone()
        records[evidence_id] = {"kind": row[0], "payload": payload}
    return records


async def propose_plan(campaign, proposal: dict[str, Any], *, host_context: dict[str, Any], now=None,
                       forward_binding=None):
    instant = utc_time(now or datetime.now(timezone.utc))
    # Retry identity uses the original choice, not a newly computed equity,
    # expiry or quote. Replaying the tool returns its original frozen plan.
    key = "agent-" + content_hash({"run_id": host_context["run_id"], "proposal": proposal})
    with campaign.plans.account_lease(campaign.broker.account_id, now=instant) as owner:
        if owner is None:
            raise RuntimeError("autonomous_planning_account_busy")
        prior = campaign.plans.get_by_idempotency(account_id=campaign.broker.account_id, idempotency_key=key)
        if prior:
            _record_forward_proposal(campaign, forward_binding, prior, host_context)
            return prior
        try:
            return await asyncio.wait_for(_propose(campaign, proposal, host_context, instant, key, forward_binding), timeout=45)
        except (ValueError, KeyError) as exc:
            monitor = getattr(campaign, "forward_monitor", None)
            if monitor and forward_binding:
                monitor.record_rejection(binding=forward_binding, proposal=proposal, reason=str(exc), model_receipt=host_context)
            raise


async def _propose(campaign, proposal, context, instant, key, forward_binding=None):
    cycle = campaign.cycle(proposal["cycle_id"])
    symbol = str(proposal["symbol"]).upper()
    research = next((row for row in cycle["results"] if row["symbol"] == symbol), None)
    if research is None:
        raise ValueError("proposal_requires_retained_symbol_history")
    account = await campaign.broker.account(now=instant)
    if any(p["symbol"] == symbol and float(p["quantity"]) != 0 for p in account["positions"]):
        raise ValueError("existing_position_requires_adoption")
    active = campaign.plans.list(account_id=campaign.broker.account_id, active_only=True)
    capacity = planning_cash_context(account_summary=account, account_id=campaign.broker.account_id,
                                    experiment_limits=campaign.experiment_limits, active_plans=active)
    if capacity["blockers"]:
        raise ValueError(";".join(capacity["blockers"]))
    records = retained_records(campaign, [research["history_id"], cycle["bulk_evidence_id"], *proposal.get("evidence_ids", [])])
    normalized = dict(proposal)
    if forward_binding and forward_binding.get("binding_eligible"):
        start = utc_time(forward_binding["starts_at"])
        deadline = utc_time(forward_binding["ends_at"]) - timedelta(minutes=70)
        normalized["not_before"] = max(start, utc_time(proposal.get("not_before") or instant)).isoformat()
        normalized["exit_not_after"] = min(deadline, utc_time(proposal.get("exit_not_after") or deadline)).isoformat()
        if "expires_at" in normalized:
            normalized["expires_at"] = min(utc_time(normalized["expires_at"]), utc_time(normalized["exit_not_after"])).isoformat()
    plan = build_agent_plan_proposal(normalized, cycle=cycle, retained_evidence=records, host_context=context,
                                     account_summary=account, planning_cash=capacity["planning_cash"], now=instant,
                                     product_snapshot=campaign.product_snapshot(symbol, now=instant))
    if forward_binding and forward_binding.get("binding_eligible"):
        plan = replace(plan, metadata={**plan.metadata,
            "forward_validation": {key: forward_binding[key] for key in ("protocol_id", "policy_version")},
            "forward_timing_policy": "entry_after_registration_window_start; terminal_exit_before_final_observation",
            "original_agent_proposal": proposal})
    record = campaign.plans.create(account_id=campaign.broker.account_id, plan=plan, idempotency_key=key, now=instant)
    _record_forward_proposal(campaign, forward_binding, record, context)
    return record


def _record_forward_proposal(campaign, binding, record, context):
    monitor = getattr(campaign, "forward_monitor", None)
    if monitor and binding and binding.get("binding_eligible"):
        try:
            monitor.record_proposal(binding=binding, plan=record, model_receipt=context)
        except (ValueError, KeyError) as exc:
            # An immutable plan can still be managed safely if qualification
            # bookkeeping fails. Permanently disclose the contaminated trial.
            monitor.registry.record_deviation(protocol_id=binding["protocol_id"], account_id=campaign.broker.account_id,
                reason="proposal_registration_failed:" + str(exc), deviation_id="proposal:" + record["plan_id"])


def close_plan(campaign, *, plan_id: str, rationale: str, host_context: dict[str, Any], now=None):
    """Request only cancellation/reduction of an existing account-owned plan."""
    instant = utc_time(now or datetime.now(timezone.utc))
    plan = campaign.plans.get(plan_id)
    if plan["account_id"] != campaign.broker.account_id:
        raise ValueError("plan_broker_account_mismatch")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 12000:
        raise ValueError("agent_exit_rationale_required")
    # Validate the exact same metadata boundary as entry proposals.
    from .agent_plan_proposal import _context
    context = _context(host_context)
    evidence_id = campaign._retain("agent_exit_decision", {
        "plan_id": plan_id, "strategy_version": plan["definition"]["strategy_version"],
        "rationale": rationale, "host_context": context,
    })
    forward = None
    monitor = getattr(campaign, "forward_monitor", None)
    if monitor and plan["definition"].get("metadata", {}).get("forward_validation"):
        try:
            forward = monitor.record_exit(plan=plan, evidence_id=evidence_id, model_receipt=context, rationale=rationale)
        except Exception as exc:
            # Failed qualification bookkeeping must never strand an owned
            # position. The actual exit evidence above remains independently retained.
            forward = {"status": "exit_validation_requires_reconciliation", "error": f"{type(exc).__name__}: {exc}",
                       "evidence_id": evidence_id, "positive_ev_qualified": False}
            campaign._retain("agent_forward_exit_diagnostic", forward)
    request = campaign.plans.request_agent_exit(account_id=campaign.broker.account_id, plan_id=plan_id,
                                                evidence_id=evidence_id, now=instant)
    return {**request, **({"forward_validation": forward} if forward is not None else {})}
