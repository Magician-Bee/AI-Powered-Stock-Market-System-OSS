from __future__ import annotations

"""Run-scoped Task Forest execution authority.

The durable PlanGraph remains the Host's policy and checkpoint representation,
but it must not be mistaken for the scheduler.  This module owns the actual
capability execution tree for one Agent Run: every tool capability becomes a
Branch with an ordered LocalPlan, and every batch is scheduled by
``ForestExecutor`` against that same tree.

Keeping this object alive for the whole Run is important.  Creating a fresh
TaskForest per tool call makes a Forest-shaped trace, not an execution
authority; it loses branch ancestry, completion state and the evidence needed
to audit a partially completed Run.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .executor import ForestExecutionReport, ForestExecutor, StepExecutionResult
from .models import Branch, BranchResult, BranchStatus, ForestLimits, StepStatus, TaskForest


StepRunner = Callable[[Any], Awaitable[StepExecutionResult] | StepExecutionResult]


@dataclass(frozen=True, slots=True)
class RuntimeForestExecution:
    """The result of one scheduling pass over the Run-scoped Forest."""

    report: ForestExecutionReport
    branch_ids: tuple[str, ...]


class RuntimeForestAuthority:
    """Make one TaskForest the capability scheduler for an Agent Run."""

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        objective: str,
        limits: ForestLimits | None = None,
        forest_id: str | None = None,
        root_branch_id: str | None = None,
        objective_version_id: str | None = None,
    ) -> None:
        self.forest = TaskForest.create(
            session_id=session_id,
            objective_version_id=objective_version_id or run_id,
            root_goal=objective,
            limits=limits or ForestLimits(),
            forest_id=forest_id,
            root_branch_id=root_branch_id,
        )
        self.run_id = run_id
        self._execution_count = 0

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        session_id: str,
        run_id: str,
        objective: str,
        forest_id: str | None = None,
        root_branch_id: str | None = None,
        objective_version_id: str | None = None,
    ) -> "RuntimeForestAuthority":
        """Restore the prior execution tree before continuing a durable Run.

        A checkpoint stores only serializable state, never live coroutines or
        executors.  Restoring completed Branches makes the next capability
        pass continue the same Forest rather than silently starting a second
        authority after recovery.
        """

        if not isinstance(snapshot, dict) or not snapshot.get("forest_id"):
            return cls(
                session_id=session_id,
                run_id=run_id,
                objective=objective,
                forest_id=forest_id,
                root_branch_id=root_branch_id,
                objective_version_id=objective_version_id,
            )
        snapshot_forest_id = str(snapshot["forest_id"])
        # A current durable Run has already reserved its Forest identity in
        # SQLite before any provider work begins.  Never resurrect an old
        # in-memory snapshot under another tree: that would make recovery
        # produce a second authority for one Run.  Legacy snapshots without a
        # supplied durable identity retain their original restore behaviour.
        if forest_id and snapshot_forest_id != forest_id:
            return cls(
                session_id=session_id,
                run_id=run_id,
                objective=objective,
                forest_id=forest_id,
                root_branch_id=root_branch_id,
                objective_version_id=objective_version_id,
            )
        authority = cls(
            session_id=session_id,
            run_id=run_id,
            objective=objective,
            forest_id=snapshot_forest_id,
            root_branch_id=root_branch_id,
            objective_version_id=objective_version_id,
        )
        entries = [item for item in snapshot.get("branches") or [] if isinstance(item, dict)]
        if not entries:
            return authority
        authority.forest.branches.clear()
        for item in sorted(entries, key=lambda value: (value.get("parent_branch_id") is not None, str(value.get("branch_id") or ""))):
            branch_id = str(item.get("branch_id") or "")
            if not branch_id:
                continue
            parent_branch_id = item.get("parent_branch_id")
            if parent_branch_id is not None and str(parent_branch_id) not in authority.forest.branches:
                continue
            try:
                status = BranchStatus(str(item.get("status") or BranchStatus.PENDING))
            except ValueError:
                status = BranchStatus.PENDING
            branch = authority.forest.add_branch(
                objective=str(item.get("objective") or objective),
                objective_version_id=run_id,
                parent_branch_id=str(parent_branch_id) if parent_branch_id else None,
                status=status,
                execution_mode=str(item.get("execution_mode") or "parallel"),
                branch_id=branch_id,
            )
            branch.local_state.update(
                {
                    key: item.get(key)
                    for key in ("source_plan_node_id", "source_call_id", "tool_name")
                    if item.get(key) is not None
                }
            )
            for step_entry in item.get("steps") or []:
                if not isinstance(step_entry, dict) or not step_entry.get("step_id"):
                    continue
                step = branch.local_plan.add_step(
                    str(step_entry.get("title") or "Host capability"),
                    step_id=str(step_entry["step_id"]),
                )
                try:
                    step.status = StepStatus(str(step_entry.get("status") or StepStatus.PENDING))
                except ValueError:
                    step.status = StepStatus.PENDING
                step.error = step_entry.get("error")
            result = item.get("result")
            if isinstance(result, dict):
                branch.result = BranchResult(
                    branch_id=branch.branch_id,
                    conclusion=str(result.get("conclusion") or "Restored capability result"),
                    evidence_ids=tuple(str(value) for value in result.get("evidence_ids") or []),
                    partial=bool(result.get("partial")),
                    unmet_criteria=tuple(str(value) for value in result.get("unmet_criteria") or []),
                )
                branch.evidence_ids = list(branch.result.evidence_ids)
        if authority.forest.branches and any(item.parent_branch_id is None for item in authority.forest.branches.values()):
            authority._execution_count = max(0, int(snapshot.get("execution_count") or 0))
            return authority
        return cls(session_id=session_id, run_id=run_id, objective=objective)

    async def execute_capabilities(
        self,
        capabilities: list[dict[str, Any]],
        *,
        runner: StepRunner,
        parallel: bool,
    ) -> RuntimeForestExecution:
        """Schedule a new set of Host-approved capabilities through this Forest.

        Each capability gets an actual child Branch and LocalStep.  Existing
        branches remain in the Forest as immutable execution history; the
        executor therefore sees the full Run tree, while it only schedules the
        newly-ready branches.
        """

        branch_ids: list[str] = []
        for capability in capabilities:
            call_id = str(capability["call_id"])
            node_id = str(capability["node_id"])
            tool_name = str(capability["tool_name"])
            branch = self.forest.add_branch(
                objective=f"Execute {tool_name} for {node_id}",
                objective_version_id=self.run_id,
                parent_branch_id=self.forest.root_branch.branch_id,
                status="ready",
                execution_mode="parallel" if parallel else "ordered",
                result_contract={"required": ()},
            )
            branch.local_state.update(
                {
                    "execution_owner": "task_forest",
                    "source_call_id": call_id,
                    "source_plan_node_id": node_id,
                    "tool_name": tool_name,
                }
            )
            branch.local_plan.add_step(
                f"Host execute {tool_name}",
                metadata={"source_plan_node_id": node_id, "call_id": call_id},
            )
            branch_ids.append(branch.branch_id)

        by_call_id = {str(item["call_id"]): dict(item) for item in capabilities}

        async def execute_step(step_context: Any) -> StepExecutionResult:
            call_id = str(step_context.branch.local_state["source_call_id"])
            return await runner(by_call_id[call_id])

        report = await ForestExecutor(self.forest, execute_step).execute()
        self._execution_count += 1
        return RuntimeForestExecution(report=report, branch_ids=tuple(branch_ids))

    def branch_for_call(self, call_id: str, execution: RuntimeForestExecution) -> Branch:
        for branch_id in execution.branch_ids:
            branch = self.forest.branches[branch_id]
            if branch.local_state.get("source_call_id") == str(call_id):
                return branch
        raise KeyError(f"Capability was not scheduled in this Forest execution: {call_id}")

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable, audit-safe view for the Run result/checkpoint."""

        def branch_payload(branch: Branch) -> dict[str, Any]:
            return {
                "branch_id": branch.branch_id,
                "parent_branch_id": branch.parent_branch_id,
                "objective": branch.objective,
                "status": branch.status.value,
                "execution_mode": branch.execution_mode,
                "source_plan_node_id": branch.local_state.get("source_plan_node_id"),
                "source_call_id": branch.local_state.get("source_call_id"),
                "tool_name": branch.local_state.get("tool_name"),
                "evidence_ids": list(branch.evidence_ids),
                "steps": [
                    {
                        "step_id": step.step_id,
                        "title": step.title,
                        "status": step.status.value,
                        "error": step.error,
                    }
                    for step in branch.local_plan.steps.values()
                ],
                "result": (
                    {
                        "conclusion": branch.result.conclusion,
                        "evidence_ids": list(branch.result.evidence_ids),
                        "partial": branch.result.partial,
                        "unmet_criteria": list(branch.result.unmet_criteria),
                    }
                    if branch.result is not None
                    else None
                ),
            }

        return {
            "schema_version": "open_stock_ai.runtime_forest_authority.v1",
            "forest_id": self.forest.forest_id,
            "root_branch_id": self.forest.root_branch.branch_id,
            "objective_version_id": self.forest.root_objective_version_id,
            "run_id": self.run_id,
            "execution_count": self._execution_count,
            "branches": [branch_payload(item) for item in self.forest.branches.values()],
        }
