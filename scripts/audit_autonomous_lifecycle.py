#!/usr/bin/env python3
"""Audit existing autonomous paper lifecycle receipts without changing a ledger.

This is a bounded evidence inventory, not a trading controller or an M1
certification. SQLite is opened with mode=ro and query_only, with no production
store constructors, migrations, model calls, quote requests or broker actions.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from open_stock_ai.execution.trading_plan import content_hash
from open_stock_ai.execution.paper_fill_evidence import project_paper_fill_evidence


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src/open_stock_ai"
AGENT_STRATEGY = "agent_discretionary_proposal_v1"


def _object(value: Any) -> dict:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        return decoded if isinstance(decoded, dict) else {}
    except (ValueError, TypeError):
        return {}


def _hash_matches(value: Any, digest: Any) -> bool:
    try:
        return bool(digest) and content_hash(value) == digest
    except (ValueError, TypeError, OverflowError):
        return False


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _fixture(value: Any) -> bool:
    if isinstance(value, dict):
        return any(("fixture" in str(key).lower() and item is True) or
                   (key in {"source_id", "price_source", "execution_mode"} and
                    any(tag in str(item).lower() for tag in ("fixture", "offline", "synthetic"))) or
                   _fixture(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_fixture(item) for item in value)
    return False


class ReadOnlyLedger:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve(strict=True)
        self.conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma query_only=on")
        self.conn.execute("begin")
        self.tables = {row[0] for row in self.conn.execute("select name from sqlite_master where type='table'")}
        self.missing: set[str] = set()

    def close(self):
        self.conn.close()

    def rows(self, table: str, **filters) -> list[dict]:
        # Identifiers come only from this script, never SQL supplied by a plan.
        if table not in self.tables:
            self.missing.add(table)
            return []
        columns = {row[1] for row in self.conn.execute(f'pragma table_info("{table}")')}
        if not set(filters) <= columns:
            self.missing.add(table + ":required_columns")
            return []
        where = " and ".join(f'"{name}"=?' for name in filters)
        sql = f'select * from "{table}"' + (" where " + where if where else "")
        return [dict(row) for row in self.conn.execute(sql, tuple(filters.values()))]


def _source_audit(ledger: ReadOnlyLedger, plan: dict, definition: dict) -> dict:
    account_id = plan["account_id"]
    records, problems = [], []
    for evidence_id in definition.get("evidence_ids") or []:
        rows = ledger.rows("autonomous_evidence", evidence_id=evidence_id, account_id=account_id)
        if len(rows) != 1:
            problems.append("missing_account_retained_evidence:" + str(evidence_id))
            continue
        row = rows[0]
        payload = _object(row.get("payload_json"))
        digest_ok = _hash_matches({"account_id": account_id, "kind": row.get("kind"), "payload": payload},
                                  str(evidence_id).removeprefix("AE-"))
        source = _object(payload.get("data_evidence"))
        history = row.get("kind") == "price_history"
        data_ok = _hash_matches(payload.get("rows"), source.get("data_sha256")) if history else None
        if not digest_ok or history and not data_ok:
            problems.append("retained_evidence_hash_mismatch:" + str(evidence_id))
        records.append({"evidence_id": evidence_id, "kind": row.get("kind"), "retained_hash_valid": digest_ok,
                        "history_data_hash_valid": data_ok, "fixture": _fixture(payload),
                        "source_id": source.get("source_id"), "source_kind": source.get("source_kind"),
                        "symbol": source.get("symbol"), "source_provenance_verified": source.get("source_provenance_verified"),
                        "instrument_identity_verified": source.get("instrument_identity_verified"),
                        "bar_count": len(payload["rows"]) if isinstance(payload.get("rows"), list) else None,
                        "last_bar": payload["rows"][-1].get("timestamp") if history and payload.get("rows")
                            and isinstance(payload["rows"][-1], dict) else None})
    histories = [item for item in records if item["kind"] == "price_history"]
    fixture = _fixture(definition) or any(item["fixture"] for item in records)
    status = "fixture" if fixture else "unknown"
    if not fixture and histories and not problems and all(
            item["source_kind"] == "exchange_official" and item["source_provenance_verified"] is True
            and item["symbol"] == plan["symbol"] and item["history_data_hash_valid"] for item in histories):
        status = "retained_official_history"
    if not histories:
        problems.append("verified_price_history_missing")
    cycle_id = _object(definition.get("metadata")).get("cycle_id")
    cycles = ledger.rows("autonomous_research_cycles", cycle_id=cycle_id, account_id=account_id) if cycle_id else []
    cycle = _object(cycles[0].get("payload_json")) if len(cycles) == 1 else {}
    cycle_ok = bool(cycle) and cycle.get("account_id") == account_id and cycle.get("cycle_id") == cycle_id and _hash_matches(
        {key: value for key, value in cycle.items() if key != "cycle_id"}, str(cycle_id).removeprefix("AC-"))
    if not cycle_ok:
        problems.append("retained_cycle_missing_or_hash_mismatch")
    if cycle and not any(isinstance(row, dict) and row.get("symbol") == plan["symbol"] and
                         row.get("history_id") in (definition.get("evidence_ids") or []) for row in cycle.get("results", [])):
        problems.append("plan_symbol_history_not_in_retained_cycle")
    return {"status": status, "cycle_id": cycle_id, "cycle_hash_valid": cycle_ok,
            "cycle_created_at": cycle.get("created_at"), "evidence": records, "problems": problems,
            "execution_quote_provenance": "not_verified_by_this_audit"}


def _retained_payload(ledger: ReadOnlyLedger, *, account_id: str, evidence_id: Any, kind: str) -> tuple[dict, str | None]:
    if not isinstance(evidence_id, str) or not evidence_id.startswith("AE-"):
        return {}, "retained_receipt_id_missing"
    rows = ledger.rows("autonomous_evidence", account_id=account_id, evidence_id=evidence_id)
    if len(rows) != 1 or rows[0].get("kind") != kind:
        return {}, "retained_receipt_missing_or_wrong_kind"
    payload = _object(rows[0].get("payload_json"))
    if not _hash_matches({"account_id": account_id, "kind": kind, "payload": payload}, evidence_id[3:]):
        return {}, "retained_receipt_hash_mismatch"
    return payload, None


def _deployment_audit(ledger: ReadOnlyLedger, execution_context: dict, *, account_id: str) -> dict:
    receipt = _object(execution_context.get("deployment_receipt"))
    result = {"status": "unknown", "receipt_id": execution_context.get("deployment_receipt_id"),
              "receipt_hash_valid": False, "source_snapshot_hash_valid": False,
              "database_location_binding_matches_audited_path": None, "problems": [],
              "source_scope_complete": False, "loaded_code_independently_verified": False,
              "assurance": "host_observation_not_independent_attestation"}
    if not receipt:
        result["problems"].append("deployment_receipt_missing")
        return result
    result["receipt_hash_valid"] = _hash_matches(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}, receipt.get("receipt_sha256"))
    if not result["receipt_hash_valid"] or receipt.get("schema_version") != "open_stock_ai.autonomous_deployment_receipt.v1":
        result["problems"].append("deployment_receipt_schema_or_hash_invalid")
    if receipt.get("account_id") != account_id or receipt.get("mode") != "paper":
        result["problems"].append("deployment_account_or_mode_mismatch")
    retained, problem = _retained_payload(ledger, account_id=account_id,
        evidence_id=result["receipt_id"], kind="deployment_context")
    if problem or retained != receipt:
        result["problems"].append("deployment_context_not_bound_to_retained_receipt")
    path_digest = hashlib.sha256(str(ledger.path).encode("utf-8")).hexdigest()
    result["database_location_binding_matches_audited_path"] = (
        _object(receipt.get("database_bindings")).get("trading_database_path_sha256") == path_digest)
    if not result["database_location_binding_matches_audited_path"]:
        result["problems"].append("deployment_database_location_mismatch_or_missing")
    source, source_problem = _retained_payload(ledger, account_id=account_id,
        evidence_id=receipt.get("source_snapshot_id"), kind="deployment_source_snapshot")
    result["source_snapshot_hash_valid"] = bool(not source_problem and
        source.get("schema_version") == "open_stock_ai.autonomous_source_snapshot.v1" and
        source.get("receipt_sha256") == receipt.get("source_snapshot_sha256") and
        _hash_matches({key: value for key, value in source.items() if key != "receipt_sha256"}, source.get("receipt_sha256")) and
        isinstance(source.get("files"), dict) and _hash_matches(source["files"], source.get("files_sha256")))
    if not result["source_snapshot_hash_valid"]:
        result["problems"].append("deployment_source_snapshot_missing_or_invalid")
    elif (receipt.get("source_file_count") != len(source["files"]) or
          not isinstance(source.get("errors"), list) or receipt.get("source_error_count") != len(source["errors"]) or
          any(receipt.get(key) != source.get(key) for key in
              ("observed_at", "capture_stage", "declared_build_commit", "instance_id", "process_id"))):
        result["problems"].append("deployment_source_snapshot_identity_mismatch")
    result["source_scope_complete"] = bool(result["source_snapshot_hash_valid"] and source.get("files") and
        source.get("errors") == [] and receipt.get("source_error_count") == 0 and not result["problems"])
    for key in ("declared_build_commit", "instance_id", "process_id", "capture_stage", "source_snapshot_id"):
        result[key] = receipt.get(key)
    result["status"] = "host_receipt_integrity_verified" if not result["problems"] else "not_verified"
    result["loaded_code_independently_verified"] = False
    return result


def _invocation_context_audit(ledger: ReadOnlyLedger, runtime: ReadOnlyLedger | None,
                            context: dict, plan: dict, proposal_call_ids: list[str]) -> dict:
    identifier = context.get("context_receipt_id")
    result = {"context_receipt_id": identifier, "tool_call_id": context.get("tool_call_id"),
              "status": "not_verified", "receipt_hash_valid": False, "runtime_events_match": False,
              "model_call_id": None, "sdk_turn_id": None, "problems": []}
    payload, problem = _retained_payload(ledger, account_id=plan["account_id"],
        evidence_id=identifier, kind="model_invocation_context")
    if problem:
        result["problems"].append("model_invocation_context:" + problem)
        return result
    result["receipt_hash_valid"] = True
    if payload.get("schema_version") != "open_stock_ai.model_invocation_context.v1":
        result["problems"].append("model_invocation_context_schema_invalid")
    if any(payload.get(key) != expected for key, expected in {
        "account_id": plan["account_id"], "run_id": context.get("run_id"),
        "session_id": context.get("session_id"), "driver_id": context.get("driver_id"),
    }.items()):
        result["problems"].append("model_invocation_context_identity_mismatch")
    model = _object(context.get("provider_model_metadata"))
    recorded_model = _object(payload.get("provider_model_metadata"))
    if any(recorded_model.get(key) != model.get(key) for key in ("provider", "model", "reasoning_effort", "thread_id", "resolution_source")):
        result["problems"].append("model_invocation_context_model_mismatch")
    result["deployment"] = _deployment_audit(ledger, _object(payload.get("execution_context")), account_id=plan["account_id"])
    if payload.get("status") != "host_sdk_turn_correlated" or payload.get("reasons") != []:
        result["problems"].append("model_invocation_context_not_sdk_correlated")
    if payload.get("host_resumed_dispatch") is not False:
        result["problems"].append("model_invocation_context_host_resume_not_excluded")
    tool = _object(payload.get("tool"))
    if tool.get("status") != "running" or not tool.get("node_id") or not tool.get("started_at") or type(tool.get("step")) is not int:
        result["problems"].append("model_invocation_context_dispatch_identity_missing")
    if (not context.get("tool_call_id") or tool.get("call_id") != context.get("tool_call_id") or
            tool.get("name") != "autonomy.propose_plan" or tool.get("call_id") not in proposal_call_ids):
        result["problems"].append("model_invocation_context_proposal_call_mismatch")
    if runtime is None:
        result["problems"].append("model_invocation_context_runtime_unavailable")
        return result
    result["runtime_location_binding_matches_audited_path"] = payload.get("runtime_database_path_sha256") == hashlib.sha256(
        str(runtime.path).encode("utf-8")).hexdigest()
    if not result["runtime_location_binding_matches_audited_path"]:
        result["problems"].append("model_invocation_context_runtime_location_mismatch_or_missing")
    calls = runtime.rows("agent_tool_calls", run_id=context.get("run_id"), call_id=tool.get("call_id"))
    call = calls[0] if len(calls) == 1 else {}
    if (not call or call.get("step") != tool.get("step") or call.get("tool_name") != tool.get("name") or
            call.get("node_id") != tool.get("node_id") or call.get("started_at") != tool.get("started_at")):
        result["problems"].append("model_invocation_context_runtime_tool_identity_mismatch")
    actual = []
    for row in runtime.rows("agent_events", run_id=context.get("run_id")):
        event = _object(row.get("payload_json"))
        for field, column in (("sequence", "sequence"), ("type", "event_type"), ("timestamp", "created_at")):
            if column in row:
                event[field] = row[column]
        actual.append(event)

    def bound_events(key: str, fields: tuple[str, ...]) -> list[dict]:
        saved = payload.get(key)
        if not isinstance(saved, list) or not saved:
            result["problems"].append(key + "_missing")
            return []
        prefix = {"model_turn_events": "model.turn.", "sdk_turn_events": "model.sdk.turn.",
                  "model_session_events": "model.session.configured"}[key]
        relevant = [event for event in actual if str(event.get("type", "")).startswith(prefix)
                    and event.get("step") == tool.get("step")]
        if len(relevant) != len(saved):
            result["problems"].append(key + "_runtime_event_inventory_mismatch")
        matched = []
        for item in saved:
            if not isinstance(item, dict) or not item.get("event_id") or type(item.get("sequence")) is not int:
                result["problems"].append(key + "_event_identity_missing")
                continue
            candidates = [event for event in actual if event.get("event_id") == item["event_id"]
                          and event.get("sequence") == item["sequence"] and event.get("run_id") == context.get("run_id")
                          and event.get("session_id") == context.get("session_id")]
            if len(candidates) != 1 or any(candidates[0].get(field) != item.get(field) for field in fields):
                result["problems"].append(key + "_runtime_event_mismatch")
                continue
            event = candidates[0]
            if event.get("step") != tool.get("step") or _fixture(event):
                result["problems"].append(key + "_wrong_step_or_fixture")
                continue
            if key == "sdk_turn_events" and any(_object(event.get("turn")).get(field) !=
                    _object(item.get("turn")).get(field) for field in ("id", "status")):
                result["problems"].append(key + "_turn_mismatch")
                continue
            matched.append(item)
        return matched

    model_events = bound_events("model_turn_events", ("type", "model_call_id", "step", "driver", "model", "reasoning_effort", "timestamp"))
    sdk_events = bound_events("sdk_turn_events", ("type", "step", "source", "model", "reasoning_effort", "timestamp"))
    sessions = bound_events("model_session_events", ("type", "step", "provider", "model", "reasoning_effort",
        "thread_id", "resolution_source", "timestamp"))
    if (len(sessions) != 1 or sessions[0].get("type") != "model.session.configured" or
            context.get("driver_id") != "codex" or recorded_model.get("provider") != "codex" or
            not recorded_model.get("thread_id") or recorded_model.get("resolution_source") != "sdk_thread_start" or
            any(sessions[0].get(key) != recorded_model.get(key) for key in
                ("provider", "model", "reasoning_effort", "thread_id", "resolution_source"))):
        result["problems"].append("model_session_receipt_missing_or_mismatch")
    starts = [event for event in model_events if event.get("type") == "model.turn.started"]
    ends = [event for event in model_events if event.get("type") == "model.turn.completed"]
    if (len(starts) != 1 or len(ends) != 1 or not starts[0].get("model_call_id") or
            starts[0].get("model_call_id") != ends[0].get("model_call_id") or
            starts[0].get("sequence", 0) >= ends[0].get("sequence", 0) or
            any(event.get("driver") != context.get("driver_id") or event.get("model") != model.get("model")
                or event.get("reasoning_effort") != model.get("reasoning_effort") for event in starts + ends)):
        result["problems"].append("precise_model_turn_pair_missing_or_mismatch")
    else:
        result["model_call_id"] = starts[0]["model_call_id"]
    sdk_starts = [event for event in sdk_events if event.get("type") == "model.sdk.turn.started"]
    sdk_ends = [event for event in sdk_events if event.get("type") == "model.sdk.turn.completed"]
    if (len(sdk_starts) != 1 or len(sdk_ends) != 1 or not _object(sdk_starts[0].get("turn")).get("id") or
            _object(sdk_starts[0].get("turn")).get("id") != _object(sdk_ends[0].get("turn")).get("id") or
            _object(sdk_ends[0].get("turn")).get("status") != "completed" or
            sdk_starts[0].get("sequence", 0) >= sdk_ends[0].get("sequence", 0) or
            any(event.get("source") != "codex_app_server" or event.get("model") != model.get("model") or
                event.get("reasoning_effort") != model.get("reasoning_effort") for event in sdk_starts + sdk_ends)):
        result["problems"].append("precise_sdk_turn_pair_missing_or_mismatch")
    else:
        result["sdk_turn_id"] = sdk_starts[0]["turn"]["id"]
    if result["model_call_id"] and result["sdk_turn_id"] and not (
        starts[0]["sequence"] < sdk_starts[0]["sequence"] < sdk_ends[0]["sequence"] < ends[0]["sequence"]):
        result["problems"].append("model_and_sdk_event_order_mismatch")
    dispatches = [event for event in actual if event.get("type") == "tool.started" and
        event.get("call_id") == tool.get("call_id") and event.get("node_id") == tool.get("node_id") and
        event.get("step") == tool.get("step") and event.get("session_id") == context.get("session_id")]
    if (len(dispatches) != 1 or type(dispatches[0].get("sequence")) is not int or len(ends) != 1 or
            ends[0]["sequence"] >= dispatches[0]["sequence"]):
        result["problems"].append("model_tool_dispatch_order_missing_or_mismatch")
    if any(event.get("type") == "approval.resumed_tool_dispatch" and event.get("step") == tool.get("step") for event in actual):
        result["problems"].append("runtime_host_resumed_dispatch_recorded")
    result["runtime_events_match"] = not result["problems"]
    result["status"] = "host_sdk_turn_correlated" if result["runtime_events_match"] else "not_verified"
    return result


def _model_audit(ledger: ReadOnlyLedger, runtime: ReadOnlyLedger | None, definition: dict, plan: dict) -> dict:
    metadata = _object(definition.get("metadata"))
    context = _object(metadata.get("agent_context"))
    model = _object(context.get("provider_model_metadata"))
    result = {"run_id": context.get("run_id"), "session_id": context.get("session_id"),
              "driver_id": context.get("driver_id"),
              "model": {key: model.get(key) for key in ("provider", "model", "reasoning_effort")},
              "context_hash_valid": _hash_matches(context, metadata.get("agent_context_sha256")),
              "status": "not_applicable" if definition.get("strategy_id") != AGENT_STRATEGY else "missing_runtime_correlation",
              "successful_proposal_call_ids": [], "completed_model_call_ids": [], "problems": []}
    if result["status"] == "not_applicable":
        return result
    if not result["context_hash_valid"] or not model.get("model"):
        result["problems"].append("actual_model_context_missing_or_hash_mismatch")
    if runtime is None or not context.get("run_id"):
        result["problems"].append("runtime_database_or_run_id_missing")
        result["retained_context"] = _invocation_context_audit(ledger, runtime, context, plan, [])
        return result
    runs = runtime.rows("agent_runs", run_id=context["run_id"])
    run = runs[0] if len(runs) == 1 else {}
    if not run or run.get("session_id") != context.get("session_id") or run.get("driver") != context.get("driver_id"):
        result["problems"].append("runtime_run_identity_mismatch_or_missing")
    run_account = _object(_object(_object(run.get("request_json")).get("metadata")).get("autonomous_model_review")).get("account_id")
    if run_account not in (None, plan["account_id"]):
        result["problems"].append("runtime_run_account_mismatch")
    # Runtime tool rows contain summaries. Recover the full result from the
    # account-owned immutable tool receipt, then bind its hash to validation.
    receipt_hashes = set()
    for row in ledger.rows("autonomous_evidence", account_id=plan["account_id"], kind="tool_execution"):
        payload = _object(row.get("payload_json"))
        receipt = _object(payload.get("result"))
        saved = _object(receipt.get("plan"))
        if (receipt.get("account_id") == plan["account_id"] and saved.get("plan_id") == plan["plan_id"]
                and saved.get("definition_hash") == plan["definition_hash"]
                and receipt.get("action") == "autonomy.propose_plan"
                and _hash_matches({"account_id": plan["account_id"], "kind": "tool_execution", "payload": payload},
                                  str(row.get("evidence_id")).removeprefix("AE-"))):
            receipt_hashes.add(content_hash({**receipt, "campaign_receipt_id": row["evidence_id"]}))
    proposal_steps = set()
    for call in runtime.rows("agent_tool_calls", run_id=context["run_id"]):
        if call.get("tool_name") != "autonomy.propose_plan" or call.get("status") != "completed":
            continue
        event = _object(call.get("result_json"))
        validation = _object(call.get("validation_json"))
        if (event.get("type") == "tool.completed" and event.get("run_id") == context["run_id"]
                and event.get("session_id") == context.get("session_id") and event.get("call_id") == call.get("call_id")
                and validation.get("passed") is True and validation.get("evidence_hash") in receipt_hashes):
            result["successful_proposal_call_ids"].append(call.get("call_id"))
            proposal_steps.add(call.get("step"))
    events = [_object(row.get("payload_json")) for row in runtime.rows("agent_events", run_id=context["run_id"])]
    started = {event.get("model_call_id") for event in events if event.get("type") == "model.turn.started"
               and event.get("driver") == context.get("driver_id") and event.get("step") in proposal_steps}
    result["completed_model_call_ids"] = sorted({str(event["model_call_id"]) for event in events
        if event.get("type") == "model.turn.completed" and event.get("model_call_id") in started
        and event.get("model_call_id") and event.get("model") == model.get("model")
        and event.get("driver") == context.get("driver_id") and event.get("step") in proposal_steps and not _fixture(event)})
    if not result["completed_model_call_ids"]:
        result["problems"].append("matching_started_and_completed_model_turn_missing")
    if not result["successful_proposal_call_ids"]:
        result["problems"].append("successful_proposal_receipt_binding_missing")
    result["retained_context"] = _invocation_context_audit(ledger, runtime, context, plan, result["successful_proposal_call_ids"])
    if result["retained_context"]["status"] != "host_sdk_turn_correlated":
        result["problems"].append("precise_model_invocation_context_not_verified")
    if not result["problems"]:
        result["status"] = "host_sdk_turn_correlated"
    result["evidence_boundary"] = "local_Host_receipts; no_independent_provider_attestation"
    return result


def _source_version(definition: dict) -> dict:
    manifest = _object(_object(definition.get("metadata")).get("builder_source_manifest"))
    if not manifest:
        return {"status": "unknown", "reason": "retained_source_manifest_missing"}
    mismatches, missing = [], []
    for name, digest in manifest.items():
        path = (SOURCE_ROOT / name).resolve()
        if not path.is_relative_to(SOURCE_ROOT.resolve()) or not path.is_file():
            missing.append(name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            mismatches.append(name)
    return {"status": "changed" if mismatches else "unknown" if missing else "retained_builder_files_match_checkout",
            "changed_files": mismatches, "missing_files": missing,
            "scope": "saved_builder_manifest_only; full_model_policy_and_deployed_service_version_not_verified"}


def _audit_plan(ledger: ReadOnlyLedger, runtime: ReadOnlyLedger | None, plan: dict) -> dict:
    definition, state = _object(plan.get("definition_json")), _object(plan.get("state_json"))
    problems = []
    definition_ok = _hash_matches(definition, plan.get("definition_hash"))
    if not definition_ok:
        problems.append("plan_definition_hash_mismatch")
    if state.get("status") != plan.get("status"):
        problems.append("plan_state_status_mismatch")
    events = ledger.rows("autonomous_trading_plan_events", plan_id=plan["plan_id"])
    intents = {}
    for event in events:
        payload = _object(event.get("payload_json"))
        for phase in ("entry", "exit"):
            intent = _object(payload.get(phase + "_intent"))
            if intent.get("order_id"):
                old = intents.get(intent["order_id"])
                if old and old != intent:
                    problems.append("dispatch_intent_changed:" + intent["order_id"])
                intents[intent["order_id"]] = intent
    order_ids = set(intents)
    order_ids.update(str(state[key]) for key in ("entry_order_id", "exit_order_id") if state.get(key))
    orders, fills, projections, fill_reports = [], [], {}, []
    for order_id in sorted(order_ids):
        broker_rows = ledger.rows("paper_broker_orders", order_id=order_id)
        oms_rows = ledger.rows("paper_orders", order_id=order_id)
        order_fills = ledger.rows("paper_fills", order_id=order_id)
        cash_entries = ledger.rows("cash_ledger", order_id=order_id)
        for fill in order_fills:
            projection = project_paper_fill_evidence(fill, ledger_entries=cash_entries)
            projections[fill.get("fill_id")] = projection
            evidence = _object(projection["fill_evidence"])
            market = _object(evidence.get("market_context"))
            execution = _object(evidence.get("execution_context"))
            fill_reports.append({"fill_id": fill.get("fill_id"), "order_id": order_id,
                **projection["fill_evidence_verification"], "fixture_detected": _fixture(evidence),
                "market_observation_recorded": bool(market),
                "market_context_sha256": content_hash(market) if evidence else None,
                "execution_model_sha256": content_hash(execution["execution_model"]) if isinstance(execution.get("execution_model"), dict) else None,
                "deployment": _deployment_audit(ledger, execution, account_id=plan["account_id"])})
        if order_id not in intents:
            problems.append("dispatch_journal_missing:" + order_id)
        if not broker_rows:
            problems.append("broker_order_missing:" + order_id)
        if broker_rows and broker_rows[0].get("status") == "filled" and not order_fills:
            problems.append("filled_broker_order_has_no_ledger_fills:" + order_id)
        for row in [*broker_rows, *oms_rows, *order_fills]:
            if row.get("account_id") != plan["account_id"] or row.get("symbol") != plan["symbol"]:
                problems.append("order_or_fill_scope_mismatch:" + order_id)
        orders.append({"order_id": order_id, "intent_present": order_id in intents,
                       "broker_status": broker_rows[0].get("status") if broker_rows else None,
                       "oms_status": oms_rows[0].get("status") if oms_rows else None,
                       "fixture_detected": any(_fixture(_object(row.get("payload_json"))) for row in broker_rows + oms_rows),
                       "fill_count": len(order_fills),
                       "filled_quantity": sum(_number(row.get("quantity")) or 0 for row in order_fills)})
        fills.extend(order_fills)
    seen, quantities = set(), Counter()
    for fill in fills:
        fill_id, order_id = fill.get("fill_id"), fill.get("order_id")
        numbers = {key: _number(fill.get(key)) for key in ("quantity", "fill_price", "commission", "tax", "slippage_cost", "net_cash_delta")}
        if not fill_id or fill_id in seen:
            problems.append("duplicate_or_missing_fill_id")
        seen.add(fill_id)
        intent = intents.get(order_id, {})
        if not intent or fill.get("side") != intent.get("side"):
            problems.append("fill_not_in_plan_dispatch_journal:" + str(fill_id))
        if (any(value is None for value in numbers.values()) or (numbers["quantity"] or 0) <= 0
                or (numbers["fill_price"] or 0) <= 0 or fill.get("side") not in {"buy", "sell"}
                or any((numbers[key] or 0) < 0 for key in ("commission", "tax", "slippage_cost"))):
            problems.append("invalid_fill_accounting:" + str(fill_id))
            continue
        quantities[fill["side"]] += numbers["quantity"]
        expected = numbers["quantity"] * numbers["fill_price"] * (-1 if fill["side"] == "buy" else 1) - numbers["commission"] - numbers["tax"]
        if abs(expected - numbers["net_cash_delta"]) > .011:
            problems.append("fill_cashflow_mismatch:" + str(fill_id))
    for order_id, intent in intents.items():
        actual = sum(_number(fill.get("quantity")) or 0 for fill in fills if fill.get("order_id") == order_id)
        limit = _number(intent.get("quantity_shares"))
        if limit is None or actual > limit:
            problems.append("fills_exceed_or_lack_frozen_order_quantity:" + order_id)
    flat = quantities["buy"] > 0 and quantities["buy"] == quantities["sell"]
    if plan.get("status") == "closed" and (not flat or _number(state.get("remaining_quantity")) != 0
            or _number(state.get("filled_quantity")) != quantities["buy"]):
        problems.append("closed_plan_not_reconciled_to_fills")
    outcome_rows = ledger.rows("autonomous_trade_outcomes", plan_id=plan["plan_id"])
    outcome_row = outcome_rows[0] if len(outcome_rows) == 1 else {}
    outcome = _object(outcome_row.get("payload_json"))
    outcome_ok = False
    outcome_fill_evidence_status = "missing"
    if outcome:
        digest = outcome.get("receipt_sha256")
        outcome_ok = (digest == outcome_row.get("receipt_sha256") and _hash_matches(
            {key: value for key, value in outcome.items() if key != "receipt_sha256"}, digest))
        if not outcome_ok:
            problems.append("outcome_hash_mismatch")
        for key, expected in {"plan_id": plan["plan_id"], "account_id": plan["account_id"], "symbol": plan["symbol"],
                              "strategy_id": definition.get("strategy_id"), "strategy_version": definition.get("strategy_version"),
                              "plan_definition_hash": plan.get("definition_hash"), "mode": "paper"}.items():
            if outcome.get(key) != expected:
                problems.append("outcome_binding_mismatch:" + key)
        if outcome_row.get("account_id") != plan["account_id"]:
            problems.append("outcome_row_account_mismatch")
        if outcome_row.get("strategy_id") != outcome.get("strategy_id"):
            problems.append("outcome_row_strategy_mismatch")
        if outcome.get("closed_at") != state.get("closed_at") or outcome_row.get("closed_at") != outcome.get("closed_at"):
            problems.append("outcome_close_time_mismatch")
        if _number(outcome.get("quantity")) != quantities["buy"]:
            problems.append("outcome_quantity_mismatch")
        expected_fills = sorted(fills, key=lambda fill: (str(fill.get("created_at")), str(fill.get("fill_id"))))
        outcome_fills = outcome.get("fills")
        raw_outcome_fills = [{key: value for key, value in fill.items() if key not in
            {"fill_evidence", "fill_evidence_verification"}} for fill in outcome_fills] if (
                isinstance(outcome_fills, list) and all(isinstance(fill, dict) for fill in outcome_fills)) else None
        if raw_outcome_fills != expected_fills:
            problems.append("outcome_fills_differ_from_ledger")
        elif outcome_fills and all("fill_evidence" in fill and "fill_evidence_verification" in fill for fill in outcome_fills):
            outcome_fill_evidence_status = "matches_ledger" if all(
                {key: fill[key] for key in ("fill_evidence", "fill_evidence_verification")} == projections.get(fill.get("fill_id"))
                for fill in outcome_fills) else "mismatch"
        pnl = _number(outcome.get("net_pnl"))
        if pnl is None or abs(pnl - sum(_number(fill.get("net_cash_delta")) or 0 for fill in fills)) > .011:
            problems.append("outcome_cashflow_mismatch")
        if pnl != _number(outcome_row.get("net_pnl")):
            problems.append("outcome_row_cashflow_mismatch")
        for key in ("commission", "tax"):
            value = _number(outcome.get(key))
            if value is None or abs(value - sum(_number(fill.get(key)) or 0 for fill in fills)) > .011:
                problems.append("outcome_cost_mismatch:" + key)
    elif plan.get("status") == "closed":
        problems.append("closed_plan_outcome_missing")
    source = _source_audit(ledger, plan, definition)
    model = _model_audit(ledger, runtime, definition, plan)
    origin = "agent_proposal" if definition.get("strategy_id") == AGENT_STRATEGY else "fixed_candidate" if definition.get("strategy_id") in {"tw_candle_breakout_20_60_v1", "tw_candle_pullback_20_60_v1"} else "unknown_strategy"
    closed_verified = bool(plan.get("status") == "closed" and flat and outcome_ok and not problems)
    return {"plan_id": plan["plan_id"], "account_id": plan["account_id"], "symbol": plan["symbol"],
            "status": plan.get("status"), "created_at": plan.get("created_at"), "updated_at": plan.get("updated_at"),
            "strategy_id": definition.get("strategy_id"), "strategy_version": definition.get("strategy_version"),
            "definition_hash": plan.get("definition_hash"), "definition_hash_valid": definition_ok,
            "decision_origin": origin, "research_source": source, "model_invocation": model,
            "fixture_detected": source["status"] == "fixture" or any(order["fixture_detected"] for order in orders)
                or any(fill["fixture_detected"] for fill in fill_reports),
            "current_source_comparison": _source_version(definition),
            "dispatch_intent_count": len(intents), "orders": orders, "fill_ids": sorted(str(item) for item in seen),
            "fill_count": len(fills), "bought_quantity": quantities["buy"], "sold_quantity": quantities["sell"],
            "fill_provenance": {"fills": fill_reports,
                "integrity_counts": dict(Counter(fill["integrity_status"] for fill in fill_reports)),
                "all_fill_receipts_integrity_verified": bool(fill_reports) and all(fill["integrity_status"] == "verified" for fill in fill_reports),
                "execution_quote_source_status": "unknown", "outcome_projection_status": outcome_fill_evidence_status,
                "scope": "per_fill_cash_receipts_only; no_order_submission_or_other_fill_fallback"},
            "remaining_quantity_from_fills": quantities["buy"] - quantities["sell"],
            "outcome_present": bool(outcome), "outcome_hash_valid": outcome_ok,
            "outcome_net_pnl": _number(outcome.get("net_pnl")),
            "closed_round_trip_receipts_verified": closed_verified,
            "lifecycle_problems": sorted(set(problems)), "m1_verified": False}


def audit(database: Path, *, runtime_database: Path | None = None, account_id: str | None = None) -> dict:
    with ExitStack() as stack:
        ledger = ReadOnlyLedger(database)
        stack.callback(ledger.close)
        runtime = None
        if runtime_database is not None:
            runtime = ledger if runtime_database.expanduser().resolve() == ledger.path else ReadOnlyLedger(runtime_database)
            if runtime is not ledger:
                stack.callback(runtime.close)
        rows = ledger.rows("autonomous_trading_plans", **({"account_id": account_id} if account_id else {}))
        plans = []
        for row in sorted(rows, key=lambda item: (str(item.get("account_id")), str(item.get("created_at")), str(item.get("plan_id")))):
            try:
                plans.append(_audit_plan(ledger, runtime, row))
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                plans.append({"plan_id": row.get("plan_id"), "account_id": row.get("account_id"),
                              "status": row.get("status"), "audit_error": type(exc).__name__,
                              "lifecycle_problems": ["malformed_retained_record"], "m1_verified": False})
        report = {"schema_version": "open_stock_ai.autonomous_lifecycle_audit.v1",
                  "audited_at": datetime.now(timezone.utc).isoformat(), "database": str(ledger.path),
                  "runtime_database": str(runtime.path) if runtime else None, "account_filter": account_id,
                  "read_only": True, "snapshot_scope": "one_read_transaction_per_database; separate_databases_not_atomic",
                  "plan_count": len(plans), "plans": plans,
                  "missing_tables_or_columns": sorted(ledger.missing),
                  "runtime_missing_tables_or_columns": sorted(runtime.missing) if runtime else [],
                  "m1_verified": False, "positive_ev_qualified": False,
                  "limitations": ["No independent provider or execution quote attestation; runtime metadata alone is not a model invocation.",
                                  "Closed paper cashflows do not establish live execution quality, profitability, or fault recovery.",
                                  "Versions are reported per plan; older outcomes are not pooled as evidence for the current policy.",
                                  "Account-wide cash, holdings, corporate actions and deployed service revision require separate reconciliation.",
                                  "No-plan or no-fill observations cannot satisfy M1; waiting remains a valid trading decision."],
                  "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        report["receipt_sha256"] = content_hash(report)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Existing trading SQLite database; never created.")
    parser.add_argument("--runtime-database", type=Path, help="Existing Agent SQLite database for optional Host run correlation.")
    parser.add_argument("--account-id", help="Audit only this account; default includes all persisted autonomous plans.")
    parser.add_argument("--output", type=Path, help="New JSON report path; refuses overwrites and database paths.")
    args = parser.parse_args()
    report = audit(args.database, runtime_database=args.runtime_database, account_id=args.account_id)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
    else:
        print(encoded, end="")
    return 0  # Audit success is deliberately distinct from M1 certification.


if __name__ == "__main__":
    raise SystemExit(main())
