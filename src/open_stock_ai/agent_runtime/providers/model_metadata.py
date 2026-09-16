from __future__ import annotations

from typing import Any

from ..evidence_projection import _now


def restore_provider_model_metadata(context: Any, resume_state: dict[str, Any] | None) -> None:
    """Restore the prior run's server-resolved selection before provider startup."""
    checkpoint = (resume_state or {}).get("checkpoint") or {}
    restored = (checkpoint.get("payload") or {}).get("context_state") or {}
    metadata = restored.get("provider_model_metadata")
    if isinstance(metadata, dict):
        context.state["provider_model_metadata"] = dict(metadata)
    else:
        selection = (resume_state or {}).get("provider_model_selection")
        if isinstance(selection, dict) and selection.get("provider") == context.driver_id == "codex":
            context.state["provider_model_metadata"] = dict(selection)


def host_review_provider_selection(request: dict[str, Any]) -> dict[str, Any] | None:
    """Read only the internal Host request field, never intent/tool arguments.

    The HTTP run request does not expose run_metadata; its projection allowlists
    context_scope and presentation-only intent. Binding the reserved identity
    here also keeps arbitrary metadata on ordinary runs from changing models.
    """
    from ...execution.trading_plan import content_hash
    from ..autonomy_contract import autonomous_pipeline_requested

    review = (request.get("metadata") or {}).get("autonomous_model_review")
    if not isinstance(review, dict) or request.get("driver_id") != "codex" or request.get("autonomy") != "paper_execute":
        return None
    account_id, cycle_id = review.get("account_id"), review.get("cycle_id")
    if (not account_id or not cycle_id or not autonomous_pipeline_requested(str(request.get("objective") or ""))
            or request.get("idempotency_key") != "autonomous-model-review:" + content_hash([account_id, cycle_id])):
        return None
    selection = review.get("provider_model_selection")
    if not isinstance(selection, dict) or selection.get("provider") != "codex":
        return None
    if any(not isinstance(selection.get(key, ""), str) for key in ("model", "reasoning_effort")):
        return None
    return {**{key: selection.get(key, "") for key in ("provider", "model", "reasoning_effort")},
            "selection_source": "host_configured_selection"}


def provider_session_metadata(driver: Any, run_id: str) -> dict[str, Any]:
    read_metadata = getattr(driver, "session_metadata", None)
    return read_metadata(run_id) if callable(read_metadata) else {}


def model_invocation_receipts(
    activity: list[dict[str, Any]], driver: str, model_id: str,
) -> list[dict[str, Any]]:
    """Project public model events into run-local invocation evidence."""
    receipts: dict[str, dict[str, Any]] = {}
    for event in activity:
        event_type = str(event.get("type") or "")
        call_id = str(event.get("model_call_id") or "")
        if not call_id or event_type not in {
            "model.turn.started", "model.turn.completed", "model.turn.failed",
        }:
            continue
        receipt = receipts.setdefault(call_id, {
            "call_id": call_id,
            "provider": str(event.get("driver") or driver),
            "model_id": str(event.get("model") or model_id),
            "reasoning_effort": event.get("reasoning_effort"),
            "status": "not_run",
            "started_at": str(event.get("timestamp") or _now()),
            "completed_at": str(event.get("timestamp") or _now()),
            "error_type": None,
            "error_message": None,
            "raw_output_preserved": False,
        })
        if event_type == "model.turn.started":
            receipt["started_at"] = str(event.get("timestamp") or receipt["started_at"])
        elif event_type == "model.turn.completed":
            receipt["status"] = "succeeded"
            receipt["completed_at"] = str(event.get("timestamp") or _now())
        else:
            receipt["status"] = "failed"
            receipt["completed_at"] = str(event.get("timestamp") or _now())
            receipt["error_type"] = str(event.get("error_type") or "provider_error")
            receipt["error_message"] = str(event.get("error") or "Model invocation failed")
    return list(receipts.values())
