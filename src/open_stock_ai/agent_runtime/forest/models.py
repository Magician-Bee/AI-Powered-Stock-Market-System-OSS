from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class ForestLimitError(RuntimeError):
    pass


class BranchStatus(StrEnum):
    PROPOSED = "proposed"
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_DECISION = "waiting_decision"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    REPAIRING = "repairing"
    REPLANNING = "replanning"
    WAITING_DEPENDENCY = "waiting_dependency"
    CANCELLED = "cancelled"


ACTIVE_BRANCH_STATUSES = {
    BranchStatus.READY,
    BranchStatus.RUNNING,
    BranchStatus.WAITING_USER_INPUT,
    BranchStatus.WAITING_DECISION,
    BranchStatus.WAITING_APPROVAL,
    BranchStatus.REPAIRING,
    BranchStatus.REPLANNING,
    BranchStatus.WAITING_DEPENDENCY,
}
TERMINAL_BRANCH_STATUSES = {
    BranchStatus.COMPLETED,
    BranchStatus.PARTIALLY_COMPLETED,
    BranchStatus.FAILED,
    BranchStatus.BLOCKED,
    BranchStatus.CANCELLED,
}


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class JoinStatus(StrEnum):
    WAITING = "waiting"
    READY = "ready"
    CONFLICT = "conflict"
    COMPLETED = "completed"


@dataclass(slots=True)
class BranchBudget:
    max_retries: int = 2
    max_tokens: int = 16_000
    max_seconds: int = 600
    retries_used: int = 0
    tokens_used: int = 0
    seconds_used: float = 0

    def charge(
        self,
        *,
        retries: int = 0,
        tokens: int = 0,
        seconds: float = 0,
    ) -> None:
        next_retries = self.retries_used + max(0, retries)
        next_tokens = self.tokens_used + max(0, tokens)
        next_seconds = self.seconds_used + max(0, seconds)
        if next_retries > self.max_retries:
            raise ForestLimitError("Branch retry budget exceeded")
        if next_tokens > self.max_tokens:
            raise ForestLimitError("Branch token budget exceeded")
        if next_seconds > self.max_seconds:
            raise ForestLimitError("Branch time budget exceeded")
        self.retries_used = next_retries
        self.tokens_used = next_tokens
        self.seconds_used = next_seconds


@dataclass(frozen=True, slots=True)
class ForestLimits:
    max_depth: int = 5
    max_active_branches: int = 4
    max_nodes: int = 128
    duplicate_similarity: float = 0.82

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise ValueError("max_depth must be positive")
        if self.max_active_branches < 1:
            raise ValueError("max_active_branches must be positive")
        if self.max_nodes < 2:
            raise ValueError("max_nodes must allow a root and one child")


