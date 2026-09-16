from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from open_stock_ai.agent_runtime.completion_contract import objective_completion_contract
from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec
from open_stock_ai.agent_runtime.memory import MemoryCandidate


class AgentRuntimeToolProvider:
    """Capabilities backed by the durable Stock AI Runtime itself."""

    provider_id = "agent_runtime"

    def __init__(self) -> None:
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="agent.run_subtasks",
                    description="Run up to four bounded Stock AI Agent child tasks and return their validated results.",
                    category="agent_runtime",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["objectives"],
                        "properties": {
                            "objectives": {
                                "type": "array",
                                "maxItems": 4,
                                "items": {"type": "string", "minLength": 1, "maxLength": 4000},
                            },
                            "role": {"type": "string", "maxLength": 200},
                            "max_steps": {"type": "integer", "minimum": 1, "maximum": 8},
                        },
                    },
                    timeout_seconds=1800,
                    long_running=True,
                    execution_backend="agent_runtime",
                    packages=("Stock AI Agent Runtime", "Codex"),
                ),
                AgentToolSpec(
                    name="memory.search",
                    description="Search project-scoped durable Agent memory.",
                    category="memory",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["query"],
                        "properties": {
                            "query": {"type": "string", "minLength": 1},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                    },
                    packages=("SQLite",),
                ),
                AgentToolSpec(
                    name="memory.write",
                    description="Write a sourced project-scoped durable memory.",
                    category="memory",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "fact_type", "content"],
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "conversation",
                                    "working",
                                    "project",
                                    "domain",
                                    "episodic",
                                    "user_preference",
                                    "artifact",
                                    "reflection",
                                ],
                            },
                            "fact_type": {
                                "type": "string",
                                "enum": ["fact", "preference", "inference", "temporary_state"],
                            },
                            "content": {"type": "string", "minLength": 1, "maxLength": 20000},
                            "expires_at": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        },
                    },
                    mutating=True,
                    rollback_support=False,
                    packages=("SQLite",),
                ),
                AgentToolSpec(
                    name="memory.archive",
                    description="Archive one durable memory without deleting its audit record.",
                    category="memory",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["memory_id"],
                        "properties": {"memory_id": {"type": "string", "minLength": 1}},
                    },
                    mutating=True,
                    rollback_support=False,
                    packages=("SQLite",),
                ),
                AgentToolSpec(
                    name="memory.update",
                    description="Correct one durable memory while preserving its identity and audit source.",
                    category="memory",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["memory_id", "content"],
                        "properties": {
                            "memory_id": {"type": "string", "minLength": 1},
                            "content": {"type": "string", "minLength": 1, "maxLength": 20000},
                        },
                    },
                    mutating=True,
                    rollback_support=False,
                    packages=("SQLite",),
                ),
                AgentToolSpec(
                    name="workflow.list",
                    description="List saved versioned Agent workflows for this project namespace.",
                    category="workflow",
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                    packages=("SQLite",),
                ),
                AgentToolSpec(
                    name="workflow.save_current",
                    description="Save the current versioned PlanGraph as a reusable project workflow.",
                    category="workflow",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 200},
                            "description": {"type": "string", "maxLength": 2000},
                        },
                    },
                    mutating=True,
                    rollback_support=False,
                    packages=("SQLite", "PlanGraph"),
                ),
                AgentToolSpec(
                    name="workflow.run",
                    description="Instantiate a saved versioned workflow as a bounded child run; the copied PlanGraph remains editable.",
                    category="workflow",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["workflow_id"],
                        "properties": {
                            "workflow_id": {"type": "string", "minLength": 1},
                            "objective": {"type": "string", "maxLength": 8000},
                            "patch": {"type": "object"},
                            "max_steps": {"type": "integer", "minimum": 1, "maximum": 8},
                        },
                    },
                    long_running=True,
                    packages=("SQLite", "PlanGraph", "Stock AI Agent Runtime"),
                ),
                AgentToolSpec(
                    name="artifact.create_text",
                    description="Create a run-scoped text or Markdown artifact with hash and provenance.",
                    category="artifact",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "content"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 200},
                            "content": {"type": "string", "maxLength": 1000000},
                            "media_type": {"type": "string", "maxLength": 100},
                            "kind": {"type": "string", "maxLength": 100},
                        },
                    },
                    mutating=True,
                    rollback_support=False,
                    packages=("SQLite", "Application Support"),
                ),
                AgentToolSpec(
                    name="artifact.list",
                    description="List artifacts created by the current run, including hashes and provenance.",
                    category="artifact",
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                    packages=("SQLite", "Application Support"),
                ),
                AgentToolSpec(
                    name="artifact.create_structured",
                    description="Create a renderer-ready structured visualization artifact with schema and evidence provenance.",
                    category="artifact",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "renderer", "document", "schema_version"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 200},
                            "renderer": {"type": "string", "enum": ["table", "chart", "graph", "timeline", "canvas", "map", "fishbone", "dag", "swimlane", "workflow", "gantt", "evidence_graph", "task_forest", "decision_matrix", "risk_table", "risk_bar", "risk_radar"]},
                            "document": {"type": ["object", "array"]},
                            "schema_version": {"type": "string", "minLength": 1, "maxLength": 100},
                            "branch_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "node_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "evidence_ids": {"type": "array", "maxItems": 100, "items": {"type": "string"}},
                            "metadata": {"type": "object"},
                        },
                    },
                    mutating=True,
                    rollback_support=False,
                    packages=("SQLite", "Application Support"),
                ),
                AgentToolSpec(
                    name="artifact.patch_structured",
                    description="Replace a selected structured Artifact document through an optimistic, Host-validated version update.",
                    category="artifact",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "artifact_id", "expected_version", "renderer", "document",
                            "schema_version", "reason", "affected_node_ids",
                        ],
                        "properties": {
                            "artifact_id": {"type": "string", "minLength": 1, "maxLength": 100},
                            "expected_version": {"type": "integer", "minimum": 1},
                            "renderer": {"type": "string", "enum": ["table", "chart", "graph", "timeline", "canvas", "map", "fishbone", "dag", "swimlane", "workflow", "gantt", "evidence_graph", "task_forest", "decision_matrix", "risk_table", "risk_bar", "risk_radar"]},
                            "document": {"type": ["object", "array"]},
                            "schema_version": {"type": "string", "minLength": 1, "maxLength": 100},
                            "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                            "affected_node_ids": {"type": "array", "maxItems": 100, "items": {"type": "string", "minLength": 1}},
                        },
                    },
                    mutating=True,
                    rollback_support=True,
                    packages=("SQLite", "Application Support"),
                ),
            )
        }

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def describe(self) -> dict[str, Any]:
        return {
            "configured": True,
            "runtime_ready": True,
            "health": "ready",
            "max_parallel_subtasks": 4,
            "max_subagent_depth": 3,
        }

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        from stock_ai.agent_service import get_agent_run_runtime, get_agent_service

        runtime = get_agent_run_runtime()
        service = get_agent_service()
        if name == "agent.run_subtasks":
            current_run = runtime.get_run(context.run_id) or {}
            current_objective = str(current_run.get("objective") or "").lstrip().lower()
            completion_contract = objective_completion_contract(
                current_objective,
                str(context.state.get("task_kind") or ""),
            )
            if completion_contract["paper_order_requested"]:
                raise PermissionError(
                    "Routine local paper-trade runs cannot delegate subtasks. "
                    "Use the current Run's verified preview and Paper Broker submission path."
                )
            if current_objective.startswith("role: critic"):
                raise PermissionError(
                    "Critic child runs must use their own Host evidence and synthesize locally; "
                    "they cannot recursively delegate another subtask."
                )
            if _parent_depth(runtime, context.run_id) >= 3:
                raise PermissionError("Maximum subagent depth reached")
            objectives = [str(item).strip() for item in arguments.get("objectives") or [] if str(item).strip()]
            if not objectives:
                raise ValueError("agent.run_subtasks requires at least one objective")
            role = str(arguments.get("role") or "specialist")
            parent_task_kind = str(context.state.get("task_kind") or "").strip()
            child_task_kind = (
                parent_task_kind
                if parent_task_kind in {
                    "general_answer",
                    "artifact_task",
                    "project_task",
                    "market_information",
                    "market_decision",
                    "market_radar",
                    "ui_task",
                    "current_information",
                }
                else ""
            )
            # Critic children have always been evidence-only by contract. A
            # legacy caller may not yet supply ``task_kind`` in its context,
            # so retain that safe default rather than leaving the critic's
            # symbol-bearing objective open to decision reclassification.
            if role.strip().lower() == "critic" and not child_task_kind:
                child_task_kind = "market_information"
            # A terse model-selected focus such as ``market.analyze_symbol
            # 2887.TW`` must not be classified in isolation.  Doing so turned
            # an analysis-only parent into a decision child simply because it
            # named a symbol, then forced a buy/sell completion gate the user
            # never requested.  Carry the Host-owned parent scope into every
            # child instead of asking people to repeat operating constraints.
            research_only_child = (
                child_task_kind == "market_information"
                and not any(
                    completion_contract[name]
                    for name in ("decision_requested", "order_requested", "paper_order_requested")
                )
            )
            max_steps = max(1, min(int(arguments.get("max_steps") or 6), 8))
            if role.strip().lower() == "critic":
                # A Critic normally needs one planning turn, multiple
                # independent evidence calls, and a final synthesis turn.
                # Do not let a model-selected micro budget create an endless
                # series of incomplete replacement critics.
                max_steps = max(max_steps, 6)
            source_node_id = str(context.state.get("current_plan_node_id") or "").strip()
            branches = (
                runtime.final_runtime.create_subtask_branches(
                    session_id=context.session_id,
                    run_id=context.run_id,
                    source_node_id=source_node_id,
                    objectives=tuple(objectives),
                    role=role,
                )
                if source_node_id
                else []
            )
            branch_by_objective = {
                str(branch.get("objective") or ""): str(branch["branch_id"])
                for branch in branches
                if branch.get("branch_id")
            }

            created_run_ids: list[str] = []

            async def run_child(objective: str) -> dict[str, Any]:
                branch_id = branch_by_objective.get(objective)
                child_objective = objective
                if role.strip().lower() == "critic" and context.symbols:
                    scoped_symbols = ", ".join(context.symbols)
                    child_objective = (
                        "Independently challenge "
                        f"the parent Run's analysis for {scoped_symbols}. Use your own "
                        "Host-validated technical and risk evidence, identify support, "
                        "contradictions, unresolved gaps, and decision impact, then "
                        "complete locally. Do not delegate, change the global objective, "
                        "create automation, or execute a trade. Use no more than three "
                        "evidence-gathering turns."
                    )
                child_objective = (
                    f"[MODEL_TASK_KIND:{child_task_kind}]\n" if child_task_kind else ""
                ) + f"Role: {role}\nTask: {child_objective}"
                if research_only_child:
                    child_objective += (
                        "\n[HOST_BOUNDARY:research_only] Complete this as evidence-led "
                        "research only. Do not produce a buy/sell decision, create an order, "
                        "or request user operating instructions."
                    )
                child = await runtime.create_run(
                    objective=child_objective,
                    symbols=list(context.symbols),
                    driver_id=context.driver_id,
                    autonomy="advisory",
                    max_steps=max_steps,
                    session_id=context.session_id,
                    parent_run_id=context.run_id,
                    run_metadata={
                        "source_branch_id": branch_id,
                        "source": "plan_subtask",
                        "source_plan_node_id": source_node_id or None,
                        "subagent_role": role,
                        "requested_focus": objective,
                    } if branch_id else {"source": "plan_subtask"},
                )
                child_run_id = str(child["run_id"])
                created_run_ids.append(child_run_id)
                result = await runtime.wait(child_run_id)
                return {
                    "branch_id": branch_id,
                    "run_id": child_run_id,
                    "result": result,
                }

            try:
                results = await asyncio.gather(
                    *(run_child(objective) for objective in objectives)
                )
            except BaseException:
                # If the parent worker times out, is cancelled, or one child
                # fails, no recursively delegated Run may continue as an
                # invisible orphan. Durable cancellation also updates each
                # linked Branch through the normal Run lifecycle.
                await asyncio.gather(
                    *(runtime.cancel(run_id) for run_id in created_run_ids),
                    return_exceptions=True,
                )
                raise
            return {
                "schema_version": "open_stock_ai.agent_subtasks.v1",
                "role": role,
                "count": len(results),
                "parallel_requested": len(results) > 1,
                "branch_ids": [item.get("branch_id") for item in results if item.get("branch_id")],
                "items": results,
            }
        if name == "memory.search":
            manager = service.memory_manager
            if manager is None:
                raise RuntimeError("Memory manager is unavailable")
            items = manager.store.search(
                namespace=manager.namespace,
                query=str(arguments["query"]),
                limit=int(arguments.get("limit") or 20),
                session_id=context.session_id,
            )
            return {"schema_version": "open_stock_ai.memory_list.v1", "count": len(items), "items": items}
        if name == "memory.write":
            manager = service.memory_manager
            if manager is None:
                raise RuntimeError("Memory manager is unavailable")
            kind = str(arguments["kind"])
            content = str(arguments["content"])
            candidate = manager.consider(MemoryCandidate(
                content=content,
                kind=kind,
                importance=0.75,
                future_relevance=0.75,
                confidence=0.7,
                source={"type": "agent_memory_proposal", "run_id": context.run_id},
                durability="long_term" if kind not in {"working", "conversation"} else "session",
                semantic_key=f"agent:{kind}:{hashlib.sha256(content.casefold().encode()).hexdigest()[:20]}",
                expires_at=arguments.get("expires_at"),
                run_id=context.run_id,
                session_id=context.session_id,
                host_verified=False,
            ))
            return {
                "schema_version": "open_stock_ai.memory_candidate_result.v1",
                "candidate_id": candidate.candidate_id,
                "decision": candidate.decision.value,
                "memory_id": candidate.record.memory_id if candidate.record else None,
                "direct_write": False,
                "requires_host_governance": True,
            }
        if name == "memory.archive":
            manager = service.memory_manager
            if manager is None:
                raise RuntimeError("Memory manager is unavailable")
            archived = manager.store.archive(str(arguments["memory_id"]))
            return {
                "schema_version": "open_stock_ai.memory_archive.v1",
                "memory_id": arguments["memory_id"],
                "confirmed": archived,
            }
        if name == "memory.update":
            manager = service.memory_manager
            if manager is None:
                raise RuntimeError("Memory manager is unavailable")
            existing = manager.store.get(str(arguments["memory_id"]))
            if existing is None:
                raise KeyError(str(arguments["memory_id"]))
            content = str(arguments["content"])
            governance = dict((existing.get("source") or {}).get("governance") or {})
            candidate = manager.consider(MemoryCandidate(
                content=content,
                kind=str(existing.get("kind") or "episodic"),
                importance=0.8,
                future_relevance=0.8,
                confidence=0.7,
                source={
                    "type": "agent_correction_proposal",
                    "run_id": context.run_id,
                    "corrects_memory_id": arguments["memory_id"],
                },
                durability="long_term",
                semantic_key=str(governance.get("semantic_key") or f"correction:{arguments['memory_id']}"),
                run_id=context.run_id,
                session_id=context.session_id,
                host_verified=False,
            ))
            return {
                "schema_version": "open_stock_ai.memory_correction_candidate.v1",
                "candidate_id": candidate.candidate_id,
                "decision": candidate.decision.value,
                "memory_id": candidate.record.memory_id if candidate.record else None,
                "corrects_memory_id": arguments["memory_id"],
                "direct_update": False,
                "requires_host_governance": True,
            }
        if name == "workflow.list":
            items = runtime.list_workflows()
            return {"schema_version": "open_stock_ai.workflow_list.v1", "count": len(items), "items": items}
        if name == "workflow.save_current":
            plan = runtime.get_plan(context.run_id)
            if not plan:
                raise RuntimeError("The current run has no PlanGraph to save")
            return runtime.save_workflow(
                name=str(arguments["name"]),
                plan=plan,
                metadata={
                    "description": str(arguments.get("description") or ""),
                    "source_run_id": context.run_id,
                },
            )
        if name == "workflow.run":
            child = await runtime.run_workflow(
                str(arguments["workflow_id"]),
                objective=str(arguments.get("objective") or "") or None,
                patch=dict(arguments.get("patch") or {}) or None,
                autonomy="advisory",
                session_id=context.session_id,
                parent_run_id=context.run_id,
                max_steps=max(1, min(int(arguments.get("max_steps") or 6), 8)),
            )
            return await runtime.wait(child["run_id"])
        if name == "artifact.create_text":
            if runtime.artifact_store is None:
                raise RuntimeError("Artifact store is unavailable")
            return runtime.artifact_store.create_text(
                session_id=context.session_id,
                run_id=context.run_id,
                name=str(arguments["name"]),
                content=str(arguments["content"]),
                media_type=str(arguments.get("media_type") or "text/plain"),
                kind=str(arguments.get("kind") or "report"),
                metadata={"created_by": "agent_tool"},
            )
        if name == "artifact.list":
            if runtime.artifact_store is None:
                raise RuntimeError("Artifact store is unavailable")
            items = runtime.artifact_store.list(context.run_id)
            return {"schema_version": "open_stock_ai.artifact_list.v1", "count": len(items), "items": items}
        if name == "artifact.create_structured":
            if runtime.artifact_store is None:
                raise RuntimeError("Artifact store is unavailable")
            return runtime.artifact_store.create_structured(
                session_id=context.session_id,
                run_id=context.run_id,
                name=str(arguments["name"]),
                renderer=str(arguments["renderer"]),
                document=arguments["document"],
                schema_version=str(arguments["schema_version"]),
                branch_id=str(arguments.get("branch_id") or "") or None,
                node_id=str(arguments.get("node_id") or "") or None,
                evidence_ids=[str(item) for item in arguments.get("evidence_ids") or []],
                metadata=dict(arguments.get("metadata") or {}),
            )
        if name == "artifact.patch_structured":
            if runtime.artifact_store is None:
                raise RuntimeError("Artifact store is unavailable")
            artifact = runtime.artifact_store.get_by_id(str(arguments["artifact_id"]))
            if artifact is None:
                raise KeyError(f"Unknown artifact: {arguments['artifact_id']}")
            if str(artifact.get("session_id") or "") != context.session_id:
                raise PermissionError("Structured Artifact belongs to a different Agent session")
            return await runtime.revise_artifact(
                artifact_id=str(arguments["artifact_id"]),
                expected_version=int(arguments["expected_version"]),
                content={
                    "renderer": str(arguments["renderer"]),
                    "document": arguments["document"],
                    "schema_version": str(arguments["schema_version"]),
                },
                changed_by="agent_tool",
                reason=str(arguments["reason"]),
                affected_node_ids=tuple(str(item) for item in arguments["affected_node_ids"]),
                emit_event=False,
            )
        raise RuntimeError(f"Agent Runtime capability is registered but not implemented: {name}")


def _parent_depth(runtime: Any, run_id: str) -> int:
    depth = 0
    current = runtime.get_run(run_id)
    seen = {run_id}
    while current and current.get("parent_run_id"):
        parent = str(current["parent_run_id"])
        if parent in seen:
            break
        seen.add(parent)
        depth += 1
        current = runtime.get_run(parent)
    return depth
