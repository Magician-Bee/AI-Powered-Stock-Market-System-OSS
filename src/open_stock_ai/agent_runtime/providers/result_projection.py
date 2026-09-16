"""Bound tool evidence for model context without replacing its body with null."""

from __future__ import annotations

import json
import re
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _fields(value: Any, keys: tuple[str, ...]) -> Any:
    return {key: value[key] for key in keys if key in value} if isinstance(value, dict) else value


def _research_pack(value: dict[str, Any]) -> dict[str, Any]:
    history = list(value.get("recent_history") or [])
    projected = _fields(value, ("schema_version", "symbol", "generated_at"))
    projected["history_window"] = {
        "returned_points": len(history),
        "first_date": history[0].get("date") if history and isinstance(history[0], dict) else None,
        "last_date": history[-1].get("date") if history and isinstance(history[-1], dict) else None,
    }
    # Quantitative evidence and its provenance precede long narrative payloads.
    projected["technical_features"] = value.get("technical_features")
    projected["market_price"] = _fields(value.get("market_price"), (
        "symbol", "name", "price", "price_source", "source_kind", "source_timestamp",
        "trading_state", "is_realtime", "is_fallback", "source_envelope",
        "execution_eligibility", "freshness_note", "reliability_note",
    ))
    projected["recent_history"] = history[-5:]
    projected["pipeline_workspace"] = value.get("pipeline_workspace")
    projected["recent_events"] = value.get("recent_events")
    for key in ("paper_position", "paper_account_summary", "learning_for_symbol"):
        if key in value:
            projected[key] = value[key]
    return projected


def _web_research(value: dict[str, Any]) -> dict[str, Any]:
    projected = _fields(value, ("schema_version", "source_count", "search_result_count"))
    projected["sources"] = [
        _fields(item, (
            "title", "url", "final_url", "published_at", "content_type", "content",
            "text", "excerpt", "search_snippet", "truncated",
        ))
        for item in list(value.get("sources") or [])[:12]
    ]
    for key in ("query", "search_providers", "fetch_failures", "search_results"):
        if key in value:
            projected[key] = value[key]
    return projected


def _plan_summary(value: dict[str, Any]) -> dict[str, Any]:
    plan = _fields(value, ("plan_id", "account_id", "symbol", "status", "revision", "definition_hash"))
    definition = value.get("definition") or {}
    plan["definition"] = _fields(definition, (
        "quantity_shares", "reference_price", "cash_budget", "position_size_pct", "entry_condition",
        "trigger_price", "not_before", "expires_at", "stop_loss", "target_price", "max_holding_seconds",
        "exit_not_after", "exit_order_policy", "strategy_id", "strategy_version", "qualification_id", "evidence_ids",
    ))
    metadata = definition.get("metadata") or {}
    plan["cost_assumptions"] = metadata.get("cost_assumptions") or {}
    plan["eligibility"] = _fields(metadata, ("eligibility", "positive_ev_qualified", "submission_risk_status", "qualification_status"))
    plan["state"] = _state_summary(value.get("state") or {})
    plan["sizing"] = _fields(metadata.get("sizing") or {}, (
        "quantity", "lot_size", "lot_type", "unsubmitted_quantity", "original_quantity_shares",
        "cash_budget", "planning_execution_price", "slippage_and_impact_reserve_bps", "reason",
        "requested_position_size_pct", "cash_allocation_pct",
    ))
    return plan


def _state_summary(value: dict[str, Any]) -> dict[str, Any]:
    return _fields(value, ("status", "filled_quantity", "remaining_quantity", "entry_order_id", "exit_order_id",
                           "wait_reason", "exit_reason", "exit_alert", "exit_replacement_count", "exit_cancel_intent",
                           "entered_at", "closed_at", "entry_receipt", "exit_receipt"))


