from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    ACTIVE_BRANCH_STATUSES,
    Branch,
    BranchBudget,
    BranchResult,
    BranchStatus,
    ForestLimitError,
    JoinStatus,
    TaskForest,
)


@dataclass(frozen=True, slots=True)
class SpawnResult:
    branch: Branch
    created: bool
    merged_duplicate: bool = False


class BranchManager:
    def __init__(self, forest: TaskForest) -> None:
        self.forest = forest

    def spawn_child(
        self,
        *,
        parent_branch_id: str,
        objective: str,
        objective_version_id: str,
        status: BranchStatus = BranchStatus.READY,
        completion_criteria: tuple[str, ...] = (),
        result_contract: dict[str, Any] | None = None,
        budget: BranchBudget | None = None,
        assigned_agent: str | None = None,
        dependencies: tuple[str, ...] = (),
        execution_mode: str = "parallel",
        semantic_merge: bool = True,
    ) -> SpawnResult:
        duplicate = self.forest.find_semantic_duplicate(
            objective,
            parent_branch_id=parent_branch_id,
        )
        if duplicate and semantic_merge:
            duplicate.local_state.setdefault("merged_objectives", []).append(objective)
            return SpawnResult(duplicate, created=False, merged_duplicate=True)
        branch = self.forest.add_branch(
            objective=objective,
            objective_version_id=objective_version_id,
            parent_branch_id=parent_branch_id,
            status=status,
            execution_mode=execution_mode,
            dependencies=dependencies,
            completion_criteria=completion_criteria,
            result_contract=result_contract,
            budget=budget,
            assigned_agent=assigned_agent,
        )
        return SpawnResult(branch, created=True)

    def add_local_step(
        self,
        branch_id: str,
        title: str,
        *,
        dependencies: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ):
        self.forest.assert_node_capacity()
        return self.forest.branches[branch_id].local_plan.add_step(
            title,
            dependencies=dependencies,
            metadata=metadata,
        )

    def activate(self, branch_id: str) -> Branch:
        branch = self.forest.branches[branch_id]
        if branch.status in {BranchStatus.PAUSED, BranchStatus.PENDING, BranchStatus.READY}:
            if self.forest.active_count >= self.forest.limits.max_active_branches:
                branch.status = BranchStatus.PENDING
                return branch
            branch.status = BranchStatus.RUNNING
        return branch

    def pause(self, branch_id: str, *, recursive: bool = False) -> Branch:
        branch = self.forest.branches[branch_id]
        if branch.status in ACTIVE_BRANCH_STATUSES | {BranchStatus.PENDING}:
            branch.local_state["status_before_pause"] = branch.status.value
            branch.status = BranchStatus.PAUSED
        if recursive:
            for child_id in branch.child_branch_ids:
                self.pause(child_id, recursive=True)
        return branch

    def resume(self, branch_id: str) -> Branch:
        branch = self.forest.branches[branch_id]
        if branch.status != BranchStatus.PAUSED:
            return branch
        prior = BranchStatus(branch.local_state.pop("status_before_pause", BranchStatus.READY))
        requested = BranchStatus.RUNNING if prior == BranchStatus.RUNNING else BranchStatus.READY
        if self.forest.active_count >= self.forest.limits.max_active_branches:
            requested = BranchStatus.PENDING
        branch.status = requested
        return branch

    def cancel(
        self,
        branch_id: str,
        *,
        reason: str,
        recursive: bool = True,
        preserve_evidence: bool = True,
    ) -> Branch:
        branch = self.forest.branches[branch_id]
        branch.local_state["cancel_reason"] = reason
        if preserve_evidence:
            branch.local_state["preserved_evidence_ids"] = list(branch.evidence_ids)
        branch.status = BranchStatus.CANCELLED
        if recursive:
            for child_id in branch.child_branch_ids:
                self.cancel(
                    child_id,
                    reason=reason,
                    recursive=True,
                    preserve_evidence=preserve_evidence,
                )
        return branch

    def complete(
        self,
        branch_id: str,
        *,
        conclusion: str,
        evidence_ids: tuple[str, ...] = (),
        decision: str | None = None,
        confidence: float | None = None,
        unmet_criteria: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> Branch:
        branch = self.forest.branches[branch_id]
        partial = bool(unmet_criteria)
        branch.result = BranchResult(
            branch_id=branch_id,
            conclusion=conclusion,
            evidence_ids=evidence_ids,
            decision=decision,
            confidence=confidence,
            partial=partial,
            unmet_criteria=unmet_criteria,
            metadata=dict(metadata or {}),
        )
        branch.evidence_ids = list(dict.fromkeys((*branch.evidence_ids, *evidence_ids)))
        branch.status = (
            BranchStatus.PARTIALLY_COMPLETED if partial else BranchStatus.COMPLETED
        )
        return branch

    def fail(
        self,
        branch_id: str,
        *,
        error: str,
        failed_step_id: str | None = None,
        evidence_ids: tuple[str, ...] = (),
    ) -> Branch:
        branch = self.forest.branches[branch_id]
        branch.local_state["failure"] = {
            "error": error,
            "failed_step_id": failed_step_id,
        }
        branch.result = BranchResult(
            branch_id=branch_id,
            conclusion=f"Branch failed locally: {error}",
            evidence_ids=evidence_ids,
            partial=True,
            unmet_criteria=(
                f"Recover failed step: {failed_step_id}"
                if failed_step_id
                else "Recover failed branch",
            ),
            metadata={"error": error, "failed_step_id": failed_step_id},
        )
        branch.status = BranchStatus.FAILED
        return branch

    def resolve_join(self, join_id: str) -> BranchResult | None:
        join = self.forest.join_nodes[join_id]
        result = join.evaluate(self.forest.branches)
        if join.status == JoinStatus.CONFLICT and join.conflict_branch_id is None:
            try:
                self.forest.assert_node_capacity(2)
            except ForestLimitError as exc:
                source_results = [
                    self.forest.branches[branch_id].result
                    for branch_id in join.required_branch_ids
                    if self.forest.branches[branch_id].result is not None
                ]
                join.result = BranchResult(
                    branch_id=join.join_id,
                    conclusion="\n".join(item.conclusion for item in source_results),
                    evidence_ids=tuple(
                        dict.fromkeys(
                            evidence_id
                            for item in source_results
                            for evidence_id in item.evidence_ids
                        )
                    ),
                    confidence=None,
                    partial=True,
                    unmet_criteria=("Conflict verification requires more node budget",),
                    metadata={
                        "conflicts": list(join.conflicts),
                        "resolution_blocked": str(exc),
                    },
                )
                join.status = JoinStatus.COMPLETED
                return join.result
            parent_id = self._common_parent(join.required_branch_ids)
            parent_id = parent_id or self.forest.root_branch.branch_id
            objective_version_id = self.forest.branches[parent_id].objective_version_id
            conflict = self.spawn_child(
                parent_branch_id=parent_id,
                objective=f"Resolve conflicting branch conclusions for: {join.objective}",
                objective_version_id=objective_version_id,
                completion_criteria=("Explain and resolve the conflicting evidence.",),
                result_contract={"required": ["resolution", "evidence_ids"]},
                dependencies=join.required_branch_ids,
                semantic_merge=False,
            ).branch
            conflict.local_state["conflicting_branch_ids"] = list(join.required_branch_ids)
            conflict.local_state["conflict_directions"] = list(join.conflicts)
            self.add_local_step(
                conflict.branch_id,
                "Verify and resolve conflicting branch conclusions",
                metadata={
                    "kind": "conflict_verification",
                    "join_id": join.join_id,
                    "conflicting_branch_ids": list(join.required_branch_ids),
                },
            )
            join.conflict_branch_id = conflict.branch_id
        return result

    def _common_parent(self, branch_ids: tuple[str, ...]) -> str | None:
        parents = {self.forest.branches[item].parent_branch_id for item in branch_ids}
        return next(iter(parents)) if len(parents) == 1 else None
