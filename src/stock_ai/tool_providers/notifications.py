from __future__ import annotations

import asyncio
from typing import Any

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec

from ..models import NotificationSendRequest
from ..mvp_features import get_notification_channels, get_notification_previews, send_notification


class NotificationToolProvider:
    """Expose existing Telegram/LINE notification services to the unified Agent."""

    provider_id = "notifications"

    def __init__(self) -> None:
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="notifications.channels",
                    description="Read configured notification channels without returning tokens or full recipient IDs.",
                    category="notifications",
                    packages=("Telegram", "LINE"),
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                ),
                AgentToolSpec(
                    name="notifications.previews",
                    description="Generate current dry-run notification previews for the market or one symbol.",
                    category="notifications",
                    packages=("OpenStockAI reports",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"symbol": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                    },
                ),
                AgentToolSpec(
                    name="notifications.send",
                    description=(
                        "Send an explicit Telegram/LINE notification through configured backend credentials. "
                        "Requires external_execute/full_execute and never returns credentials."
                    ),
                    category="notifications",
                    mutating=True,
                    requires_external_execution=True,
                    packages=("Telegram Bot API", "LINE Messaging API"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "body", "channels"],
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 500},
                            "body": {"type": "string", "minLength": 1, "maxLength": 3500},
                            "channels": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 2,
                                "uniqueItems": True,
                                "items": {"type": "string", "enum": ["telegram", "line"]},
                            },
                            "related_symbols": {
                                "type": "array",
                                "maxItems": 8,
                                "items": {"type": "string", "maxLength": 40},
                            },
                            "dry_run": {"type": "boolean"},
                        },
                    },
                ),
            )
        }

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def describe(self) -> dict[str, Any]:
        status = get_notification_channels()
        configured = sum(1 for item in status.get("items") or [] if item.get("configured"))
        return {
            "configured": configured > 0,
            "runtime_ready": True,
            "health": "ready" if configured else "preview_only",
            "configured_channel_count": configured,
            "credential_values_exposed": False,
            "live_send_requires_explicit_autonomy": True,
        }

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"Unknown notification capability: {name}")
        if spec.requires_external_execution and not context.allow_external_actions:
            raise PermissionError(f"{name} requires autonomy=external_execute or full_execute")
        if name == "notifications.channels":
            return await asyncio.to_thread(get_notification_channels)
        if name == "notifications.previews":
            symbol = str(arguments.get("symbol") or "").strip() or None
            return await asyncio.to_thread(get_notification_previews, symbol=symbol)
        if name == "notifications.send":
            request = NotificationSendRequest.model_validate(arguments)
            return await asyncio.to_thread(send_notification, request)
        raise RuntimeError(f"Notification capability is registered but not implemented: {name}")
