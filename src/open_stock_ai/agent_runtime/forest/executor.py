from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable

from .branch_manager import BranchManager
from .models import (
    Branch,
    BranchBudget,
    BranchResult,
    BranchStatus,
    JoinStatus,
    LocalStep,
    TaskForest,
)
from .scheduler import GlobalForestScheduler, LocalBranchScheduler, LocalExecutionSummary


class ForestExecutionStatus(StrEnum):
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ChildBranchSpec:
    objective: str
    step_titles: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    completion_criteria: tuple[str, ...] = ()
    result_contract: dict[str, Any] = field(default_factory=dict)
    budget: BranchBudget | None = None
    assigned_agent: str | None = None
    semantic_merge: bool = True


@dataclass(frozen=True, slots=True)
class StepExecutionResult:
    payload: dict[str, Any] = field(default_factory=dict)
    conclusion: str = ""
    evidence_ids: tuple[str, ...] = ()
    decision: str | None = None
    confidence: float | None = None
    criteria_met: tuple[str, ...] = ()
    unmet_criteria: tuple[str, ...] = ()
    tokens_used: int = 0
    retries_used: int = 0
    child_branches: tuple[ChildBranchSpec, ...] = ()

    def __post_init__(self) -> None:
        if self.tokens_used < 0 or self.retries_used < 0:
            raise ValueError("Step usage cannot be negative")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("Step confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class CompletionGateResult:
    completed: bool
    required_incomplete_branch_ids: tuple[str, ...]
    insufficient_evidence_branch_ids: tuple[str, ...]
    open_blocking_branch_ids: tuple[str, ...]
    pending_approval_branch_ids: tuple[str, ...]
    critical_failure_branch_ids: tuple[str, ...]
    invalid_result_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ForestExecutionReport:
    forest_id: str
    status: ForestExecutionStatus
    completed_branch_ids: tuple[str, ...]
    partially_completed_branch_ids: tuple[str, ...]
    failed_branch_ids: tuple[str, ...]
    blocked_branch_ids: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    confidence_impact: float
    next_recovery: tuple[str, ...]
    join_results: tuple[BranchResult, ...]
    completion_gate: CompletionGateResult
    peak_active_branches: int


StepExecutor = Callable[["BranchExecutionContext"], Awaitable[Any] | Any]


class BranchExecutionContext:
    """The only mutation surface exposed to a running local Step."""

    def __init__(
        self,
        *,
        forest: TaskForest,
        manager: BranchManager,
        branch: Branch,
        step: LocalStep,
    ) -> None:
        self.forest = forest
        self.manager = manager
        self.branch = branch
        self.step = step

    def spawn_child(self, spec: ChildBranchSpec) -> Branch:
        duplicate = self.forest.find_semantic_duplicate(
            spec.objective,
            parent_branch_id=self.branch.branch_id,
        )
        if duplicate is None or not spec.semantic_merge:
            self.forest.assert_node_capacity(1 + len(spec.step_titles))
        spawned = self.manager.spawn_child(
            parent_branch_id=self.branch.branch_id,
            objective=spec.objective,
            objective_version_id=self.branch.objective_version_id,
            completion_criteria=spec.completion_criteria,
            result_contract=spec.result_contract,
            budget=spec.budget,
            assigned_agent=spec.assigned_agent,
            dependencies=spec.dependencies,
            semantic_merge=spec.semantic_merge,
        )
        child = spawned.branch
        if spawned.created:
            previous_step_id: str | None = None
            for title in spec.step_titles:
                dependencies = (previous_step_id,) if previous_step_id else ()
                child_step = self.manager.add_local_step(
                    child.branch_id,
                    title,
                    dependencies=dependencies,
                )
                previous_step_id = child_step.step_id
        return child


class ForestCompletionGate:
    """Host-owned P78 validation; model output alone cannot complete a Forest."""

    def evaluate(self, forest: TaskForest) -> CompletionGateResult:
        root_id = forest.root_branch.branch_id
        work = [
            branch
            for branch in forest.branches.values()
            if branch.branch_id != root_id or len(forest.branches) == 1
        ]
        required = [branch for branch in work if branch.local_state.get("required", True)]
        required_incomplete = tuple(
            branch.branch_id
            for branch in required
            if branch.status != BranchStatus.COMPLETED
        )
        insufficient_evidence = tuple(
            branch.branch_id
            for branch in required
            if _requires_evidence(branch)
            and (branch.result is None or not branch.result.evidence_ids)
        )
        open_blocking = tuple(
            branch.branch_id
            for branch in work
            if branch.status
            in {
                BranchStatus.PENDING,
                BranchStatus.READY,
                BranchStatus.RUNNING,
                BranchStatus.WAITING_USER_INPUT,
                BranchStatus.WAITING_DECISION,
                BranchStatus.PAUSED,
                BranchStatus.REPAIRING,
                BranchStatus.REPLANNING,
                BranchStatus.WAITING_DEPENDENCY,
            }
        )
        pending_approval = tuple(
            branch.branch_id
            for branch in work
            if branch.status == BranchStatus.WAITING_APPROVAL
        )
        critical_failures = tuple(
            branch.branch_id
            for branch in work
            if branch.local_state.get("critical", False)
            and branch.status in {BranchStatus.FAILED, BranchStatus.BLOCKED}
        )
        invalid_results = tuple(
            [
                branch.branch_id
                for branch in work
                if branch.status
                in {BranchStatus.COMPLETED, BranchStatus.PARTIALLY_COMPLETED}
                and branch.result is None
            ]
            + [
                join.join_id
                for join in forest.join_nodes.values()
                if join.status != JoinStatus.COMPLETED
                or join.result is None
                or join.result.partial
            ]
        )
        completed = not any(
            (
                required_incomplete,
                insufficient_evidence,
                open_blocking,
                pending_approval,
                critical_failures,
                invalid_results,
            )
        )
        return CompletionGateResult(
            completed=completed,
            required_incomplete_branch_ids=required_incomplete,
            insufficient_evidence_branch_ids=insufficient_evidence,
            open_blocking_branch_ids=open_blocking,
            pending_approval_branch_ids=pending_approval,
            critical_failure_branch_ids=critical_failures,
            invalid_result_ids=invalid_results,
        )


class ForestExecutor:
    """Runs a recursive Task Forest with parallel Branch and ordered Step semantics."""

    def __init__(
        self,
        forest: TaskForest,
        step_executor: StepExecutor,
        *,
        completion_gate: ForestCompletionGate | None = None,
    ) -> None:
        self.forest = forest
        self.step_executor = step_executor
        self.manager = BranchManager(forest)
        self.global_scheduler = GlobalForestScheduler(forest)
        self.local_scheduler = LocalBranchScheduler()
        self.completion_gate = completion_gate or ForestCompletionGate()
        self._peak_active = 0

    async def execute(self) -> ForestExecutionReport:
        tasks: dict[str, asyncio.Task[None]] = {}
        while True:
            self._resolve_joins()
            for branch in self.global_scheduler.runnable(set(tasks)):
                branch.status = BranchStatus.RUNNING
                tasks[branch.branch_id] = asyncio.create_task(self._run_branch(branch))
            self._peak_active = max(self._peak_active, len(tasks))

            if tasks:
                done, _ = await asyncio.wait(
                    tuple(tasks.values()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    branch_id = next(
                        key for key, candidate in tasks.items() if candidate is task
                    )
                    del tasks[branch_id]
                    await task
                continue

            self._resolve_joins()
            if not self.global_scheduler.runnable(set()):
                break

        return self._report()

    async def _run_branch(self, branch: Branch) -> None:
        if not branch.local_plan.steps and branch.branch_id != self.forest.root_branch.branch_id:
            self.manager.complete(
                branch.branch_id,
                conclusion="Branch has no executable Local Plan.",
                unmet_criteria=("Local plan has no steps",),
            )
            return
        try:
            summary = await self.local_scheduler.execute(branch, self._execute_step)
        except asyncio.CancelledError:
            branch.status = BranchStatus.CANCELLED
            branch.local_state["cancel_reason"] = "Forest executor cancelled"
            raise
        if not summary.succeeded:
            self.manager.fail(
                branch.branch_id,
                error=summary.error or "Local step failed",
                failed_step_id=summary.failed_step_id,
                evidence_ids=_evidence(summary.outputs),
            )
            return
        self._complete_branch(branch, summary)

    async def _execute_step(self, branch: Branch, step: LocalStep) -> StepExecutionResult:
        context = BranchExecutionContext(
            forest=self.forest,
            manager=self.manager,
            branch=branch,
            step=step,
        )
        value = self.step_executor(context)
        if inspect.isawaitable(value):
            value = await value
        result = _normalise_result(value)
        for child in result.child_branches:
            context.spawn_child(child)
        return result

    def _complete_branch(self, branch: Branch, summary: LocalExecutionSummary) -> None:
        results = [
            item for item in summary.outputs if isinstance(item, StepExecutionResult)
        ]
        criteria_met = {
            criterion for item in results for criterion in item.criteria_met
        }
        unmet = list(
            dict.fromkeys(
                criterion
                for item in results
                for criterion in item.unmet_criteria
            )
        )
        unmet.extend(
            criterion
            for criterion in branch.completion_criteria
            if criterion not in criteria_met and criterion not in unmet
        )
        evidence = _evidence(results)
        conclusions = [item.conclusion for item in results if item.conclusion]
        conclusion = conclusions[-1] if conclusions else f"Completed: {branch.objective}"
        decisions = [item.decision for item in results if item.decision]
        confidences = [item.confidence for item in results if item.confidence is not None]
        required_fields = set(branch.result_contract.get("required", ()))
        if required_fields & {"evidence", "evidence_ids"} and not evidence:
            unmet.append("Result contract requires evidence_ids")
        if required_fields & {"resolution", "conclusion"} and not conclusions:
            unmet.append("Result contract requires a conclusion")
        if "decision" in required_fields and not decisions:
            unmet.append("Result contract requires a decision")
        self.manager.complete(
            branch.branch_id,
            conclusion=conclusion,
            evidence_ids=evidence,
            decision=decisions[-1] if decisions else None,
            confidence=(sum(confidences) / len(confidences) if confidences else None),
            unmet_criteria=tuple(dict.fromkeys(unmet)),
            metadata={"completed_step_ids": list(summary.completed_step_ids)},
        )

    def _resolve_joins(self) -> None:
        for join in tuple(self.forest.join_nodes.values()):
            if join.status != JoinStatus.COMPLETED:
                self.manager.resolve_join(join.join_id)

    def _report(self) -> ForestExecutionReport:
        gate = self.completion_gate.evaluate(self.forest)
        root_id = self.forest.root_branch.branch_id
        work = [
            branch
            for branch in self.forest.branches.values()
            if branch.branch_id != root_id or len(self.forest.branches) == 1
        ]
        completed = tuple(
            item.branch_id for item in work if item.status == BranchStatus.COMPLETED
        )
        partial = tuple(
            item.branch_id
            for item in work
            if item.status == BranchStatus.PARTIALLY_COMPLETED
        )
        failed = tuple(item.branch_id for item in work if item.status == BranchStatus.FAILED)
        blocked = tuple(item.branch_id for item in work if item.status == BranchStatus.BLOCKED)
        if gate.completed and not (partial or failed or blocked):
            status = ForestExecutionStatus.COMPLETED
        elif completed or partial:
            status = ForestExecutionStatus.PARTIALLY_COMPLETED
        else:
            status = ForestExecutionStatus.FAILED
        missing = tuple(
            dict.fromkeys(
                criterion
                for branch in work
                if branch.result is not None
                for criterion in branch.result.unmet_criteria
            )
        )
        confidence_impact = (
            min(1.0, (len(failed) + len(blocked) + len(partial)) / max(1, len(work)))
            if work
            else 0.0
        )
        recovery = tuple(
            dict.fromkeys(
                [
                    f"Repair branch {branch_id} from its failed local step"
                    for branch_id in (*failed, *blocked)
                ]
                + [f"Satisfy: {criterion}" for criterion in missing]
            )
        )
        return ForestExecutionReport(
            forest_id=self.forest.forest_id,
            status=status,
            completed_branch_ids=completed,
            partially_completed_branch_ids=partial,
            failed_branch_ids=failed,
            blocked_branch_ids=blocked,
            missing_evidence=missing,
            confidence_impact=confidence_impact,
            next_recovery=recovery,
            join_results=tuple(
                join.result
                for join in self.forest.join_nodes.values()
                if join.result is not None
            ),
            completion_gate=gate,
            peak_active_branches=self._peak_active,
        )


def _normalise_result(value: Any) -> StepExecutionResult:
    if isinstance(value, StepExecutionResult):
        return value
    if value is None:
        return StepExecutionResult()
    if isinstance(value, dict):
        known = {
            "payload",
            "conclusion",
            "evidence_ids",
            "decision",
            "confidence",
            "criteria_met",
            "unmet_criteria",
            "tokens_used",
            "retries_used",
            "child_branches",
        }
        kwargs = {key: item for key, item in value.items() if key in known}
        kwargs.setdefault("payload", {key: item for key, item in value.items() if key not in known})
        for key in ("evidence_ids", "criteria_met", "unmet_criteria", "child_branches"):
            if key in kwargs:
                kwargs[key] = tuple(kwargs[key])
        if "child_branches" in kwargs:
            kwargs["child_branches"] = tuple(
                item if isinstance(item, ChildBranchSpec) else ChildBranchSpec(**item)
                for item in kwargs["child_branches"]
            )
        return StepExecutionResult(**kwargs)
    return StepExecutionResult(payload={"value": value})


def _evidence(results: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            evidence_id
            for item in results
            if isinstance(item, StepExecutionResult)
            for evidence_id in item.evidence_ids
        )
    )


def _requires_evidence(branch: Branch) -> bool:
    required = set(branch.result_contract.get("required", ()))
    return bool(required & {"evidence", "evidence_ids"})
