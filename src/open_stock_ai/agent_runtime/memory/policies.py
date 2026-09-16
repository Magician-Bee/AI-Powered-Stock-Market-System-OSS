from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MemoryLayer(str, Enum):
    """The six durable cognition layers defined by the final runtime contract."""

    WORKING = "working"
    SESSION = "session"
    USER_PREFERENCE = "user_preference"
    PROJECT_STATE = "project_state"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


LEGACY_KIND_ALIASES = {
    "conversation": MemoryLayer.SESSION,
    "project": MemoryLayer.PROJECT_STATE,
    "domain": MemoryLayer.PROJECT_STATE,
    "artifact": MemoryLayer.PROJECT_STATE,
    "reflection": MemoryLayer.PROCEDURAL,
}
MEMORY_KINDS = {
    *(layer.value for layer in MemoryLayer),
    *LEGACY_KIND_ALIASES,
}
FACT_TYPES = {"fact", "preference", "inference", "temporary_state"}


def canonical_memory_layer(kind: str | MemoryLayer) -> MemoryLayer:
    if isinstance(kind, MemoryLayer):
        return kind
    try:
        return MemoryLayer(kind)
    except ValueError:
        try:
            return LEGACY_KIND_ALIASES[kind]
        except KeyError as exc:
            raise ValueError(f"Unsupported memory kind: {kind}") from exc


def preference_semantics(source: dict[str, Any]) -> dict[str, Any]:
    """Return explicit advisory semantics for a preference memory.

    Preferences inform a later decision; they never become an executable rule.
    The copy avoids mutating caller-owned evidence dictionaries.
    """

    return {**source, "advisory": True, "enforcement": "contextual_only"}


def validate_memory(
    *,
    kind: str,
    fact_type: str,
    source: dict[str, Any],
    expires_at: str | None,
) -> None:
    layer = canonical_memory_layer(kind)
    if fact_type not in FACT_TYPES:
        raise ValueError(f"Unsupported memory fact type: {fact_type}")
    if not source or not source.get("type"):
        raise ValueError("Memory requires a traceable source")
    if layer is MemoryLayer.USER_PREFERENCE and source.get("enforce_as_rule"):
        raise ValueError("User preferences are advisory and cannot become hard rules")
    if layer is MemoryLayer.PROJECT_STATE and source.get("contains_live_market_state") and not expires_at:
        raise ValueError("Live market state requires an expiry and cannot become permanent domain memory")


def is_current(memory: dict[str, Any]) -> bool:
    expires_at = memory.get("expires_at")
    if not expires_at:
        return True
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)
