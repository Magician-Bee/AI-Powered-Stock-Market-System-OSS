from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class ProviderCapabilityProfile(BaseModel):
    provider: str
    model: str | None = None
    context_window: int | None = None
    native_tool_calling: bool = False
    json_schema: bool = False
    parallel_tool_calls: bool = False
    streaming: bool = False
    reasoning_format: Literal["none", "think_xml", "opaque", "unknown"] = "unknown"
    persistent_session: bool = False
    schema_fallback: bool = True
    recommended_protocol: Literal["universal_v1", "advanced_v1"] = "universal_v1"
    conformance_passed: bool = False


class ProviderConformanceCheck(BaseModel):
    capability: str
    passed: bool
    measured: bool = True
    detail: str = ""


class ProviderConformanceReport(BaseModel):
    schema_version: Literal["open_stock_ai.provider_conformance.v1"] = (
        "open_stock_ai.provider_conformance.v1"
    )
    provider: str
    model: str | None = None
    checked_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    session_id: str
    checks: list[ProviderConformanceCheck]
    core_passed: bool
    recommended_protocol: Literal["universal_v1", "advanced_v1"]


class ProviderConformanceSuite:
    """Execute behavioral capability probes against a real provider interface."""

    UNIVERSAL_SCHEMA: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "answer", "actions"],
        "properties": {
            "status": {"type": "string", "enum": ["final", "need_tools"]},
            "answer": {"type": "string"},
            "actions": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["tool", "arguments"],
                    "properties": {
                        "tool": {"type": "string"},
                        "arguments": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {},
                        },
                    },
                },
            },
        },
    }

    async def probe(
        self,
        provider: Any,
        *,
        session_id: str,
        include_expensive: bool = False,
    ) -> ProviderConformanceReport:
        capabilities = provider.capabilities()
        raw_profile = capabilities.get("profile") if isinstance(capabilities, dict) else {}
        profile = ProviderCapabilityProfile.model_validate(
            raw_profile
            or {
                "provider": str(getattr(provider, "provider_id", "unknown")),
                "model": capabilities.get("model") if isinstance(capabilities, dict) else None,
            }
        )
        nonce = f"CONF-{uuid4().hex[:10]}"
        padding = ("長內容測試。" * 1400) if include_expensive else ""
        prompt = (
            "這是 Provider conformance probe，不是使用者任務。請回傳繁體中文 answer，"
            f"內容必須包含「能力測試通過」與 {nonce}。actions 必須依序包含兩個唯讀工具："
            "system.capabilities 與 market.search_taiwan_securities，arguments 使用 JSON object。"
            f"{padding}"
        )
        checks: list[ProviderConformanceCheck] = []
        try:
            result = await provider.generate_structured(
                session_id,
                prompt,
                self.UNIVERSAL_SCHEMA,
            )
        except Exception as exc:
            checks.extend(
                [
                    ProviderConformanceCheck(
                        capability=name,
                        passed=False,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                    for name in (
                        "json_object",
                        "json_schema",
                        "tool_calling",
                        "parallel_tools",
                        "traditional_chinese",
                        "reasoning_format",
                        "long_context",
                    )
                ]
            )
        else:
            answer = str(result.get("answer") or result.get("summary") or "")
            actions = result.get("actions") or result.get("tool_calls") or []
            action_names = [
                str(item.get("tool") or item.get("name") or "")
                for item in actions
                if isinstance(item, dict)
            ]
            checks.extend(
                [
                    ProviderConformanceCheck(
                        capability="json_object",
                        passed=isinstance(result, dict),
                        detail="Provider returned one normalized JSON object",
                    ),
                    ProviderConformanceCheck(
                        capability="json_schema",
                        passed=(
                            str(result.get("status") or "") in {"final", "need_tools"}
                            or str(result.get("state") or "") in {"complete", "continue"}
                        ),
                        detail="Required state/status field was returned",
                    ),
                    ProviderConformanceCheck(
                        capability="tool_calling",
                        passed="system.capabilities" in action_names,
                        detail=f"observed actions={action_names}",
                    ),
                    ProviderConformanceCheck(
                        capability="parallel_tools",
                        passed={
                            "system.capabilities",
                            "market.search_taiwan_securities",
                        }.issubset(action_names),
                        detail=f"observed actions={action_names}",
                    ),
                    ProviderConformanceCheck(
                        capability="traditional_chinese",
                        passed="能力測試通過" in answer,
                        detail="Traditional-Chinese marker check",
                    ),
                    ProviderConformanceCheck(
                        capability="reasoning_format",
                        passed="<think>" not in answer.casefold(),
                        detail="No reasoning tags leaked into normalized answer",
                    ),
                    ProviderConformanceCheck(
                        capability="long_context",
                        passed=nonce in answer if include_expensive else False,
                        measured=include_expensive,
                        detail=(
                            "Nonce survived long-context probe"
                            if include_expensive
                            else "Skipped during lightweight negotiation"
                        ),
                    ),
                ]
            )
        checks.extend(
            [
                ProviderConformanceCheck(
                    capability="streaming",
                    passed=profile.streaming,
                    measured=False,
                    detail="Provider interface declaration; use the full E2E probe to measure stream events",
                ),
                ProviderConformanceCheck(
                    capability="persistent_session",
                    passed=profile.persistent_session,
                    measured=False,
                    detail="Provider interface declaration; session continuity is audited by run E2E",
                ),
            ]
        )
        required = {
            "json_object",
            "json_schema",
            "tool_calling",
            "traditional_chinese",
            "reasoning_format",
        }
        core_passed = all(
            item.passed
            for item in checks
            if item.capability in required
        ) and required.issubset({item.capability for item in checks})
        recommended = (
            "advanced_v1"
            if core_passed
            and profile.recommended_protocol == "advanced_v1"
            and profile.native_tool_calling
            and profile.json_schema
            else "universal_v1"
        )
        return ProviderConformanceReport(
            provider=profile.provider,
            model=profile.model,
            session_id=session_id,
            checks=checks,
            core_passed=core_passed,
            recommended_protocol=recommended,
        )


def negotiated_profile(
    profile: ProviderCapabilityProfile,
    report: ProviderConformanceReport,
) -> ProviderCapabilityProfile:
    return profile.model_copy(
        update={
            "conformance_passed": report.core_passed,
            "recommended_protocol": report.recommended_protocol,
        }
    )
