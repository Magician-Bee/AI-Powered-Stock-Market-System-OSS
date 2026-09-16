from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Callable

from .models import (
    Branch,
    BranchStatus,
    LocalStep,
    StepStatus,
    TaskForest,
    TERMINAL_BRANCH_STATUSES,
)


StepRunner = Callable[[Branch, LocalStep], Awaitable[Any] | Any]


@dataclass(frozen=True, slots=True)
class LocalExecutionSummary:
    branch_id: str
    completed_step_ids: tuple[str, ...]
    failed_step_id: str | None = None
    error: str | None = None
    outputs: tuple[Any, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.failed_step_id is None


class LocalBranchScheduler:
    """Executes one Branch's insertion-ordered LocalPlan, one Step at a time."""

    async def execute(self, branch: Branch, runner: StepRunner) -> LocalExecutionSummary:
        completed: list[str] = [
            step.step_id
            for step in branch.local_plan.steps.values()
            if step.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}
        ]
        outputs: list[Any] = []
        while True:
            ordered = tuple(branch.local_plan.steps.values())
            failed = next(
                (
                    step
                    for step in ordered
                    if step.status in {StepStatus.FAILED, StepStatus.BLOCKED}
                ),
                None,
            )
            if failed is not None:
                error = failed.error or f"Local step is {failed.status.value}"
                branch.local_plan.block_remaining(error)
                return LocalExecutionSummary(
                    branch_id=branch.branch_id,
                    completed_step_ids=tuple(completed),
                    failed_step_id=failed.step_id,
                    error=error,
                    outputs=tuple(outputs),
                )
            remaining = [
                step
                for step in ordered
                if step.status
                not in {
                    StepStatus.COMPLETED,
                    StepStatus.SKIPPED,
                    StepStatus.FAILED,
                    StepStatus.BLOCKED,
                    StepStatus.CANCELLED,
                }
            ]
            if not remaining:
                return LocalExecutionSummary(
                    branch_id=branch.branch_id,
                    completed_step_ids=tuple(completed),
                    outputs=tuple(outputs),
                )

            branch.local_plan.ready_steps()
            step = remaining[0]
            if step.status != StepStatus.READY:
                reason = f"Ordered local step has unmet dependencies: {step.step_id}"
                branch.local_plan.block_remaining(reason)
                return LocalExecutionSummary(
                    branch_id=branch.branch_id,
                    completed_step_ids=tuple(completed),
                    failed_step_id=step.step_id,
                    error=reason,
                    outputs=tuple(outputs),
                )

            branch.local_plan.start(step.step_id)
            started = monotonic()
            try:
                output = runner(branch, step)
                if inspect.isawaitable(output):
                    output = await output
                elapsed = monotonic() - started
                branch.budget.charge(
                    retries=int(_usage(output, "retries_used", 0)),
                    tokens=int(_usage(output, "tokens_used", 0)),
                    seconds=elapsed,
                )
                branch.local_plan.complete(step.step_id, _payload(output))
            except asyncio.CancelledError:
                step.status = StepStatus.CANCELLED
                step.error = "Local Branch execution cancelled"
                branch.local_plan.block_remaining(step.error)
                raise
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                if step.status == StepStatus.RUNNING:
                    branch.local_plan.fail(step.step_id, error)
                branch.local_plan.block_remaining(error)
                return LocalExecutionSummary(
                    branch_id=branch.branch_id,
                    completed_step_ids=tuple(completed),
                    failed_step_id=step.step_id,
                    error=error,
                    outputs=tuple(outputs),
                )
            completed.append(step.step_id)
            outputs.append(output)


class GlobalForestScheduler:
    """Selects runnable Branches while respecting dependencies and active limits."""

    def __init__(self, forest: TaskForest) -> None:
        self.forest = forest

    def runnable(self, running_branch_ids: set[str]) -> tuple[Branch, ...]:
        available = self.forest.limits.max_active_branches - len(running_branch_ids)
        if available <= 0:
            return ()
        ready: list[Branch] = []
        for branch in self.forest.branches.values():
            if branch.branch_id in running_branch_ids:
                continue
            if branch.status not in {
                BranchStatus.PENDING,
                BranchStatus.READY,
                BranchStatus.RUNNING,
            }:
                continue
            try:
                dependency_state = self._dependency_state(branch)
            except KeyError as exc:
                branch.status = BranchStatus.BLOCKED
                branch.local_state["dependency_error"] = str(exc)
                continue
            if dependency_state == "blocked":
                failed = [
                    item
                    for item in branch.dependencies
                    if self.forest.branches[item].status
                    in {BranchStatus.FAILED, BranchStatus.BLOCKED, BranchStatus.CANCELLED}
                ]
                branch.status = BranchStatus.BLOCKED
                branch.local_state["blocked_by_branch_ids"] = failed
                continue
            if dependency_state == "waiting":
                branch.status = BranchStatus.PENDING
                continue
            branch.status = BranchStatus.READY
            ready.append(branch)
            if len(ready) >= available:
                break
        return tuple(ready)

    def has_unfinished_work(self) -> bool:
        return any(
            branch.status not in TERMINAL_BRANCH_STATUSES
            and branch.status != BranchStatus.PAUSED
            for branch in self.forest.branches.values()
        )

    def _dependency_state(self, branch: Branch) -> str:
        unknown = set(branch.dependencies) - self.forest.branches.keys()
        if unknown:
            raise KeyError(f"Unknown Branch dependencies: {sorted(unknown)}")
        statuses = [self.forest.branches[item].status for item in branch.dependencies]
        if any(
            status in {BranchStatus.FAILED, BranchStatus.BLOCKED, BranchStatus.CANCELLED}
            for status in statuses
        ):
            return "blocked"
        if all(
            status in {BranchStatus.COMPLETED, BranchStatus.PARTIALLY_COMPLETED}
            for status in statuses
        ):
            return "ready"
        return "waiting"


def _usage(output: Any, name: str, default: int) -> Any:
    if output is None:
        return default
    if isinstance(output, dict):
        return output.get(name, default)
    return getattr(output, name, default)


def _payload(output: Any) -> dict[str, Any]:
    if output is None:
        return {}
    if isinstance(output, dict):
        value = output.get("payload", output)
        return dict(value) if isinstance(value, dict) else {"value": value}
    value = getattr(output, "payload", None)
    if isinstance(value, dict):
        return dict(value)
    return {"value": value} if value is not None else {}
