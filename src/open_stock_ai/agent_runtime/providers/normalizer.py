from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..repair.deterministic import DeterministicRepair


REASONING_TAGS = re.compile(
    r"<(?:think|thinking|reasoning)>[\s\S]*?</(?:think|thinking|reasoning)>",
    re.IGNORECASE,
)
TRAILING_COMMA = re.compile(r",\s*([}\]])")


class ProviderProtocolError(ValueError):
    def __init__(self, code: str, message: str, *, raw_output: str) -> None:
        super().__init__(message)
        self.code = code
        self.raw_output = raw_output


@dataclass(frozen=True)
class NormalizedProviderOutput:
    payload: dict[str, Any]
    raw_output: str
    reasoning_tags_removed: bool = False
    repairs: tuple[str, ...] = ()
    schema_mapping: dict[str, str] = field(default_factory=dict)


class ProviderOutputNormalizer:
    """Normalize provider formatting without judging the answer's semantics."""

    def normalize(self, value: Any) -> NormalizedProviderOutput:
        if isinstance(value, dict):
            payload, mapping = self._map_universal_protocol(dict(value))
            return NormalizedProviderOutput(
                payload=payload,
                raw_output=json.dumps(value, ensure_ascii=False),
                schema_mapping=mapping,
            )
        raw = str(value or "")
        text = raw.strip()
        without_reasoning = REASONING_TAGS.sub("", text).strip()
        reasoning_removed = without_reasoning != text
        try:
            repaired = DeterministicRepair().repair(
                without_reasoning,
                known_wrappers=(),
            )
        except ValueError as exc:
            raise ProviderProtocolError(
                "protocol_invalid_json",
                str(exc),
                raw_output=raw,
            ) from exc
        if not isinstance(repaired.value, dict):
            raise ProviderProtocolError(
                "protocol_invalid_json",
                "Model provider did not return a JSON object",
                raw_output=raw,
            )
        strategy_names = {
            "remove_json_fence": "removed_json_fence",
            "remove_trailing_comma": "removed_trailing_comma",
            "extract_json_value": "extracted_json_object",
        }
        repairs = [strategy_names.get(item, item) for item in repaired.strategies]
        payload = dict(repaired.value)
        normalized, mapping = self._map_universal_protocol(payload)
        return NormalizedProviderOutput(
            payload=normalized,
            raw_output=raw,
            reasoning_tags_removed=reasoning_removed,
            repairs=tuple(repairs),
            schema_mapping=mapping,
        )

    @staticmethod
    def _strip_fence(text: str) -> str:
        match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
        return match.group(1).strip() if match else text

    @staticmethod
    def _decode_object(text: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _extract_object(text: str) -> dict[str, Any] | None:
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                payload, _end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    @staticmethod
    def _map_universal_protocol(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        mapping: dict[str, str] = {}
        normalized = dict(payload)
        status = str(normalized.get("status") or "").strip().casefold()
        state = str(normalized.get("state") or "").strip().casefold()
        if "state" not in normalized and status in {
            "need_tools",
            "continue",
            "in_progress",
            "final",
            "complete",
            "completed",
            "done",
            "waiting_user_input",
            "waiting_decision",
        }:
            normalized["state"] = (
                "continue"
                if status in {"need_tools", "continue", "in_progress"}
                else (
                    status
                    if status in {"waiting_user_input", "waiting_decision"}
                    else "complete"
                )
            )
            mapping["status"] = "state"
        elif state in {
            "need_tools", "in_progress", "final", "completed", "done",
            "waiting_user_input", "waiting_decision",
        }:
            normalized["state"] = (
                "continue"
                if state in {"need_tools", "in_progress"}
                else (
                    state
                    if state in {"waiting_user_input", "waiting_decision"}
                    else "complete"
                )
            )
            mapping["state_alias"] = "state"
        if "summary" not in normalized:
            if isinstance(normalized.get("answer"), str):
                normalized["summary"] = normalized["answer"]
                mapping["answer"] = "summary"
            elif isinstance(normalized.get("message"), str):
                normalized["summary"] = normalized["message"]
                mapping["message"] = "summary"
        if "tool_calls" not in normalized and isinstance(normalized.get("actions"), list):
            normalized["tool_calls"] = [
                {
                    "id": str(item.get("id") or _universal_tool_call_id(item, index)),
                    "name": item.get("tool") or item.get("name"),
                    "arguments": item.get("arguments") or {},
                }
                for index, item in enumerate(normalized["actions"])
                if isinstance(item, dict) and (item.get("tool") or item.get("name"))
            ]
            mapping["actions"] = "tool_calls"
        if "state" not in normalized:
            valid_calls = [
                item
                for item in normalized.get("tool_calls") or []
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
            if valid_calls:
                # A requested Host action can only mean "continue". Never infer
                # completion from provider-specific formatting.
                normalized["state"] = "continue"
                mapping["tool_calls"] = "state"
            elif any(
                isinstance(normalized.get(key), str) and normalized[key].strip()
                for key in ("answer", "message", "summary")
            ):
                # Completion remains subject to the orchestrator's evidence and
                # plan validators; this only repairs the missing protocol field.
                normalized["state"] = "complete"
                mapping["answer_without_actions"] = "state"
        if "structured_result" not in normalized and isinstance(normalized.get("result"), dict):
            normalized["structured_result"] = normalized["result"]
            mapping["result"] = "structured_result"
        if "routing_patch" not in normalized and isinstance(normalized.get("routing"), dict):
            normalized["routing_patch"] = normalized["routing"]
            mapping["routing"] = "routing_patch"
        if "completion_evaluation" not in normalized:
            evidence_ids = [
                str(item)
                for item in normalized.get("evidence_ids") or []
                if str(item).strip()
            ]
            remaining_gaps = [
                str(item)
                for item in normalized.get("remaining_gaps") or []
                if str(item).strip()
            ]
            normalized["completion_evaluation"] = {
                "criteria_met": normalized.get("state") == "complete" and not remaining_gaps,
                "criterion_results": [],
                "evidence_ids": evidence_ids,
                "remaining_gaps": remaining_gaps,
            }
            mapping["evidence_ids"] = "completion_evaluation"
        for item in normalized.get("tool_calls") or []:
            if not isinstance(item, dict) or not isinstance(item.get("arguments"), str):
                continue
            try:
                arguments = json.loads(item["arguments"])
            except json.JSONDecodeError:
                continue
            if isinstance(arguments, dict):
                item["arguments"] = arguments
                mapping["tool_calls.arguments"] = "parsed_json_string"
        return normalized, mapping


def _universal_tool_call_id(item: dict[str, Any], index: int) -> str:
    """Return a cross-turn stable ID for an action without a provider ID.

    The Universal schema intentionally omits an action ID. An ordinal-only ID
    (``universal-1``) collides as soon as a later model turn requests a
    different first tool, corrupting evidence bindings on durable replay.
    Binding the generated ID to tool identity and arguments keeps exact retries
    idempotent while making distinct calls unique across the whole Run.
    """

    identity = {
        "tool": item.get("tool") or item.get("name"),
        "arguments": item.get("arguments") or {},
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"universal-{index + 1}-{digest}"
