from __future__ import annotations

"""Host-signed, order-bound human approval receipts for live OMS use."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrokerOrderIntent


class BrokerHumanApprovalReceipt(BaseModel):
    """A short-lived approval that cannot be repurposed for another order."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_human_approval_receipt.v1"] = (
        "stock_ai.broker_human_approval_receipt.v1"
    )
    receipt_id: str = Field(default_factory=lambda: f"BHA-{uuid4().hex}")
    intent_id: str
    order_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: datetime
    expires_at: datetime
    approval_surface: Literal["host_user_confirmation"] = "host_user_confirmation"
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class BrokerHumanApprovalAuthority:
    """Host-only receipt signer; it is intentionally not an Agent tool.

    The authority's key is supplied by the application host (normally from a
    secure store).  OMS only accepts a receipt that validates with the same
    authority and exact live-order fingerprint.
    """

    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("broker approval signing key must be at least 32 bytes")
        self._signing_key = bytes(signing_key)

    def issue(
        self,
        intent: BrokerOrderIntent,
        *,
        user_requested: bool,
        ttl_seconds: int = 120,
        issued_at: datetime | None = None,
    ) -> BrokerHumanApprovalReceipt:
        if intent.environment != "live":
            raise ValueError("human broker approval receipts are for live orders only")
        if user_requested is not True:
            raise PermissionError("live order approval requires an explicit user confirmation")
        if not 1 <= ttl_seconds <= 300:
            raise ValueError("live order approval lifetime must be between 1 and 300 seconds")
        now = _utc(issued_at or datetime.now(timezone.utc))
        payload = {
            "receipt_id": f"BHA-{uuid4().hex}",
            "intent_id": intent.intent_id,
            "order_fingerprint": order_fingerprint(intent),
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "approval_surface": "host_user_confirmation",
        }
        return BrokerHumanApprovalReceipt(
            **payload,
            signature=self._signature(payload),
        )

    def verify(
        self,
        intent: BrokerOrderIntent,
        receipt: BrokerHumanApprovalReceipt,
        *,
        verified_at: datetime | None = None,
    ) -> None:
        now = _utc(verified_at or datetime.now(timezone.utc))
        if intent.environment != "live":
            raise PermissionError("human approval receipts cannot authorize non-live orders")
        if receipt.intent_id != intent.intent_id:
            raise PermissionError("human approval receipt is bound to another order intent")
        if receipt.order_fingerprint != order_fingerprint(intent):
            raise PermissionError("human approval receipt does not match the submitted order")
        issued_at = _utc(receipt.issued_at)
        expires_at = _utc(receipt.expires_at)
        if expires_at <= issued_at or now > expires_at:
            raise PermissionError("human approval receipt has expired")
        if issued_at > now + timedelta(seconds=5):
            raise PermissionError("human approval receipt issue time is invalid")
        expected = self._signature(_receipt_payload(receipt))
        if not hmac.compare_digest(receipt.signature, expected):
            raise PermissionError("human approval receipt signature is invalid")

    def _signature(self, payload: dict) -> str:
        material = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._signing_key, material, hashlib.sha256).hexdigest()


def order_fingerprint(intent: BrokerOrderIntent) -> str:
    """Return the canonical immutable order fields that a human approved."""

    payload = {
        "intent_id": intent.intent_id,
        "broker_id": intent.broker_id,
        "account_alias": intent.account_alias,
        "instrument_id": intent.instrument_id,
        "side": intent.side,
        "quantity": str(intent.quantity),
        "price_type": intent.price_type,
        "limit_price": str(intent.limit_price) if intent.limit_price is not None else None,
        "time_in_force": intent.time_in_force,
        "session": intent.session,
        "order_purpose": intent.order_purpose,
        "environment": intent.environment,
        "risk_approval_id": intent.risk_approval_id,
        "idempotency_key": intent.idempotency_key,
    }
    material = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _receipt_payload(receipt: BrokerHumanApprovalReceipt) -> dict:
    """Use one timestamp representation for issue and verification signatures."""

    return {
        "receipt_id": receipt.receipt_id,
        "intent_id": receipt.intent_id,
        "order_fingerprint": receipt.order_fingerprint,
        "issued_at": _utc(receipt.issued_at).isoformat(),
        "expires_at": _utc(receipt.expires_at).isoformat(),
        "approval_surface": receipt.approval_surface,
    }
