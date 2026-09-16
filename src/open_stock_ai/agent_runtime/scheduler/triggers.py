from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any


def next_cron(expression: str, after: datetime) -> datetime:
    """Return the next UTC occurrence for the supported five-field cron form."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron_expression must contain five fields: minute hour day month weekday")
    current = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(527_040):
        weekday = (current.weekday() + 1) % 7
        values = (current.minute, current.hour, current.day, current.month, weekday)
        limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
        if all(field_matches(field, value, limits[index]) for index, (field, value) in enumerate(zip(fields, values))):
            return current
        current += timedelta(minutes=1)
    raise ValueError("cron_expression has no occurrence within one year")


def field_matches(field: str, value: int, limits: tuple[int, int]) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        if step < 1:
            raise ValueError("cron step must be positive")
        return value % step == 0
    allowed = set()
    for part in field.split(","):
        if "-" in part:
            start, end = (int(item) for item in part.split("-", 1))
            allowed.update(range(start, end + 1))
        else:
            allowed.add(int(part))
    if any(item < limits[0] or item > limits[1] for item in allowed):
        raise ValueError(f"cron field is outside {limits[0]}..{limits[1]}")
    return value in allowed


def event_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
