from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from .trace_contract import stable_span_id, stable_trace_id


SENSITIVE_EVENT_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "api-key",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "private-key",
)


FINAL_RUNTIME_EVENT_TYPES = frozenset(
    {
        "session.created", "session.title.updated", "objective.created", "objective.revised",
        "forest.created", "forest.revised", "branch.created", "branch.started", "branch.paused",
        "branch.resumed", "branch.completed", "branch.failed", "branch.repaired", "branch.cancelled",
        "join.started", "join.conflict_detected", "join.completed", "plan.created", "plan.revised",
        "step.started", "step.completed", "step.failed", "tool.requested", "tool.started",
        "tool.progress", "tool.completed", "tool.failed", "research.started",
        "research.query_created", "research.source_found", "research.source_opened",
        "research.evidence_added", "research.evidence_selected", "research.conflict_detected", "reflection.started",
        "reflection.completed", "reflection.evidence_rejected", "interaction.proposed", "interaction.awaiting_user",
        "interaction.user_answered", "proposal.received", "proposal.evaluated", "proposal.accepted",
        "proposal.modified", "proposal.rejected", "repair.started", "repair.host_applied",
        "repair.model_requested", "repair.strategy_changed", "repair.completed",
        "automation.intent_created", "automation.compiling", "automation.validating",
        "automation.testing", "automation.publishing", "automation.active", "automation.paused",
        "automation.failed", "automation.expired", "memory.candidate", "memory.saved",
        "memory.updated", "memory.superseded", "artifact.created", "artifact.updated",
        "artifact.selected", "result.draft", "result.final",
    }
)


def redact_runtime_value(value: Any) -> Any:
    """Recursively redact secrets before an event can reach storage or a client."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            redacted[key] = (
                "[redacted]"
                if any(part in lowered for part in SENSITIVE_EVENT_KEY_PARTS)
                else redact_runtime_value(item)
            )
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_runtime_value(item) for item in value]
    return value


def build_runtime_event(
    event_type: str,
    *,
    sequence: int,
    run_id: str,
    session_id: str,
    payload: dict[str, Any] | None = None,
    plan_id: str | None = None,
    plan_revision: int | None = None,
    previous_event_id: str | None = None,
    visibility: str | None = None,
    timestamp: str | None = None,
    objective_id: str | None = None,
    forest_id: str | None = None,
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Build the durable event envelope while retaining compatibility aliases."""
    safe_payload = redact_runtime_value(payload or {})
    step_id = str(
        safe_payload.get("step_id")
        or safe_payload.get("node_id")
        or safe_payload.get("step")
        or ""
    ) or None
    tool_call_id = str(
        safe_payload.get("tool_call_id") or safe_payload.get("call_id") or ""
    ) or None
    approval = safe_payload.get("approval")
    approval_id = str(
        safe_payload.get("approval_id")
        or (approval.get("approval_id") if isinstance(approval, dict) else "")
        or ""
    ) or None
    event_id = f"ARE-{uuid4().hex}"
    resolved_visibility = visibility or (
        "debug"
        if event_type.startswith(("model.sdk.", "provider.conformance."))
        else "user"
    )
    summary = str(
        safe_payload.get("summary")
        or safe_payload.get("reason_summary")
        or safe_payload.get("label")
        or event_type.replace(".", " ")
    )
    correlation_id = str(
        safe_payload.get("correlation_id")
        or tool_call_id
        or step_id
        or run_id
    )
    trace_id = str(safe_payload.get("trace_id") or stable_trace_id(run_id))
    span_id = str(safe_payload.get("span_id") or stable_span_id(event_id))
    parent_span_id = safe_payload.get("parent_span_id")
    if parent_span_id is None and previous_event_id:
        parent_span_id = stable_span_id(previous_event_id)
    envelope = {
        "event_id": event_id,
        "sequence": int(sequence),
        "session_id": session_id,
        "run_id": run_id,
        "objective_id": objective_id or safe_payload.get("objective_id"),
        "forest_id": forest_id or safe_payload.get("forest_id"),
        "branch_id": branch_id or safe_payload.get("branch_id"),
        "plan_id": plan_id,
        "plan_revision": plan_revision,
        "step_id": step_id,
        "node_id": safe_payload.get("node_id") or step_id,
        "parent_step_id": safe_payload.get("parent_step_id"),
        "tool_call_id": tool_call_id,
        "approval_id": approval_id,
        "artifact_id": safe_payload.get("artifact_id"),
        "type": event_type,
        "status": safe_payload.get("status"),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "payload": safe_payload,
        "visibility": resolved_visibility,
        "redacted": True,
        "correlation_id": correlation_id,
        "causation_id": safe_payload.get("causation_id") or previous_event_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
    }
    # Older API/UI consumers use flat fields. They remain aliases of the
    # already-redacted payload, never an independent or less-safe source.
    reserved_keys = set(envelope)
    envelope.update(
        {
            key: value
            for key, value in safe_payload.items()
            if key not in reserved_keys
        }
    )
    return envelope


