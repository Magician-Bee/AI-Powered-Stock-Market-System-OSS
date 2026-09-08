from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping
from uuid import uuid4

from .intent import NotificationPolicy
from .store import AutomationStore


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    ACKNOWLEDGED = "acknowledged"
    SNOOZED = "snoozed"
    UNSUBSCRIBED = "unsubscribed"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class NotificationReceipt:
    delivery_id: str
    automation_id: str
    channel: str
    status: DeliveryStatus
    dedup_key: str
    fallback_used: bool
    provider_receipt: Mapping[str, Any]
    reason: str | None = None


class NotificationManager:
    def __init__(
        self,
        store: AutomationStore,
        senders: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any] | bool]] | None = None,
    ) -> None:
        self.store = store
        self.senders = dict(senders or {})

    def deliver(
        self,
        *,
        automation_id: str,
        user_id: str,
        decision: Mapping[str, Any],
        payload: Mapping[str, Any],
        policy: NotificationPolicy,
        execution_id: str | None = None,
        now: datetime | None = None,
    ) -> NotificationReceipt:
        current = _utc(now)
        dedup_key = _delivery_fingerprint(automation_id, user_id, decision)
        channels = list(policy.channels)
        primary = channels[0]
        history = self.store.list_deliveries(automation_id)
        duplicate = next(
            (
                item
                for item in reversed(history)
                if item.get("dedup_key") == dedup_key
                and item.get("status")
                in {
                    DeliveryStatus.DELIVERED.value,
                    DeliveryStatus.ACKNOWLEDGED.value,
                    DeliveryStatus.SNOOZED.value,
                    DeliveryStatus.EXPIRED.value,
                }
            ),
            None,
        )
        if duplicate:
            return self._record(
                automation_id=automation_id,
                user_id=user_id,
                channel=primary,
                dedup_key=dedup_key,
                status=DeliveryStatus.SUPPRESSED,
                payload=payload,
                execution_id=execution_id,
                now=current,
                reason="duplicate decision notification",
            )
        last_delivered = next((item for item in reversed(history) if item.get("delivered_at")), None)
        if last_delivered and policy.cooldown_seconds:
            delivered_at = _parse(last_delivered["delivered_at"])
            if current < delivered_at + timedelta(seconds=policy.cooldown_seconds):
                return self._record(
                    automation_id=automation_id,
                    user_id=user_id,
                    channel=primary,
                    dedup_key=dedup_key,
                    status=DeliveryStatus.SUPPRESSED,
                    payload=payload,
                    execution_id=execution_id,
                    now=current,
                    reason="notification cooldown active",
                )
        attempted: list[str] = []
        blocked_channels: list[str] = []
        last_error = "no configured sender"
        for index, channel in enumerate(channels):
            if index and not policy.allow_fallback:
                break
            blocked = _blocked_by_history(
                [item for item in history if item["channel"] == channel],
                current,
            )
            if blocked:
                blocked_channels.append(f"{channel}: {blocked}")
                last_error = blocked
                continue
            attempted.append(channel)
            sender = self.senders.get(channel)
            if sender is None:
                last_error = f"no sender configured for {channel}"
                continue
            try:
                raw = sender(payload)
                provider_receipt = dict(raw) if isinstance(raw, Mapping) else {"accepted": bool(raw)}
                if not bool(provider_receipt.get("accepted", True)):
                    last_error = str(provider_receipt.get("error") or f"{channel} rejected delivery")
                    continue
                provider_receipt["attempted_channels"] = attempted
                return self._record(
                    automation_id=automation_id,
                    user_id=user_id,
                    channel=channel,
                    dedup_key=dedup_key,
                    status=DeliveryStatus.DELIVERED,
                    payload=payload,
                    execution_id=execution_id,
                    now=current,
                    provider_receipt=provider_receipt,
                    fallback_used=index > 0,
                    expiry_seconds=policy.expires_after_seconds,
                )
            except Exception as exc:  # provider isolation is part of channel fallback
                last_error = f"{type(exc).__name__}: {exc}"
        return self._record(
            automation_id=automation_id,
            user_id=user_id,
            channel=attempted[-1] if attempted else primary,
            dedup_key=dedup_key,
            status=(
                DeliveryStatus.SUPPRESSED
                if blocked_channels and not attempted
                else DeliveryStatus.FAILED
            ),
            payload=payload,
            execution_id=execution_id,
            now=current,
            reason="; ".join(blocked_channels) if blocked_channels and not attempted else last_error,
            provider_receipt={"attempted_channels": attempted, "blocked_channels": blocked_channels},
        )

    def acknowledge(self, delivery_id: str, *, now: datetime | None = None) -> NotificationReceipt:
        current = _utc(now)
        delivery = self.store.update_delivery(
            delivery_id,
            status=DeliveryStatus.ACKNOWLEDGED.value,
            acknowledged_at=current.isoformat(),
            updated_at=current.isoformat(),
        )
        return _receipt(delivery, reason="acknowledged")

    def snooze(self, delivery_id: str, *, until: datetime, now: datetime | None = None) -> NotificationReceipt:
        current = _utc(now)
        wake = _utc(until)
        if wake <= current:
            raise ValueError("snooze deadline must be in the future")
        delivery = self.store.update_delivery(
            delivery_id,
            status=DeliveryStatus.SNOOZED.value,
            snoozed_until=wake.isoformat(),
            updated_at=current.isoformat(),
        )
        return _receipt(delivery, reason="snoozed")

    def unsubscribe(
        self,
        *,
        automation_id: str,
        user_id: str,
        channel: str,
        now: datetime | None = None,
    ) -> NotificationReceipt:
        return self._record(
            automation_id=automation_id,
            user_id=user_id,
            channel=channel,
            dedup_key=f"control:unsubscribe:{channel}",
            status=DeliveryStatus.UNSUBSCRIBED,
            payload={"channel": channel},
            now=_utc(now),
            reason="user unsubscribed",
        )

    def expire(self, delivery_id: str, *, now: datetime | None = None) -> NotificationReceipt:
        current = _utc(now)
        existing = self.store.get_delivery(delivery_id)
        if not existing:
            raise KeyError(f"unknown delivery: {delivery_id}")
        expires_at = existing.get("expires_at")
        if expires_at and current < _parse(expires_at):
            raise ValueError("delivery has not expired")
        delivery = self.store.update_delivery(
            delivery_id,
            status=DeliveryStatus.EXPIRED.value,
            updated_at=current.isoformat(),
        )
        return _receipt(delivery, reason="expired")

    def _record(
        self,
        *,
        automation_id: str,
        user_id: str,
        channel: str,
        dedup_key: str,
        status: DeliveryStatus,
        payload: Mapping[str, Any],
        execution_id: str | None = None,
        now: datetime,
        reason: str | None = None,
        provider_receipt: Mapping[str, Any] | None = None,
        fallback_used: bool = False,
        expiry_seconds: int | None = None,
    ) -> NotificationReceipt:
        delivery_id = f"AND-{uuid4().hex}"
        timestamp = now.isoformat()
        receipt = dict(provider_receipt or {})
        receipt["fallback_used"] = fallback_used
        self.store.insert_delivery(
            {
                "delivery_id": delivery_id,
                "automation_id": automation_id,
                "execution_id": execution_id,
                "user_id": user_id,
                "channel": channel,
                "dedup_key": dedup_key,
                "status": status.value,
                "payload": dict(payload),
                "provider_receipt": receipt,
                "error": reason if status in {DeliveryStatus.FAILED, DeliveryStatus.SUPPRESSED} else None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "delivered_at": timestamp if status == DeliveryStatus.DELIVERED else None,
                "expires_at": (now + timedelta(seconds=expiry_seconds)).isoformat() if expiry_seconds else None,
            }
        )
        return NotificationReceipt(
            delivery_id,
            automation_id,
            channel,
            status,
            dedup_key,
            fallback_used,
            receipt,
            reason,
        )


