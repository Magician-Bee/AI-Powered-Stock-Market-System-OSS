from __future__ import annotations

from typing import Any


def condition_matches(payload: dict[str, Any], condition: dict[str, Any]) -> bool:
    """Evaluate a declarative payload condition without executing provider code."""
    path = str(condition.get("path") or "").strip()
    if not path:
        return False
    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return False
        current = current[segment]
    if "equals" in condition:
        return current == condition["equals"]
    if "greater_than" in condition:
        return isinstance(current, (int, float)) and current > condition["greater_than"]
    if "less_than" in condition:
        return isinstance(current, (int, float)) and current < condition["less_than"]
    return False
