from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..forest.branch_manager import BranchManager
from ..forest.models import BranchStatus, TaskForest
from ..session.objective_manager import ObjectiveManager


class SteeringIntent(StrEnum):
    ANSWER_AGENT_QUESTION = "answer_agent_question"
    APPEND_REQUIREMENT = "append_requirement"
    MODIFY_REQUIREMENT = "modify_requirement"
    CORRECT_FACT = "correct_fact"
    SOFT_STEER = "soft_steer"
    HARD_STEER = "hard_steer"
    FORK_BRANCH = "fork_branch"
    CANCEL_BRANCH = "cancel_branch"
    PAUSE_BRANCH = "pause_branch"
    RESUME_BRANCH = "resume_branch"
    MODIFY_ARTIFACT = "modify_artifact"
    GENERAL_QUESTION = "general_question"
    NEW_GOAL = "new_goal"


@dataclass(frozen=True, slots=True)
class SteeringOutcome:
    intent: SteeringIntent
    objective_version_id: str
    affected_branch_ids: tuple[str, ...]
    created_branch_ids: tuple[str, ...] = ()
    cancelled_branch_ids: tuple[str, ...] = ()
    preserved_branch_ids: tuple[str, ...] = ()
    message: str = ""


class SteeringRouter:
    def __init__(
        self,
        *,
        forest: TaskForest,
        branch_manager: BranchManager,
        objective_manager: ObjectiveManager,
    ) -> None:
        self.forest = forest
        self.branches = branch_manager
        self.objectives = objective_manager

    def classify(self, message: str) -> SteeringIntent:
        value = message.strip().casefold()
        if any(token in value for token in (
            "不是", "更正", "股票代號錯", "應該是", "改為", "改成", "換成", "改用",
        )):
            return SteeringIntent.HARD_STEER
        if any(token in value for token in ("暫停", "pause")):
            return SteeringIntent.PAUSE_BRANCH
        if any(token in value for token in ("恢復", "繼續該分支", "resume")):
            return SteeringIntent.RESUME_BRANCH
        if any(token in value for token in ("取消", "cancel")):
            return SteeringIntent.CANCEL_BRANCH
        if any(token in value for token in ("原本", "另外", "同時比較", "fork")):
            return SteeringIntent.FORK_BRANCH
        if any(token in value for token in ("順便", "再查", "也查", "也要查", "補充")):
            return SteeringIntent.SOFT_STEER
        if value.endswith(("?", "？")):
            return SteeringIntent.GENERAL_QUESTION
        return SteeringIntent.APPEND_REQUIREMENT

    def route(
        self,
        *,
        session_id: str,
        message_id: str,
        message: str,
        target_branch_id: str | None = None,
        intent: SteeringIntent | None = None,
        affected_branch_ids: tuple[str, ...] = (),
        replacement_objective: str | None = None,
    ) -> SteeringOutcome:
        resolved = intent or self.classify(message)
        current = self.objectives.current(session_id)
        if current is None:
            raise KeyError(f"Session has no objective: {session_id}")
        target_id = target_branch_id or self.forest.root_branch.branch_id

        if resolved in {SteeringIntent.SOFT_STEER, SteeringIntent.APPEND_REQUIREMENT}:
            version = self.objectives.revise(
                session_id=session_id,
                added_requirements=(message,),
                message_id=message_id,
            )
            spawn = self.branches.spawn_child(
                parent_branch_id=target_id,
                objective=message,
                objective_version_id=version.objective_id,
                status=BranchStatus.READY,
            )
            self.forest.branches[target_id].local_plan.revision += 1
            preserved = tuple(
                item.branch_id
                for item in self.forest.branches.values()
                if item.branch_id not in {target_id, spawn.branch.branch_id}
                and item.status != BranchStatus.CANCELLED
            )
            return SteeringOutcome(
                intent=resolved,
                objective_version_id=version.objective_id,
                affected_branch_ids=(target_id,),
                created_branch_ids=(spawn.branch.branch_id,) if spawn.created else (),
                preserved_branch_ids=preserved,
                message="Added a local child branch; unrelated branches continue.",
            )

        if resolved in {SteeringIntent.HARD_STEER, SteeringIntent.CORRECT_FACT}:
            version = self.objectives.revise(
                session_id=session_id,
                objective=replacement_objective or current.objective,
                added_requirements=(message,),
                message_id=message_id,
            )
            impacted = affected_branch_ids or (target_id,)
            created: list[str] = []
            cancelled: list[str] = []
            for branch_id in impacted:
                branch = self.forest.branches[branch_id]
                if branch.parent_branch_id is None:
                    branch.objective_version_id = version.objective_id
                    branch.local_state["hard_steer_message"] = message
                    continue
                old_objective = branch.objective
                parent_id = branch.parent_branch_id
                self.branches.cancel(
                    branch_id,
                    reason=f"Invalidated by hard steering: {message}",
                    recursive=True,
                    preserve_evidence=True,
                )
                cancelled.append(branch_id)
                replacement = self.branches.spawn_child(
                    parent_branch_id=parent_id,
                    objective=old_objective,
                    objective_version_id=version.objective_id,
                    status=BranchStatus.READY,
                    completion_criteria=branch.completion_criteria,
                    result_contract=branch.result_contract,
                    semantic_merge=False,
                ).branch
                replacement.local_state["replaces_branch_id"] = branch_id
                replacement.local_state["hard_steer_message"] = message
                created.append(replacement.branch_id)
            preserved = tuple(
                item.branch_id
                for item in self.forest.branches.values()
                if item.branch_id not in set(cancelled + created)
                and item.status != BranchStatus.CANCELLED
            )
            return SteeringOutcome(
                intent=resolved,
                objective_version_id=version.objective_id,
                affected_branch_ids=impacted,
                created_branch_ids=tuple(created),
                cancelled_branch_ids=tuple(cancelled),
                preserved_branch_ids=preserved,
                message="Invalidated evidence was preserved and only affected branches were rebuilt.",
            )

        if resolved in {SteeringIntent.FORK_BRANCH, SteeringIntent.NEW_GOAL}:
            version = self.objectives.revise(
                session_id=session_id,
                added_requirements=(message,),
                message_id=message_id,
            )
            spawn = self.branches.spawn_child(
                parent_branch_id=self.forest.root_branch.branch_id,
                objective=message,
                objective_version_id=version.objective_id,
                status=BranchStatus.READY,
                semantic_merge=False,
            )
            return SteeringOutcome(
                intent=resolved,
                objective_version_id=version.objective_id,
                affected_branch_ids=(self.forest.root_branch.branch_id,),
                created_branch_ids=(spawn.branch.branch_id,),
                preserved_branch_ids=tuple(
                    item.branch_id
                    for item in self.forest.branches.values()
                    if item.branch_id != spawn.branch.branch_id
                ),
                message="Forked a sibling branch while preserving the original branch tree.",
            )

        if resolved == SteeringIntent.PAUSE_BRANCH:
            branch = self.branches.pause(target_id, recursive=True)
        elif resolved == SteeringIntent.RESUME_BRANCH:
            branch = self.branches.resume(target_id)
        elif resolved == SteeringIntent.CANCEL_BRANCH:
            branch = self.branches.cancel(target_id, reason=message, recursive=True)
        else:
            return SteeringOutcome(
                intent=resolved,
                objective_version_id=current.objective_id,
                affected_branch_ids=(),
                preserved_branch_ids=tuple(self.forest.branches),
                message="Message is conversational and does not mutate the Task Forest.",
            )
        return SteeringOutcome(
            intent=resolved,
            objective_version_id=current.objective_id,
            affected_branch_ids=(branch.branch_id,),
            message=f"Branch state changed to {branch.status.value}.",
        )
