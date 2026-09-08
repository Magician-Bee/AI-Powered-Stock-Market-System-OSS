from __future__ import annotations

from datetime import datetime, timezone

from open_stock_ai.agent_runtime.automation import (
    AutomationIntent,
    AutomationKind,
    NotificationPolicy,
)
from open_stock_ai.agent_runtime.automation.notifications import DeliveryStatus
from open_stock_ai.agent_runtime.final_runtime import FinalAgentRuntime
from stock_ai.automation_notifications import build_automation_notification_senders


NOW = datetime(2026, 8, 13, 2, 46, tzinfo=timezone.utc)


def _intent(*, channels: tuple[str, ...]) -> AutomationIntent:
    return AutomationIntent(
        goal="重大證據改變時通知",
        user_id="local-user",
        session_id=None,
        symbol="2330",
        kind=AutomationKind.EVENT_WATCH,
        trigger={"type": "event", "event": "material_change"},
        observations=({"type": "market_event"},),
        analysis=({"type": "strategy_reanalysis"},),
        actions=({"type": "notify", "message": "結論已改變"},),
        notification_policy=NotificationPolicy(
            channels=channels,
            cooldown_seconds=0,
        ),
    )


def test_final_runtime_injects_external_notification_sender_and_persists_receipt(tmp_path):
    calls: list[dict] = []

    def backend(request: dict):
        calls.append(request)
        return {
            "sent_count": 1,
            "items": [{
                "channel": "line",
                "configured": True,
                "attempted": True,
                "sent": True,
                "mode": "ready",
                "target_hint": "1234",
                "status_code": 200,
                "detail": "Delivered.",
            }],
        }

    runtime = FinalAgentRuntime(
        tmp_path / "runtime.sqlite",
        notification_senders=build_automation_notification_senders(backend),
    )
    activated = runtime.automations.confirm_and_activate(
        _intent(channels=("line",)),
        user_confirmed=True,
        now=NOW,
    )
    automation_id = activated.automation["automation_id"]
    receipt = runtime.automations.notifications.deliver(
        automation_id=automation_id,
        user_id="local-user",
        decision={"conclusion": "risk changed"},
        payload={"title": "風險更新", "message": "結論已改變", "related_symbols": ["2330"]},
        policy=_intent(channels=("line",)).notification_policy,
        now=NOW,
    )

    assert receipt.status is DeliveryStatus.DELIVERED
    assert receipt.channel == "line"
    assert receipt.provider_receipt["configured"] is True
    assert calls == [{
        "title": "風險更新",
        "body": "結論已改變",
        "channels": ["line"],
        "related_symbols": ["2330"],
        "dry_run": False,
    }]
    persisted = runtime.automation_store.get_delivery(receipt.delivery_id)
    assert persisted["status"] == "delivered"
    assert persisted["provider_receipt"]["target_hint"] == "1234"


def test_unconfigured_external_channel_falls_back_to_durable_in_app(tmp_path):
    def backend(_: dict):
        return {
            "sent_count": 0,
            "items": [{
                "channel": "telegram",
                "configured": False,
                "attempted": False,
                "sent": False,
                "mode": "not_configured",
                "target_hint": None,
                "status_code": None,
                "detail": "Missing Telegram credentials.",
            }],
        }

    runtime = FinalAgentRuntime(
        tmp_path / "runtime.sqlite",
        notification_senders=build_automation_notification_senders(backend),
    )
    intent = _intent(channels=("telegram", "in_app"))
    activated = runtime.automations.confirm_and_activate(intent, user_confirmed=True, now=NOW)
    receipt = runtime.automations.notifications.deliver(
        automation_id=activated.automation["automation_id"],
        user_id="local-user",
        decision={"conclusion": "changed"},
        payload={"message": "fallback"},
        policy=intent.notification_policy,
        now=NOW,
    )

    assert receipt.status is DeliveryStatus.DELIVERED
    assert receipt.channel == "in_app"
    assert receipt.fallback_used is True
    assert receipt.provider_receipt["attempted_channels"] == ["telegram", "in_app"]
