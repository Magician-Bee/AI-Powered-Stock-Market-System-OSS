"""Connect real campaign decisions and accounting to prospective validation.

This is Host bookkeeping, not another model or a permission to trade. A failed
experiment remains visible while ordinary paper position protection continues.
"""
from __future__ import annotations

from datetime import datetime, timedelta, time, timezone
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from open_stock_ai.execution.paper_decision_context import paper_decision_policy_metadata
from open_stock_ai.execution.trading_plan import content_hash, utc_time
from open_stock_ai.research.agent_forward_validation import AgentForwardValidation, freeze_agent_policy
from .agent_run_store import TERMINAL_RUN_STATUSES
from .market_intelligence.product_projection import product_assessment


def host_policy_sources(configuration):
    root = Path(__file__).resolve().parents[1]
    groups = {"tools": {}, "execution": {}, "config": {"host_public_configuration": content_hash(configuration)}}
    for package in ("open_stock_ai", "stock_ai"):
        for path in sorted((root / package).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            group = "tools" if "/agent_runtime/" in relative or "/tool_providers/" in relative else "execution"
            groups[group][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("market_data_sources.yaml", "data_availability_contracts.yaml"):
        path = root.parent / "config" / name
        groups["config"][f"config/{name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return groups


def _review_result(result):
    """Retain the decision, not a second copy of the durable tool transcript."""
    compact = {key: result[key] for key in ("status", "summary", "decision") if key in result}
    validation = result.get("completion_validation")
    if isinstance(validation, dict):
        compact["completion_validation"] = {
            "passed": validation.get("passed"), "validation_sha256": content_hash(validation),
            "checks": [{key: check[key] for key in ("name", "passed", "reason", "message") if key in check}
                       for check in validation.get("checks", []) if isinstance(check, dict)]}
    return compact


def _forward_universe(features, *, now=None):
    """Freeze only instruments supported by the discretionary plan builder."""
    accepted, excluded = set(), []
    for row in features:
        symbol = row.get("symbol")
        reasons = product_assessment(row, now=now)["reasons"]
        if reasons:
            excluded.append({"symbol": symbol, "reasons": reasons})
        else:
            accepted.add(symbol)
    return sorted(accepted), {"source_count": len(features), "accepted_count": len(accepted),
                              "excluded_count": len(excluded), "excluded": excluded,
                              "selection_policy": "retained_verified_ordinary_products_at_prospective_registration"}


class AutonomousForwardMonitor:
    def __init__(self, campaign, *, registry=None):
        self.campaign = campaign
        self.account_id = campaign.broker.account_id
        self.registry = registry or AgentForwardValidation(campaign.plans.store)
        self._terminal_revisions = {}
        with campaign.plans.store._connect() as conn:
            conn.execute("""create table if not exists autonomous_forward_reviews (
                account_id text not null, run_id text not null, cycle_id text not null,
                binding_json text not null, primary key(account_id,run_id,cycle_id))""")
            conn.commit()

    def _binding(self, run_id, cycle_id):
        with self.campaign.plans.store._connect() as conn:
            row = conn.execute("select binding_json from autonomous_forward_reviews where account_id=? and run_id=? and cycle_id=?",
                               (self.account_id, run_id, cycle_id)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, run_id, cycle_id, value):
        with self.campaign.plans.store._connect() as conn:
            conn.execute("insert into autonomous_forward_reviews values (?,?,?,?) on conflict(account_id,run_id,cycle_id) do update set binding_json=excluded.binding_json",
                         (self.account_id, run_id, cycle_id, json.dumps(value, ensure_ascii=False, allow_nan=False)))
            conn.commit()
        return value

    def _with_protocol(self, binding, protocol):
        deadline = utc_time(protocol["ends_at"]) - timedelta(minutes=70)
        eligible = protocol.get("status") not in {"evaluated", "abandoned"} and utc_time(self.registry.clock()) < deadline
        value = {**binding, "protocol_id": protocol["protocol_id"], "starts_at": protocol["starts_at"],
                 "ends_at": protocol["ends_at"], "entry_cutoff": deadline.isoformat(),
                 "exit_deadline": deadline.isoformat(), "binding_eligible": eligible,
                 "status": "registered" if eligible else "registration_window_closed"}
        return self._save(binding["run_id"], binding["cycle_id"], value)

    def _freeze(self, *, model_receipt, tool_manifest, prompt_template, max_steps):
        campaign = self.campaign
        configuration = {"costs": campaign.costs, "experiment_limits": campaign.experiment_limits,
                         "max_steps": max_steps,
                         "protocol_terminal_liquidation_at_last_observation_minus70minutes": True,
                         "calendar_snapshot_sha256": getattr(campaign.calendar, "snapshot_sha256", None),
                         "calendar_source": getattr(campaign.calendar, "source", None),
                         "paper_execution_model": getattr(campaign.broker, "paper_quote_execution_model", None),
                         "central_risk": {key: getattr(campaign.risk, key) for key in (
                             "max_position_size_pct", "max_daily_loss_pct", "max_total_drawdown_pct",
                             "max_symbol_exposure_pct", "max_total_paper_exposure_pct", "max_industry_exposure_pct")}}
        return freeze_agent_policy(model_receipt=model_receipt, prompt_template=prompt_template,
            decision_policy={**paper_decision_policy_metadata(), "host_configuration": configuration},
            tool_manifest=tool_manifest, source_hashes=host_policy_sources(configuration),
            required_evidence=["price_history", "cost_model"])

    def prepare(self, *, cycle_id, run, model_receipt, tool_manifest, prompt_template):
        """Freeze the actual model and Host policy before the first decision."""
        campaign, registry = self.campaign, self.registry
        cycle = campaign.cycle(cycle_id)
        policy = self._freeze(model_receipt=model_receipt, tool_manifest=tool_manifest, prompt_template=prompt_template,
                              max_steps=(run.get("request") or {}).get("max_steps"))
        registry.register_policy(policy)
        prior = self._binding(run["run_id"], cycle_id)
        if prior and prior.get("protocol_id"):
            if prior["policy_version"] != policy["policy_version"]:
                registry.record_deviation(protocol_id=prior["protocol_id"], account_id=self.account_id,
                                          reason="host_policy_changed_during_review", deviation_id="policy-change:" + run["run_id"])
                return self._save(run["run_id"], cycle_id,
                                  {**prior, "status": "policy_changed_during_review", "binding_eligible": False})
            if prior["status"] == "policy_changed_during_review":
                return prior
            protocol = next(p for p in registry.list_protocols(account_id=self.account_id)
                            if p["protocol_id"] == prior["protocol_id"])
            return self._with_protocol(prior, protocol)
        binding = {"policy_version": policy["policy_version"], "protocol_id": None, "binding_eligible": False,
                   "run_id": run["run_id"], "cycle_id": cycle_id, "model_receipt": model_receipt,
                   "evidence_ids": [cycle["bulk_evidence_id"], *[item["history_id"] for item in cycle["results"]]],
                   "status": "awaiting_registration"}
        if prior:
            # No protocol ever existed for this attempt. A corrected Host may
            # register prospectively, while retaining the earlier failure.
            attempts = list(prior.get("previous_registration_attempts", []))
            attempt = {key: prior[key] for key in ("policy_version", "status", "error", "universe_selection") if key in prior}
            if attempt not in attempts:
                attempts.append(attempt)
            binding["previous_registration_attempts"] = attempts
        protocols = registry.list_protocols(account_id=self.account_id)
        matching = [p for p in protocols if p["policy_version"] == policy["policy_version"] and p["costs"] == campaign.costs]
        if matching:
            # The same frozen policy does not silently restart a failed or
            # completed trial to search for a more favorable qualification.
            return self._with_protocol(binding, matching[-1])
        active = [p for p in protocols if p["status"] in {"scheduled", "collecting"}]
        for protocol in active:
            registry.record_deviation(protocol_id=protocol["protocol_id"], account_id=self.account_id,
                                      reason="campaign_policy_changed", deviation_id="policy-change:" + policy["policy_version"])
            try:
                registry.abandon_protocol(protocol_id=protocol["protocol_id"], account_id=self.account_id,
                                           reason="new_frozen_policy_requires_a_new_prospective_trial")
            except ValueError as exc:
                binding.update(status="previous_policy_has_unresolved_positions", error=str(exc))
                return self._save(run["run_id"], cycle_id, binding)
        try:
            now = utc_time(registry.clock())
            start = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            local = start.astimezone(ZoneInfo("Asia/Taipei"))
            day, slots = local.date(), []
            # This schedule is frozen before returns exist. Failed or missed
            # daily observations cannot later be filled from historical data.
            while len(slots) < 120:
                slot = datetime.combine(day, time(14, 35), tzinfo=ZoneInfo("Asia/Taipei"))
                if slot > start and campaign.calendar.day_status(day)["trading_day"]:
                    slots.append(slot.astimezone(timezone.utc).isoformat())
                day += timedelta(days=1)
            features = campaign._evidence(cycle["bulk_evidence_id"], "market_screen")["features"]
            symbols, selection = _forward_universe(features, now=now)
            selection_id = campaign._retain("forward_universe_selection", {
                **selection, "symbols": symbols, "source_evidence_id": cycle["bulk_evidence_id"],
                "cycle_id": cycle_id, "policy_version": policy["policy_version"]})
            binding["universe_selection"] = {key: value for key, value in selection.items() if key != "excluded"}
            binding["universe_selection"]["evidence_id"] = selection_id
            binding["evidence_ids"].append(selection_id)
            protocol = registry.register_protocol(account_id=self.account_id, policy_version=policy["policy_version"],
                symbols=symbols, costs=campaign.costs, starts_at=start, ends_at=slots[-1],
                evaluate_at=utc_time(slots[-1]) + timedelta(hours=4), observation_times=slots)
            return self._with_protocol(binding, protocol)
        except (ValueError, KeyError) as exc:
            binding.update(status="registration_requires_inputs", error=str(exc))
        return self._save(run["run_id"], cycle_id, binding)

    def record_proposal(self, *, binding, plan, model_receipt):
        costs = self.campaign._retain("cost_model", {"cost_assumptions": plan["definition"]["metadata"]["cost_assumptions"],
                                                   "basis": "registered_paper_cost_scenario_not_empirical_execution"})
        return self.registry.record_decision(protocol_id=binding["protocol_id"], account_id=self.account_id,
            decision_id="plan:" + plan["plan_id"], policy_version=binding["policy_version"], action="propose",
            model_receipt=model_receipt, evidence_ids=[*plan["definition"]["evidence_ids"], costs],
            rationale=plan["definition"]["rationale"], symbol=plan["symbol"], plan_id=plan["plan_id"])

    def record_rejection(self, *, binding, proposal, reason, model_receipt):
        if not binding or not binding.get("binding_eligible"):
            return None
        evidence_id = self.campaign._retain("agent_proposal_rejection", {
            "run_id": binding["run_id"], "cycle_id": binding["cycle_id"], "proposal": proposal, "reason": reason})
        return self.registry.record_decision(protocol_id=binding["protocol_id"], account_id=self.account_id,
            decision_id="reject:" + evidence_id, policy_version=binding["policy_version"],
            action="reject", model_receipt=model_receipt, evidence_ids=[*binding["evidence_ids"], evidence_id],
            rationale=reason)

    def record_exit(self, *, plan, evidence_id, model_receipt, rationale):
        """Keep exits attributable even when continuity cannot qualify the trial."""
        reference = plan["definition"].get("metadata", {}).get("forward_validation")
        if not reference:
            return None
        policy_version = "continuity_unverified"
        with self.campaign.plans.store._connect() as conn:
            rows = conn.execute("select binding_json from autonomous_forward_reviews where account_id=? and run_id=?",
                                (self.account_id, model_receipt["run_id"])).fetchall()
        bindings = [json.loads(row[0]) for row in rows]
        known = [b for b in bindings if b.get("status") in {"registered", "registration_window_closed"}]
        if known and len({b["policy_version"] for b in known}) == 1:
            frozen = self.registry.policy(known[0]["policy_version"])
            current = self._freeze(model_receipt=model_receipt, prompt_template=frozen["prompt_template"],
                tool_manifest=frozen["tool_manifest"], max_steps=frozen["decision_policy"]["host_configuration"]["max_steps"])
            self.registry.register_policy(current)
            policy_version = current["policy_version"]
        return self.registry.record_exit_decision(protocol_id=reference["protocol_id"], account_id=self.account_id,
            decision_id="exit:" + evidence_id, plan_id=plan["plan_id"], policy_version=policy_version,
            model_receipt=model_receipt, evidence_ids=[evidence_id], rationale=rationale)

    def capture_review(self, *, cycle_id, run, model_receipt):
        """Persist the actual terminal review, including cash and failed reviews."""
        binding = self._binding(run["run_id"], cycle_id)
        if (not binding or not binding.get("protocol_id")
                or binding.get("status") not in {"registered", "registration_window_closed"}
                or utc_time(self.registry.clock()) > utc_time(binding["ends_at"])
                or run.get("status") not in TERMINAL_RUN_STATUSES):
            return None
        result = run.get("result") or {}
        actual_model = {"driver_id": model_receipt.get("driver_id", "codex"),
                        "provider_model_metadata": model_receipt.get("provider_model_metadata", model_receipt)}
        evidence_id = self.campaign._retain("agent_forward_review", {
            "run_id": run["run_id"], "cycle_id": cycle_id, "status": run["status"],
            "completed_at": run.get("completed_at"), "result_sha256": content_hash(result),
            "result": _review_result(result), "model_receipt": actual_model,
            "error_sha256": content_hash(run.get("error")),
            "error": {key: run["error"][key] for key in ("type", "message", "category") if key in run["error"]}
                     if isinstance(run.get("error"), dict) else None})
        proposals = [r for r in self.registry.records(protocol_id=binding["protocol_id"], account_id=self.account_id, kind="decision")
                     if r.get("action") == "propose" and r.get("plan_id") and
                     (self.campaign.plans.get(r["plan_id"])["definition"].get("metadata", {}).get("agent_context", {}).get("run_id") == run["run_id"])]
        action = "hold" if run["status"] == "completed" else "reject"
        return self.registry.record_decision(protocol_id=binding["protocol_id"], account_id=self.account_id,
            decision_id="review:" + run["run_id"] + ":" + evidence_id, policy_version=binding["policy_version"], action=action,
            model_receipt=model_receipt, evidence_ids=[*binding["evidence_ids"], evidence_id],
            rationale=("review_completed_with_no_additional_orders" if proposals else "review_ended_without_an_accepted_plan") + ":" + run["status"])

    def reconcile_reviews(self, run_store):
        """Capture only already-bound runs, including failures before activation."""
        with self.campaign.plans.store._connect() as conn:
            rows = conn.execute("select run_id,cycle_id from autonomous_forward_reviews where account_id=?",
                                (self.account_id,)).fetchall()
        captured, errors, loaded = 0, [], {}
        for run_id, cycle_id in rows:
            try:
                with run_store._connect() as conn:
                    header = conn.execute("select status,updated_at from agent_runs where run_id=?", (run_id,)).fetchone()
                if not header or header[0] not in TERMINAL_RUN_STATUSES:
                    continue
                revision = tuple(header)
                if self._terminal_revisions.get((run_id, cycle_id)) == revision:
                    continue
                if run_id not in loaded:
                    loaded[run_id] = run_store.get_run(run_id)
                run = loaded[run_id]
                binding = self._binding(run_id, cycle_id)
                model = (run.get("result") or {}).get("provider_model_metadata") or binding["model_receipt"]
                receipt = self.capture_review(cycle_id=cycle_id, run=run, model_receipt=model)
                captured += int(receipt is not None)
                self._terminal_revisions[(run_id, cycle_id)] = revision
            except (ValueError, KeyError, RuntimeError) as exc:
                errors.append({"run_id": run_id, "cycle_id": cycle_id, "error": str(exc)})
        return {"captured": captured, "errors": errors, "model_calls": 0}

    def status(self):
        protocols = self.registry.list_protocols(account_id=self.account_id)
        rows = []
        for protocol in protocols:
            with self.campaign.plans.store._connect() as conn:
                counts = dict(conn.execute("select kind,count(*) from agent_forward_records where protocol_id=? group by kind",
                                           (protocol["protocol_id"],)).fetchall())
            rows.append({key: protocol.get(key) for key in ("protocol_id", "policy_version", "status", "starts_at", "ends_at", "evaluate_at", "evaluation")}
                        | {"minimum_observation_days": protocol["minimum_observation_days"],
                           "minimum_closed_trades": protocol["minimum_closed_trades"],
                           "observations": counts.get("observation", 0), "decisions": counts.get("decision", 0),
                           "outcomes": counts.get("outcome", 0), "deviations": counts.get("deviation", 0),
                           "qualification_continuity_intact": counts.get("deviation", 0) == 0})
        return {"schema_version": "open_stock_ai.autonomous_forward_status.v1", "protocols": rows,
                "live_execution_eligible": False, "scope": "each_frozen_paper_policy_and_registered_cost_scenario"}

    async def sync(self):
        """Bookkeep outcomes and due observations; no model or order dispatch."""
        now = utc_time(self.registry.clock())
        errors = []
        for protocol in self.registry.list_protocols(account_id=self.account_id):
            if protocol["status"] in {"abandoned", "evaluated"}:
                continue
            pid = protocol["protocol_id"]
            if now >= utc_time(protocol["evaluate_at"]):
                self.registry.evaluate(protocol_id=pid, account_id=self.account_id)
                continue
            policy_changed = False
            try:
                frozen = self.registry.policy(protocol["policy_version"])
                current = self._freeze(model_receipt=frozen["model"], tool_manifest=frozen["tool_manifest"],
                    prompt_template=frozen["prompt_template"],
                    max_steps=frozen["decision_policy"]["host_configuration"]["max_steps"])
                if current["policy_version"] != protocol["policy_version"]:
                    self.registry.record_deviation(protocol_id=pid, account_id=self.account_id,
                        reason="host_execution_policy_changed_during_forward_window",
                        deviation_id="host-policy:" + current["policy_version"])
                    policy_changed = True
            except (ValueError, KeyError, RuntimeError) as exc:
                # Trading already proceeded through its own protection gates.
                # Unknown continuity cannot become qualified evidence.
                errors.append({"protocol_id": pid, "stage": "policy_continuity", "error": str(exc)})
                continue
            records = self.registry.records(protocol_id=pid, account_id=self.account_id)
            sealed = {r["plan_id"] for r in records["outcome"]}
            for record in records["decision"]:
                if record.get("action") != "propose" or record["plan_id"] in sealed:
                    continue
                plan = self.campaign.plans.get(record["plan_id"])
                if plan["status"] == "closed":
                    try:
                        self.registry.seal_outcome(protocol_id=pid, account_id=self.account_id, plan_id=plan["plan_id"])
                    except ValueError as exc:
                        errors.append({"protocol_id": pid, "stage": "outcome", "error": str(exc)})
            if policy_changed or records["deviation"]:
                try:
                    self.registry.abandon_protocol(protocol_id=pid, account_id=self.account_id,
                        reason="frozen_policy_changed; start_new_prospective_trial_after_account_is_flat")
                except ValueError as exc:
                    if str(exc) != "abandonment_requires_flat_account_without_active_plans":
                        errors.append({"protocol_id": pid, "stage": "policy_retirement", "error": str(exc)})
                # Once continuity is broken, later observations cannot repair
                # the preregistered experiment. Keep managing owned risk, but
                # do not spend more daily evidence on this invalid protocol.
                continue
            observed = {r["scheduled_at"] for r in records["observation"]}
            for slot in protocol["observation_times"]:
                if slot in observed or not utc_time(slot) <= now <= utc_time(slot) + timedelta(hours=4):
                    continue
                try:
                    from open_stock_ai.execution.forward_daily_mark import retain_forward_daily_mark
                    mark = await retain_forward_daily_mark(self.campaign, scheduled_at=slot, now=now)
                    if mark["status"] != "ready":
                        errors.append({"protocol_id": pid, "stage": "observation", "scheduled_at": slot,
                                       "status": mark["status"], "blockers": mark["blockers"]})
                        continue
                    self.registry.record_daily_observation(protocol_id=pid, account_id=self.account_id,
                                                          scheduled_at=slot, evidence_ids=mark["evidence_ids"])
                except (ValueError, KeyError, RuntimeError) as exc:
                    errors.append({"protocol_id": pid, "stage": "observation", "error": str(exc)})
        return {"errors": errors, "model_calls": 0}
