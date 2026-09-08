from __future__ import annotations

"""Host-signed restricted-live activation receipts.

An individual order approval answers "may this exact order be sent?".  This
separate receipt answers "is this account currently allowed to attempt a
restricted live order at all?"  Neither receipt is an Agent capability.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .contracts import BrokerId, BrokerOrderIntent


class BrokerRestrictedLiveActivationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_restricted_live_activation_receipt.v1"] = (
        "stock_ai.broker_restricted_live_activation_receipt.v1"
    )
    receipt_id: str = Field(default_factory=lambda: f"BLA-{uuid4().hex}")
    broker_id: BrokerId
    account_alias: str = Field(min_length=1)
    allowed_instruments: tuple[str, ...] = Field(min_length=1)
    max_order_notional: Decimal = Field(gt=0)
    max_daily_notional: Decimal = Field(gt=0)
    issued_at: datetime
    expires_at: datetime
    approval_surface: Literal["host_restricted_live_activation"] = (
        "host_restricted_live_activation"
    )
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class BrokerRestrictedLiveActivationAuthority:
    """Host-only signer for the narrow activation boundary before live OMS.

    A valid receipt cannot enable process configuration by itself.  It only
    authorizes a pre-configured host OMS to consider a tightly bounded order.
    The application's capability level remains the primary fail-closed gate.
    """

    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("restricted-live activation signing key must be at least 32 bytes")
        self._signing_key = bytes(signing_key)

    def issue(
        self,
        *,
        broker_id: BrokerId,
        account_alias: str,
        allowed_instruments: tuple[str, ...],
        max_order_notional: Decimal,
        max_daily_notional: Decimal,
        user_requested: bool,
        ttl_seconds: int = 300,
        issued_at: datetime | None = None,
    ) -> BrokerRestrictedLiveActivationReceipt:
        if user_requested is not True:
            raise PermissionError("restricted-live activation requires explicit user confirmation")
        if not 1 <= ttl_seconds <= 900:
            raise ValueError("restricted-live activation lifetime must be between 1 and 900 seconds")
        instruments = _restricted_instruments(allowed_instruments)
        if max_order_notional <= 0 or max_daily_notional <= 0:
            raise ValueError("restricted-live notional limits must be positive")
        if max_order_notional > max_daily_notional:
            raise ValueError("restricted-live order limit cannot exceed daily limit")
        now = _utc(issued_at or datetime.now(timezone.utc))
        payload = {
            "receipt_id": f"BLA-{uuid4().hex}",
            "broker_id": broker_id,
            "account_alias": account_alias,
            "allowed_instruments": instruments,
            "max_order_notional": str(max_order_notional),
            "max_daily_notional": str(max_daily_notional),
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "approval_surface": "host_restricted_live_activation",
        }
        return BrokerRestrictedLiveActivationReceipt(
            **payload, signature=self._signature(payload)
        )

    def verify(
        self,
        intent: BrokerOrderIntent,
        receipt: BrokerRestrictedLiveActivationReceipt,
        *,
        daily_notional_before_order: Decimal,
        verified_at: datetime | None = None,
    ) -> None:
        now = _utc(verified_at or datetime.now(timezone.utc))
        if intent.environment != "live":
            raise PermissionError("restricted-live activation cannot authorize non-live orders")
        if receipt.broker_id != intent.broker_id or receipt.account_alias != intent.account_alias:
            raise PermissionError("restricted-live activation is bound to another broker account")
        if intent.side != "buy":
            raise PermissionError("restricted-live activation permits long-only buy orders")
        if intent.instrument_id not in receipt.allowed_instruments:
            raise PermissionError("instrument is not in the restricted-live activation allowlist")
        if not intent.instrument_id.startswith("TWSE:"):
            raise PermissionError("restricted-live activation permits TWSE cash equities only")
        expected_price = intent.limit_price
        if expected_price is None:
            raise PermissionError("restricted-live activation requires a bounded limit price")
        notional = intent.quantity * expected_price
        if notional > receipt.max_order_notional:
            raise PermissionError("order exceeds restricted-live activation notional limit")
        if daily_notional_before_order < 0:
            raise PermissionError("restricted-live daily notional cannot be negative")
        if daily_notional_before_order + notional > receipt.max_daily_notional:
            raise PermissionError("order exceeds restricted-live activation daily notional limit")
        issued_at = _utc(receipt.issued_at)
        expires_at = _utc(receipt.expires_at)
        if expires_at <= issued_at or now > expires_at:
            raise PermissionError("restricted-live activation receipt has expired")
        if issued_at > now + timedelta(seconds=5):
            raise PermissionError("restricted-live activation issue time is invalid")
        if not hmac.compare_digest(receipt.signature, self._signature(_receipt_payload(receipt))):
            raise PermissionError("restricted-live activation receipt signature is invalid")

    def _signature(self, payload: dict) -> str:
        material = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hmac.new(self._signing_key, material, hashlib.sha256).hexdigest()


def _restricted_instruments(values: tuple[str, ...]) -> tuple[str, ...]:
    instruments = tuple(sorted({str(item).strip() for item in values if str(item).strip()}))
    if not instruments:
        raise ValueError("restricted-live activation requires an instrument allowlist")
    if any(not item.startswith("TWSE:") for item in instruments):
        raise ValueError("restricted-live activation allowlist permits TWSE cash equities only")
    return instruments


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _receipt_payload(receipt: BrokerRestrictedLiveActivationReceipt) -> dict:
    return {
        "receipt_id": receipt.receipt_id,
        "broker_id": receipt.broker_id,
        "account_alias": receipt.account_alias,
        "allowed_instruments": tuple(sorted(receipt.allowed_instruments)),
        "max_order_notional": str(receipt.max_order_notional),
        "max_daily_notional": str(receipt.max_daily_notional),
        "issued_at": _utc(receipt.issued_at).isoformat(),
        "expires_at": _utc(receipt.expires_at).isoformat(),
        "approval_surface": receipt.approval_surface,
    }
