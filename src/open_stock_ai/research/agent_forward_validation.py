"""Host-only, prospective paper-policy experiments; never a broker permission.

The Host supplies actual model/configuration and retains source evidence. Hashes
provide integrity, not authentication of model/client-supplied provenance. The
clock is injected only for deterministic tests; public callers cannot backdate
observations. A fixed-time circular block bootstrap is an approximate procedure
for weakly dependent stationary returns, not a guarantee under regime change.
References: Kunsch (1989), doi:10.1214/aos/1176347265; Harvey, Liu & Zhu,
https://www.nber.org/papers/w20592. Qualification is scoped to a frozen paper
cost scenario; simulated execution never qualifies a live account.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from random import Random
import re
import sqlite3
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from open_stock_ai.execution.agent_plan_proposal import _context
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.trade_outcomes import AutonomousOutcomeLedger
from open_stock_ai.execution.trading_plan import content_hash, utc_time
from open_stock_ai.execution.trading_plan_store import TradingPlanStore
from .candle_qualification import QUALIFICATION_REQUIREMENTS


SCHEMA = "open_stock_ai.agent_forward_validation.v1"
MODEL_FIELDS = ("model", "reasoning_effort", "provider", "model_revision")
MIN_DAYS = QUALIFICATION_REQUIREMENTS["holdout_minimum_bars"]
MIN_TRADES = QUALIFICATION_REQUIREMENTS["holdout_minimum_closed_trades"]


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _model(receipt):
    driver = receipt.get("driver_id", "codex")
    value = receipt.get("provider_model_metadata", receipt)
    checked = _context({"run_id": "host-policy", "session_id": "host-policy", "driver_id": driver,
                        "provider_model_metadata": value})["provider_model_metadata"]
    if not str(checked.get("reasoning_effort") or "").strip():
        raise ValueError("actual_reasoning_effort_required")
    return {"driver_id": driver, **{key: checked[key] for key in MODEL_FIELDS if key in checked}}


def freeze_agent_policy(*, model_receipt: dict, prompt_template: str, decision_policy: dict,
                        tool_manifest: Any, source_hashes: dict, required_evidence: list[str]) -> dict:
    """Stable across runs/plans; actual model, prompt, tools and code must match.