@dataclass(frozen=True, slots=True)
class AgentToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    category: str
    output_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    risk_class: str = "read_only"
    required_permissions: tuple[str, ...] = ()
    resource_scope: str = "stock_ai"
    timeout_seconds: int = 120
    retry_policy: dict[str, Any] = field(
        default_factory=lambda: {"max_attempts": 1, "backoff": "none"}
    )
    idempotency: str = "none"
    side_effects: tuple[str, ...] = ()
    preconditions: tuple[dict[str, Any], ...] = ()
    postconditions: tuple[dict[str, Any], ...] = ()
    validator: str = "default"
    rollback_support: bool = False
    execution_backend: str = "in_process"
    event_visibility: str = "arguments_results_evidence"
    long_running: bool = False
    mutating: bool = False
    destructive: bool = False
    requires_paper_execution: bool = False
    requires_project_execution: bool = False
    requires_external_execution: bool = False
    requires_full_execution: bool = False
    skills: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    schedules: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        risk_class = self.risk_class
        if self.mutating and risk_class == "read_only":
            if self.destructive:
                risk_class = "local_destructive"
            elif self.requires_paper_execution:
                risk_class = "financial_paper"
            elif self.requires_external_execution:
                risk_class = "external_side_effect"
            else:
                risk_class = "local_reversible"
        permissions = list(self.required_permissions)
        for required, permission in (
            (self.requires_project_execution, "project_execute"),
            (self.requires_paper_execution, "paper_execute"),
            (self.requires_external_execution, "external_execute"),
            (self.requires_full_execution, "full_execute"),
        ):
            if required and permission not in permissions:
                permissions.append(permission)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "category": self.category,
            "risk_class": risk_class,
            "required_permissions": permissions,
            "resource_scope": self.resource_scope,
            "timeout_seconds": self.timeout_seconds,
            "retry_policy": dict(self.retry_policy),
            "idempotency": self.idempotency,
            "side_effects": list(self.side_effects),
            "preconditions": list(self.preconditions),
            "postconditions": list(self.postconditions),
            "validator": self.validator,
            "rollback_support": self.rollback_support,
            "execution_backend": self.execution_backend,
            "event_visibility": self.event_visibility,
            "long_running": self.long_running,
            "mutating": self.mutating,
            "destructive": self.destructive,
            "requires_paper_execution": self.requires_paper_execution,
            "requires_project_execution": self.requires_project_execution,
            "requires_external_execution": self.requires_external_execution,
            "requires_full_execution": self.requires_full_execution,
            "skills": list(self.skills),
            "packages": list(self.packages),
            "schedules": list(self.schedules),
        }


@dataclass(slots=True)
class AgentRunContext:
    run_id: str
    autonomy: str
    symbols: tuple[str, ...]
    driver_id: str = "codex"
    session_id: str = ""
    parent_run_id: str | None = None
    allow_paper_orders: bool = False
    allow_project_actions: bool = False
    allow_external_actions: bool = False
    previewed_orders: set[str] = field(default_factory=set)
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentTurnInput:
    run_id: str
    objective: str
    system_prompt: str
    transcript: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...]
    output_schema: dict[str, Any]
    metadata: dict[str, Any]
    event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "protocol": "open_stock_ai.agent_turn.v1",
            "run_id": self.run_id,
            "objective": self.objective,
            "system_prompt": self.system_prompt,
            "transcript": list(self.transcript),
            "tools": list(self.tools),
            "output_schema": self.output_schema,
            "metadata": self.metadata,
        }


class AgentDriver(Protocol):
    driver_id: str

    def describe(self) -> dict[str, Any]: ...

    async def start_run(self, context: AgentRunContext) -> None: ...

    async def decide(self, turn: AgentTurnInput) -> dict[str, Any]: ...

    async def close_run(self, run_id: str) -> None: ...


class AgentToolRegistry(Protocol):
    def manifest(self) -> list[dict[str, Any]]: ...

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]: ...
