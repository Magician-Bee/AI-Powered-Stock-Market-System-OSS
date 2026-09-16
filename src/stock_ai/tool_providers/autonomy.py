from __future__ import annotations

import math
from typing import Any

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec
from open_stock_ai.agent_runtime.autonomy_contract import CAMPAIGN_MUTATIONS, campaign_execution_authorized
from open_stock_ai.execution.agent_plan_proposal import AGENT_PLAN_PROPOSAL_SCHEMA


class AutonomousTradingToolProvider:
    provider_id = "autonomous_trading"

    def __init__(self):
        self._specs = {s.name: s for s in (
            AgentToolSpec(name="autonomy.status", category="paper",
                          description="Read the isolated campaign broker account, own paper decision gates, forward validation and durable plan index. Use this account for sizing. If plan details are abbreviated, pass plan_ids from plan_index to inspect those plans without another model task; account totals remain whole-account.",
                          input_schema={"type": "object", "properties": {
                              "plan_ids": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True,
                                           "items": {"type": "string", "minLength": 1}}}, "additionalProperties": False}),
            AgentToolSpec(name="autonomy.evidence", category="market_research",
                          description="Read account-retained evidence without fetching again. For market comparison, pass up to 20 evidence_ids with view=summary in one call; it returns compact technical/source summaries without raw bars. Then request detailed bars only for finalists. A single evidence_id defaults to a recent-bar detail view. Retained records and hashes remain unchanged.",
                          input_schema={"type": "object", "properties": {
                              "evidence_id": {"type": "string", "minLength": 1},
                              "evidence_ids": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True,
                                               "items": {"type": "string", "minLength": 1}},
                              "view": {"type": "string", "enum": ["detail", "summary"]},
                              "bar_limit": {"type": "integer", "minimum": 10, "maximum": 250},
                              "symbols": {"type": "array", "maxItems": 20, "items": {"type": "string"}}},
                              "oneOf": [{"required": ["evidence_id"]}, {"required": ["evidence_ids"]}],
                              "additionalProperties": False}),
            AgentToolSpec(name="autonomy.coverage", category="market_research",
                          description="Read the persistent per-security coverage ledger without network access. It distinguishes never researched, current, stale, failed and identity-unattributed deep research, and reports each required data domain with source, evidence time, expiry and next update need. Filter for missing or stale domains to choose up to 20 canonical symbols for autonomy.research; a ledger row is coverage state, not proof that every domain is complete.",
                          input_schema={"type": "object", "properties": {
                              "symbols": {"type": "array", "maxItems": 20, "uniqueItems": True,
                                          "items": {"type": "string", "pattern": "^[0-9]{4}[A-Z0-9]{0,2}\\.TW(?:O)?$"}},
                              "deep_status": {"type": "string", "enum": ["never_researched", "current", "stale", "failed", "not_attributed"]},
                              "domain": {"type": "string", "enum": ["identity", "daily_price", "intraday_price", "order_book", "price_history", "financials", "revenue", "ownership_flows", "news_events", "industry", "cross_market"]},
                              "needs_update": {"type": "boolean", "description": "Requires domain. True returns rows whose selected domain is missing, partial, stale or conflicted."},
                              "new_entry_eligible": {"type": "boolean"},
                              "after": {"type": "string", "description": "Opaque next_after cursor from the previous response."},
                              "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
                              "additionalProperties": False}),
            AgentToolSpec(name="autonomy.research", category="market_research",
                          timeout_seconds=600, long_running=True,
                          description="Read an owned cycle by cycle_id, or run one whole-market bulk scan with up to 20 official-history reviews. Supply canonical Taiwan instruments outside the previous cycle; omitted symbols use coverage rotation. Requested symbols must appear exactly once in the current official market screen and any Host instrument scope. Unknown and non-stock products remain researchable when their history source supports them; only verified supported ordinary stocks receive stock-cost candidate evaluations or new trading plans. Missing bulk quotes do not prevent history research. Proposals and execution recheck current Host product identity, sources and risk. Use the returned cycle_id for proposals and activation in this same model run. Reports verified histories separately from fixed-strategy evaluation failures and product restrictions. No extra LLM calls or increase to the existing research limit; one cycle is not full-market deep coverage.",
                          input_schema={"type": "object", "properties": {"deep_limit": {"type": "integer", "minimum": 1, "maximum": 20},
                                        "symbols": {"type": "array", "minItems": 1, "maxItems": 20, "uniqueItems": True,
                                                    "items": {"type": "string", "pattern": "^[0-9]{4}[A-Z0-9]{0,2}\\.TW(?:O)?$"},
                                                    "description": "Explicit research candidates, at most deep_limit. Canonical .TW/.TWO symbols only. Cannot be combined with cycle_id."},
                                        "cycle_id": {"type": "string", "description": "Read a retained cycle without downloading data again; omit to start new research."}}, "additionalProperties": False}),
            AgentToolSpec(name="autonomy.propose_plan", category="paper", mutating=True, requires_paper_execution=True,
                          description="Retain your own evidence-based trading plan from a researched symbol: choose shares or equity percent (bounded experiment <=5%), timing, trigger, stop, target and exit deadline. Cite owned campaign AE evidence IDs or successful read-only tool call IDs from this run; Host resolves call references to retained evidence. Use technical, fundamental, chip and event tools to form your rationale; frozen candidate signals are comparative evidence, not mandatory choices. Unproven positive EV does not by itself prohibit this authorized paper experiment. Other strategy engines' research permission flags are diagnostic; this plan still requires its own Host authorization, source checks, account risk, costs and executable quote before submission. Returns a durable plan, not a fill or proof of positive EV. Requires subsequent autonomy.activate(use_candidate_plans=false).",
                          input_schema=AGENT_PLAN_PROPOSAL_SCHEMA),
            AgentToolSpec(name="autonomy.close_plan", category="paper", mutating=True, requires_paper_execution=True,
                          description="Reassess and cancel a waiting owned plan, or request reduction of its actual filled position through the same broker pipeline. Persist why the thesis changed; never opens a short or liquidates another account.",
                          input_schema={"type": "object", "required": ["plan_id", "rationale"], "properties": {
                              "plan_id": {"type": "string", "minLength": 1}, "rationale": {"type": "string", "minLength": 1, "maxLength": 12000}}, "additionalProperties": False}),
            AgentToolSpec(name="autonomy.activate", category="paper", mutating=True, requires_paper_execution=True,
                          description="Activate a retained research cycle in the isolated bounded paper campaign. This persistent control and future model reviews cover the whole account, so a whole-market mandate is required; instrument-only mandates cannot activate it. Host freezes quantity, timing and protection; positive EV is reported separately from experiment permission.",
                          input_schema={"type": "object", "required": ["cycle_id"], "properties": {"cycle_id": {"type": "string", "minLength": 1},
                              "use_candidate_plans": {"type": "boolean", "default": False, "description": "Explicitly select the frozen candle baseline if desired. Default activates only plans you proposed; no valid setup may remain waiting with zero plans."}}, "additionalProperties": False}),
            AgentToolSpec(name="autonomy.manage", category="paper", mutating=True, requires_paper_execution=True,
                          description="Reconcile and manage every active campaign plan once using actual quotes and broker receipts. Waiting conditions do not spend model tokens. Does not fabricate fills to force a test pass.",
                          input_schema={"type": "object", "properties": {}, "additionalProperties": False}),
        )}

    def manifest(self):
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name):
        return name in self._specs

    def describe(self):
        return {"configured": True, "mode": "paper", "shared_broker_plan_pipeline": True, "live_enabled": False}

    @staticmethod
    def _evidence_summary(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        rows = payload.get("rows")
        if kind == "price_history" and isinstance(rows, list) and rows:
            clean = [row for row in rows if isinstance(row, dict) and isinstance(row.get("close"), (int, float))
                     and math.isfinite(float(row["close"])) and float(row["close"]) > 0]
            if not clean:
                return {"bar_count": len(rows), "usable_bar_count": 0}
            closes = [float(row["close"]) for row in clean]
            volumes = [float(row.get("volume") or 0) for row in clean]
            def change(period):
                return round((closes[-1] / closes[-1-period] - 1) * 100, 6) if len(closes) > period else None
            deltas = [right-left for left, right in zip(closes, closes[1:])][-14:]
            gains = sum(max(0.0, value) for value in deltas)
            losses = sum(max(0.0, -value) for value in deltas)
            rsi = None if not deltas else (100.0 if losses == 0 else 100 - 100 / (1 + gains / losses))
            ranges = []
            for index, row in enumerate(clean[-14:]):
                high, low = float(row.get("high") or row["close"]), float(row.get("low") or row["close"])
                previous = closes[max(0, len(clean)-14+index-1)]
                ranges.append(max(high-low, abs(high-previous), abs(low-previous)))
            source = payload.get("data_evidence") or {}
            return {
                "symbol": source.get("symbol"), "bar_count": len(rows), "usable_bar_count": len(clean),
                "first_bar": clean[0].get("timestamp"), "last_bar": clean[-1].get("timestamp"),
                "latest_ohlcv": {key: clean[-1].get(key) for key in ("open", "high", "low", "close", "volume")},
                "return_pct": {str(period): change(period) for period in (5, 20, 60)},
                "sma": {str(period): round(sum(closes[-period:]) / period, 6) if len(closes) >= period else None
                        for period in (20, 60)},
                "rsi14": round(rsi, 6) if rsi is not None else None,
                "atr14": round(sum(ranges) / len(ranges), 6) if ranges else None,
                "volume_ratio20": (round(volumes[-1] / (sum(volumes[-20:]) / min(20, len(volumes))), 6)
                                   if volumes and sum(volumes[-20:]) > 0 else None),
                "source": {key: source.get(key) for key in ("source_id", "source_kind", "symbol",
                           "source_provenance_verified", "instrument_identity_verified", "coverage_complete")},
            }
        features = payload.get("features")
        if kind == "market_screen" and isinstance(features, list):
            return {"feature_count": len(features),
                    "symbols_with_close": sum(isinstance(item.get("close"), (int, float)) for item in features if isinstance(item, dict)),
                    "data_as_of": sorted({str(item.get("data_as_of")) for item in features
                                          if isinstance(item, dict) and item.get("data_as_of")})[-5:]}
        return {key: value for key, value in payload.items()
                if isinstance(value, (str, int, float, bool)) or value is None}

    @classmethod
    def _evidence_view(cls, service, evidence_id: str, *, summary: bool, bar_limit: int, symbols: list[str]) -> dict[str, Any]:
        from open_stock_ai.execution.agent_campaign_actions import retained_records
        from open_stock_ai.execution.trading_plan import content_hash
        saved = retained_records(service, [evidence_id])[evidence_id]
        payload = dict(saved["payload"])
        view = {"evidence_id": evidence_id, "kind": saved["kind"],
                "retained_payload_sha256": content_hash(payload)}
        if summary:
            view["summary"] = cls._evidence_summary(saved["kind"], payload)
            return view
        if isinstance(payload.get("rows"), list):
            view["retained_bar_count"] = len(payload["rows"])
            payload["rows"] = payload["rows"][-bar_limit:]
            view["is_partial_view"] = len(payload["rows"]) < view["retained_bar_count"]
        if isinstance(payload.get("features"), list):
            features = payload["features"]
            requested = set(symbols)
            payload["features"] = [item for item in features if not requested or item.get("symbol") in requested][:20]
            view.update(retained_feature_count=len(features), is_partial_view=len(payload["features"]) < len(features))
        view["payload_view"] = payload
        return view

    async def execute(self, name: str, arguments: dict[str, Any], context: AgentRunContext):
        from stock_ai.autonomous_trading_service import get_autonomous_campaign
        if name not in self._specs:
            raise ValueError("unknown_autonomy_tool")
        if self._specs[name].requires_paper_execution and not context.allow_paper_orders:
            raise PermissionError("autonomous_campaign_requires_paper_execution")
        if name in CAMPAIGN_MUTATIONS and not campaign_execution_authorized(context):
            raise PermissionError("autonomous_campaign_requires_explicit_host_authorization")
        entry_symbols = frozenset(str(symbol).strip().upper() for symbol in context.symbols)
        if context.symbols and name == "autonomy.propose_plan" and str(arguments.get("symbol") or "").strip().upper() not in entry_symbols:
            raise PermissionError("autonomous_plan_symbol_outside_host_instrument_scope")
        if context.symbols and name == "autonomy.activate":
            raise PermissionError("instrument_mandate_cannot_activate_whole_account_campaign: persistent activation and future model reviews require a whole-market mandate")
        research_symbols = None
        if name == "autonomy.research":
            # Tool-schema validation does not enforce every JSON Schema keyword.
            # Validate before service initialization or any data fetch, including
            # direct provider calls and the mutually exclusive readback path.
            from open_stock_ai.execution.autonomous_campaign import validate_research_symbols
            if "cycle_id" in arguments:
                if not isinstance(arguments["cycle_id"], str) or not arguments["cycle_id"].strip():
                    raise ValueError("research_cycle_id_required")
                if "symbols" in arguments:
                    raise ValueError("research_symbols_cannot_combine_with_cycle_id")
            if "symbols" in arguments and arguments["symbols"] is None:
                raise ValueError("research_symbols_must_be_nonempty_list")
            research_symbols = validate_research_symbols(
                arguments.get("symbols"), deep_limit=arguments.get("deep_limit", 20))
            if research_symbols is not None and entry_symbols and not set(research_symbols) <= entry_symbols:
                raise PermissionError("autonomous_research_symbol_outside_host_instrument_scope")
        management_scope = {"entry_symbols": entry_symbols} if context.symbols else {}
        service = get_autonomous_campaign()
        if name == "autonomy.status":
            from stock_ai.autonomous_trading_service import autonomous_status_snapshot
            authorized = bool(context.allow_paper_orders and campaign_execution_authorized(context))
            if authorized:
                from stock_ai.autonomous_trading_service import get_autonomous_model_review
                run = get_autonomous_model_review(service).runtime.store.get_run(context.run_id) or {}
                try:
                    service.assert_activation_authorized(authorized_at=run.get("created_at"))
                except (ValueError, PermissionError):
                    authorized = False
            snapshot = await autonomous_status_snapshot(service, authorized=authorized)
            if arguments.get("plan_ids"):
                requested = set(arguments["plan_ids"])
                snapshot["retained_plan_count"] = len(snapshot["plans"])
                snapshot["plans"] = [plan for plan in snapshot["plans"] if plan["plan_id"] in requested]
                snapshot["requested_plan_ids"] = list(arguments["plan_ids"])
                snapshot["missing_plan_ids"] = sorted(requested - {p["plan_id"] for p in snapshot["plans"]})
            return snapshot
        if name == "autonomy.evidence":
            if arguments.get("evidence_ids"):
                if arguments.get("view", "summary") != "summary":
                    raise ValueError("batch_evidence_requires_summary_view")
                items = [self._evidence_view(service, evidence_id, summary=True, bar_limit=0, symbols=[])
                         for evidence_id in arguments["evidence_ids"]]
                return {"schema_version": "open_stock_ai.autonomy_evidence_batch.v1",
                        "view": "summary", "count": len(items), "evidence": items}
            return self._evidence_view(service, arguments["evidence_id"],
                summary=arguments.get("view") == "summary",
                bar_limit=int(arguments.get("bar_limit", 60)), symbols=list(arguments.get("symbols", [])))
        if name == "autonomy.coverage":
            return service.coverage_ledger.query(
                symbols=arguments.get("symbols"),
                deep_status=arguments.get("deep_status"),
                domain=arguments.get("domain"),
                needs_update=arguments.get("needs_update"),
                new_entry_eligible=arguments.get("new_entry_eligible"),
                after=arguments.get("after"),
                limit=arguments.get("limit", 50),
            )
        if name == "autonomy.research":
            if arguments.get("cycle_id"):
                return service.cycle(arguments["cycle_id"])
            return await service.research(deep_limit=arguments.get("deep_limit", 20),
                                          **({"symbols": list(research_symbols)} if research_symbols is not None else {}))
        host_context = {"run_id": context.run_id, "session_id": context.session_id, "driver_id": context.driver_id,
                        "provider_model_metadata": context.state.get("provider_model_metadata") or {}}
        from stock_ai.autonomous_trading_service import get_autonomous_model_review
        from .autonomy_model_context import retain_model_invocation_context
        model_context = None

        def attach_model_context():
            evidence = retain_model_invocation_context(
                service, name=name, context=context,
                run_store_loader=lambda: get_autonomous_model_review(service).runtime.store,
            )
            host_context.update({key: evidence[key] for key in ("context_receipt_id", "tool_call_id") if key in evidence})
            return evidence
        if name in {"autonomy.propose_plan", "autonomy.activate"}:
            from stock_ai.autonomous_trading_service import get_autonomous_model_review
            review = get_autonomous_model_review(service)
            run = review.runtime.store.get_run(context.run_id) or {}
            authorized_at = run.get("created_at")
            service.assert_activation_authorized(authorized_at=authorized_at)
            background_review = ((run.get("request") or {}).get("metadata") or {}).get("autonomous_model_review")
            if background_review and (not service.status()["enabled"] or not review.status()["enabled"]):
                raise PermissionError("autonomous_campaign_paused_since_review_started")
            if name == "autonomy.propose_plan" and arguments.get("evidence_ids"):
                from .autonomy_evidence import resolve_proposal_evidence_ids
                arguments = {**arguments, "evidence_ids": resolve_proposal_evidence_ids(
                    service, identifiers=arguments["evidence_ids"], context=context, run_store=review.runtime.store)}
            forward_binding = (review.prepare_forward_review(cycle_id=arguments["cycle_id"], run=run,
                                                             model_receipt=host_context)
                               if getattr(service, "forward_monitor", None) else None)
        if name == "autonomy.propose_plan":
            from open_stock_ai.execution.agent_campaign_actions import propose_plan
            model_context = attach_model_context()
            plan = await propose_plan(service, arguments, host_context=host_context,
                                      **({"forward_binding": forward_binding} if forward_binding else {}))
            receipt = {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper", "action": name,
                       "account_id": service.broker.account_id, "cycle_id": arguments["cycle_id"], "plan": plan,
                       "forward_validation": forward_binding}
        elif name == "autonomy.close_plan":
            from open_stock_ai.execution.agent_campaign_actions import close_plan
            owned = service.plans.get(arguments["plan_id"])
            if owned["account_id"] != service.broker.account_id:
                raise ValueError("plan_broker_account_mismatch")
            rationale = arguments["rationale"]
            if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 12000:
                raise ValueError("agent_exit_rationale_required")
            if getattr(service, "forward_monitor", None):
                from stock_ai.autonomous_trading_service import get_autonomous_model_review
                review = get_autonomous_model_review(service)
                run = review.runtime.store.get_run(context.run_id) or {}
                current = ((run.get("request") or {}).get("metadata") or {}).get("autonomous_model_review") or {}
                cycle_id = current.get("cycle_id") or owned["definition"].get("metadata", {}).get("cycle_id")
                if cycle_id:
                    review.prepare_forward_review(cycle_id=cycle_id, run=run, model_receipt=host_context)
            model_context = attach_model_context()
            request = close_plan(service, plan_id=arguments["plan_id"], rationale=arguments["rationale"], host_context=host_context)
            receipt = {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper", "action": name,
                       "account_id": service.broker.account_id, "exit_request": request, "management": await service.manage(**management_scope),
                       "plan": service.plans.get(arguments["plan_id"])}
        elif name == "autonomy.activate":
            cycle = service.cycle(arguments["cycle_id"])
            from datetime import datetime, timezone
            from open_stock_ai.execution.trading_plan import utc_time
            if not 0 <= (datetime.now(timezone.utc) - utc_time(cycle["created_at"])).total_seconds() <= 86400:
                raise ValueError("research_cycle_requires_refresh")
            background_review = ((run.get("request") or {}).get("metadata") or {}).get("autonomous_model_review")
            if background_review and (not service.status()["enabled"] or not review.status()["enabled"]):
                raise PermissionError("autonomous_campaign_paused_since_review_started")
            if arguments.get("use_candidate_plans", False):
                result = await service.create_plans(cycle_id=arguments["cycle_id"])
                if result.get("status") == "account_busy":
                    raise RuntimeError("autonomous_planning_account_busy")
            else:
                result = {"cycle_id": cycle["cycle_id"], "plans": [p for p in service.status()["plans"]
                          if p["definition"].get("metadata", {}).get("cycle_id") == cycle["cycle_id"]],
                          "skipped": [], "selection": "agent_discretionary"}
            review.register_current_review(cycle_id=cycle["cycle_id"], run_id=context.run_id,
                                           provider_model_metadata=context.state.get("provider_model_metadata"))
            service.configure(enabled=True, authorized_at=authorized_at)
            review.configure(enabled=True, authorized_at=authorized_at)
            receipt = {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper", "action": name,
                       "account_id": service.broker.account_id, **result, "forward_validation": forward_binding,
                       "model_review": review.status(), "management": await service.manage(**management_scope)}
        else:
            receipt = {"schema_version": "open_stock_ai.autonomy_tool_receipt.v1", "mode": "paper", "action": name,
                       "account_id": service.broker.account_id, "management": await service.manage(**management_scope)}
        # Activation retains provenance only after its existing stop/pause
        # checks and successful configuration. Rejected activation must not
        # create a new receipt or an apparent successful control action.
        receipt["model_invocation_context"] = model_context if model_context is not None else attach_model_context()
        receipt["campaign_receipt_id"] = service._retain("tool_execution", {"arguments": arguments, "result": receipt})
        return receipt