_RESEARCH_ERROR_CODES = frozenset({
    "history_identity_or_provenance_not_verified", "insufficient_completed_history",
    "history_contains_future_bar", "history_not_strictly_chronological",
    "history_data_hash_mismatch", "history_normalized_hash_mismatch",
    "official_history_hash_mismatch_before_normalization",
})
_SOURCE_ERROR_CODES = frozenset({
    "canonical_taiwan_instrument_identity_required", "official_status_not_ok",
    "official_response_month_mismatch", "official_response_symbol_mismatch",
    "official_tpex_table_contract_changed", "official_tpex_volume_units_unverified",
    "official_tpex_turnover_units_unverified", "official_ohlcv_field_contract_changed",
    "official_duplicate_or_wrong_month_bar", "official_nonfinite_measurement",
    "official_invalid_ohlcv_bar", "official_month_has_no_verified_candles",
    "official_cache_request_or_hash_mismatch", "official_response_redirected_to_other_host",
    "official_cache_or_response_host_mismatch", "monthly_requests_stopped_after_three_source_failures",
})


def _error_code(value: Any, codes: frozenset[str], *, unknown: str) -> str:
    if not isinstance(value, str):
        return unknown
    error_type, separator, message = value[:256].partition(":")
    code = message.strip() if separator else error_type.strip()
    if len(value) <= 256 and code in codes:
        return code
    if error_type in {"TimeoutError", "ConnectTimeout", "ReadTimeout"}:
        return "source_timeout"
    if error_type in {"ConnectionError", "ConnectError", "NetworkError", "URLError", "HTTPError"}:
        return "source_transport_error"
    if error_type in {"OSError", "IOError", "FileNotFoundError", "PermissionError"}:
        return "source_io_error"
    return unknown