@dataclass(slots=True)
class LocalStep:
    step_id: str
    title: str
    dependencies: tuple[str, ...] = ()
    status: StepStatus = StepStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LocalPlan:
    plan_id: str = field(default_factory=lambda: f"BLP-{uuid4().hex}")
    revision: int = 1
    steps: dict[str, LocalStep] = field(default_factory=dict)

    def add_step(
        self,
        title: str,
        *,
        dependencies: tuple[str, ...] = (),
        step_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LocalStep:
        unknown = set(dependencies) - self.steps.keys()
        if unknown:
            raise KeyError(f"Unknown local step dependencies: {sorted(unknown)}")
        item = LocalStep(
            step_id=step_id or f"BST-{uuid4().hex}",
            title=title.strip(),
            dependencies=dependencies,
            metadata=dict(metadata or {}),
        )
        if not item.title:
            raise ValueError("Local step title cannot be empty")
        if item.step_id in self.steps:
            raise ValueError(f"Local step already exists: {item.step_id}")
        self.steps[item.step_id] = item
        self.revision += 1
        return item

    def ready_steps(self) -> tuple[LocalStep, ...]:
        completed = {
            item.step_id
            for item in self.steps.values()
            if item.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}
        }
        ready: list[LocalStep] = []
        for item in self.steps.values():
            if item.status == StepStatus.PENDING and set(item.dependencies) <= completed:
                item.status = StepStatus.READY
            if item.status == StepStatus.READY:
                ready.append(item)
        return tuple(ready)

    def start(self, step_id: str) -> LocalStep:
        self.ready_steps()
        step = self.steps[step_id]
        if step.status != StepStatus.READY:
            raise ValueError(f"Local step is not ready: {step_id}")
        step.status = StepStatus.RUNNING
        return step

    def complete(self, step_id: str, result: dict[str, Any] | None = None) -> LocalStep:
        step = self.steps[step_id]
        if step.status not in {StepStatus.READY, StepStatus.RUNNING}:
            raise ValueError(f"Local step cannot complete from {step.status}: {step_id}")
        step.status = StepStatus.COMPLETED
        step.result = dict(result or {})
        self.ready_steps()
        return step

    def fail(self, step_id: str, error: str) -> LocalStep:
        step = self.steps[step_id]
        step.status = StepStatus.FAILED
        step.error = error
        for item in self.steps.values():
            if step_id in item.dependencies and item.status == StepStatus.PENDING:
                item.status = StepStatus.BLOCKED
        return step

    def block_remaining(self, reason: str) -> None:
        """Close the local execution boundary after one ordered step fails."""

        for item in self.steps.values():
            if item.status in {StepStatus.PENDING, StepStatus.READY}:
                item.status = StepStatus.BLOCKED
                item.error = reason

    @property
    def is_complete(self) -> bool:
        return bool(self.steps) and all(
            item.status in {StepStatus.COMPLETED, StepStatus.SKIPPED}
            for item in self.steps.values()
        )


@dataclass(frozen=True, slots=True)
class BranchResult:
    branch_id: str
    conclusion: str
    evidence_ids: tuple[str, ...] = ()
    decision: str | None = None
    confidence: float | None = None
    partial: bool = False
    unmet_criteria: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("Branch result confidence must be between 0 and 1")


@dataclass(slots=True)
class Branch:
    branch_id: str
    forest_id: str
    objective_version_id: str
    objective: str
    parent_branch_id: str | None = None
    depth: int = 0
    status: BranchStatus = BranchStatus.PENDING
    execution_mode: str = "parallel"
    local_plan: LocalPlan = field(default_factory=LocalPlan)
    child_branch_ids: list[str] = field(default_factory=list)
    dependencies: tuple[str, ...] = ()
    completion_criteria: tuple[str, ...] = ()
    evidence_ids: list[str] = field(default_factory=list)
    result_contract: dict[str, Any] = field(default_factory=dict)
    budget: BranchBudget = field(default_factory=BranchBudget)
    local_state: dict[str, Any] = field(default_factory=dict)
    result: BranchResult | None = None
    assigned_agent: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_BRANCH_STATUSES


