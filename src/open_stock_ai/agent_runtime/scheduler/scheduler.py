from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .misfire_policy import validate_misfire_policy
from .market_schedule import align_market_due, market_calendar_name, next_market_cron
from .triggers import next_cron


class SchedulePlanner:
    """Validate durable schedule requests and calculate their next wake-up time.

    It deliberately has no provider dependency: every firing becomes a fresh
    Stock AI Run selected by the durable supervisor.
    """

    _TRIGGERS = frozenset({"one_shot", "interval", "cron", "event", "condition"})

    def __init__(self, market_calendar: Any | None = None) -> None:
        self.market_calendar = market_calendar

    def prepare(self, payload: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        trigger_type = str(payload.get("trigger_type") or "one_shot")
        if trigger_type not in self._TRIGGERS:
            raise ValueError(f"Unsupported schedule trigger_type: {trigger_type}")
        normalized = {
            "trigger_type": trigger_type,
            "misfire_policy": validate_misfire_policy(payload.get("misfire_policy")),
        }
        calendar_name = market_calendar_name(payload)
        if calendar_name:
            normalized["market_calendar"] = calendar_name
        if trigger_type in {"one_shot", "interval"}:
            if not payload.get("next_run_at"):
                raise ValueError(f"{trigger_type} schedules require next_run_at")
            normalized["next_run_at"] = _utc_time(str(payload["next_run_at"]))
            normalized["next_run_at"] = align_market_due(
                datetime.fromisoformat(normalized["next_run_at"]), payload, self.market_calendar
            ).isoformat()
        if trigger_type == "interval" and int(payload.get("interval_seconds") or 0) < 60:
            raise ValueError("interval schedules require interval_seconds >= 60")
        if trigger_type == "cron":
            expression = str(payload.get("cron_expression") or "").strip()
            if not expression:
                raise ValueError("cron schedules require cron_expression")
            normalized["next_run_at"] = next_market_cron(
                expression, current, next_cron, payload, self.market_calendar
            ).isoformat()
        if trigger_type == "event" and not str(payload.get("event_type") or "").strip():
            raise ValueError("event schedules require event_type")
        if trigger_type == "condition" and not isinstance(payload.get("condition"), dict):
            raise ValueError("condition schedules require a declarative condition object")
        return normalized

    def next_cron(self, expression: str, after: datetime) -> datetime:
        return next_cron(expression, after)

    def next_occurrence(self, payload: dict[str, Any], *, after: datetime) -> datetime | None:
        trigger_type = str(payload.get("trigger_type") or "one_shot")
        if trigger_type == "one_shot" and payload.get("market_calendar"):
            value = str(payload.get("next_run_at") or "").replace("Z", "+00:00")
            if value:
                candidate = max(datetime.fromisoformat(value), after.astimezone(timezone.utc))
                return align_market_due(candidate, payload, self.market_calendar)
            return None
        if trigger_type == "interval":
            interval = int(payload.get("interval_seconds") or 0)
            if interval < 60:
                return None
            candidate = after.astimezone(timezone.utc) + timedelta(seconds=interval)
            return align_market_due(candidate, payload, self.market_calendar)
        if trigger_type == "cron":
            return next_market_cron(
                str(payload.get("cron_expression") or ""),
                after.astimezone(timezone.utc),
                next_cron,
                payload,
                self.market_calendar,
            )
        return None


def _utc_time(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()
