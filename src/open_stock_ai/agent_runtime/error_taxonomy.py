from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentExecutionError:
    category: str
    recoverable: bool
    retryable: bool
    action_hints: tuple[str, ...]
    exception_type: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.execution_error.v1",
            "category": self.category,
            "recoverable": self.recoverable,
            "retryable": self.retryable,
            "action_hints": list(self.action_hints),
            "exception_type": self.exception_type,
            "message": self.message,
        }


def classify_error(exc: BaseException) -> AgentExecutionError:
    name = type(exc).__name__
    message = str(exc)
    lowered = message.casefold()
    if isinstance(exc, TimeoutError):
        return AgentExecutionError(
            "timeout", True, True, ("retry_same_step", "choose_alternate_tool"), name, message
        )
    if any(
        marker in lowered
        for marker in (
            "a last trade or exchange close is required",
            "a realtime last trade is required",
            "market price is unavailable",
            "no market price is available",
            "selected quote is not eligible for this execution horizon",
        )
    ):
        # A just-refreshed market connector can briefly expose a quote that is
        # valid for research but not yet valid for execution.  The tool's
        # declared two-attempt policy performs one bounded re-check; it is not
        # an invitation to keep re-planning the same order indefinitely.
        return AgentExecutionError(
            "market_quote_pending",
            True,
            True,
            ("retry_same_step", "choose_alternate_tool"),
            name,
            message,
        )
    if isinstance(exc, PermissionError):
        return AgentExecutionError(
            "policy_denied", True, False, ("request_approval", "revise_plan"), name, message
        )
    if isinstance(exc, (ValueError, TypeError)):
        return AgentExecutionError(
            "invalid_arguments", True, False, ("repair_arguments", "revise_plan"), name, message
        )
    if "stale" in lowered or "expired" in lowered or "changed" in lowered:
        return AgentExecutionError(
            "stale_state", True, False, ("refresh_snapshot", "revise_plan"), name, message
        )
    if "connection" in lowered or "transport" in lowered:
        return AgentExecutionError(
            "transport", True, True, ("restart_worker", "retry_same_step"), name, message
        )
    return AgentExecutionError(
        "execution_failure", True, False, ("insert_diagnostic_step", "revise_plan"), name, message
    )
