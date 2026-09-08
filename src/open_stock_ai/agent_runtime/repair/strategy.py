from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any

from .contracts import ErrorReceipt, FailureFingerprint, canonical_hash


class RecoveryLevel(IntEnum):
    HOST_REPAIR = 0
    MODEL_LOCAL_PATCH = 1
    TOOL_ARGUMENT_REWRITE = 2
    RETRY_TEMPORARY_FAILURE = 3
    ALTERNATIVE_TOOL = 4
    ALTERNATIVE_SOURCE = 5
    REPLAN_BRANCH = 6
    PROVIDER_FALLBACK = 7
    ASK_USER = 8
    PARTIAL_COMPLETION = 9

    @property
    def strategy(self) -> str:
        return (
            "host_repair",
            "model_local_patch",
            "tool_argument_rewrite",
            "retry_temporary_failure",
            "alternative_tool",
            "alternative_source",
            "replan_branch",
            "provider_fallback",
            "ask_user",
            "partial_completion",
        )[int(self)]


class ChaosFault(StrEnum):
    INVALID_JSON = "invalid_json"
    WRONG_ARGUMENTS = "wrong_arguments"
    TOOL_TIMEOUT = "tool_timeout"
    API_500 = "api_500"
    API_RATE_LIMIT = "api_rate_limit"
    NETWORK_DROP = "network_drop"
    WEB_SOURCE_MISSING = "web_source_missing"
    BROWSER_CRASH = "browser_crash"
    PROVIDER_CRASH = "provider_crash"
    HOST_RESTART = "host_restart"
    DB_RESTART = "db_restart"
    DUPLICATE_EVENT = "duplicate_event"
    USER_INTERRUPT = "user_interrupt"
    CONFLICTING_USER_MESSAGE = "conflicting_user_message"


_FAULT_START_LEVEL: dict[ChaosFault, RecoveryLevel] = {
    ChaosFault.INVALID_JSON: RecoveryLevel.HOST_REPAIR,
    ChaosFault.WRONG_ARGUMENTS: RecoveryLevel.HOST_REPAIR,
    ChaosFault.TOOL_TIMEOUT: RecoveryLevel.RETRY_TEMPORARY_FAILURE,
    ChaosFault.API_500: RecoveryLevel.RETRY_TEMPORARY_FAILURE,
    ChaosFault.API_RATE_LIMIT: RecoveryLevel.RETRY_TEMPORARY_FAILURE,
    ChaosFault.NETWORK_DROP: RecoveryLevel.RETRY_TEMPORARY_FAILURE,
    ChaosFault.WEB_SOURCE_MISSING: RecoveryLevel.ALTERNATIVE_SOURCE,
    ChaosFault.BROWSER_CRASH: RecoveryLevel.ALTERNATIVE_TOOL,
    ChaosFault.PROVIDER_CRASH: RecoveryLevel.PROVIDER_FALLBACK,
    ChaosFault.HOST_RESTART: RecoveryLevel.REPLAN_BRANCH,
    ChaosFault.DB_RESTART: RecoveryLevel.REPLAN_BRANCH,
    ChaosFault.DUPLICATE_EVENT: RecoveryLevel.HOST_REPAIR,
    ChaosFault.USER_INTERRUPT: RecoveryLevel.ASK_USER,
    ChaosFault.CONFLICTING_USER_MESSAGE: RecoveryLevel.ASK_USER,
}


