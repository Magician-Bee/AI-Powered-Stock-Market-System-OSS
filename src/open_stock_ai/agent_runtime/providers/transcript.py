from __future__ import annotations

import json
from typing import Any
from .result_projection import project_tool_result

from ..branch_result import BranchResultCompressor
from ..untrusted_content import (
    is_external_content_tool,
    label_tool_observation,
)


def safe_arguments(value: Any) -> Any:
    """Return a provider-safe copy with credential-like fields redacted."""

    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            lowered = str(key).casefold()
            if any(
                token in lowered
                for token in ("password", "secret", "token", "api_key", "authorization")
            ):
                safe[key] = "[redacted]"
            else:
                safe[key] = safe_arguments(item)
        return safe
    if isinstance(value, list):
        return [safe_arguments(item) for item in value]
    return value


def estimate_context_tokens(value: Any) -> int:
    """Use a stable, provider-neutral estimate for Host budget enforcement."""

    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        encoded = str(value)
    return max(1, (len(encoded) + 3) // 4)


def compact_replay_result(observation: dict[str, Any], *, max_chars: int = 6_000) -> Any:
    """Keep bounded evidence and protocol receipts for the next model turn."""

    result = observation.get("result")
    if not isinstance(result, dict):
        return project_tool_result(safe_arguments(result), max_chars=max_chars)
    if result.get("schema_version") == "open_stock_ai.ui_command_result.v1":
        acknowledgement = result.get("acknowledgement")
        return project_tool_result({
            "schema_version": result["schema_version"],
            "acknowledgement": safe_arguments(acknowledgement)
            if isinstance(acknowledgement, dict)
            else None,
        }, max_chars=max_chars)
    if result.get("schema_version") in {
        "stock_ai.institutional_flow_evidence.v1",
        "stock_ai.monthly_revenue_evidence.v1",
    }:
        return project_tool_result({
            "schema_version": result.get("schema_version"),
            "symbol": result.get("symbol"),
            "count": result.get("count"),
            "data_status": result.get("data_status"),
            "items": safe_arguments(list(result.get("items") or [])[:12]),
        }, max_chars=max_chars)
    return project_tool_result(safe_arguments(result), max_chars=max_chars)


def result_summary(observation: dict[str, Any]) -> dict[str, Any]:
    """Project a durable tool observation into a bounded provider summary."""

    if not observation.get("ok"):
        return observation.get("error") or {}
    result = observation.get("result")
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    if result.get("schema_version") == "stock_ai.taifex_foreign_open_interest.v1":
        return {
            "schema_version": result.get("schema_version"),
            "latest_trade_date": result.get("latest_trade_date"),
            "interpretation": result.get("interpretation"),
            "position": result.get("position"),
            "source": result.get("source"),
        }
    if result.get("schema_version") == "open_stock_ai.web_research.v1":
        return {
            "schema_version": result.get("schema_version"),
            "query": result.get("query"),
            "search_result_count": result.get("search_result_count"),
            "source_count": result.get("source_count"),
            "search_providers": result.get("search_providers") or [],
        }
    keys = (
        "schema_version",
        "symbol",
        "count",
        "recommendation_bucket",
        "execution_permission",
        "status",
        "execution_boundary",
    )
    return {key: result.get(key) for key in keys if key in result}


def _validation_provenance(observation: dict[str, Any]) -> dict[str, Any] | None:
    """Reference the one full validation beside a result, without repeating it."""

    validation = observation.get("validation")
    if not isinstance(validation, dict):
        return None
    return {
        key: validation[key]
        for key in ("schema_version", "passed", "validator", "evidence_hash")
        if key in validation
    }


def _provider_validation(value: Any) -> Any:
    """Keep failed checks and all receipts; abbreviate successful check detail."""

    if not isinstance(value, dict) or not isinstance(value.get("checks"), list):
        return value
    return {
        **value,
        "checks": [
            {"name": check["name"], "passed": True}
            if isinstance(check, dict) and check.get("passed") is True and check.get("name")
            else check
            for check in value["checks"]
        ],
    }


def _provider_policy_feedback(value: Any, disclosed_tools: set[str]) -> Any:
    """Align repair guidance with this turn's actual tool surface, without truncation."""

    if not isinstance(value, dict):
        return value
    projected = dict(value)
    available = value.get("available_tools")
    if isinstance(available, list) and all(isinstance(name, str) for name in available):
        projected["available_tools"] = [name for name in available if name in disclosed_tools]
        projected["available_tools_scope"] = "current_host_disclosed_tool_schemas"
        projected["full_registry_tool_count"] = len(available)
    for key in ("completion_validation", "validation"):
        if key in projected:
            projected[key] = _provider_validation(projected[key])
    return projected


def provider_conversation_history(
    session_history: list[dict[str, Any]],
    *,
    task_kind: str,
) -> list[dict[str, Any]]:
    """Keep dialogue continuity without treating old answers as current evidence."""

    current_information_tasks = {
        "market_information",
        "market_decision",
        "market_radar",
        "current_information",
    }
    projected: list[dict[str, Any]] = []
    for item in session_history[-40:]:
        if not isinstance(item, dict):
            continue
        entry = safe_arguments(item)
        if (
            task_kind in current_information_tasks
            and str(entry.get("role") or "").casefold() == "assistant"
        ):
            entry = {
                **entry,
                "content": {
                    "summary": (
                        "Historical assistant output is conversational context only. "
                        "Do not cite or copy it as evidence; use this run's Host tool results."
                    )
                },
                "source": {
                    **(
                        entry.get("source")
                        if isinstance(entry.get("source"), dict)
                        else {}
                    ),
                    "evidence_scope": "historical_non_evidentiary",
                },
            }
        projected.append(entry)
    return projected


def provider_transcript_v2(
    *,
    package: Any,
    trace: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose only scoped context plus a bounded Host-result handoff."""

    evidence_ids = [
        str(item.get("call_id") or item.get("node_id"))
        for item in trace
        if item.get("ok") is True and (item.get("call_id") or item.get("node_id"))
    ]
    compressed = BranchResultCompressor().compress(
        conclusion=(
            f"Host completed {sum(1 for item in trace if item.get('ok') is True)} "
            "validated observations for the active branch."
        ),
        confidence=1.0,
        raw_outputs=[result_summary(item) for item in trace[-20:]],
        evidence_ids=evidence_ids[-20:],
        contradictions=[
            str((item.get("error") or {}).get("message") or item.get("tool") or "tool_failed")
            for item in trace[-20:]
            if item.get("ok") is False
        ],
        branch_id=str((package.context.get("active_branch") or {}).get("branch_id") or ""),
    )
    compact: list[dict[str, Any]] = [
        {
            "role": "host",
            "type": "context_package.v2",
            "content": package.to_provider_payload(),
        },
        {
            "role": "host",
            "type": "branch_result.compressed",
            "content": compressed.to_context(),
        },
    ]
    history = next(
        (item for item in transcript if item.get("type") == "conversation_history"),
        None,
    )
    if history is not None:
        history_items = list(history.get("content") or [])[-8:]
        compact.append(
            {
                "role": "host",
                "type": "conversation_history",
                "content": safe_arguments(history_items),
            }
        )

    def append_tool_results() -> None:
        if not trace:
            return
        recent_trace = trace[-12:]
        result_budget = min(8_000, 24_000 // len(recent_trace))

        def provider_result(item: dict[str, Any]) -> Any:
            tool = str(item.get("tool") or "unknown_tool")
            result = compact_replay_result(item, max_chars=result_budget)
            if not is_external_content_tool(tool):
                return result
            return label_tool_observation(
                result,
                tool=tool,
                call_id=str(item.get("call_id") or "") or None,
                provenance=_validation_provenance(item),
            )

        def provider_summary(item: dict[str, Any]) -> Any:
            tool = str(item.get("tool") or "unknown_tool")
            if not is_external_content_tool(tool):
                return result_summary(item)
            return label_tool_observation(
                result_summary(item),
                tool=tool,
                call_id=str(item.get("call_id") or "") or None,
                provenance=_validation_provenance(item),
            )

        compact.append(
            {
                "role": "host",
                "type": "tool_results",
                "content": [
                    {
                        "call_id": item.get("call_id"),
                        "tool": item.get("tool"),
                        "ok": item.get("ok"),
                        "result_summary": provider_summary(item),
                        "result": provider_result(item),
                        "validation": _provider_validation(item.get("validation")),
                    }
                    for item in recent_trace
                ],
            }
        )

    latest_is_policy_feedback = (
        bool(transcript) and transcript[-1].get("type") == "policy_feedback"
    )
    if latest_is_policy_feedback:
        append_tool_results()
    for item in transcript[-6:]:
        if item.get("type") not in {
            "control_message",
            "interaction_response",
            "information_clarification_auto_continue",
            "policy_feedback",
            "research_plan",
            "critic_evidence_budget_closed",
            "critic_join_completed",
        }:
            continue
        compact.append(
            {
                "role": str(item.get("role") or "host"),
                "type": str(item.get("type")),
                "content": safe_arguments(
                    _provider_policy_feedback(
                        item.get("content"),
                        {str(tool.get("name")) for tool in package.tool_schemas},
                    )
                    if item.get("type") == "policy_feedback"
                    else item.get("content")
                ),
            }
        )
    if not latest_is_policy_feedback:
        append_tool_results()
    return compact


def transcript_as_json(transcript: tuple[dict[str, Any], ...]) -> str:
    return json.dumps(list(transcript), ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "compact_replay_result",
    "estimate_context_tokens",
    "provider_conversation_history",
    "provider_transcript_v2",
    "result_summary",
    "safe_arguments",
    "transcript_as_json",
]
