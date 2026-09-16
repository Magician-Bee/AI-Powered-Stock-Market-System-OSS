from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceRecoveryPlan:
    failed_branch_id: str
    failed_source: str
    action: str
    alternatives: tuple[str, ...]
    unaffected_branches: tuple[str, ...]
    restart_whole_task: bool = False


class BranchSourceRecovery:
    """Choose source/tool fallback while preserving successful sibling branches."""

    def plan(
        self,
        *,
        failed_branch_id: str,
        failed_source: str,
        active_branches: tuple[str, ...],
        alternative_sources: tuple[str, ...] = (),
        browser_available: bool = True,
    ) -> SourceRecoveryPlan:
        unaffected = tuple(item for item in active_branches if item != failed_branch_id)
        if alternative_sources:
            action = "alternative_source"
            alternatives = alternative_sources
        elif browser_available:
            action = "browser_fallback"
            alternatives = ("browser.read",)
        else:
            action = "partial_completion"
            alternatives = ()
        return SourceRecoveryPlan(
            failed_branch_id=failed_branch_id,
            failed_source=failed_source,
            action=action,
            alternatives=alternatives,
            unaffected_branches=unaffected,
        )