def error_receipt_for_fault(
    fault: ChaosFault,
    *,
    component: str = "agent_runtime",
    branch_id: str | None = None,
) -> ErrorReceipt:
    expected_actual = {
        ChaosFault.INVALID_JSON: ("valid JSON", "invalid JSON"),
        ChaosFault.WRONG_ARGUMENTS: ("schema-valid arguments", "wrong arguments"),
        ChaosFault.TOOL_TIMEOUT: ("result within timeout", "timeout"),
        ChaosFault.API_500: ("2xx response", "HTTP 500"),
        ChaosFault.API_RATE_LIMIT: ("request accepted", "HTTP 429"),
        ChaosFault.NETWORK_DROP: ("connected transport", "connection dropped"),
        ChaosFault.WEB_SOURCE_MISSING: ("available source", "HTTP 404"),
        ChaosFault.BROWSER_CRASH: ("healthy browser", "browser process exited"),
        ChaosFault.PROVIDER_CRASH: ("healthy provider", "provider process exited"),
        ChaosFault.HOST_RESTART: ("continuous host", "host restarted"),
        ChaosFault.DB_RESTART: ("available database", "database restarted"),
        ChaosFault.DUPLICATE_EVENT: ("unique event", "duplicate event"),
        ChaosFault.USER_INTERRUPT: ("stable objective", "user interrupt"),
        ChaosFault.CONFLICTING_USER_MESSAGE: ("consistent requirement", "conflicting message"),
    }[fault]
    retryable = fault not in {
        ChaosFault.USER_INTERRUPT,
        ChaosFault.CONFLICTING_USER_MESSAGE,
    }
    return ErrorReceipt(
        category=fault.value,
        component=component,
        location="$",
        expected=expected_actual[0],
        actual=expected_actual[1],
        retryable=retryable,
        branch_id=branch_id,
    )


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    level: RecoveryLevel
    strategy: str
    branch_id: str | None
    identical_retry_blocked: bool = False
    preserve_other_branches: bool = True


@dataclass(frozen=True, slots=True)
class _AttemptSignature:
    output_hash: str
    arguments_hash: str
    fingerprint: str
    strategy: str


class IdenticalRetryGuard:
    """Reject an attempt only when all four P39 dimensions are unchanged."""

    def __init__(self) -> None:
        self._last_by_branch: dict[str, _AttemptSignature] = {}
        self.identical_retry_count = 0

    def register(
        self,
        *,
        branch_id: str,
        output: Any,
        arguments: Any,
        fingerprint: FailureFingerprint,
        strategy: str,
    ) -> bool:
        signature = _AttemptSignature(
            output_hash=canonical_hash(output),
            arguments_hash=canonical_hash(arguments),
            fingerprint=fingerprint.digest,
            strategy=strategy,
        )
        identical = self._last_by_branch.get(branch_id) == signature
        self._last_by_branch[branch_id] = signature
        if identical:
            self.identical_retry_count += 1
        return not identical


class RecoveryStrategyLadder:
    def __init__(self, guard: IdenticalRetryGuard | None = None) -> None:
        self.guard = guard or IdenticalRetryGuard()

    def initial_level(self, receipt: ErrorReceipt) -> RecoveryLevel:
        try:
            return _FAULT_START_LEVEL[ChaosFault(receipt.category)]
        except ValueError:
            if receipt.category in {"invalid_arguments", "schema_validation"}:
                return RecoveryLevel.HOST_REPAIR
            if receipt.category in {"timeout", "transport", "rate_limit", "api_500"}:
                return RecoveryLevel.RETRY_TEMPORARY_FAILURE
            if receipt.category in {"source_missing", "source_stale"}:
                return RecoveryLevel.ALTERNATIVE_SOURCE
            if receipt.category in {"provider_failure", "provider_unavailable"}:
                return RecoveryLevel.PROVIDER_FALLBACK
            return RecoveryLevel.REPLAN_BRANCH

    def decide(
        self,
        receipt: ErrorReceipt,
        *,
        fingerprint: FailureFingerprint,
        output: Any,
        arguments: Any,
        previous_level: RecoveryLevel | None = None,
    ) -> RecoveryDecision:
        level = self.initial_level(receipt) if previous_level is None else previous_level
        allowed = self.guard.register(
            branch_id=receipt.branch_id or "run",
            output=output,
            arguments=arguments,
            fingerprint=fingerprint,
            strategy=level.strategy,
        )
        if not allowed:
            level = RecoveryLevel(min(int(level) + 1, int(RecoveryLevel.PARTIAL_COMPLETION)))
        return RecoveryDecision(
            level=level,
            strategy=level.strategy,
            branch_id=receipt.branch_id,
            identical_retry_blocked=not allowed,
        )

    @staticmethod
    def next(decision: RecoveryDecision) -> RecoveryDecision:
        level = RecoveryLevel(min(int(decision.level) + 1, int(RecoveryLevel.PARTIAL_COMPLETION)))
        return RecoveryDecision(
            level=level,
            strategy=level.strategy,
            branch_id=decision.branch_id,
            preserve_other_branches=True,
        )