def _research_errors(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Project actionable references, never arbitrary source/exception prose.

    Reason counts describe the whole returned error collection. IDs are kept
    whole so a model can actually read the retained evidence with an existing
    tool. Unknown text stays unknown instead of being treated as instructions.
    """
    errors = value if isinstance(value, list) else [] if value is None else [value]
    counts: dict[str, int] = {}
    index = []
    invalid_references = missing_symbols = without_evidence = 0
    for item in errors:
        row = item if isinstance(item, dict) else {}
        reason = _error_code(row.get("error"), _RESEARCH_ERROR_CODES, unknown="unclassified_research_error")
        counts[reason] = counts.get(reason, 0) + 1
        brief = {"error": reason}
        symbol = row.get("symbol")
        if isinstance(symbol, str) and re.fullmatch(r"[0-9]{4,6}\.TW(?:O)?", symbol):
            brief["symbol"] = symbol
        else:
            missing_symbols += 1
        for key in ("source_attempt_id", "history_id"):
            identifier = row.get(key)
            if isinstance(identifier, str) and re.fullmatch(r"AE-[0-9a-f]{64}", identifier):
                brief[key] = identifier
            elif identifier is not None:
                invalid_references += 1
        if not any(key in brief for key in ("source_attempt_id", "history_id")):
            without_evidence += 1
        index.append(brief)
    summary = {"count": len(errors), "reason_counts": dict(sorted(counts.items())),
               "indexed_count": 0, "omitted_count": len(errors),
               "without_retained_evidence_count": without_evidence,
               "invalid_reference_count": invalid_references, "missing_symbol_count": missing_symbols,
               "evidence_read_tool": "autonomy.evidence"}
    return summary, index


def _source_requests(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Count all months and surface actual failures before successes or stops."""
    requests = value if isinstance(value, list) else [] if value is None else [value]
    statuses: dict[str, int] = {}
    failures: dict[str, int] = {}
    reasons: dict[str, int] = {}
    indexed = []
    stopped = 0
    for position, item in enumerate(requests):
        row = item if isinstance(item, dict) else {}
        status = row.get("status") if row.get("status") in ("verified", "rejected") else "unknown"
        statuses[status] = statuses.get(status, 0) + 1
        brief = {"status": status}
        reason = None
        failure = None
        if status != "verified":
            failure = row.get("failure_kind") if row.get("failure_kind") in ("coverage_gap", "verification_failure") else "unknown"
            failures[failure] = failures.get(failure, 0) + 1
            reason = _error_code(row.get("reason"), _SOURCE_ERROR_CODES, unknown="unclassified_source_error")
            reasons[reason] = reasons.get(reason, 0) + 1
            if reason == "monthly_requests_stopped_after_three_source_failures":
                stopped += 1
                continue
            brief.update(failure_kind=failure, reason=reason)
        month = row.get("month")
        if isinstance(month, str) and re.fullmatch(r"[0-9]{4}(?:0[1-9]|1[0-2])", month):
            brief["month"] = month
        count = row.get("row_count")
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 10_000_000:
            brief["row_count"] = count
        digest = row.get("raw_sha256")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            brief["raw_sha256"] = digest
        if isinstance(row.get("cache_hit"), bool):
            brief["cache_hit"] = row["cache_hit"]
        for key in ("raw_retention_status", "cache_write_status"):
            if row.get(key) in ("retained", "failed", "not_configured"):
                brief[key] = row[key]
        # Preserve receipt order within each priority; a late failure must not
        # disappear merely because the original list begins with old stops.
        priority = 0 if failure == "verification_failure" else 2 if status == "verified" else 1
        indexed.append((priority, position, brief))
    summary = {"count": len(requests), "status_counts": statuses, "failure_counts": failures,
               "reason_counts": reasons, "stopped_request_count": stopped,
               "indexed_count": 0, "omitted_count": len(indexed),
               "detail_order": "verification_failures_then_other_failures_then_verified; stops_counted_only"}
    return summary, [row for _, _, row in sorted(indexed)]


def _candidate_errors(researched: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep fixed-candidate failures separate from usable history provenance."""
    stages: dict[str, int] = {}
    reasons: dict[str, int] = {}
    errors = []
    for row in researched:
        items = row.get("evaluation_errors")
        items = items if isinstance(items, list) else [] if items is None else [items]
        for item in items:
            item = item if isinstance(item, dict) else {}
            stage = item.get("stage")
            stage = stage if stage in ("signal_generation", "candidate_evaluation", "qualification_retention") else "unknown"
            reason = _error_code(item.get("error"), frozenset(), unknown="unclassified_candidate_evaluation_error")
            stages[stage] = stages.get(stage, 0) + 1
            reasons[reason] = reasons.get(reason, 0) + 1
            brief = {"stage": stage, "error": reason}
            for key, pattern, container in (
                ("symbol", r"[0-9]{4,6}\.TW(?:O)?", row),
                ("history_id", r"AE-[0-9a-f]{64}", row),
                ("strategy_id", r"[A-Za-z0-9_.-]{1,96}", item),
                ("strategy_version", r"[0-9a-f]{64}", item),
            ):
                value = container.get(key)
                if isinstance(value, str) and re.fullmatch(pattern, value):
                    brief[key] = value
            errors.append(brief)
    return {"error_count": len(errors), "stage_counts": stages, "reason_counts": reasons,
            "indexed_count": 0, "omitted_count": len(errors),
            "scope": "fixed_candidate_evaluation_not_history_source_gate"}, errors


def _autonomy_result(value: dict[str, Any], budget: int) -> dict[str, Any]:
    """Reserve independent sections so long narratives cannot consume IDs.

    Full receipts stay in Host storage. This projection deliberately omits
    metadata manifests and repeated plan definitions from management results.
    """
    schema = value.get("schema_version")
    result = _bounded(_fields(value, (
        "schema_version", "cycle_id", "campaign_receipt_id", "action", "account_id", "mode", "status",
        "enabled", "created_at", "bulk_evidence_id", "universe_count", "ordinary_stock_count", "usable_bulk_count",
        "deep_selected_count", "deep_success_count", "deep_success_count_scope",
        "candidate_evaluation_success_count", "candidate_evaluation_error_count", "security_master_refresh_status", "model_calls", "selection", "evidence_id", "kind",
        "retained_payload_sha256", "retained_bar_count", "retained_feature_count", "is_partial_view",
        "latest_cycle_id", "last_research_at", "last_disabled_at", "eligibility", "positive_ev_qualified", "account_observed_at",
        "item_count", "next_after",
    )), min(budget, 1_400))
    result["_projection"] = "bounded_autonomy_view; full evidence and receipts remain in Host storage"

    def add(key, item, cap, *, rows=False, newest=False):
        available = min(cap, budget - len(_json(result)) - len(_json(key)) - 64)
        if available < 64:
            result["_projection_truncated"] = True
            return
        if rows:
            # Bound whole records, not the first twelve elements of a history.
            selected = [_bounded(row, min(available, 2_400)) for row in item]
            while selected and len(_json(selected)) > available:
                selected.pop(0 if newest else -1)
            result[key] = selected
            if len(selected) < len(item):
                result["_projection_truncated"] = True
        else:
            result[key] = _bounded(item, available)

    if schema == "open_stock_ai.security_research_coverage_query.v1":
        summary = value.get("summary") or {}
        compact_summary = _fields(summary, (
            "schema_version", "snapshot_id", "status", "account_id", "observed_at", "universe_expires_at", "source_cycle_id",
            "scope", "security_count", "new_entry_eligible_count", "deep_research_status_counts",
            "lifecycle_status_counts", "data_domain_counts", "complete_deep_coverage", "all_domains_current",
        ))
        add("summary", compact_summary, 1_500)
        add("filters", value.get("filters") or {}, 350)
        item_index = []
        details = []
        for item in value.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_index.append(_fields(item, (
                "entity_id", "symbol", "new_entry_eligible", "deep_research_status",
            )))
            brief = _fields(item, (
                "coverage_key", "entity_id", "symbol", "venue", "name", "lifecycle_status", "product_type",
                "new_entry_eligible", "exclusion_reasons", "deep_research_status", "last_deep_research_at",
                "last_deep_research_cycle_id", "last_deep_research_evidence_id", "last_deep_research_error",
            ))
            domains = item.get("data_domains") or {}
            if len(domains) == 1:
                brief["data_domains"] = domains
            else:
                brief["ready_current_domains"] = [
                    domain for domain, state in domains.items()
                    if isinstance(state, dict) and state.get("availability") == "ready" and state.get("freshness") == "current"
                ]
                brief["needs_update_domains"] = [
                    domain for domain, state in domains.items()
                    if isinstance(state, dict) and state.get("needs_update") is True
                ]
            details.append(brief)
        add("item_index", item_index[:20], 3_000, rows=True)
        add("items", details[:4], 1_200, rows=True)
        result["returned_item_count"] = len(result.get("item_index") or [])
        result["retained_item_count"] = len(item_index)
        if len(item_index) > 20 or result["returned_item_count"] < min(20, len(item_index)) or len(details) > 4:
            result["_projection_truncated"] = True
        return result

    if schema == "open_stock_ai.autonomous_research_cycle.v1":
        if isinstance(value.get("requested_symbols"), list):
            # Already validated by Host; whole short symbols avoid the generic
            # twelve-item list truncation when a model requested twenty names.
            symbols = [symbol for symbol in value["requested_symbols"]
                       if isinstance(symbol, str) and re.fullmatch(r"[0-9]{4}[A-Z0-9]{0,2}\.TW(?:O)?", symbol)]
            add("requested_symbols", symbols, 400, rows=True)
            if len(symbols) != len(value["requested_symbols"]):
                result["_projection_truncated"] = True
        elif "requested_symbols" in value:
            result["requested_symbols"] = None
        add("cost_assumptions", value.get("cost_assumptions") or {}, 500)
        researched = [row for row in value.get("results") or [] if isinstance(row, dict)]
        history_index = []
        for row in researched:
            brief = _fields(row, ("symbol", "history_id", "last_bar", "bar_count"))
            if "candidate_evaluation_status" in row:
                state = row["candidate_evaluation_status"]
                brief["candidate_evaluation_status"] = state if state in (
                    "complete", "partial", "failed", "not_evaluated_unsupported_product") else "unknown"
            if row.get("evaluation_errors"):
                brief["evaluation_error_count"] = len(row["evaluation_errors"]) if isinstance(row["evaluation_errors"], list) else 1
            history_index.append(brief)
        candidate_summary, candidate_errors = _candidate_errors(researched)
        if candidate_errors:
            add("candidate_evaluation_summary", candidate_summary, 700)
        # Preserve the twenty-history index before optional candidate details;
        # the total tool-result budget remains unchanged.
        add("results", history_index, 4_800, rows=True)
        product_blocked = [row for row in researched if row.get("candidate_evaluation_status") == "not_evaluated_unsupported_product"]
        if product_blocked:
            reason_counts = {}
            for row in product_blocked:
                for reason in row.get("candidate_evaluation_reasons") or []:
                    if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,100}", reason):
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
            add("product_candidate_restrictions", {"count": len(product_blocked), "reason_counts": reason_counts,
                "scope": "history_retained_but_current_stock_cost_candidates_not_admitted"}, 700)
        summary, errors = _research_errors(value.get("errors"))
        if errors:
            add("error_summary", summary, 1_000)
            available = max(0, budget - len(_json(result)) - 160)
            selected = []
            for error in errors:
                if len(_json([*selected, error])) > available:
                    break
                selected.append(error)
            result["errors"] = selected
            # Reserve room above for these counts/markers. Unlike generic
            # nested truncation, omitted error rows never become partial IDs.
            if isinstance(result.get("error_summary"), dict):
                result["error_summary"].update(indexed_count=len(selected), omitted_count=len(errors) - len(selected))
            if len(selected) < len(errors):
                result["_projection_truncated"] = True
        else:
            result["errors"] = []
        if candidate_errors:
            available = max(0, budget - len(_json(result)) - 160)
            selected = []
            for error in candidate_errors:
                if len(_json([*selected, error])) > available:
                    break
                selected.append(error)
            result["candidate_evaluation_errors"] = selected
            if isinstance(result.get("candidate_evaluation_summary"), dict):
                result["candidate_evaluation_summary"].update(indexed_count=len(selected), omitted_count=len(candidate_errors) - len(selected))
            if len(selected) < len(candidate_errors):
                result["_projection_truncated"] = True
        candidates = []
        for row in researched[:4]:
            for candidate in (row.get("candidates") or [])[:2]:
                brief = _fields(candidate, ("strategy_id", "strategy_version", "qualification_id", "positive_ev_qualified", "research_paper_candidate_eligible"))
                brief["symbol"] = row.get("symbol")
                brief["signal"] = _fields(candidate.get("signal") or {}, ("action", "confidence", "entry_price", "position_size_pct", "stop_loss", "target_price"))
                candidates.append(brief)
        add("candidate_summaries", candidates, 1_600, rows=True)
        return result

    if value.get("kind") and isinstance(value.get("payload_view"), dict):
        payload = value["payload_view"]
        source = _fields(payload.get("data_evidence") or {}, (
            "symbol", "source_kind", "source_id", "data_sha256", "raw_source_data_sha256", "source_provenance_verified",
            "instrument_identity_verified", "coverage_complete", "corporate_actions_verified", "historical_vintage_verified",
            "execution_costs_verified", "bar_completion_normalization", "fixture_only_not_market_or_ev_proof",
        ))
        add("data_evidence", source, 900)
        if value.get("kind") == "history_source_attempt":
            summary, requests = _source_requests(payload.get("source_request_receipts"))
            add("source_request_summary", summary, 2_400)
            available = max(0, budget - len(_json(result)) - 160)
            selected = []
            for request in requests:
                if len(_json([*selected, request])) > available:
                    break
                selected.append(request)
            result["source_request_receipts"] = selected
            if isinstance(result.get("source_request_summary"), dict):
                result["source_request_summary"].update(indexed_count=len(selected), omitted_count=len(requests) - len(selected))
            result["is_partial_view"] = bool(value.get("is_partial_view") or summary["stopped_request_count"] or len(selected) < len(requests))
            if len(selected) < len(requests):
                result["_projection_truncated"] = True
        elif isinstance(payload.get("rows"), list):
            bars = [_fields(row, ("timestamp", "date", "open", "high", "low", "close", "volume")) for row in payload["rows"]]
            add("recent_rows", bars[-40:], 4_800, rows=True, newest=True)
            result["returned_bar_count"] = len(result.get("recent_rows") or [])
            result["is_partial_view"] = bool(value.get("is_partial_view") or result["returned_bar_count"] < len(bars))
        else:
            add("payload_view", payload, 4_800)
        return result

    # Outstanding exits must remain visible even when account/history details
    # consume the context budget. Retained plan IDs allow a subsequent read.
    records = list(value.get("plans") or [])
    if isinstance(value.get("plan"), dict):
        records.append(value["plan"])
    records.extend((value.get("management") or {}).get("results") or [])
    alerts = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        state = record.get("state") or {}
        if state.get("exit_alert"):
            alerts[record.get("plan_id")] = {
                **_fields(record, ("plan_id", "symbol")),
                **_fields(state, ("status", "remaining_quantity", "exit_order_id", "exit_alert")),
            }
    if alerts:
        add("exit_alerts", list(alerts.values()), 1_500, rows=True)
        result["retained_exit_alert_count"] = len(alerts)
    account = value.get("account")
    decision_context = value.get("paper_decision_context")
    if isinstance(decision_context, dict):
        compact = {key: _fields(decision_context.get(key) or {}, ("status", "blockers", "required_for_bounded_experiment"))
                   for key in ("plan_creation", "submission", "positive_ev")}
        compact["planning_cash"] = _fields(decision_context.get("planning_cash") or {}, (
            "planning_cash", "blockers", "max_order_notional_pct", "max_total_exposure_pct", "budget_basis"))
        compact["central_limits"] = _fields(decision_context.get("central_limits") or {}, ("max_daily_loss_pct",))
        compact["policy"] = _fields(decision_context.get("policy") or {}, ("other_framework_flags", "closed_quote_effect"))
        add("paper_decision_context", compact, 1_450)
    if isinstance(account, dict):
        add("account", _fields(account, (
            "account_id", "mode", "base_currency", "cash_balance", "available_cash", "settled_cash_balance",
            "holdings_market_value", "total_equity", "today_pnl", "current_drawdown_pct", "realized_pnl",
            "unrealized_pnl", "position_count", "order_count", "fill_count", "valuation_basis",
        )), 900)
        positions = account.get("positions") or []
        reservations = account.get("open_order_reservations") or []
        add("account_positions", [_fields(row, ("symbol", "quantity", "average_cost", "last_price", "market_value",
                                                "position_size_pct", "realized_pnl", "unrealized_pnl"))
                                  for row in positions], 1_800, rows=True)
        add("open_order_reservations", [_fields(row, ("order_id", "symbol", "side", "remaining_quantity", "reservation_price",
                                                      "estimated_remaining_cost", "valid", "blockers"))
                                         for row in reservations], 900, rows=True)
        result["retained_account_position_count"] = len(positions)
        result["retained_open_reservation_count"] = len(reservations)
    if isinstance(value.get("model_review"), dict):
        add("model_review", _fields(value["model_review"], ("enabled", "session_id", "provider_model_selection", "used_today", "remaining_today")), 500)
    if isinstance(value.get("forward_validation"), dict):
        forward = value["forward_validation"]
        compact = _fields(forward, ("status", "binding_eligible", "protocol_id", "policy_version", "starts_at", "ends_at", "error", "scope"))
        if isinstance(forward.get("protocols"), list):
            compact["protocols"] = []
            for protocol in forward["protocols"][-2:]:
                item = _fields(protocol, ("protocol_id", "policy_version", "status", "evaluate_at", "decisions", "observations", "outcomes"))
                item["evaluation"] = _fields(protocol.get("evaluation") or {}, ("positive_ev_qualified", "reasons", "qualification_scope"))
                compact["protocols"].append(item)
        add("forward_validation", compact, 1_000)
    management = value.get("management")
    if isinstance(management, dict):
        compact = _fields(management, ("account_id", "enabled", "mode", "status", "errors"))
        compact["results"] = [{**_fields(row, ("plan_id", "symbol", "status")),
                                "state": _state_summary(row.get("state") or {})}
                               for row in management.get("results") or [] if isinstance(row, dict)]
        add("management", compact, 1_450)
    if isinstance(value.get("plan"), dict):
        add("plan", _plan_summary(value["plan"]), 3_300)
    if isinstance(value.get("plans"), list):
        terminal = {"closed", "cancelled", "expired", "invalidated", "rejected"}
        plans = sorted((plan for plan in value["plans"] if isinstance(plan, dict)),
                       key=lambda plan: plan.get("status") in terminal)
        add("plan_index", [_fields(plan, ("plan_id", "symbol", "status")) for plan in plans], 1_000, rows=True)
        add("plans", [_plan_summary(plan) for plan in plans], 3_300, rows=True)
        result["retained_plan_count"] = value.get("retained_plan_count", len(value["plans"]))
        if value.get("requested_plan_ids"):
            add("requested_plan_ids", value["requested_plan_ids"], 350)
            add("missing_plan_ids", value.get("missing_plan_ids", []), 350)
    for key in ("exit_request", "skipped", "errors"):
        if key in value:
            add(key, value[key], 450)
    if isinstance(value.get("learning"), dict):
        add("learning", _fields(value["learning"], ("closed_trade_count", "net_pnl", "winning_trades", "losing_trades", "positive_ev_qualified")), 450)
    return result


def _bounded(value: Any, budget: int, *, depth: int = 0) -> Any:
    """Keep JSON shape, with explicit omission markers and a strict char budget."""
    if budget < 32:
        return "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(_json(value)) <= budget:
            return value
        low, high = 0, min(len(value), budget)
        while low < high:
            middle = (low + high + 1) // 2
            if len(_json(value[:middle] + "…")) <= budget:
                low = middle
            else:
                high = middle - 1
        return value[:low] + "…"
    if depth >= 7:
        return {"_projection_truncated": True}
    if isinstance(value, list):
        projected = []
        remaining = budget - 32
        for index, item in enumerate(value[:12]):
            if remaining < 64:
                break
            # Give each recent row/source a share instead of allowing the first
            # long article to remove every other source's evidence.
            item_budget = remaining // min(len(value) - index, 12 - index)
            bounded = _bounded(item, item_budget - 1, depth=depth + 1)
            projected.append(bounded)
            remaining -= len(_json(bounded)) + 1
        if len(projected) < len(value):
            projected.append({"_omitted_items": len(value) - len(projected)})
        return projected
    if isinstance(value, dict):
        projected = {}
        remaining = budget - 34
        for key, item in list(value.items())[:40]:
            cost = len(_json(str(key))) + 2
            if remaining - cost < 32:
                break
            bounded = _bounded(item, remaining - cost, depth=depth + 1)
            projected[str(key)] = bounded
            remaining -= cost + len(_json(bounded))
        if len(projected) < len(value):
            projected["_projection_truncated"] = True
        return projected
    return _bounded(str(value), budget, depth=depth)


def project_tool_result(result: Any, *, max_chars: int = 6_000) -> Any:
    """Preserve the actual tool payload, bounded before Host token accounting."""
    value = result
    if isinstance(value, dict) and (
        value.get("schema_version") in {"open_stock_ai.autonomous_research_cycle.v1", "open_stock_ai.autonomy_tool_receipt.v1", "open_stock_ai.autonomous_status.v1", "open_stock_ai.security_research_coverage_query.v1"}
        or value.get("evidence_id") and value.get("kind") and isinstance(value.get("payload_view"), dict)
        or value.get("account_id") and "automatic_research" in value and isinstance(value.get("plans"), list)
    ):
        return _autonomy_result(value, max_chars)
    if isinstance(value, dict) and value.get("schema_version") == "open_stock_ai.agent_research_pack.v1":
        value = _research_pack(value)
    elif isinstance(value, dict) and value.get("schema_version") == "open_stock_ai.web_research.v1":
        value = _web_research(value)
    return _bounded(value, max_chars)
