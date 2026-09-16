from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .intent import AutomationIntent, AutomationKind
from .model_routing import ModelRoleRouter


_COMPILER_SEAL = "open-stock-ai-automation-compiler-v1"


def canonical_compiler_digest(
    backend: str,
    definition: Mapping[str, Any],
    credential_refs: tuple[str, ...] | list[str],
) -> str:
    """Return the canonical, deployable compiler-product digest.

    Backends must recompute this value before they accept a serialized
    compiler product.  A correctly shaped 64-character digest alone proves
    nothing about whether its workflow definition was altered after compile.
    """
    encoded = json.dumps(
        {
            "backend": str(backend),
            "definition": dict(definition),
            "credential_refs": list(credential_refs),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    backend: str
    definition: Mapping[str, Any]
    user_artifact: Mapping[str, Any]
    credential_refs: tuple[str, ...]
    model_routes: Mapping[str, Mapping[str, str | None]]
    digest: str
    compiler_seal: str = _COMPILER_SEAL
    schema_version: str = "open_stock_ai.compiled_automation.v1"


@dataclass(frozen=True, slots=True)
class WorkflowValidation:
    valid: bool
    errors: tuple[str, ...]


class WorkflowCompiler:
    def __init__(self, role_router: ModelRoleRouter | None = None) -> None:
        self.role_router = role_router or ModelRoleRouter()

    def compile(self, intent: AutomationIntent, *, credential_refs: tuple[str, ...] = ()) -> CompiledWorkflow:
        backend = self._backend(intent)
        if backend == "n8n" and any(not _valid_credential_ref(item) for item in credential_refs):
            raise ValueError("n8n credentials must be opaque credential references")
        definition = self._definition(intent, backend)
        artifact = self._artifact(intent)
        routes = self.role_router.workflow_routes(intent.cost_policy)
        digest = canonical_compiler_digest(backend, definition, credential_refs)
        return CompiledWorkflow(backend, definition, artifact, tuple(credential_refs), routes, digest)

    def validate(self, workflow: CompiledWorkflow) -> WorkflowValidation:
        errors: list[str] = []
        if workflow.compiler_seal != _COMPILER_SEAL:
            errors.append("workflow was not produced by WorkflowCompiler")
        if workflow.backend not in {"internal_scheduler", "n8n"}:
            errors.append("unsupported automation backend")
        if not workflow.definition.get("trigger"):
            errors.append("compiled workflow has no trigger")
        if not workflow.definition.get("stages"):
            errors.append("compiled workflow has no stages")
        serialized_artifact = json.dumps(workflow.user_artifact, ensure_ascii=False).casefold()
        for secret in ("http request", "webhook url", "credential", "n8n node", "node type"):
            if secret in serialized_artifact:
                errors.append(f"user artifact leaks execution detail: {secret}")
        return WorkflowValidation(not errors, tuple(errors))

    @staticmethod
    def require_compiler_product(workflow: CompiledWorkflow) -> None:
        if not isinstance(workflow, CompiledWorkflow) or workflow.compiler_seal != _COMPILER_SEAL:
            raise TypeError("backend accepts only WorkflowCompiler output")

    def _backend(self, intent: AutomationIntent) -> str:
        trigger_type = str(intent.trigger.get("type") or "").casefold()
        interval = int(intent.trigger.get("interval_seconds") or 0)
        internal = intent.kind in {AutomationKind.CONDITION_WATCH, AutomationKind.EVENT_WATCH}
        internal = internal or trigger_type in {"price_crossing", "market_event", "condition", "event", "agent_wakeup"}
        internal = internal or (interval and interval < 3600)
        return "internal_scheduler" if internal else "n8n"

    def _definition(self, intent: AutomationIntent, backend: str) -> dict[str, Any]:
        stages = [
            {"op": "observe", "items": [dict(item) for item in intent.observations]},
            {"op": "cheap_filter", "condition": dict(intent.decision_logic)},
            {"op": "agent_reanalysis", "analysis": [dict(item) for item in intent.analysis]},
            {"op": "decision_changed_gate", "meaningful_only": intent.notification_policy.meaningful_only},
            {"op": "actions", "items": [dict(item) for item in intent.actions]},
        ]
        return {
            "engine": backend,
            "trigger": dict(intent.trigger),
            "stages": stages,
            "notification": {
                "channels": list(intent.notification_policy.channels),
                "cooldown_seconds": intent.notification_policy.cooldown_seconds,
                "allow_fallback": intent.notification_policy.allow_fallback,
            },
        }

    def _artifact(self, intent: AutomationIntent) -> dict[str, Any]:
        evidence = [str(item.get("label") or item.get("type") or "取得最新資料") for item in intent.observations]
        return {
            "schema_version": "open_stock_ai.automation_artifact.v1",
            "title": intent.goal,
            "status": "正在建立自動化流程",
            "steps": [
                *(evidence or ["取得最新資料"]),
                "分析資料與證據",
                "判斷策略是否改變",
                "無變化則記錄後結束",
                "有重大變化才重新分析並通知",
            ],
        }


def _valid_credential_ref(value: str) -> bool:
    return value.startswith(("secret://", "credential-ref://", "vault://")) and len(value) > 12
