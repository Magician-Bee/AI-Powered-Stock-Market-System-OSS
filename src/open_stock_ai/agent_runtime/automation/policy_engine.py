from __future__ import annotations

from dataclasses import dataclass

from .intent import AutomationIntent, AutomationKind


@dataclass(frozen=True, slots=True)
class AutomationPolicyDecision:
    allowed: bool
    auto_activatable: bool
    external_permission_required: bool
    trade_approval_required: bool
    risk_class: str
    reason: str


class AutomationPolicyEngine:
    """Keep monitoring, external delivery and trading approval boundaries separate."""

    def evaluate(self, intent: AutomationIntent, *, external_permission: bool = False) -> AutomationPolicyDecision:
        action_types = {str(action.get("type") or "").casefold() for action in intent.actions}
        if action_types & {"live_trade", "place_order", "broker_order", "execute_trade"}:
            return AutomationPolicyDecision(
                False,
                False,
                False,
                True,
                "financial_real_action",
                "real trading cannot be embedded in an autonomous Automation; use the separate risk and explicit trade approval flow",
            )
        external_channels = {
            channel.casefold()
            for channel in intent.notification_policy.channels
            if channel.casefold() not in {"in_app", "system"}
        }
        external_actions = bool(action_types & {"email", "line", "telegram", "webhook", "external_message"})
        permission_required = bool(external_channels or external_actions or intent.kind == AutomationKind.CROSS_SYSTEM)
        if permission_required and not external_permission:
            return AutomationPolicyDecision(
                True,
                False,
                True,
                False,
                "external_side_effect",
                "monitoring may be prepared, but external delivery requires user permission",
            )
        return AutomationPolicyDecision(
            True,
            True,
            False,
            False,
            "low_risk_monitoring" if not permission_required else "permitted_external_delivery",
            "low-risk monitoring and permitted notification may activate automatically",
        )