source_hashes must contain tools/execution/config maps of relative path to
SHA256. Hosts compute these from their loaded source/config, never arguments.
"""
    if not prompt_template.strip() or not decision_policy or not tool_manifest or not required_evidence:
        raise ValueError("complete_frozen_agent_policy_required")
    for group in ("tools", "execution", "config"):
        hashes = source_hashes.get(group)
        if not isinstance(hashes, dict) or not hashes or any(not re.fullmatch(r"[a-f0-9]{64}", str(v)) for v in hashes.values()):
            raise ValueError("tools_execution_config_source_hashes_required")
    body = {"schema_version": SCHEMA, "model": _model(model_receipt), "prompt_template": prompt_template,
            "decision_policy": _copy(decision_policy), "tool_manifest": _copy(tool_manifest),
            "source_hashes": _copy(source_hashes), "required_evidence": sorted(set(required_evidence)),
            "evaluator_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return {**body, "policy_version": content_hash(body)}


def _sealed(body):
    return {**body, "receipt_sha256": content_hash(body)}


def _int(value, lower, upper, name):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError("invalid_" + name)
    return value


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or positive and value == 0:
        raise ValueError("invalid_" + name)
    return float(value)


def _bootstrap(values, *, alpha, block_length, resamples, seed):
    """Fixed circular blocks of daily portfolio returns, not correlated trades."""
    if not values:
        return {"mean_daily_net_return_pct": None, "lower_confidence_bound_pct": None}
    rng, draws = Random(seed), []
    for _ in range(resamples):
        sample = []
        while len(sample) < len(values):
            start = rng.randrange(len(values))
            sample.extend(values[(start+j) % len(values)] for j in range(block_length))
        draws.append(mean(sample[:len(values)]))
    draws.sort()
    return {"mean_daily_net_return_pct": mean(values),
            "lower_confidence_bound_pct": draws[max(0, math.floor(alpha*resamples)-1)],
            "method": "fixed_circular_block_bootstrap_daily_portfolio_returns",
            "one_sided_alpha": alpha, "block_length": block_length, "resamples": resamples,
            "tail_resample_count": alpha*resamples}


class AgentForwardValidation:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.plans = TradingPlanStore(store)
        self.outcomes = AutonomousOutcomeLedger(store)
        with store._connect() as conn:
            conn.executescript("""
                create table if not exists agent_forward_policies (
                    policy_version text primary key, payload_json text not null);
                create table if not exists agent_forward_protocols (
                    sequence integer primary key autoincrement, protocol_id text unique not null,
                    account_id text not null, policy_version text not null,
                    registered_at text not null, starts_at text not null, ends_at text not null,
                    evaluate_at text not null, payload_json text not null);
                create table if not exists agent_forward_records (
                    protocol_id text not null, kind text not null, record_id text not null,
                    recorded_at text not null, payload_json text not null,
                    primary key(protocol_id,kind,record_id));
                create table if not exists agent_forward_evaluations (
                    protocol_id text primary key, payload_json text not null);
            """)

    def register_policy(self, policy: dict) -> dict:
        body = {k: v for k, v in policy.items() if k != "policy_version"}
        if policy.get("policy_version") != content_hash(body) or policy.get("schema_version") != SCHEMA:
            raise ValueError("frozen_policy_hash_mismatch")
        # Re-validate the schema even when a caller recomputes its own digest.
        rebuilt = freeze_agent_policy(model_receipt=policy["model"], prompt_template=policy["prompt_template"],
                                      decision_policy=policy["decision_policy"], tool_manifest=policy["tool_manifest"],
                                      source_hashes=policy["source_hashes"], required_evidence=policy["required_evidence"])
        if rebuilt != policy:
            raise ValueError("frozen_policy_or_evaluator_version_mismatch")
        with self.store._connect() as conn:
            conn.execute("insert or ignore into agent_forward_policies values (?,?)", (policy["policy_version"], json.dumps(policy)))
            conn.commit()
        return _copy(policy)

    def policy(self, policy_version):
        with self.store._connect() as conn:
            row = conn.execute("select payload_json from agent_forward_policies where policy_version=?", (policy_version,)).fetchone()
        if not row:
            raise ValueError("forward_policy_not_registered")
        result = json.loads(row[0])
        if content_hash({k: v for k, v in result.items() if k != "policy_version"}) != policy_version:
            raise ValueError("forward_policy_integrity_failure")
        return result

    def _account(self, account_id):
        with self.store._connect() as conn:
            if not conn.execute("select 1 from paper_accounts where account_id=?", (account_id,)).fetchone():
                raise ValueError("existing_isolated_paper_account_required")
        return PaperOMS(self.store, account_id=account_id, read_environment=False).portfolio_summary(as_of=utc_time(self.clock()))

    @staticmethod
    def _flat_account_guard(conn, account_id):
        if (conn.execute("select 1 from paper_positions where account_id=? and quantity<>0", (account_id,)).fetchone()
                or conn.execute("select 1 from autonomous_trading_plans where account_id=? and status not in ('closed','cancelled','rejected','expired','invalidated')", (account_id,)).fetchone()
                or conn.execute("select 1 from paper_broker_orders where account_id=? and status in ('submitted','acknowledged','open','triggered','partially_filled')", (account_id,)).fetchone()):
            raise ValueError("operation_requires_flat_account_without_active_plans_or_orders")

    def register_protocol(self, *, account_id: str, policy_version: str, symbols: list[str], costs: dict,
                          starts_at, ends_at, evaluate_at, observation_times: list[str],
                          minimum_closed_trades=MIN_TRADES, minimum_observation_days=MIN_DAYS,
                          benchmark=None, block_length=5, resamples=10000, fixture=False) -> dict:
        self.policy(policy_version)
        start, end, evaluate = map(utc_time, (starts_at, ends_at, evaluate_at))
        slots = [utc_time(x) for x in observation_times]
        symbols = sorted(set(symbols))
        if not symbols or any(not re.fullmatch(r"\d{4,6}\.TW(O)?", s) for s in symbols):
            raise ValueError("frozen_canonical_taiwan_universe_required")
        if not start < end <= evaluate or not slots or slots != sorted(set(slots)) or slots[-1] != end or slots[0] <= start:
            raise ValueError("fixed_future_observation_schedule_required")
        if len({x.date() for x in slots}) != len(slots):
            raise ValueError("one_observation_per_utc_day_required")
        days = _int(minimum_observation_days, MIN_DAYS, 10000, "minimum_observation_days")
        trades = _int(minimum_closed_trades, MIN_TRADES, 100000, "minimum_closed_trades")
        if len(slots) < days:
            raise ValueError("insufficient_registered_observation_days")
        benchmark = benchmark or {"kind": "cash", "daily_return_pct": 0.0}
        if benchmark != {"kind": "cash", "daily_return_pct": 0.0}:
            raise ValueError("initial_forward_benchmark_must_be_zero_return_cash")
        required_costs = {"commission_bps", "minimum_commission", "sell_tax_bps", "slippage_bps", "market_impact_bps"}
        if not required_costs <= costs.keys():
            raise ValueError("complete_forward_cost_scenario_required")
        costs = {k: _number(v, k) for k, v in costs.items()}
        if costs["commission_bps"] <= 0 or costs["minimum_commission"] <= 0 or costs["sell_tax_bps"] <= 0 or costs["slippage_bps"] + costs["market_impact_bps"] < 25:
            raise ValueError("nonzero_fee_tax_and_25bps_adverse_floor_required")
        spec = {"account_id": account_id, "policy_version": policy_version, "symbols": symbols, "costs": costs,
                "starts_at": start.isoformat(), "ends_at": end.isoformat(), "evaluate_at": evaluate.isoformat(),
                "observation_times": [x.isoformat() for x in slots], "minimum_closed_trades": trades,
                "minimum_observation_days": days, "benchmark": benchmark,
                "block_length": _int(block_length, 2, 60, "block_length"),
                "resamples": _int(resamples, 1000, 100000, "resamples"), "fixture": bool(fixture)}
        identifier = "FP-" + content_hash(spec)
        account = self._account(account_id)
        with self.store._connect() as conn:
            conn.execute("begin immediate")
            old = conn.execute("select payload_json from agent_forward_protocols where protocol_id=?", (identifier,)).fetchone()
            if old:
                return json.loads(old[0])
            now = utc_time(self.clock())
            if now >= start:
                raise ValueError("protocol_must_precede_future_samples")
            if conn.execute("select 1 from agent_forward_protocols p where account_id=? and starts_at<? and ends_at>? and not exists (select 1 from agent_forward_evaluations e where e.protocol_id=p.protocol_id and json_extract(e.payload_json,'$.status')='abandoned')", (account_id, end.isoformat(), start.isoformat())).fetchone():
                raise ValueError("overlapping_account_experiments_forbidden")
            if account["position_count"] or self.plans.list(account_id=account_id, active_only=True):
                raise ValueError("protocol_requires_flat_account_without_active_plans")
            self._flat_account_guard(conn, account_id)
            # Public account summaries round cents; fees/tax retain fractional
            # cents internally. That normal rounding is not an account change.
            if abs(float(conn.execute("select cash_balance from paper_accounts where account_id=?", (account_id,)).fetchone()[0])-account["cash_balance"]) > .011:
                raise ValueError("account_changed_during_protocol_registration")
            # Across all accounts/protocols in this store, sum 1/(j*(j+1)) <= 1.
            # Failed/abandoned trials consume their alpha; deletion is not an API.
            sequence = int(conn.execute("select coalesce(max(sequence),0)+1 from agent_forward_protocols").fetchone()[0])
            alpha = .05/(sequence*(sequence+1))
            body = {"schema_version": SCHEMA, "protocol_id": identifier, **spec, "registered_at": now.isoformat(),
                    "initial_equity": account["total_equity"], "initial_cash": account["cash_balance"],
                    "family_sequence": sequence, "one_sided_alpha": alpha,
                    "multiplicity_method": "global_store_protocol_alpha_spending_0.05_over_j_jplus1",
                    "evaluation_policy": "single_fixed_time_no_optional_stopping_no_posthoc_universe_selection"}
            payload = _sealed(body)
            conn.execute("insert into agent_forward_protocols values (?,?,?,?,?,?,?,?,?)", (sequence, identifier, account_id, policy_version,
                         now.isoformat(), start.isoformat(), end.isoformat(), evaluate.isoformat(), json.dumps(payload)))
            conn.commit()
        return payload

    def protocol(self, protocol_id, *, account_id):
        with self.store._connect() as conn:
            row = conn.execute("select payload_json from agent_forward_protocols where protocol_id=? and account_id=?", (protocol_id, account_id)).fetchone()
        if not row:
            raise ValueError("forward_protocol_account_mismatch")
        result = json.loads(row[0])
        if result["receipt_sha256"] != content_hash({k: v for k, v in result.items() if k != "receipt_sha256"}):
            raise ValueError("forward_protocol_integrity_failure")
        return result

    def _records(self, protocol_id, kind):
        with self.store._connect() as conn:
            rows = conn.execute("select payload_json from agent_forward_records where protocol_id=? and kind=? order by recorded_at,record_id", (protocol_id, kind)).fetchall()
        records = [json.loads(r[0]) for r in rows]
        if any(r["receipt_sha256"] != content_hash({k: v for k, v in r.items() if k != "receipt_sha256"}) for r in records):
            raise ValueError("forward_record_integrity_failure")
        return records

    def records(self, *, protocol_id, account_id, kind=None):
        self.protocol(protocol_id, account_id=account_id)
        if kind is not None and kind not in {"decision", "exit_decision", "observation", "outcome", "deviation"}:
            raise ValueError("invalid_forward_record_kind")
        return self._records(protocol_id, kind) if kind else {k: self._records(protocol_id, k) for k in ("decision", "exit_decision", "observation", "outcome", "deviation")}

    def record_deviation(self, *, protocol_id, account_id, reason, evidence_ids=(), deviation_id=None):
        protocol = self.protocol(protocol_id, account_id=account_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("protocol_deviation_reason_required")
        body = {"reason": reason, "evidence": self._evidence(account_id, evidence_ids)}
        return self._append(protocol, "deviation", deviation_id or content_hash(body), body)

    def list_protocols(self, *, account_id):
        with self.store._connect() as conn:
            rows = conn.execute("select p.payload_json,e.payload_json from agent_forward_protocols p left join agent_forward_evaluations e using(protocol_id) where p.account_id=? order by p.sequence", (account_id,)).fetchall()
        result = []
        for raw, evaluated in rows:
            protocol, evaluation = json.loads(raw), json.loads(evaluated) if evaluated else None
            status = evaluation.get("status", "evaluated") if evaluation else "scheduled" if utc_time(self.clock()) < utc_time(protocol["starts_at"]) else "collecting"
            result.append({**protocol, "status": status, "evaluation": evaluation})
        return result

    def abandon_protocol(self, *, protocol_id, account_id, reason):
        protocol = self.protocol(protocol_id, account_id=account_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("abandonment_reason_required")
        account = self._account(account_id)
        if account["position_count"] or self.plans.list(account_id=account_id, active_only=True):
            raise ValueError("abandonment_requires_flat_account_without_active_plans")
        result = _sealed({"schema_version": SCHEMA, "status": "abandoned", "protocol_id": protocol_id,
                          "account_id": account_id, "policy_version": protocol["policy_version"],
                          "evaluated_at": utc_time(self.clock()).isoformat(), "passed": False,
                          "positive_ev_qualified": False, "live_execution_eligible": False,
                          "reasons": ["abandoned_without_qualification"], "abandonment_reason": reason,
                          "protocol_sha256": protocol["receipt_sha256"]})
        with self.store._connect() as conn:
            conn.execute("begin immediate")
            old = conn.execute("select payload_json from agent_forward_evaluations where protocol_id=?", (protocol_id,)).fetchone()
            if old:
                return json.loads(old[0])
            self._flat_account_guard(conn, account_id)
            conn.execute("insert into agent_forward_evaluations values (?,?)", (protocol_id, json.dumps(result)))
            conn.commit()
        return result

    def _append(self, protocol, kind, record_id, body, *, deadline=None):
        with self.store._connect() as conn:
            conn.execute("begin immediate")
            now = utc_time(self.clock())
            prior = conn.execute("select payload_json from agent_forward_records where protocol_id=? and kind=? and record_id=?", (protocol["protocol_id"], kind, record_id)).fetchone()
            if prior:
                old = json.loads(prior[0])
                if {k: v for k, v in old.items() if k not in {"recorded_at", "receipt_sha256"}} != body:
                    raise ValueError("immutable_forward_record_conflict")
                return old
            if conn.execute("select 1 from agent_forward_evaluations where protocol_id=?", (protocol["protocol_id"],)).fetchone():
                raise ValueError("forward_evaluation_already_sealed")
            earliest = protocol["registered_at"] if kind in {"decision", "exit_decision", "deviation"} else protocol["starts_at"]
            if not utc_time(earliest) <= now <= utc_time(deadline or protocol["evaluate_at"]) or now >= utc_time(protocol["evaluate_at"]):
                raise ValueError("observation_outside_registered_window")
            latest = conn.execute("select max(recorded_at) from agent_forward_records where protocol_id=?", (protocol["protocol_id"],)).fetchone()[0]
            if latest and now < utc_time(latest):
                raise ValueError("host_clock_regressed_no_backfill")
            value = _sealed({**body, "recorded_at": now.isoformat()})
            conn.execute("insert into agent_forward_records values (?,?,?,?,?)", (protocol["protocol_id"], kind, record_id, now.isoformat(), json.dumps(value)))
            conn.commit()
        return value

    def _evidence(self, account_id, evidence_ids):
        result = []
        with self.store._connect() as conn:
            for identifier in sorted(set(evidence_ids)):
                row = conn.execute("select kind,payload_json from autonomous_evidence where evidence_id=? and account_id=?", (identifier, account_id)).fetchone()
                if not row:
                    raise ValueError("owned_retained_forward_evidence_required")
                kind, payload = row[0], json.loads(row[1])
                if identifier != "AE-" + content_hash({"account_id": account_id, "kind": kind, "payload": payload}):
                    raise ValueError("retained_forward_evidence_hash_mismatch")
                source = payload.get("data_evidence", payload)
                available = source.get("available_at", source.get("acquired_at"))
                if available and utc_time(available) > utc_time(self.clock()):
                    raise ValueError("future_source_not_available_at_decision")
                fixture_sources = [payload, source, payload.get("market_observation", {}), *payload.get("quotes", [])]
                result.append({"evidence_id": identifier, "kind": kind, "payload_sha256": content_hash(payload),
                               "source_provenance_verified": source.get("source_provenance_verified") is True,
                               "fixture": any(s.get("fixture_only_not_market_or_ev_proof") or s.get("fixture") or s.get("is_fixture") for s in fixture_sources if isinstance(s, dict)),
                               "source_kind": source.get("source_kind"),
                               **({"daily_mark": payload} if kind == "forward_daily_mark" else {})})
        return result

    def record_decision(self, *, protocol_id, account_id, decision_id, policy_version, action,
                        model_receipt, evidence_ids, rationale, symbol=None, plan_id=None):
        protocol = self.protocol(protocol_id, account_id=account_id)
        if policy_version != protocol["policy_version"] or _model(model_receipt) != self.policy(policy_version)["model"]:
            raise ValueError("forward_actual_model_or_policy_mismatch")
        if action not in {"hold", "reject", "propose"} or not decision_id or not rationale or (symbol is not None and symbol not in protocol["symbols"]):
            raise ValueError("invalid_forward_decision")
        if (action == "propose") != bool(plan_id):
            raise ValueError("proposal_requires_owned_unsubmitted_plan")
        plan_hash = None
        if plan_id:
            plan = self.plans.get(plan_id)
            if plan["account_id"] != account_id or plan["symbol"] != symbol:
                raise ValueError("forward_plan_account_or_symbol_mismatch")
            definition = plan["definition"]
            reference = definition.get("metadata", {}).get("forward_validation")
            if reference != {"protocol_id": protocol_id, "policy_version": policy_version}:
                raise ValueError("plan_requires_frozen_forward_policy_reference")
            if definition.get("metadata", {}).get("cost_assumptions") != protocol["costs"]:
                raise ValueError("plan_cost_scenario_differs_from_protocol")
            if _model(definition["metadata"]["agent_context"]) != self.policy(policy_version)["model"]:
                raise ValueError("plan_actual_model_differs_from_protocol")
            prior = [r for r in self._records(protocol_id, "decision") if r.get("plan_id") == plan_id]
            if prior and prior[0]["decision_id"] != decision_id:
                raise ValueError("plan_already_bound_to_another_decision")
            if not prior and (plan["state"].get("entry_order_id") or not utc_time(protocol["registered_at"]) <= utc_time(plan["created_at"]) <= utc_time(self.clock())):
                raise ValueError("cannot_attach_plan_after_submission_or_backfill")
            if not definition.get("not_before") or utc_time(definition["not_before"]) < utc_time(protocol["starts_at"]):
                raise ValueError("forward_plan_entry_must_start_after_protocol")
            plan_hash = plan["definition_hash"]
        evidence = self._evidence(account_id, evidence_ids)
        body = {"decision_id": decision_id, "policy_version": policy_version, "action": action, "symbol": symbol,
                "rationale": rationale, "model": _model(model_receipt), "plan_id": plan_id,
                "plan_definition_hash": plan_hash, "evidence": evidence}
        return self._append(protocol, "decision", decision_id, body, deadline=protocol["ends_at"])

    def record_exit_decision(self, *, protocol_id, account_id, decision_id, plan_id,
                             policy_version, model_receipt, evidence_ids, rationale):
        """Retain an explicit close decision without changing the entry policy.

        A different actual model/policy is observed, not rejected. The Host may
        still exit safely; this original experiment cannot claim continuity.
        Frozen automatic stop/target exits need no additional model decision.
        """
        protocol = self.protocol(protocol_id, account_id=account_id)
        decisions = [r for r in self._records(protocol_id, "decision") if r.get("plan_id") == plan_id]
        plan = self.plans.get(plan_id)
        if (not decision_id or not rationale or len(decisions) != 1 or plan["account_id"] != account_id
                or plan["definition_hash"] != decisions[0]["plan_definition_hash"]):
            raise ValueError("exit_decision_requires_original_owned_forward_plan")
        model = _model(model_receipt)
        body = {"decision_id": decision_id, "action": "close", "plan_id": plan_id,
                "plan_definition_hash": plan["definition_hash"], "policy_version": policy_version,
                "model": model, "rationale": rationale, "evidence": self._evidence(account_id, evidence_ids),
                "continuity_verified": policy_version == protocol["policy_version"] and model == self.policy(protocol["policy_version"])["model"]}
        return self._append(protocol, "exit_decision", decision_id, body)

    def seal_outcome(self, *, protocol_id, account_id, plan_id):
        protocol = self.protocol(protocol_id, account_id=account_id)
        decisions = [r for r in self._records(protocol_id, "decision") if r.get("plan_id") == plan_id]
        if len(decisions) != 1:
            raise ValueError("outcome_requires_preregistered_owned_decision")
        decision, plan = decisions[0], self.plans.get(plan_id)
        if plan["account_id"] != account_id or plan["definition_hash"] != decision["plan_definition_hash"]:
            raise ValueError("forward_outcome_plan_binding_mismatch")
        with self.store._connect() as conn:
            row = conn.execute("select payload_json from autonomous_trade_outcomes where plan_id=? and account_id=?", (plan_id, account_id)).fetchone()
            if not row:
                raise ValueError("sealed_owned_outcome_required")
            outcome = json.loads(row[0])
            conn.row_factory = sqlite3.Row
            fills = [dict(r) for r in conn.execute("select * from paper_fills where account_id=? and order_id in (select json_extract(payload_json,'$.entry_intent.order_id') from autonomous_trading_plan_events where plan_id=? union select json_extract(payload_json,'$.exit_intent.order_id') from autonomous_trading_plan_events where plan_id=?) order by created_at,fill_id", (account_id, plan_id, plan_id))]
            allocations = []
            order_ids = sorted({f["order_id"] for f in fills})
            for table, quantity_column, price_field, schema in (
                ("paper_odd_lot_proxy_allocations", "allocated_quantity", "observed_board_trade_price", "open_stock_ai.odd_lot_board_proxy.v1"),
                ("paper_board_trade_allocations", "quantity", "observed_trade_price", "open_stock_ai.board_lot_trade_simulation.v1"),
            ):
                if order_ids and conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
                    placeholders = ",".join("?" for _ in order_ids)
                    allocations.extend({**dict(r), "price_field": price_field, "receipt_schema": schema} for r in conn.execute(
                        f"select order_id,fill_sequence,{quantity_column} as allocated_quantity,receipt_json from {table} where account_id=? and order_id in ({placeholders}) and {quantity_column}>0",
                        (account_id, *order_ids)))
        earliest = max(utc_time(decision["recorded_at"]), utc_time(protocol["starts_at"]))
        if any(not earliest <= utc_time(f["created_at"]) <= utc_time(protocol["ends_at"]) for f in fills):
            raise ValueError("forward_outcome_contains_predecision_or_out_of_window_fill")
        verified = self.outcomes.record(plan=plan, fills=fills, mode="paper")
        if verified != outcome:
            raise ValueError("forward_outcome_ledger_integrity_failure")
        costs = protocol["costs"]
        cost_bound = True
        for order_id in {f["order_id"] for f in fills}:
            order_fills = [f for f in fills if f["order_id"] == order_id]
            gross = sum(f["quantity"]*f["fill_price"] for f in order_fills)
            if sum(f["commission"] for f in order_fills) + .011 < max(costs["minimum_commission"], gross*costs["commission_bps"]/10000):
                cost_bound = False
        for fill in fills:
            reference = float(fill["reference_price"])
            # The OMS reference already embeds the scenario's additional impact;
            # recover the original observed quote from its owned allocation.
            # Each allocation may substantiate at most one actual fill.
            for index, allocation in enumerate(allocations):
                if (allocation["order_id"] != fill["order_id"] or allocation["allocated_quantity"] != fill["quantity"]
                        or fill["fill_id"] != f"PBF-{fill['order_id']}-{allocation['fill_sequence']}"):
                    continue
                receipt = json.loads(allocation["receipt_json"])
                digest = hashlib.sha256(json.dumps({k: v for k, v in receipt.items() if k != "receipt_sha256"},
                                                   sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                stamp = utc_time(receipt["source_identity"]["exchange_timestamp"])
                if (digest == receipt.get("receipt_sha256") and receipt.get("eligible") is True
                        and receipt.get("schema_version") == allocation["receipt_schema"]
                        and receipt.get("simulated_fill_price") == fill["fill_price"]
                        and stamp <= utc_time(fill["created_at"]) <= stamp+timedelta(seconds=60)):
                    reference = float(receipt[allocation["price_field"]])
                    allocations.pop(index)
                    break
            adverse = (fill["fill_price"]/reference-1)*10000*(1 if fill["side"] == "buy" else -1) if reference > 0 else -1
            if adverse + 1e-7 < costs["slippage_bps"] + costs["market_impact_bps"]:
                cost_bound = False
            if fill["side"] == "sell" and fill["tax"] + .011 < fill["quantity"]*fill["fill_price"]*costs["sell_tax_bps"]/10000:
                cost_bound = False
        return self._append(protocol, "outcome", plan_id, {"plan_id": plan_id, "decision_id": decision["decision_id"],
                            "cost_scenario_verified": cost_bound, "outcome": outcome})

    def record_daily_observation(self, *, protocol_id, account_id, scheduled_at, evidence_ids):
        protocol = self.protocol(protocol_id, account_id=account_id)
        slot, now = utc_time(scheduled_at), utc_time(self.clock())
        if slot.isoformat() not in protocol["observation_times"] or not slot <= now <= slot + timedelta(hours=4):
            raise ValueError("daily_observation_must_be_timely_no_historical_backfill")
        account = self._account(account_id)
        evidence = self._evidence(account_id, evidence_ids)
        marks = [e["daily_mark"] for e in evidence if "daily_mark" in e]
        verified_mark = False
        for mark in marks:
            if mark.get("schema_version") != "open_stock_ai.forward_daily_mark.v1" or mark.get("account_id") != account_id or mark.get("scheduled_at") != slot.isoformat():
                continue
            if not mark.get("observed_at") or not slot <= utc_time(mark["observed_at"]) <= now:
                continue
            snapshot = mark.get("snapshot") or {}
            if any(snapshot.get(k) != account.get(k) for k in ("account_id", "cash_balance", "total_equity", "positions")):
                continue
            quotes = {q.get("symbol"): q for q in mark.get("quotes", [])}
            valid = bool(mark.get("market_evidence_ids")) or bool(account["positions"])
            for position in account["positions"]:
                q = quotes.get(position["symbol"], {})
                if q.get("quantity") != position["quantity"] or q.get("price") != position["last_price"] or q.get("source_provenance_verified") is not True:
                    valid = False
                    break
                stamp = q.get("exchange_timestamp")
                if not stamp or utc_time(stamp) > now or utc_time(stamp).astimezone(ZoneInfo("Asia/Taipei")).date() != slot.astimezone(ZoneInfo("Asia/Taipei")).date():
                    valid = False
                    break
            if valid:
                # Identity/hash and membership are rechecked even for cash-only
                # marks; the helper verifies the actual official market day.
                self._evidence(account_id, mark.get("market_evidence_ids", []))
                verified_mark = True
        body = {"scheduled_at": slot.isoformat(), "account_snapshot": account,
                "evidence": evidence, "valuation_verified": verified_mark}
        # Snapshot timestamps may change on retries; compare stored observation
        # by its fixed slot instead of rewriting it with the newer account NAV.
        old = [r for r in self._records(protocol_id, "observation") if r["scheduled_at"] == slot.isoformat()]
        if old:
            return old[0]
        return self._append(protocol, "observation", slot.isoformat(), body)

    def evaluate(self, *, protocol_id, account_id):
        protocol = self.protocol(protocol_id, account_id=account_id)
        with self.store._connect() as conn:
            # Freeze membership and seal once under the same write lock used by
            # append. A concurrent late observation cannot change this sample.
            conn.execute("begin immediate")
            return self._evaluate_locked(protocol, conn)

    def _evaluate_locked(self, protocol, conn):
        protocol_id, account_id = protocol["protocol_id"], protocol["account_id"]
        old = conn.execute("select payload_json from agent_forward_evaluations where protocol_id=?", (protocol_id,)).fetchone()
        if old:
            return json.loads(old[0])
        if utc_time(self.clock()) < utc_time(protocol["evaluate_at"]):
            raise ValueError("fixed_evaluation_time_not_reached")
        decisions = self._records(protocol_id, "decision")
        exits = self._records(protocol_id, "exit_decision")
        observations = self._records(protocol_id, "observation")
        sealed = self._records(protocol_id, "outcome")
        deviations = self._records(protocol_id, "deviation")
        reasons = []
        if self.policy(protocol["policy_version"])["evaluator_source_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
            reasons.append("registered_evaluator_source_changed")
        if deviations:
            reasons.append("protocol_deviation_requires_new_prospective_trial")
        if any(not row["continuity_verified"] for row in exits):
            reasons.append("exit_decision_policy_or_model_continuity_not_verified")
        if [r["scheduled_at"] for r in observations] != protocol["observation_times"]:
            reasons.append("missing_registered_observations_no_cherry_picking")
        if not decisions:
            reasons.append("no_prospective_decisions")
        if any(not row["valuation_verified"] for row in observations):
            reasons.append("daily_account_valuation_not_source_bound")
        required = set(self.policy(protocol["policy_version"])["required_evidence"])
        if any(row["action"] == "propose" and not required <= {e["kind"] for e in row["evidence"]} for row in decisions):
            reasons.append("policy_required_decision_evidence_missing")
        plan_ids = {r["plan_id"] for r in decisions if r.get("plan_id")}
        plans = self.plans.list(account_id=account_id)
        for plan in plans:
            created = utc_time(plan["created_at"])
            if plan["plan_id"] in plan_ids or utc_time(protocol["registered_at"]) <= created <= utc_time(protocol["ends_at"]):
                if plan["plan_id"] not in plan_ids:
                    reasons.append("unregistered_plan_in_experiment_account")
                elif plan["status"] not in {"closed", "cancelled", "rejected", "expired", "invalidated"}:
                    reasons.append("unresolved_plan_or_open_position")
                elif plan["status"] == "closed" and plan["plan_id"] not in {r["plan_id"] for r in sealed}:
                    reasons.append("closed_outcome_not_sealed")
        # Matching total cash alone cannot expose two omitted trades whose PnL
        # happens to cancel. Every account fill must belong to a sealed outcome.
        owned_fills = {f["fill_id"] for row in sealed for f in row["outcome"]["fills"]}
        observed_fills = {row[0] for row in conn.execute(
            "select fill_id from paper_fills where account_id=? and julianday(created_at)>=julianday(?) and julianday(created_at)<=julianday(?)",
            (account_id, protocol["registered_at"], protocol["ends_at"]))}
        if observed_fills != owned_fills:
            reasons.append("account_fills_not_fully_attributed_to_registered_outcomes")
        evidence = [item for row in decisions+exits+observations for item in row["evidence"]]
        if protocol["fixture"] or any(e["fixture"] for e in evidence):
            reasons.append("fixture_observations_not_native_market_evidence")
        if not evidence or not any(e["source_provenance_verified"] and e["source_kind"] in {"exchange_official", "licensed_vendor", "mixed_verified_market"} for e in evidence):
            reasons.append("verified_native_market_source_missing")
        if len(sealed) < protocol["minimum_closed_trades"]:
            reasons.append("insufficient_closed_trades")
        if any(not r["cost_scenario_verified"] for r in sealed):
            reasons.append("observed_execution_cost_below_registered_scenario")
        if len(observations) < protocol["minimum_observation_days"]:
            reasons.append("insufficient_observation_days")
        equity, returns = float(protocol["initial_equity"]), []
        for observation in observations:
            value = _number(observation["account_snapshot"]["total_equity"], "observed_equity", positive=True)
            returns.append((value/equity-1)*100)
            equity = value
        expectation = _bootstrap(returns, alpha=protocol["one_sided_alpha"], block_length=protocol["block_length"],
                                 resamples=protocol["resamples"], seed=int(protocol_id[-12:], 16))
        if protocol["one_sided_alpha"]*protocol["resamples"] < 5:
            reasons.append("multiple_test_tail_resolution_insufficient")
        if expectation["lower_confidence_bound_pct"] is None or expectation["lower_confidence_bound_pct"] <= 0:
            reasons.append("positive_daily_net_expectancy_not_established")
        net_pnl = sum(r["outcome"]["net_pnl"] for r in sealed)
        if net_pnl <= 0 or equity <= protocol["initial_equity"]:
            reasons.append("positive_account_and_closed_trade_net_profit_not_established")
        if observations:
            last = observations[-1]["account_snapshot"]
            if last["position_count"] or abs(last["cash_balance"]-protocol["initial_cash"]-net_pnl) > .02:
                reasons.append("terminal_account_not_flat_or_cashflows_not_attributed")
        body = {"schema_version": SCHEMA, "protocol_id": protocol_id, "account_id": account_id,
                "policy_version": protocol["policy_version"], "protocol_sha256": protocol["receipt_sha256"],
                "evaluated_at": utc_time(self.clock()).isoformat(), "passed": not reasons,
                "positive_ev_qualified": not reasons, "live_execution_eligible": False,
                "qualification_scope": "prospective_paper_policy_under_registered_cost_scenario_and_universe",
                "reasons": sorted(set(reasons)), "daily_net_expectancy": expectation, "closed_trade_count": len(sealed),
                "decision_counts": {action: sum(r["action"] == action for r in decisions) for action in ("propose", "hold", "reject")},
                "explicit_close_decision_count": len(exits),
                "observation_count": len(observations), "net_pnl": net_pnl,
                "account_return_pct": (equity/protocol["initial_equity"]-1)*100,
                "records_sha256": content_hash({"decisions": decisions, "exit_decisions": exits, "observations": observations, "outcomes": sealed, "deviations": deviations}),
                "limitations": ["bootstrap_assumes_weak_dependence_and_stationarity_no_regime_guarantee",
                                "paper_cost_and_matching_assumptions_not_empirically_verified_broker_execution",
                                "cash_benchmark_only_no_claim_of_market_alpha", "not_all_historical_or_external_research_families"]}
        result = _sealed(body)
        conn.execute("insert into agent_forward_evaluations values (?,?)", (protocol_id, json.dumps(result)))
        conn.commit()
        return result

    def verify_qualification(self, receipt, *, account_id, policy_version):
        """Only the exact evaluation already sealed in this Host store qualifies."""
        try:
            if receipt.get("account_id") != account_id or receipt.get("policy_version") != policy_version:
                return False
            with self.store._connect() as conn:
                row = conn.execute("select e.payload_json from agent_forward_evaluations e join agent_forward_protocols p using(protocol_id) where e.protocol_id=? and p.account_id=? and p.policy_version=?", (receipt["protocol_id"], account_id, policy_version)).fetchone()
            return bool(row and json.loads(row[0]) == receipt and receipt["passed"] and receipt["positive_ev_qualified"]
                        and not receipt["reasons"] and receipt["receipt_sha256"] == content_hash({k: v for k, v in receipt.items() if k != "receipt_sha256"}))
        except (ValueError, TypeError, KeyError):
            return False
