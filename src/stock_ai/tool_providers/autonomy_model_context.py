"""Retain bounded Host model provenance without deciding or executing trades.

Only the current run/node and its model step are read. These local receipts
correlate an SDK turn; they do not independently attest a provider or loaded
code. Missing provenance must never strand existing paper position protection.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable


_MODEL_FIELDS = ("provider", "model", "reasoning_effort", "model_provider", "thread_id", "resolution_source")
_EVENT_TYPES = ("model.turn.started", "model.turn.completed", "model.turn.failed",
                "model.session.configured", "model.sdk.turn.started", "model.sdk.turn.completed",
                "approval.resumed_tool_dispatch", "tool.started")
_EVENT_FIELDS = ("step", "model_call_id", "driver", "model", "reasoning_effort", "source",
                 "event_id", "provider", "model_provider", "thread_id", "resolution_source", "call_id", "node_id")
_DEPLOYMENT_FIELDS = ("schema_version", "account_id", "mode", "observed_at", "capture_stage",
                      "declared_build_commit", "instance_id", "process_id", "source_snapshot_id",
                      "source_snapshot_sha256", "source_file_count", "source_error_count", "assurance", "receipt_sha256")
_MAX_EVENTS = 32


def _scalar(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > 512 or re.search(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}|\bbearer\s+", value, re.I):
            return None
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _fields(value: Any, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: clean for key in keys if (clean := _scalar(value.get(key))) is not None}


def _deployment(service: Any) -> dict:
    value = getattr(service, "execution_context", {})
    if not isinstance(value, dict):
        return {}
    result = _fields(value, ("deployment_receipt_id",))
    receipt = value.get("deployment_receipt")
    if isinstance(receipt, dict):
        result["deployment_receipt"] = _fields(receipt, _DEPLOYMENT_FIELDS)
        result["deployment_receipt"]["database_bindings"] = _fields(
            receipt.get("database_bindings"), ("trading_database_path_sha256",))
    return result


def _collect(payload: dict, *, context: Any, name: str, run_store: Any) -> None:
    reasons = payload["reasons"]
    path = getattr(run_store, "path", None)
    if path is not None:
        payload["runtime_database_path_sha256"] = hashlib.sha256(str(Path(path).expanduser().resolve()).encode()).hexdigest()
    with run_store._connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("begin")
        run = conn.execute("""select session_id,driver,
            json_extract(request_json,'$.metadata.autonomous_model_review.account_id') as account_id
            from agent_runs where run_id=?""", (context.run_id,)).fetchone()
        if not run or run["session_id"] != context.session_id or run["driver"] != context.driver_id:
            reasons.append("runtime_run_identity_mismatch_or_missing")
            return
        if run["account_id"] not in (None, payload["account_id"]):
            reasons.append("runtime_run_account_mismatch")
            return
        calls = conn.execute("""select step,call_id,tool_name,status,started_at from agent_tool_calls
            where run_id=? and node_id=? and status='running' limit 2""",
            (context.run_id, payload["tool"]["node_id"])).fetchall()
        if len(calls) != 1:
            reasons.append("running_tool_call_missing" if not calls else "running_tool_call_ambiguous")
            return
        call = calls[0]
        if call["tool_name"] != name or not call["call_id"] or not isinstance(call["step"], int) or call["step"] < 1:
            reasons.append("running_tool_call_identity_mismatch")
            return
        payload["tool"].update(_fields(dict(call), ("step", "call_id", "status", "started_at")))
        # SQL projects only public lifecycle fields, never event prose, model
        # inputs, tool arguments, result bodies or checkpoint transcripts.
        projection = ",".join(f"json_extract(payload_json,'$.{key}') as {key}" for key in _EVENT_FIELDS)
        rows = conn.execute(f"""select sequence,event_type,created_at,{projection},
            json_extract(payload_json,'$.turn.id') as turn_id,
            json_extract(payload_json,'$.turn.status') as turn_status
            from agent_events where run_id=? and event_type in ({','.join('?' for _ in _EVENT_TYPES)})
            and json_extract(payload_json,'$.step')=?
            and (event_type!='tool.started' or json_extract(payload_json,'$.call_id')=?)
            order by sequence limit ?""",
            (context.run_id, *_EVENT_TYPES, call["step"], call["call_id"], _MAX_EVENTS + 1)).fetchall()
    if len(rows) > _MAX_EVENTS:
        reasons.append("model_step_event_limit_exceeded")
    events = []
    for row in rows[:_MAX_EVENTS]:
        event = {"type": row["event_type"], "sequence": row["sequence"], "timestamp": _scalar(row["created_at"]),
                 **_fields(dict(row), _EVENT_FIELDS)}
        if row["turn_id"] is not None or row["turn_status"] is not None:
            event["turn"] = _fields({"id": row["turn_id"], "status": row["turn_status"]}, ("id", "status"))
        events.append(event)
    payload["model_turn_events"] = [e for e in events if e["type"].startswith("model.turn.")]
    payload["sdk_turn_events"] = [e for e in events if e["type"].startswith("model.sdk.turn.")]
    payload["model_session_events"] = [e for e in events if e["type"] == "model.session.configured"]
    payload["host_resumed_dispatch"] = any(e["type"] == "approval.resumed_tool_dispatch" for e in events)
    if payload["host_resumed_dispatch"]:
        reasons.append("host_resumed_dispatch_is_not_a_new_model_turn")
    starts = [e for e in payload["model_turn_events"] if e["type"] == "model.turn.started"]
    ends = [e for e in payload["model_turn_events"] if e["type"] == "model.turn.completed"]
    if len(starts) != 1 or len(ends) != 1 or not starts[0].get("model_call_id") or starts[0].get("model_call_id") != ends[0].get("model_call_id"):
        reasons.append("model_turn_pair_missing_or_ambiguous")
    if any(e["type"] == "model.turn.failed" for e in payload["model_turn_events"]):
        reasons.append("model_turn_failure_recorded")
    model = payload["provider_model_metadata"]
    if context.driver_id != "codex" or model.get("provider") != "codex":
        reasons.append("codex_sdk_provider_not_verified")
    if not all(model.get(key) for key in ("model", "reasoning_effort", "thread_id")) or model.get("resolution_source") != "sdk_thread_start":
        reasons.append("sdk_resolved_model_session_missing")
    if any(e.get("model") != model.get("model") or e.get("driver") != context.driver_id or
           e.get("reasoning_effort") != model.get("reasoning_effort") for e in starts + ends):
        reasons.append("model_turn_selection_mismatch")
    sessions = payload["model_session_events"]
    if len(sessions) != 1 or any(sessions[0].get(key) != model.get(key) for key in
                               ("provider", "model", "reasoning_effort", "thread_id", "resolution_source")):
        reasons.append("model_session_receipt_missing_or_mismatch")
    sdk_starts = [e for e in payload["sdk_turn_events"] if e["type"] == "model.sdk.turn.started"]
    sdk_ends = [e for e in payload["sdk_turn_events"] if e["type"] == "model.sdk.turn.completed"]
    if (len(sdk_starts) != 1 or len(sdk_ends) != 1 or not sdk_starts[0].get("turn", {}).get("id")
            or sdk_starts[0].get("turn", {}).get("id") != sdk_ends[0].get("turn", {}).get("id")
            or sdk_ends[0].get("turn", {}).get("status") != "completed"):
        reasons.append("completed_sdk_turn_pair_missing_or_ambiguous")
    if any(e.get("source") != "codex_app_server" or e.get("model") != model.get("model") or
           e.get("reasoning_effort") != model.get("reasoning_effort") for e in sdk_starts + sdk_ends):
        reasons.append("sdk_turn_source_or_selection_mismatch")
    dispatches = [e for e in events if e["type"] == "tool.started" and e.get("node_id") == payload["tool"]["node_id"]]
    if len(dispatches) != 1:
        reasons.append("tool_dispatch_event_missing_or_ambiguous")
    elif len(starts) == len(ends) == len(sdk_starts) == len(sdk_ends) == 1:
        if not (starts[0]["sequence"] < sdk_starts[0]["sequence"] < sdk_ends[0]["sequence"]
                < ends[0]["sequence"] < dispatches[0]["sequence"]):
            reasons.append("model_tool_event_order_mismatch")


def retain_model_invocation_context(service: Any, *, name: str, context: Any,
                                    run_store_loader: Callable[[], Any]) -> dict:
    """Return an auditable summary; provenance failures are never trade gates."""
    payload = {"schema_version": "open_stock_ai.model_invocation_context.v1",
               "account_id": service.broker.account_id, "run_id": context.run_id,
               "session_id": context.session_id, "driver_id": context.driver_id,
               "tool": {"name": name, "node_id": _scalar(context.state.get("current_plan_node_id"))},
               "provider_model_metadata": _fields(context.state.get("provider_model_metadata"), _MODEL_FIELDS),
               "execution_context": _deployment(service), "status": "unknown", "reasons": [],
               "model_turn_events": [], "sdk_turn_events": [], "model_session_events": [],
               "host_resumed_dispatch": False,
               "assurance": "local_host_sdk_event_correlation_not_independent_provider_or_loaded_code_attestation"}
    if not payload["tool"]["node_id"]:
        payload["reasons"].append("host_plan_node_id_missing")
    else:
        try:
            _collect(payload, context=context, name=name, run_store=run_store_loader())
        except Exception as exc:
            # Do not expose exception text, SQL, paths or credentials. A read
            # failure leaves protection available and the evidence unknown.
            payload["reasons"].append("runtime_receipt_read_failed:" + type(exc).__name__)
    payload["reasons"] = sorted(set(payload["reasons"]))
    if not payload["reasons"]:
        payload["status"] = "host_sdk_turn_correlated"
    result = {"status": payload["status"], "reasons": payload["reasons"], "retention_status": "retained"}
    if payload["tool"].get("call_id"):
        result["tool_call_id"] = payload["tool"]["call_id"]
    try:
        result["context_receipt_id"] = service._retain("model_invocation_context", payload)
    except Exception as exc:
        result.update(status="unknown", retention_status="unavailable",
                      reasons=[*payload["reasons"], "context_retention_failed:" + type(exc).__name__])
    return result
