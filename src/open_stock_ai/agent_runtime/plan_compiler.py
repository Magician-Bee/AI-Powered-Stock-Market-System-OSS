from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .plan_graph import PlanGraph


@dataclass(frozen=True, slots=True)
class PlanCompileResult:
    valid: bool
    errors: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, Any], ...]
    executable_nodes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.plan_compile_result.v1",
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "executable_nodes": list(self.executable_nodes),
        }


class PlanCompiler:
    """Compile provider-neutral plans against the Host capability contract."""

    def __init__(self, manifest: list[dict[str, Any]]) -> None:
        self.manifest = {
            str(item.get("name") or ""): dict(item)
            for item in manifest
            if str(item.get("name") or "")
        }

    def compile(self, plan: PlanGraph, *, autonomy: str) -> PlanCompileResult:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        node_ids = set(plan.nodes)
        for node in plan.nodes.values():
            missing = [item for item in node.dependencies if item not in node_ids]
            if missing:
                errors.append(_error(node.node_id, "missing_dependency", {"dependencies": missing}))
            if node.node_id in node.dependencies:
                errors.append(_error(node.node_id, "self_dependency"))
            capability_name = node_capability(node)
            if node.node_type == "approval" and capability_name is None:
                errors.append(_error(node.node_id, "approval_requires_target_tool"))
                continue
            if capability_name is None:
                continue
            tool = self.manifest.get(capability_name)
            if tool is None:
                errors.append(_error(node.node_id, "unknown_capability", {"tool": capability_name}))
                continue
            schema_errors = validate_json_value(
                node_execution_arguments(node),
                tool.get("input_schema") or {},
            )
            for detail in schema_errors:
                errors.append(_error(node.node_id, "invalid_tool_arguments", detail))
            permission_error = _permission_error(tool, autonomy)
            if permission_error:
                errors.append(_error(node.node_id, permission_error, {"tool": node.tool_name}))
            if (
                node.node_type != "approval"
                and bool(tool.get("mutating"))
                and not node.postconditions
            ):
                errors.append(
                    _error(node.node_id, "mutation_requires_postcondition", {"tool": capability_name})
                )
        cycle = _find_cycle(plan)
        if cycle:
            errors.append({"code": "cyclic_dependencies", "nodes": cycle})
        if plan.nodes and not any(node.node_type == "finalize" for node in plan.nodes.values()):
            warnings.append({"code": "missing_finalize_node"})
        if not plan.completion_criteria:
            errors.append({"code": "missing_completion_criteria"})
        executable = tuple(node.node_id for node in plan.ready_nodes()) if not errors else ()
        return PlanCompileResult(not errors, tuple(errors), tuple(warnings), executable)


def validate_json_value(value: Any, schema: dict[str, Any], path: str = "$") -> list[dict[str, Any]]:
    if not schema:
        return []
    errors: list[dict[str, Any]] = []
    if "anyOf" in schema:
        variants = schema.get("anyOf") or []
        if not any(not validate_json_value(value, item, path) for item in variants if isinstance(item, dict)):
            errors.append({"path": path, "message": "value does not match any allowed schema"})
        return errors
    expected = schema.get("type")
    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(str(expected), True)
    if not type_ok:
        return [{"path": path, "message": f"expected {expected}"}]
    if "enum" in schema and value not in schema["enum"]:
        errors.append({"path": path, "message": "value is not in enum"})
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for required in schema.get("required") or []:
            if required not in value:
                errors.append({"path": f"{path}.{required}", "message": "required property is missing"})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append({"path": f"{path}.{key}", "message": "additional property is forbidden"})
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, dict):
                errors.extend(validate_json_value(item, child, f"{path}.{key}"))
    if isinstance(value, list):
        if len(value) > int(schema.get("maxItems") or len(value)):
            errors.append({"path": path, "message": "array exceeds maxItems"})
        child = schema.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                errors.extend(validate_json_value(item, child, f"{path}[{index}]"))
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength") or 0):
            errors.append({"path": path, "message": "string is shorter than minLength"})
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append({"path": path, "message": "string exceeds maxLength"})
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append({"path": path, "message": "number is below minimum"})
        if "maximum" in schema and value > schema["maximum"]:
            errors.append({"path": path, "message": "number exceeds maximum"})
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append({"path": path, "message": "number is not above exclusiveMinimum"})
    return errors


def _permission_error(tool: dict[str, Any], autonomy: str) -> str | None:
    if tool.get("requires_full_execution") and autonomy != "full_execute":
        return "full_execution_required"
    if tool.get("requires_paper_execution") and autonomy not in {"paper_execute", "full_execute"}:
        return "paper_execution_required"
    if tool.get("requires_project_execution") and autonomy not in {"project_execute", "full_execute"}:
        return "project_execution_required"
    if tool.get("requires_external_execution") and autonomy not in {"external_execute", "full_execute"}:
        return "external_execution_required"
    return None


def node_capability(node: Any) -> str | None:
    if node.node_type == "tool":
        return str(node.tool_name or "")
    defaults = {
        "subtask": "agent.run_subtasks",
        "subagent": "agent.run_subtasks",
        "schedule": "schedule.create",
        "workflow": "workflow.run",
    }
    if node.node_type == "approval":
        return str(node.tool_name or node.metadata.get("target_tool") or "") or None
    if node.node_type in defaults:
        return str(node.tool_name or defaults[node.node_type])
    return None


def node_execution_arguments(node: Any) -> dict[str, Any]:
    arguments = dict(node.arguments)
    if node.node_type in {"subtask", "subagent"} and not arguments.get("objectives"):
        objective = str(arguments.pop("objective", "") or node.title).strip()
        arguments["objectives"] = [objective]
        if node.node_type == "subagent":
            arguments.setdefault("role", str(node.metadata.get("role") or "specialist"))
    return arguments


def _find_cycle(plan: PlanGraph) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    trail: list[str] = []

    def visit(node_id: str) -> list[str]:
        if node_id in visiting:
            index = trail.index(node_id)
            return [*trail[index:], node_id]
        if node_id in visited:
            return []
        visiting.add(node_id)
        trail.append(node_id)
        for dependency in plan.nodes[node_id].dependencies:
            if dependency in plan.nodes:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        trail.pop()
        visiting.remove(node_id)
        visited.add(node_id)
        return []

    for node_id in plan.nodes:
        cycle = visit(node_id)
        if cycle:
            return cycle
    return []


def _error(node_id: str, code: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"node_id": node_id, "code": code, **(detail or {})}