@dataclass(slots=True)
class JoinNode:
    join_id: str
    forest_id: str
    required_branch_ids: tuple[str, ...]
    objective: str
    status: JoinStatus = JoinStatus.WAITING
    result: BranchResult | None = None
    conflicts: tuple[str, ...] = ()
    conflict_branch_id: str | None = None

    def evaluate(self, branches: dict[str, Branch]) -> BranchResult | None:
        unavailable = [
            branch_id
            for branch_id in self.required_branch_ids
            if branch_id not in branches
            or branches[branch_id].status not in TERMINAL_BRANCH_STATUSES
        ]
        if unavailable:
            self.status = JoinStatus.WAITING
            return None
        self.status = JoinStatus.READY
        failed_branch_ids = tuple(
            branch_id
            for branch_id in self.required_branch_ids
            if branches[branch_id].status
            in {BranchStatus.FAILED, BranchStatus.BLOCKED, BranchStatus.CANCELLED}
        )
        results = [branches[item].result for item in self.required_branch_ids]
        concrete = [item for item in results if item is not None]
        directions = {_decision_direction(item) for item in concrete}
        directions.discard(None)
        if _has_direction_conflict(directions):
            self.conflicts = tuple(sorted(str(item) for item in directions))
            verifier = (
                branches.get(self.conflict_branch_id)
                if self.conflict_branch_id is not None
                else None
            )
            if verifier is None or verifier.status not in {
                BranchStatus.COMPLETED,
                BranchStatus.PARTIALLY_COMPLETED,
            }:
                self.status = JoinStatus.CONFLICT
                return None
            if verifier.result is not None:
                concrete.append(verifier.result)
        evidence = tuple(
            dict.fromkeys(
                evidence_id
                for item in concrete
                for evidence_id in item.evidence_ids
            )
        )
        conclusion = "\n".join(item.conclusion for item in concrete if item.conclusion)
        confidence_values = [item.confidence for item in concrete if item.confidence is not None]
        verifier_result = (
            branches[self.conflict_branch_id].result
            if self.conflict_branch_id in branches
            else None
        )
        unmet = tuple(
            dict.fromkeys(
                (
                    *(f"Branch unavailable: {branch_id}" for branch_id in failed_branch_ids),
                    *(
                        criterion
                        for item in concrete
                        for criterion in item.unmet_criteria
                    ),
                )
            )
        )
        self.result = BranchResult(
            branch_id=self.join_id,
            conclusion=conclusion,
            evidence_ids=evidence,
            decision=(
                verifier_result.decision
                if verifier_result is not None and verifier_result.decision
                else next((item.decision for item in concrete if item.decision), None)
            ),
            confidence=(
                sum(confidence_values) / len(confidence_values)
                if confidence_values
                else None
            ),
            partial=bool(failed_branch_ids) or any(item.partial for item in concrete),
            unmet_criteria=unmet,
            metadata={
                "failed_branch_ids": list(failed_branch_ids),
                "conflicts": list(self.conflicts),
                "resolved_by": self.conflict_branch_id,
            },
        )
        self.status = JoinStatus.COMPLETED
        return self.result


