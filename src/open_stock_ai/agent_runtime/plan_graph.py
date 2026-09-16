from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


PLAN_NODE_TYPES = {
    "reasoning",
    "tool",
    "subtask",
    "subagent",
    "approval",
    "schedule",
    "workflow",
    "validation",
    "checkpoint",
    "finalize",
}
PLAN_NODE_STATUSES = {
    "proposed",
    "pending",
    "ready",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "blocked",
    "skipped",
    "cancelled",
}


@dataclass(slots=True)
class PlanNode:
    node_id: str
    node_type: str
    title: str
    dependencies: tuple[str, ...] = ()
    status: str = "pending"
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    postconditions: tuple[dict[str, Any], ...] = ()
    parent_id: str | None = None
    mandatory: bool = False
    description: str | None = None
    order_index: int = 0
    assigned_agent: str | None = None
    capability: str | None = None
    skill_ids: tuple[str, ...] = ()
    tool_call_ids: tuple[str, ...] = ()
    reason_summary: str | None = None
    result_summary: str | None = None
    error_summary: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.node_type not in PLAN_NODE_TYPES:
            raise ValueError(f"Unsupported plan node type: {self.node_type}")
        if self.status not in PLAN_NODE_STATUSES:
            raise ValueError(f"Unsupported plan node status: {self.status}")
        if self.node_type == "tool" and not self.tool_name:
            raise ValueError("Tool plan nodes require tool_name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "title": self.title,
            "dependencies": list(self.dependencies),
            "status": self.status,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "postconditions": list(self.postconditions),
            "parent_id": self.parent_id,
            "mandatory": self.mandatory,
            "description": self.description,
            "order_index": self.order_index,
            "assigned_agent": self.assigned_agent,
            "capability": self.capability,
            "skill_ids": list(self.skill_ids),
            "tool_call_ids": list(self.tool_call_ids),
            "reason_summary": self.reason_summary,
            "result_summary": self.result_summary,
            "error_summary": self.error_summary,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlanNode:
        return cls(
            node_id=str(value.get("node_id") or f"node-{uuid4().hex[:12]}"),
            node_type=str(value.get("node_type") or "reasoning"),
            title=str(value.get("title") or "Untitled step"),
            dependencies=tuple(str(item) for item in value.get("dependencies") or []),
            status=str(value.get("status") or "pending"),
            tool_name=str(value["tool_name"]) if value.get("tool_name") else None,
            arguments=dict(value.get("arguments") or {}),
            postconditions=tuple(
                dict(item) for item in value.get("postconditions") or [] if isinstance(item, dict)
            ),
            parent_id=str(value["parent_id"]) if value.get("parent_id") else None,
            mandatory=bool(value.get("mandatory", False)),
            description=str(value["description"]) if value.get("description") else None,
            order_index=int(value.get("order_index") or 0),
            assigned_agent=(
                str(value["assigned_agent"]) if value.get("assigned_agent") else None
            ),
            capability=str(value["capability"]) if value.get("capability") else None,
            skill_ids=tuple(str(item) for item in value.get("skill_ids") or ()),
            tool_call_ids=tuple(str(item) for item in value.get("tool_call_ids") or ()),
            reason_summary=(
                str(value["reason_summary"]) if value.get("reason_summary") else None
            ),
            result_summary=(
                str(value["result_summary"]) if value.get("result_summary") else None
            ),
            error_summary=(
                str(value["error_summary"]) if value.get("error_summary") else None
            ),
            started_at=str(value["started_at"]) if value.get("started_at") else None,
            completed_at=(
                str(value["completed_at"]) if value.get("completed_at") else None
            ),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(slots=True)
class PlanGraph:
    plan_id: str
    objective: str
    assumptions: list[str]
    constraints: list[str]
    nodes: dict[str, PlanNode]
    completion_criteria: list[str]
    fallback_rules: list[str]
    revision_number: int = 1

    @classmethod
    def create(cls, objective: str, *, plan_id: str | None = None) -> PlanGraph:
        return cls(
            plan_id=plan_id or f"AP-{uuid4().hex}",
            objective=objective.strip(),
            assumptions=[],
            constraints=[
                "Host executes every capability and validates the result.",
                "Live brokerage is unavailable.",
                "Private chain-of-thought is never exposed.",
            ],
            nodes={},
            completion_criteria=["The user objective is satisfied by validated evidence."],
            fallback_rules=[
                "Refresh the environment snapshot after stale or conflicting evidence.",
                "Revise the plan after a failed validation instead of claiming success.",
            ],
            revision_number=1,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlanGraph:
        raw_nodes = value.get("nodes") or []
        nodes = {
            node.node_id: node
            for node in (
                PlanNode.from_dict(item)
                for item in raw_nodes
                if isinstance(item, dict)
            )
        }
        return cls(
            plan_id=str(value.get("plan_id") or f"AP-{uuid4().hex}"),
            objective=str(value.get("objective") or ""),
            assumptions=[str(item) for item in value.get("assumptions") or []],
            constraints=[str(item) for item in value.get("constraints") or []],
            nodes=nodes,
            completion_criteria=[str(item) for item in value.get("completion_criteria") or []],
            fallback_rules=[str(item) for item in value.get("fallback_rules") or []],
            revision_number=max(1, int(value.get("revision_number") or 1)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.plan_graph.v1",
            "plan_id": self.plan_id,
            "objective": self.objective,
            "assumptions": list(self.assumptions),
            "constraints": list(self.constraints),
            "nodes": [
                node.to_dict()
                for node in sorted(
                    self.nodes.values(),
                    key=lambda item: (item.order_index, item.node_id),
                )
            ],
            "completion_criteria": list(self.completion_criteria),
            "fallback_rules": list(self.fallback_rules),
            "revision_number": self.revision_number,
        }

    def apply_patch(self, patch: dict[str, Any]) -> PlanGraph:
        operations = patch.get("operations") or []
        if not isinstance(operations, list):
            raise ValueError("Plan patch operations must be an array")
        snapshot = self.to_dict()
        try:
            for operation in operations:
                if not isinstance(operation, dict):
                    raise ValueError("Each plan patch operation must be an object")
                action = str(operation.get("op") or "")
                if action == "add_node":
                    node = PlanNode.from_dict(dict(operation.get("node") or {}))
                    if node.node_id in self.nodes:
                        raise ValueError(f"Plan node already exists: {node.node_id}")
                    if not node.order_index:
                        node.order_index = len(self.nodes)
                    self.nodes[node.node_id] = node
                elif action == "update_node":
                    node_id = str(operation.get("node_id") or "")
                    current = self._mutable_node(node_id)
                    changes = dict(operation.get("changes") or {})
                    merged = {**current.to_dict(), **changes, "node_id": node_id}
                    self.nodes[node_id] = PlanNode.from_dict(merged)
                elif action == "remove_node":
                    node_id = str(operation.get("node_id") or "")
                    current = self._mutable_node(node_id)
                    if current.mandatory:
                        raise ValueError(f"Mandatory plan node cannot be removed: {node_id}")
                    if any(node_id in node.dependencies for node in self.nodes.values()):
                        raise ValueError(f"Plan node still has dependants: {node_id}")
                    self.nodes.pop(node_id)
                elif action == "reorder_node":
                    node_id = str(operation.get("node_id") or "")
                    current = self._mutable_node(node_id)
                    current.order_index = max(0, int(operation.get("order_index") or 0))
                elif action in {"add_dependency", "remove_dependency"}:
                    node_id = str(operation.get("node_id") or "")
                    dependency_id = str(operation.get("dependency_id") or "")
                    current = self._mutable_node(node_id)
                    if dependency_id not in self.nodes:
                        raise ValueError(f"Unknown dependency node: {dependency_id}")
                    dependencies = list(current.dependencies)
                    if action == "add_dependency" and dependency_id not in dependencies:
                        dependencies.append(dependency_id)
                    if action == "remove_dependency":
                        dependencies = [
                            item for item in dependencies if item != dependency_id
                        ]
                    current.dependencies = tuple(dependencies)
                elif action == "set_completion_criteria":
                    self.completion_criteria = [
                        str(item)
                        for item in operation.get("criteria") or []
                        if str(item).strip()
                    ]
                elif action == "set_fallback_rules":
                    self.fallback_rules = [
                        str(item)
                        for item in operation.get("rules") or []
                        if str(item).strip()
                    ]
                elif action == "add_assumption":
                    value = str(operation.get("value") or "").strip()
                    if value and value not in self.assumptions:
                        self.assumptions.append(value)
                elif action == "add_constraint":
                    value = str(operation.get("value") or "").strip()
                    if value and value not in self.constraints:
                        self.constraints.append(value)
                else:
                    raise ValueError(f"Unsupported plan patch operation: {action}")
            self._assert_acyclic()
            self.revision_number += 1
        except Exception:
            restored = PlanGraph.from_dict(snapshot)
            self.plan_id = restored.plan_id
            self.objective = restored.objective
            self.assumptions = restored.assumptions
            self.constraints = restored.constraints
            self.nodes = restored.nodes
            self.completion_criteria = restored.completion_criteria
            self.fallback_rules = restored.fallback_rules
            self.revision_number = restored.revision_number
            raise
        return self

    def ready_nodes(self) -> list[PlanNode]:
        completed = {
            node.node_id for node in self.nodes.values() if node.status in {"completed", "skipped"}
        }
        return [
            node
            for node in self.nodes.values()
            if node.status in {"pending", "ready"}
            and all(dependency in completed for dependency in node.dependencies)
        ]

    def mark(self, node_id: str, status: str) -> None:
        if status not in PLAN_NODE_STATUSES:
            raise ValueError(f"Unsupported plan node status: {status}")
        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(node_id)
        if node.status == "completed" and status != "completed":
            raise ValueError(f"Completed plan node cannot transition to {status}: {node_id}")
        node.status = status
        now = datetime.now(timezone.utc).isoformat()
        if status == "running" and node.started_at is None:
            node.started_at = now
        if status in {"completed", "failed", "cancelled", "skipped"}:
            node.completed_at = now

    def _mutable_node(self, node_id: str) -> PlanNode:
        node = self.nodes.get(node_id)
        if node is None:
            raise ValueError(f"Unknown plan node: {node_id}")
        if node.status in {"running", "completed"}:
            raise ValueError(f"Started or completed plan node cannot be rewritten: {node_id}")
        return node

    def _assert_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise ValueError("Plan dependencies contain a cycle")
            visiting.add(node_id)
            for dependency in self.nodes[node_id].dependencies:
                if dependency not in self.nodes:
                    raise ValueError(f"Unknown dependency node: {dependency}")
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in self.nodes:
            visit(node_id)
