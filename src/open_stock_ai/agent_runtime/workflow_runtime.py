from __future__ import annotations

from copy import deepcopy
from typing import Any

from .plan_compiler import PlanCompiler
from .plan_graph import PlanGraph


class WorkflowRuntime:
    def __init__(self, manifest: list[dict[str, Any]]) -> None:
        self.compiler = PlanCompiler(manifest)

    def instantiate(
        self,
        workflow: dict[str, Any],
        *,
        objective: str | None = None,
        patch: dict[str, Any] | None = None,
        autonomy: str = "advisory",
    ) -> tuple[PlanGraph, dict[str, Any]]:
        payload = deepcopy(workflow.get("plan") or {})
        if objective:
            payload["objective"] = objective
        # A saved workflow is guidance, not a partially-finished run.  Every
        # instantiation receives a new identity and fresh executable states.
        payload["plan_id"] = None
        payload["revision_number"] = 1
        for node in payload.get("nodes") or []:
            if isinstance(node, dict):
                node["status"] = "pending"
        plan = PlanGraph.from_dict(payload)
        if patch:
            plan.apply_patch(patch)
        compiled = self.compiler.compile(plan, autonomy=autonomy)
        return plan, compiled.to_dict()