@dataclass(slots=True)
class TaskForest:
    forest_id: str
    session_id: str
    root_objective_version_id: str
    root_goal: str
    revision: int = 1
    limits: ForestLimits = field(default_factory=ForestLimits)
    branches: dict[str, Branch] = field(default_factory=dict)
    join_nodes: dict[str, JoinNode] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        objective_version_id: str,
        root_goal: str,
        limits: ForestLimits | None = None,
        forest_id: str | None = None,
        root_branch_id: str | None = None,
    ) -> TaskForest:
        forest = cls(
            forest_id=forest_id or f"TF-{uuid4().hex}",
            session_id=session_id,
            root_objective_version_id=objective_version_id,
            root_goal=root_goal.strip(),
            limits=limits or ForestLimits(),
        )
        forest.add_branch(
            objective=root_goal,
            objective_version_id=objective_version_id,
            parent_branch_id=None,
            status=BranchStatus.RUNNING,
            execution_mode="master",
            branch_id=root_branch_id,
        )
        return forest

    @property
    def root_branch(self) -> Branch:
        return next(item for item in self.branches.values() if item.parent_branch_id is None)

    @property
    def node_count(self) -> int:
        return (
            len(self.branches)
            + len(self.join_nodes)
            + sum(len(item.local_plan.steps) for item in self.branches.values())
        )

    @property
    def active_count(self) -> int:
        return sum(1 for item in self.branches.values() if item.is_active)

    def add_branch(
        self,
        *,
        objective: str,
        objective_version_id: str,
        parent_branch_id: str | None,
        status: BranchStatus = BranchStatus.PENDING,
        execution_mode: str = "parallel",
        dependencies: tuple[str, ...] = (),
        completion_criteria: tuple[str, ...] = (),
        result_contract: dict[str, Any] | None = None,
        budget: BranchBudget | None = None,
        assigned_agent: str | None = None,
        branch_id: str | None = None,
    ) -> Branch:
        if self.node_count + 1 > self.limits.max_nodes:
            raise ForestLimitError("Task Forest node budget exceeded")
        parent = self.branches.get(parent_branch_id) if parent_branch_id else None
        if parent_branch_id and parent is None:
            raise KeyError(f"Unknown parent branch: {parent_branch_id}")
        depth = parent.depth + 1 if parent else 0
        if depth > self.limits.max_depth:
            raise ForestLimitError("Task Forest maximum branch depth exceeded")
        if status in ACTIVE_BRANCH_STATUSES and self.active_count >= self.limits.max_active_branches:
            status = BranchStatus.PENDING
        item = Branch(
            branch_id=branch_id or f"BR-{uuid4().hex}",
            forest_id=self.forest_id,
            objective_version_id=objective_version_id,
            objective=objective.strip(),
            parent_branch_id=parent_branch_id,
            depth=depth,
            status=status,
            execution_mode=execution_mode,
            dependencies=dependencies,
            completion_criteria=completion_criteria,
            result_contract=dict(result_contract or {}),
            budget=budget or BranchBudget(),
            assigned_agent=assigned_agent,
        )
        self.branches[item.branch_id] = item
        if parent:
            parent.child_branch_ids.append(item.branch_id)
        self.revision += 1
        return item

    def find_semantic_duplicate(
        self,
        objective: str,
        *,
        parent_branch_id: str | None,
    ) -> Branch | None:
        for branch in self.branches.values():
            if branch.parent_branch_id != parent_branch_id:
                continue
            if _similarity(branch.objective, objective) >= self.limits.duplicate_similarity:
                return branch
        return None

    def add_join(
        self,
        *,
        required_branch_ids: tuple[str, ...],
        objective: str,
    ) -> JoinNode:
        if self.node_count + 1 > self.limits.max_nodes:
            raise ForestLimitError("Task Forest node budget exceeded")
        unknown = set(required_branch_ids) - self.branches.keys()
        if unknown:
            raise KeyError(f"Unknown branches for join: {sorted(unknown)}")
        if len(required_branch_ids) < 2:
            raise ValueError("JoinNode requires at least two branches")
        item = JoinNode(
            join_id=f"JN-{uuid4().hex}",
            forest_id=self.forest_id,
            required_branch_ids=required_branch_ids,
            objective=objective.strip(),
        )
        self.join_nodes[item.join_id] = item
        self.revision += 1
        return item

    def assert_node_capacity(self, additional: int = 1) -> None:
        if self.node_count + additional > self.limits.max_nodes:
            raise ForestLimitError("Task Forest node budget exceeded")


def _tokens(value: str) -> set[str]:
    lowered = value.casefold()
    tokens = set(re.findall(r"[a-z0-9]{2,}", lowered))
    for run in re.findall(r"[\u3400-\u9fff]+", lowered):
        tokens.update(run[index : index + 2] for index in range(max(1, len(run) - 1)))
    return tokens


def _similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return float(left.strip().casefold() == right.strip().casefold())
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _decision_direction(result: BranchResult) -> str | None:
    value = (result.decision or result.metadata.get("direction") or "").casefold()
    aliases = {
        "buy": "positive",
        "bullish": "positive",
        "long": "positive",
        "加碼": "positive",
        "偏多": "positive",
        "sell": "negative",
        "bearish": "negative",
        "short": "negative",
        "減碼": "negative",
        "偏空": "negative",
        "hold": "neutral",
        "neutral": "neutral",
        "觀望": "neutral",
        "中性": "neutral",
    }
    for token, direction in aliases.items():
        if token in value:
            return direction
    return value or None


def _has_direction_conflict(directions: set[str]) -> bool:
    return "positive" in directions and "negative" in directions
