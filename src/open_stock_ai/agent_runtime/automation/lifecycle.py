from __future__ import annotations

from enum import StrEnum


class AutomationState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    TESTING = "testing"
    ACTIVE = "active"
    PAUSED = "paused"
    TRIGGERED = "triggered"
    EXPIRED = "expired"
    FAILED = "failed"
    ARCHIVED = "archived"
    DELETED = "deleted"


class Lifecycle:
    _ALLOWED = {
        AutomationState.DRAFT: {AutomationState.VALIDATED, AutomationState.FAILED, AutomationState.DELETED},
        AutomationState.VALIDATED: {AutomationState.TESTING, AutomationState.DRAFT, AutomationState.FAILED},
        AutomationState.TESTING: {
            AutomationState.ACTIVE,
            AutomationState.PAUSED,
            AutomationState.FAILED,
            AutomationState.DRAFT,
            AutomationState.ARCHIVED,
        },
        AutomationState.ACTIVE: {
            AutomationState.PAUSED,
            AutomationState.TRIGGERED,
            AutomationState.EXPIRED,
            AutomationState.FAILED,
            AutomationState.ARCHIVED,
        },
        AutomationState.TRIGGERED: {
            AutomationState.ACTIVE,
            AutomationState.PAUSED,
            AutomationState.EXPIRED,
            AutomationState.FAILED,
        },
        AutomationState.PAUSED: {AutomationState.ACTIVE, AutomationState.ARCHIVED, AutomationState.EXPIRED},
        AutomationState.FAILED: {AutomationState.DRAFT, AutomationState.ARCHIVED, AutomationState.DELETED},
        AutomationState.EXPIRED: {AutomationState.ARCHIVED, AutomationState.DELETED},
        AutomationState.ARCHIVED: {AutomationState.DELETED},
        AutomationState.DELETED: set(),
    }

    def can_transition(self, current: AutomationState | str, target: AutomationState | str) -> bool:
        return AutomationState(target) in self._ALLOWED[AutomationState(current)]

    def require(self, current: AutomationState | str, target: AutomationState | str) -> AutomationState:
        source = AutomationState(current)
        destination = AutomationState(target)
        if destination not in self._ALLOWED[source]:
            raise ValueError(f"invalid automation lifecycle transition: {source.value} -> {destination.value}")
        return destination
