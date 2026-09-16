from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class AutomationKind(StrEnum):
    NO_AUTOMATION = "no_automation"
    ONE_SHOT = "one_shot"
    RECURRING = "recurring"
    CONDITION_WATCH = "condition_watch"
    EVENT_WATCH = "event_watch"
    CROSS_SYSTEM = "cross_system_workflow"


_FORBIDDEN_TECHNICAL_KEYS = {
    "n8n",
    "nodes",
    "node_type",
    "webhook_url",
    "credential",
    "credentials",
    "http_request",
    "workflow_json",
}


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    channels: tuple[str, ...] = ("in_app",)
    cooldown_seconds: int = 3600
    meaningful_only: bool = True
    expires_after_seconds: int | None = None
    allow_fallback: bool = True

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | None) -> "NotificationPolicy":
        raw = dict(value or {})
        channels = tuple(str(item).strip() for item in raw.get("channels", ["in_app"]) if str(item).strip())
        cooldown = int(raw.get("cooldown_seconds", 3600))
        expiry = raw.get("expires_after_seconds")
        if cooldown < 0:
            raise ValueError("notification cooldown_seconds cannot be negative")
        if expiry is not None and int(expiry) <= 0:
            raise ValueError("notification expires_after_seconds must be positive")
        return cls(
            channels=channels or ("in_app",),
            cooldown_seconds=cooldown,
            meaningful_only=bool(raw.get("meaningful_only", True)),
            expires_after_seconds=int(expiry) if expiry is not None else None,
            allow_fallback=bool(raw.get("allow_fallback", True)),
        )


@dataclass(frozen=True, slots=True)
class CostPolicy:
    budget_class: str = "balanced"
    role_models: Mapping[str, str] = field(default_factory=dict)
    max_reanalysis_per_day: int = 24

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | None) -> "CostPolicy":
        raw = dict(value or {})
        budget = str(raw.get("budget_class") or "balanced")
        if budget not in {"economy", "balanced", "quality"}:
            raise ValueError("cost budget_class must be economy, balanced, or quality")
        maximum = int(raw.get("max_reanalysis_per_day", 24))
        if maximum < 1:
            raise ValueError("max_reanalysis_per_day must be positive")
        return cls(
            budget_class=budget,
            role_models={str(key): str(model) for key, model in dict(raw.get("role_models") or {}).items()},
            max_reanalysis_per_day=maximum,
        )


@dataclass(frozen=True, slots=True)
class AutomationIntent:
    goal: str
    user_id: str
    kind: AutomationKind
    trigger: Mapping[str, Any]
    observations: tuple[Mapping[str, Any], ...] = ()
    analysis: tuple[Mapping[str, Any], ...] = ()
    decision_logic: Mapping[str, Any] = field(default_factory=dict)
    actions: tuple[Mapping[str, Any], ...] = ()
    notification_policy: NotificationPolicy = field(default_factory=NotificationPolicy)
    lifecycle: Mapping[str, Any] = field(default_factory=dict)
    cost_policy: CostPolicy = field(default_factory=CostPolicy)
    symbol: str | None = None
    session_id: str | None = None
    schema_version: str = "open_stock_ai.automation_intent.v1"

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("automation goal is required")
        if not self.user_id.strip():
            raise ValueError("automation user_id is required")
        if self.kind == AutomationKind.NO_AUTOMATION:
            raise ValueError("no_automation cannot be compiled into an AutomationIntent")
        _reject_technical_details(self.to_semantic_dict())
        if not isinstance(self.trigger, Mapping) or not self.trigger:
            raise ValueError("automation trigger is required")
        if not self.actions:
            raise ValueError("automation requires at least one semantic action")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AutomationIntent":
        raw = dict(payload)
        kind = AutomationKind(str(raw.get("kind") or _infer_kind(raw.get("trigger"))))
        return cls(
            goal=str(raw.get("goal") or "").strip(),
            user_id=str(raw.get("user_id") or "").strip(),
            kind=kind,
            symbol=str(raw.get("symbol") or "").strip().upper() or None,
            session_id=str(raw.get("session_id") or "").strip() or None,
            trigger=dict(raw.get("trigger") or {}),
            observations=tuple(dict(item) for item in raw.get("observations") or ()),
            analysis=tuple(dict(item) for item in raw.get("analysis") or ()),
            decision_logic=dict(raw.get("decision_logic") or {}),
            actions=tuple(dict(item) for item in raw.get("actions") or ()),
            notification_policy=NotificationPolicy.from_value(raw.get("notification_policy")),
            lifecycle=dict(raw.get("lifecycle") or {}),
            cost_policy=CostPolicy.from_value(raw.get("cost_policy")),
        )

    def to_semantic_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload


def _infer_kind(trigger: Any) -> str:
    trigger_type = str(dict(trigger or {}).get("type") or "condition").casefold()
    return {
        "once": AutomationKind.ONE_SHOT.value,
        "one_shot": AutomationKind.ONE_SHOT.value,
        "recurring": AutomationKind.RECURRING.value,
        "schedule": AutomationKind.RECURRING.value,
        "condition": AutomationKind.CONDITION_WATCH.value,
        "price_crossing": AutomationKind.CONDITION_WATCH.value,
        "event": AutomationKind.EVENT_WATCH.value,
        "cross_system": AutomationKind.CROSS_SYSTEM.value,
    }.get(trigger_type, AutomationKind.CONDITION_WATCH.value)


def _reject_technical_details(value: Any, path: str = "intent") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN_TECHNICAL_KEYS:
                raise ValueError(f"technical execution detail is forbidden in AutomationIntent: {path}.{key}")
            _reject_technical_details(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_technical_details(child, f"{path}[{index}]")