def _blocked_by_history(deliveries: list[Mapping[str, Any]], now: datetime) -> str | None:
    if any(delivery.get("status") == DeliveryStatus.UNSUBSCRIBED.value for delivery in deliveries):
        return "channel is unsubscribed"
    for delivery in reversed(deliveries):
        snoozed_until = delivery.get("snoozed_until")
        if (
            delivery.get("status") == DeliveryStatus.SNOOZED.value
            and snoozed_until
            and now < _parse(str(snoozed_until))
        ):
            return "notification is snoozed"
    return None


def _delivery_fingerprint(automation_id: str, user_id: str, decision: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"automation_id": automation_id, "user_id": user_id, "decision": _decision_projection(decision)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decision_projection(decision: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("conclusion", "action", "rating", "risk_level", "recommendation")
    projection = {key: decision.get(key) for key in keys if key in decision}
    if projection:
        return projection
    volatile = {
        "confidence",
        "evidence_ids",
        "observed_at",
        "reanalyzed_run_id",
        "run_id",
        "timestamp",
    }
    return {
        str(key): value
        for key, value in sorted(decision.items(), key=lambda item: str(item[0]))
        if str(key) not in volatile
    }


def _receipt(delivery: Mapping[str, Any], *, reason: str | None = None) -> NotificationReceipt:
    provider = dict(delivery.get("provider_receipt") or {})
    return NotificationReceipt(
        str(delivery["delivery_id"]),
        str(delivery["automation_id"]),
        str(delivery["channel"]),
        DeliveryStatus(str(delivery["status"])),
        str(delivery["dedup_key"]),
        bool(provider.get("fallback_used")),
        provider,
        reason,
    )


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc(parsed)


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)
