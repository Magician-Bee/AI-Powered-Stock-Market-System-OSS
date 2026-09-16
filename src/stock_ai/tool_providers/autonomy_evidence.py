"""Resolve proposal citations through Host receipts, never model-supplied bodies."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from open_stock_ai.execution.trading_plan import content_hash


def _owned(campaign, identifier):
    with campaign.plans.store._connect() as conn:
        row = conn.execute("select kind,payload_json from autonomous_evidence where account_id=? and evidence_id=?",
                           (campaign.broker.account_id, identifier)).fetchone()
    if not row:
        raise ValueError("owned_retained_evidence_required")
    payload = json.loads(row[1])
    if identifier != "AE-" + content_hash({"account_id": campaign.broker.account_id, "kind": row[0], "payload": payload}):
        raise ValueError("retained_evidence_hash_mismatch")
    return payload


def _account_scope(value: Any, account_id: str):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"account_id", "broker_account_id", "portfolio_account_id"} and child not in (None, "", account_id):
                raise ValueError("tool_evidence_account_mismatch")
            _account_scope(child, account_id)
    elif isinstance(value, list):
        for child in value:
            _account_scope(child, account_id)


def resolve_proposal_evidence_ids(campaign, *, identifiers, context, run_store):
    """Return stable account-owned AE IDs for current-run successful citations.

    The safe checkpoint contains the full result; tool call rows contain only
    summaries. Both must agree with the Host validator's original result hash.
    Checkpoint IDs/timestamps are deliberately excluded from retention identity,
    so later checkpoints and retries resolve to the same plan arguments.
    """
    if not isinstance(identifiers, list) or any(not isinstance(key, str) or not key.strip() for key in identifiers):
        raise ValueError("proposal_evidence_reference_invalid: identifiers_must_be_strings")
    unresolved = [key for key in identifiers if not key.startswith("AE-")]
    trace, calls = {}, {}
    if unresolved:
        with run_store._connect() as conn:
            run = conn.execute("select session_id,request_json from agent_runs where run_id=?", (context.run_id,)).fetchone()
            checkpoint = conn.execute("select session_id,payload_json,snapshot_hash from agent_checkpoints where run_id=? and status='safe' order by sequence desc,created_at desc limit 1",
                                      (context.run_id,)).fetchone()
        if not run or run[0] != context.session_id or not checkpoint or checkpoint[0] != context.session_id:
            raise ValueError("proposal_evidence_reference_invalid: owned_run_checkpoint_required")
        scope = (json.loads(run[1]).get("metadata") or {}).get("autonomous_model_review") or {}
        if scope.get("account_id", campaign.broker.account_id) != campaign.broker.account_id:
            raise ValueError("proposal_evidence_reference_invalid: run_account_mismatch")
        if hashlib.sha256(checkpoint[1].encode()).hexdigest() != checkpoint[2]:
            raise ValueError("proposal_evidence_reference_invalid: checkpoint_integrity_failure")
        for row in json.loads(checkpoint[1]).get("trace", []):
            if row.get("call_id") in unresolved:
                if row["call_id"] in trace:
                    raise ValueError("proposal_evidence_reference_invalid: ambiguous_tool_call")
                trace[row["call_id"]] = row
        calls = {row["tool_call_id"]: row for row in run_store.tool_calls(context.run_id)}
    resolved = []
    for identifier in identifiers:
        try:
            if identifier.startswith("AE-"):
                _owned(campaign, identifier)
                resolved.append(identifier)
                continue
            row, call = trace.get(identifier), calls.get(identifier)
            if not row or not call or call["run_id"] != context.run_id or call["status"] != "completed" or row.get("ok") is not True:
                raise ValueError("successful_current_run_tool_call_required")
            if call.get("risk_class") != "read_only" or row.get("tool") != call["tool_name"]:
                raise ValueError("read_only_tool_receipt_required")
            if content_hash(row.get("arguments") or {}) != content_hash(call.get("arguments_redacted") or {}):
                raise ValueError("tool_arguments_receipt_mismatch")
            event = call.get("result_summary") or {}
            provenance = event.get("provenance") or (event.get("payload") or {}).get("provenance") or {}
            if (event.get("run_id") != context.run_id or event.get("session_id") != context.session_id
                    or event.get("type") != "tool.completed" or event.get("call_id") != identifier
                    or provenance.get("success") is not True or provenance.get("request_id") != identifier
                    or provenance.get("tool") != row["tool"] or not provenance.get("provider")):
                raise ValueError("host_tool_provenance_mismatch")
            result = row.get("result")
            digest = content_hash(result)
            for validation in (row.get("validation"), call.get("validation")):
                if not validation or validation.get("passed") is not True or validation.get("evidence_hash") != digest:
                    raise ValueError("validated_tool_result_hash_required")
            if not isinstance(result, dict):
                raise ValueError("structured_tool_evidence_required")
            if row["tool"] == "autonomy.evidence":
                requested = row["arguments"].get("evidence_ids")
                if requested:
                    items = result.get("evidence")
                    if (result.get("schema_version") != "open_stock_ai.autonomy_evidence_batch.v1"
                            or not isinstance(items, list)
                            or [item.get("evidence_id") for item in items] != requested):
                        raise ValueError("retained_batch_evidence_identity_mismatch")
                    canonicals = []
                    for item in items:
                        canonical = item.get("evidence_id")
                        if content_hash(_owned(campaign, canonical)) != item.get("retained_payload_sha256"):
                            raise ValueError("retained_batch_evidence_hash_mismatch")
                        canonicals.append(canonical)
                    resolved.extend(canonicals)
                    continue
                canonical = result.get("evidence_id")
                if (canonical != row["arguments"].get("evidence_id")
                        or content_hash(_owned(campaign, canonical)) != result.get("retained_payload_sha256")):
                    raise ValueError("retained_tool_evidence_identity_mismatch")
            elif row["tool"] == "autonomy.research":
                retained = campaign.cycle(result["cycle_id"])
                if retained != result:
                    raise ValueError("retained_cycle_result_mismatch")
                canonical = retained["bulk_evidence_id"]
                _owned(campaign, canonical)
            else:
                # This legacy aggregate includes another account's portfolio
                # beside public research. Preserve only its market observations.
                omitted = [key for key in ("paper_position", "paper_account_summary", "learning_for_symbol", "pipeline_workspace")
                           if row["tool"] == "market.research_pack" and key in result]
                view = {key: value for key, value in result.items() if key not in omitted}
                _account_scope(view, campaign.broker.account_id)
                canonical = campaign._retain("tool_observation", {
                    "schema_version": "open_stock_ai.autonomous_tool_evidence.v1", "account_id": campaign.broker.account_id,
                    "run_id": context.run_id, "session_id": context.session_id, "tool_call_id": identifier,
                    "tool_name": row["tool"], "source_event_id": event.get("event_id"),
                    "completed_at": call.get("completed_at"), "host_tool_provenance": provenance,
                    "result_sha256": digest, "result_view_sha256": content_hash(view), "result_view": view,
                    "omitted_non_campaign_fields": omitted, "source_provenance_verified": False,
                    "evidence_scope": "validated_host_tool_observation; not_independent_source_verification_or_current_account_state",
                })
            resolved.append(canonical)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"proposal_evidence_reference_invalid: {identifier}: {exc}") from exc
    return list(dict.fromkeys(resolved))
