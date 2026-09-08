from __future__ import annotations

"""Adapters from the Final Automation Runtime to configured app channels.

The Automation domain owns deduplication, cooldown, fallback and delivery
receipts.  This module only adapts a confirmed notification action to the
existing LINE/Telegram transport.  Credentials remain in ``Settings`` and are
never copied into AutomationIntent or provider-facing payloads.
"""

from collections.abc import Callable, Mapping
from typing import Any

from .mvp_features import send_notification


DeliveryBackend = Callable[[dict[str, Any]], Mapping[str, Any]]
AutomationSender = Callable[[Mapping[str, Any]], Mapping[str, Any] | bool]


def build_automation_notification_senders(
    delivery_backend: DeliveryBackend | None = None,
) -> dict[str, AutomationSender]:
    """Return external senders for the canonical NotificationManager.

    The sender is registered even when a channel is not configured.  In that
    case the existing transport returns ``not_configured`` and the manager can
    durably record the failure or continue to the next policy channel.
    """

    backend = delivery_backend or send_notification
    return {
        channel: _channel_sender(channel, backend)
        for channel in ("telegram", "line")
    }


def _channel_sender(channel: str, backend: DeliveryBackend) -> AutomationSender:
    def deliver(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        message = str(payload.get("message") or payload.get("body") or "").strip()
        title = str(payload.get("title") or "Stock AI Agent 自動化通知").strip()
        symbols = [
            str(item).strip().upper()
            for item in payload.get("related_symbols") or ()
            if str(item).strip()
        ]
        result = dict(
            backend(
                {
                    "title": title,
                    "body": message or "Agent 偵測到具有意義的決策變化。",
                    "channels": [channel],
                    "related_symbols": symbols,
                    "dry_run": False,
                }
            )
        )
        items = [dict(item) for item in result.get("items") or () if isinstance(item, Mapping)]
        item = items[0] if items else {}
        accepted = int(result.get("sent_count") or 0) > 0 and item.get("sent") is True
        return {
            "accepted": accepted,
            "channel": channel,
            "mode": str(item.get("mode") or "error"),
            "attempted": item.get("attempted") is True,
            "configured": item.get("configured") is True,
            "status_code": item.get("status_code"),
            "target_hint": item.get("target_hint"),
            "detail": str(item.get("detail") or "Notification transport returned no receipt."),
            "error": None if accepted else str(item.get("detail") or f"{channel} delivery failed"),
        }

    return deliver
