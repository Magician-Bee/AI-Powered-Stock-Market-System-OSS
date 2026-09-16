from __future__ import annotations

from dataclasses import dataclass

from .trigger_engine import should_refresh


@dataclass(frozen=True)
class ScheduleDecision:
    refresh: bool
    reason: str
    mode: str = "event_driven_incremental"


def schedule_for_event(event_type: str) -> ScheduleDecision:
    refresh = should_refresh(event_type)
    return ScheduleDecision(
        refresh=refresh,
        reason=(
            f"{event_type} matches an intelligence refresh condition"
            if refresh
            else f"{event_type} does not require a full market rescan"
        ),
    )
