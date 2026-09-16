from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .error_taxonomy import AgentExecutionError


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: str
    retry_delay_seconds: float
    reason: str
    requires_model_replan: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.recovery_decision.v1",
            "action": self.action,
            "retry_delay_seconds": self.retry_delay_seconds,
            "reason": self.reason,
            "requires_model_replan": self.requires_model_replan,
        }


class RecoveryEngine:
    def decide(
        self,
        error: AgentExecutionError,
        *,
        attempt: int,
        max_attempts: int,
        mutation_started: bool,
        rollback_available: bool,
    ) -> RecoveryDecision:
        if mutation_started and rollback_available:
            return RecoveryDecision(
                "rollback_mutation",
                0,
                "A mutation failed after it started and has a rollback token.",
                True,
            )
        if error.retryable and attempt < max_attempts:
            return RecoveryDecision(
                "retry_same_step",
                min(2 ** max(0, attempt - 1), 8),
                f"{error.category} is retryable within the tool retry policy.",
                False,
            )
        if error.category == "stale_state":
            return RecoveryDecision("refresh_snapshot", 0, error.message, True)
        if error.category == "invalid_arguments":
            return RecoveryDecision("repair_arguments", 0, error.message, True)
        if error.category == "policy_denied":
            return RecoveryDecision("revise_plan", 0, error.message, True)
        return RecoveryDecision("revise_plan", 0, error.message, True)
