from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


BranchStatus = Literal["completed", "failed", "blocked", "cancelled"]


@dataclass(frozen=True, slots=True)
class BranchOutcome:
    branch_id: str
    status: BranchStatus
    summary: str = ""
    missing_evidence: tuple[str, ...] = ()
    confidence_impact: float = 0.0
    next_recovery: str | None = None


@dataclass(frozen=True, slots=True)
class PartialCompletionReport:
    status: Literal["completed", "partially_completed", "failed"]
    completed_branches: tuple[str, ...]
    failed_branches: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    confidence_impact: float
    next_recovery: tuple[str, ...]

    @classmethod
    def from_outcomes(cls, outcomes: list[BranchOutcome]) -> "PartialCompletionReport":
        completed = tuple(item.branch_id for item in outcomes if item.status == "completed")
        failed = tuple(item.branch_id for item in outcomes if item.status != "completed")
        status: Literal["completed", "partially_completed", "failed"]
        status = "completed" if not failed else "partially_completed" if completed else "failed"
        return cls(
            status=status,
            completed_branches=completed,
            failed_branches=failed,
            missing_evidence=tuple(dict.fromkeys(value for item in outcomes for value in item.missing_evidence)),
            confidence_impact=min(1.0, sum(max(0.0, item.confidence_impact) for item in outcomes)),
            next_recovery=tuple(item.next_recovery for item in outcomes if item.next_recovery),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": "open_stock_ai.partial_completion.v1", **asdict(self)}


@dataclass(frozen=True, slots=True)
class CompletionGateResult:
    complete: bool
    blockers: tuple[str, ...]


class CompletionGate:
    """Host-owned completion criteria; model completion claims are not inputs."""

    def evaluate(
        self,
        *,
        required_branches: set[str],
        outcomes: list[BranchOutcome],
        evidence_sufficient: bool,
        open_blocking_questions: int,
        pending_approvals: int,
        critical_tool_failures: int,
        final_result_valid: bool,
    ) -> CompletionGateResult:
        completed = {item.branch_id for item in outcomes if item.status == "completed"}
        blockers: list[str] = []
        if missing := sorted(required_branches - completed):
            blockers.append("required_branches:" + ",".join(missing))
        if not evidence_sufficient:
            blockers.append("insufficient_evidence")
        if open_blocking_questions:
            blockers.append("open_blocking_questions")
        if pending_approvals:
            blockers.append("pending_approvals")
        if critical_tool_failures:
            blockers.append("critical_tool_failures")
        if not final_result_valid:
            blockers.append("invalid_final_result")
        return CompletionGateResult(complete=not blockers, blockers=tuple(blockers))
