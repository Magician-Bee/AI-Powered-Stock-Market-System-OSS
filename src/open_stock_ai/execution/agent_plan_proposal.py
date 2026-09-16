"""Build a discretionary draft from Host-retained research and Agent choices.

Only ``proposal`` is model input. The caller must load ``cycle`` and
``retained_evidence`` from its account-scoped store and inject actual run/model
metadata and account state. Content hashes detect corruption; they are not an
authentication mechanism for client-supplied records. This module neither
approves risk, retains a plan, activates a campaign nor calls a broker/model.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from open_stock_ai.strategy.execution_policy import plan_signal_order, policy_metadata, single_order_quantity
from open_stock_ai.types import TradingSignal
from .trading_plan import ExitOrderPolicy, TradingPlan, content_hash, utc_time
from .product_admission import assess_new_entry_product


STRATEGY_ID = "agent_discretionary_proposal_v1"
AGENT_PLAN_PROPOSAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["cycle_id", "symbol", "stop_loss", "rationale"],
    "oneOf": [{"required": ["quantity_shares"]}, {"required": ["position_size_pct"]}],
    "properties": {
        "cycle_id": {"type": "string", "minLength": 1},
        "symbol": {"type": "string", "pattern": r"^[0-9]{4,6}\.TW(O)?$"},
        "quantity_shares": {"type": "integer", "minimum": 1,
                            "description": "Shares, not lots. A mixed quantity >=1000 is rounded down to complete 1000-share lots and the residual is disclosed."},
        "position_size_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 100,
                              "description": "New order cash budget as a percent of decision-time equity, including estimated entry costs."},
        "stop_loss": {"type": "number", "exclusiveMinimum": 0},
        "target_price": {"type": "number", "exclusiveMinimum": 0},
        "entry_condition": {"type": "string", "enum": ["immediate", "price_at_or_above", "price_at_or_below"]},
        "trigger_price": {"type": "number", "exclusiveMinimum": 0},
        "not_before": {"type": "string", "format": "date-time"},
        "expires_at": {"type": "string", "format": "date-time"},
        "max_holding_seconds": {"type": "integer", "minimum": 60},
        "exit_not_after": {"type": "string", "format": "date-time"},
        "exit_order_policy": {
            "type": "object", "additionalProperties": False,
            "description": "Optional bounded cancel-and-replace policy for unfilled exits. Set an explicit minimum sell limit no higher than stop_loss; Host includes that floor in entry risk. Without this policy, overdue exits alert and keep their original limit.",
            "required": ["wait_seconds", "max_replacements", "minimum_limit_price"],
            "properties": {
                "wait_seconds": {"type": "integer", "minimum": 1},
                "max_replacements": {"type": "integer", "minimum": 1, "maximum": 10},
                "minimum_limit_price": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        "rationale": {"type": "string", "minLength": 1, "maxLength": 12000},
        "evidence_ids": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
    },
}


def _number(value: Any, name: str, *, zero_allowed: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid_{name}")
    value = float(value)
    if not math.isfinite(value) or value < 0 or value == 0 and not zero_allowed:
        raise ValueError(f"invalid_{name}")
    return value


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("proposal_records_must_be_finite_json") from exc


def _context(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"run_id", "session_id", "driver_id", "parent_run_id", "provider_model_metadata",
               "tool_call_id", "context_receipt_id"}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError("host_proposal_context_fields_not_allowed")
    result = _json_copy(dict(value))
    for key in ("run_id", "session_id", "driver_id"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ValueError(f"host_proposal_{key}_required")
    model = result.get("provider_model_metadata")
    if not isinstance(model, dict) or not isinstance(model.get("model"), str) or not model["model"].strip():
        raise ValueError("actual_host_model_metadata_required")

    def reject_credentials(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(term in normalized for term in ("apikey", "secret", "password", "credential", "authorization", "cookie")) or normalized in {
                    "token", "accesstoken", "refreshtoken", "idtoken", "bearer", "headers", "privatekey",
                }:
                    raise ValueError("credentials_forbidden_in_proposal_context")
                reject_credentials(child)
        elif isinstance(node, list):
            for child in node:
                reject_credentials(child)
        elif isinstance(node, str) and (re.search(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}", node) or node.lower().startswith("bearer ")):
            raise ValueError("credentials_forbidden_in_proposal_context")

    reject_credentials(result)
    return result


def proposal_source_manifest() -> dict[str, str]:
    """Bind the builder and every local rule/type that can change the draft."""
    root = Path(__file__).resolve().parents[1]
    paths = [Path(__file__).resolve(), root / "execution/trading_plan.py", root / "execution/taiwan_market_rules.py",
             root / "strategy/execution_policy.py", root / "types.py", root / "execution/product_admission.py",
             root / "product_identity.py"]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def build_agent_plan_proposal(
    proposal: Mapping[str, Any], *, cycle: Mapping[str, Any],
    retained_evidence: Mapping[str, Mapping[str, Any]], host_context: Mapping[str, Any],
    account_summary: Mapping[str, Any], now: datetime,
    planning_cash: float | None = None,
    product_snapshot: dict[str, Any] | None = None,
) -> TradingPlan:
    """Freeze an unqualified long-entry draft using explicitly supplied Host state.

    ``retained_evidence`` is ``{id: {"kind": kind, "payload": saved_payload}}``.
    The first plan evidence is always its retained price history for the shared
    campaign risk resolver. Optional cited overlays must also be loaded by Host.
    Percentage sizing shares the replay/live policy; explicit shares can only
    round down. Central submission risk must recheck cash, reservations,
    exposure, costs, source freshness and experiment permission at execution.
    """
    instant = utc_time(now)
    if not isinstance(proposal, Mapping) or set(proposal) - set(AGENT_PLAN_PROPOSAL_SCHEMA["properties"]):
        raise ValueError("agent_proposal_fields_not_allowed")
    args = _json_copy(dict(proposal))
    if any(key not in args for key in AGENT_PLAN_PROPOSAL_SCHEMA["required"]):
        raise ValueError("agent_proposal_required_fields_missing")
    if ("quantity_shares" in args) == ("position_size_pct" in args):
        raise ValueError("exactly_one_sizing_instruction_required")
    context = _context(host_context)
    saved_cycle = _json_copy(dict(cycle))
    cycle_id = "AC-" + content_hash({key: value for key, value in saved_cycle.items() if key != "cycle_id"})
    if saved_cycle.get("cycle_id") != cycle_id or args["cycle_id"] != cycle_id:
        raise ValueError("retained_proposal_cycle_mismatch")
    account_id = saved_cycle.get("account_id")
    if not isinstance(account_id, str) or not account_id or account_summary.get("account_id") != account_id:
        raise ValueError("proposal_account_scope_mismatch")
    if not 0 <= (instant - utc_time(saved_cycle["created_at"])).total_seconds() <= 86400:
        raise ValueError("research_cycle_requires_refresh")
    symbol = args["symbol"].strip().upper() if isinstance(args["symbol"], str) else ""
    if not re.fullmatch(r"[0-9]{4,6}\.TW(?:O)?", symbol):
        raise ValueError("canonical_taiwan_symbol_required")
    matches = [row for row in saved_cycle.get("results", []) if row.get("symbol") == symbol]
    if len(matches) != 1:
        raise ValueError("proposal_requires_retained_symbol_history")
    research = matches[0]
    feature = research.get("feature") if isinstance(research.get("feature"), dict) else {}
    product_admission = assess_new_entry_product(symbol=symbol, market="TW", product_snapshot=product_snapshot,
        now=instant, expected_entity_id=feature.get("entity_id") or "")
    if feature.get("symbol") != symbol or str(feature.get("exchange") or "").upper() != str(
            product_admission["classification"].get("venue") or "").upper():
        raise ValueError("product_research_identity_mismatch")
    if not product_admission["allowed"]:
        raise ValueError("product_admission_rejected:" + ";".join(product_admission["reasons"]))
    history_id, bulk_id = research["history_id"], saved_cycle["bulk_evidence_id"]
    cited = args.get("evidence_ids", [])
    if not isinstance(cited, list) or any(not isinstance(item, str) or not item.strip() for item in cited):
        raise ValueError("invalid_proposal_evidence_ids")
    evidence_ids = tuple(dict.fromkeys([history_id, bulk_id, *cited]))
    evidence_manifest, evidence_payloads = {}, {}
    for evidence_id in evidence_ids:
        record = retained_evidence.get(evidence_id)
        if not isinstance(record, Mapping) or not isinstance(record.get("kind"), str) or not isinstance(record.get("payload"), Mapping):
            raise ValueError("proposal_evidence_must_be_host_retained")
        kind, payload = record["kind"], _json_copy(dict(record["payload"]))
        if "AE-" + content_hash({"account_id": account_id, "kind": kind, "payload": payload}) != evidence_id:
            raise ValueError("retained_proposal_evidence_mismatch")
        if evidence_id == history_id and kind != "price_history" or evidence_id == bulk_id and kind != "market_screen":
            raise ValueError("retained_proposal_evidence_kind_mismatch")
        evidence_manifest[evidence_id] = {"kind": kind, "payload_sha256": content_hash(payload)}
        evidence_payloads[evidence_id] = payload
    history = evidence_payloads[history_id]
    rows, source = history.get("rows"), history.get("data_evidence", {})
    if not isinstance(rows, list) or not rows or source.get("symbol") != symbol or source.get("source_provenance_verified") is not True:
        raise ValueError("retained_proposal_history_identity_required")
    if source.get("data_sha256") != content_hash(rows):
        raise ValueError("retained_proposal_history_hash_mismatch")
    timestamps = [utc_time(row["timestamp"]) for row in rows]
    if any(stamp > instant for stamp in timestamps) or any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("proposal_history_must_be_completed_and_chronological")
    if rows[-1]["timestamp"] != research.get("last_bar"):
        raise ValueError("retained_proposal_last_bar_mismatch")
    price = _number(rows[-1]["close"], "retained_reference_price")
    stop = _number(args["stop_loss"], "stop_loss")
    target = _number(args["target_price"], "target_price") if "target_price" in args else None
    exit_policy = None
    if "exit_order_policy" in args:
        raw_policy = args["exit_order_policy"]
        if (not isinstance(raw_policy, dict)
                or set(raw_policy) != {"wait_seconds", "max_replacements", "minimum_limit_price"}):
            raise ValueError("invalid_exit_order_policy_fields")
        exit_policy = ExitOrderPolicy(**raw_policy)
    rationale = args["rationale"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 12000:
        raise ValueError("proposal_rationale_required")
    condition = args.get("entry_condition", "immediate")
    if condition not in {"immediate", "price_at_or_above", "price_at_or_below"}:
        raise ValueError("unsupported_entry_condition")
    trigger = _number(args["trigger_price"], "trigger_price") if "trigger_price" in args else None
    if condition == "immediate" and trigger is not None:
        raise ValueError("immediate_entry_has_no_trigger_price")
    if condition != "immediate" and (trigger is None or trigger <= stop or target is not None and trigger >= target):
        raise ValueError("conditional_entry_trigger_must_be_inside_bracket")
    observed_close = price
    # A breakout priced above the last close must reserve its intended entry
    # price, otherwise every actual trigger would exceed a share-sized budget.
    if condition != "immediate":
        price = trigger
    if stop >= price or target is not None and target <= price:
        raise ValueError("invalid_proposal_bracket")
    not_before = utc_time(args["not_before"]).isoformat() if "not_before" in args else None
    start = max(instant, utc_time(not_before)) if not_before else instant
    expires_at = utc_time(args["expires_at"]) if "expires_at" in args else start + timedelta(days=1)
    if expires_at <= start:
        raise ValueError("proposal_entry_expiry_must_be_future")
    deadline = utc_time(args["exit_not_after"]) if "exit_not_after" in args else None
    if deadline is not None and deadline <= start:
        raise ValueError("proposal_exit_deadline_must_follow_entry_start")
    holding = args.get("max_holding_seconds", 30 * 86400)
    if isinstance(holding, bool) or not isinstance(holding, int) or holding < 60:
        raise ValueError("invalid_holding_period")
    equity = _number(account_summary.get("total_equity"), "account_equity")
    cash = _number(account_summary.get("cash_balance"), "account_cash", zero_allowed=True)
    cash = min(cash, _number(account_summary.get("available_cash", cash), "available_cash", zero_allowed=True))
    if planning_cash is not None:
        cash = min(cash, _number(planning_cash, "planning_cash", zero_allowed=True))
    costs = saved_cycle.get("cost_assumptions", {})
    commission = _number(costs.get("commission_bps"), "commission_bps", zero_allowed=True)
    minimum = _number(costs.get("minimum_commission"), "minimum_commission", zero_allowed=True)
    exchange = _number(costs.get("exchange_fee_bps", 0), "exchange_fee_bps", zero_allowed=True)
    slippage = _number(costs.get("slippage_bps", 0), "slippage_bps", zero_allowed=True)
    impact = _number(costs.get("market_impact_bps", 0), "market_impact_bps", zero_allowed=True)
    reserve_price = price * (1 + (slippage + impact) / 10000)
    if not research.get("feature", {}).get("is_etf"):
        from .taiwan_market_rules import tick_size
        reserve_decimal = Decimal(str(price)) * (Decimal(1)+(Decimal(str(slippage))+Decimal(str(impact)))/Decimal(10000))
        tick = tick_size(reserve_decimal)
        reserve_price = float((reserve_decimal/tick).to_integral_value(rounding=ROUND_CEILING)*tick)
    if "quantity_shares" in args:
        requested = args["quantity_shares"]
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
            raise ValueError("positive_integer_shares_required")
        sizing = single_order_quantity(requested, market="TW")
        quantity = sizing["quantity"]
        notional = quantity * price
        reserve_notional = quantity * reserve_price
        budget = reserve_notional + max(minimum, reserve_notional * commission / 10000) + reserve_notional * exchange / 10000
        pct = notional / equity * 100
        if budget > cash + .01 or pct > 100:
            raise ValueError("requested_shares_exceed_host_planning_cash")
        sizing.update(original_quantity_shares=requested, position_size_pct=pct,
                      cash_budget=budget, eligible=True, sizing_basis="explicit_shares_with_cost_reserve")
    else:
        pct = _number(args["position_size_pct"], "position_size_pct")
        signal = TradingSignal(symbol=symbol, market="TW", action="buy", confidence=None, horizon="swing",
                               reason=rationale, entry_price=price, stop_loss=stop, target_price=target, position_size_pct=pct)
        sizing = plan_signal_order(signal, equity=equity, cash=cash, reference_price=reserve_price,
                                   position_quantity=0, commission_bps=commission, exchange_fee_bps=exchange, minimum_commission=minimum)
        if not sizing["eligible"]:
            raise ValueError(f"proposal_sizing_rejected:{sizing['reason']}")
        quantity, budget = sizing["quantity"], sizing["cash_budget"]
        # Reserve an additive exchange fee even when minimum commission binds;
        # this may only reduce the shared policy's proposed quantity.
        minimum_fee_capacity = math.floor(max(0, budget-minimum) / (reserve_price * (1+exchange/10000)))
        if quantity > minimum_fee_capacity:
            adjusted = single_order_quantity(minimum_fee_capacity, market="TW")
            quantity = adjusted["quantity"]
            if quantity < 1:
                raise ValueError("proposal_sizing_rejected:entry_cost_reserve_exceeds_budget")
            sizing.update(**adjusted, reason="entry_cost_reserve_reduced_quantity")
    sizing["planning_execution_price"] = reserve_price
    sizing["slippage_and_impact_reserve_bps"] = slippage + impact
    sizing["requested_position_size_pct"] = pct
    pct = quantity * price / equity * 100
    sizing["position_size_pct"] = pct
    sizing["cash_allocation_pct"] = budget / equity * 100
    spec = {
        "symbol": symbol, "market": "TW", "sizing_instruction": {key: args[key] for key in ("quantity_shares", "position_size_pct") if key in args},
        "stop_loss": stop, "target_price": target, "entry_condition": condition, "trigger_price": trigger,
        "not_before": not_before, "expires_at": expires_at.isoformat(), "max_holding_seconds": holding,
        "exit_not_after": deadline.isoformat() if deadline else None, "rationale": rationale,
        "reference_price": price, "quantity_shares": quantity, "cash_budget": budget,
        "observed_history_close": observed_close,
        "position_size_pct": pct, "cycle_id": cycle_id, "evidence_ids": list(evidence_ids),
        "product_admission_sha256": product_admission["receipt_sha256"],
        **({"exit_order_policy": dict(args["exit_order_policy"])} if exit_policy else {}),
    }
    manifest = proposal_source_manifest()
    version = content_hash({"schema_version": "open_stock_ai.agent_plan_proposal.v1", "strategy_id": STRATEGY_ID,
                            "proposal_spec": spec, "source_manifest": manifest, "execution_policy": policy_metadata()})
    metadata = {
        "schema_version": "open_stock_ai.agent_plan_proposal.v1", "cycle_id": cycle_id,
        "proposal_spec": spec, "proposal_spec_sha256": content_hash(spec), "builder_source_manifest": manifest,
        "agent_context": context, "agent_context_sha256": content_hash(context),
        "evidence_manifest": evidence_manifest, "sizing": sizing, "execution_policy": policy_metadata(),
        "cost_assumptions": _json_copy(costs), "cost_assumptions_status": "host_retained_planning_assumptions",
        "account_snapshot_sha256": content_hash(dict(account_summary)),
        "product_admission": product_admission,
        "eligibility": "bounded_experiment", "positive_ev_qualified": False,
        "qualification_status": "discretionary_proposal_has_no_strategy_performance_qualification",
        "submission_risk_status": "requires_central_risk_recheck", "required_evidence": ["price_history", "cost_model"],
    }
    return TradingPlan(symbol=symbol, strategy_id=STRATEGY_ID, strategy_version=version, evidence_ids=evidence_ids,
                       reference_price=price, position_size_pct=pct, stop_loss=stop, target_price=target,
                       quantity_shares=quantity, cash_budget=budget, entry_condition=condition, trigger_price=trigger,
                       not_before=not_before, expires_at=expires_at.isoformat(), max_holding_seconds=holding,
                       exit_not_after=deadline.isoformat() if deadline else None, rationale=rationale,
                       qualification_id=None, metadata=metadata, exit_order_policy=exit_policy)
