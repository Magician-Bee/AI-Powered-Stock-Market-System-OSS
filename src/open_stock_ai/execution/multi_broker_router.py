from __future__ import annotations

"""Deterministic, receipt-only routing across broker candidates.

The router chooses among host-supplied observations.  It never owns broker
credentials and never submits an order; a real adapter must still pass the
separate OMS and approval boundaries.
"""

import hashlib
import json
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping


SCHEMA_VERSION = "open_stock_ai.multi_broker_routing_receipt.v1"


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _number(value: Any, *, minimum: float = 0.0) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number >= minimum else None


def _timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def verify_multi_broker_routing_receipt(receipt: Mapping[str, Any]) -> bool:
    """Verify the content hash of a routing decision without live I/O."""

    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid multi broker routing schema")
    payload = dict(receipt)
    actual = str(payload.pop("receipt_sha256", ""))
    if len(actual) != 64 or actual != _sha256(payload):
        raise ValueError("multi broker routing receipt hash mismatch")
    return True


class MultiBrokerRouter:
    """Select a deterministic route from explicitly observed broker candidates."""

    def __init__(self, *, max_quote_age_seconds: float = 5.0) -> None:
        if _number(max_quote_age_seconds, minimum=0.0) is None:
            raise ValueError("max_quote_age_seconds_invalid")
        self.max_quote_age_seconds = float(max_quote_age_seconds)

    def route(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        as_of: str,
        candidates: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        required_broker_ids: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_side = str(side or "").strip().lower()
        normalized_as_of = _timestamp(as_of)
        if not normalized_symbol or normalized_side not in {"buy", "sell"}:
            raise ValueError("multi_broker_route_symbol_or_side_invalid")
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("multi_broker_route_quantity_invalid")
        if normalized_as_of is None:
            raise ValueError("multi_broker_route_as_of_invalid")
        if not isinstance(candidates, (list, tuple)):
            raise ValueError("multi_broker_route_candidates_invalid")

        observations: list[dict[str, Any]] = []
        eligible: list[dict[str, Any]] = []
        required = {str(item).strip() for item in required_broker_ids if str(item).strip()}
        for raw in candidates:
            if not isinstance(raw, Mapping):
                observations.append({"broker_id": None, "eligible": False, "blockers": ["candidate_invalid"]})
                continue
            broker_id = str(raw.get("broker_id") or "").strip()
            blockers: list[str] = []
            if not broker_id:
                blockers.append("broker_id_missing")
            if required and broker_id not in required:
                blockers.append("broker_not_in_required_allowlist")
            if raw.get("health_status") != "healthy":
                blockers.append("broker_health_not_healthy")
            if raw.get("route_permission") is not True:
                blockers.append("broker_route_permission_missing")
            quote_at = _timestamp(raw.get("quote_at"))
            quote_age = (
                (normalized_as_of - quote_at).total_seconds()
                if quote_at is not None
                else None
            )
            if quote_age is None or quote_age < 0 or quote_age > self.max_quote_age_seconds:
                blockers.append("broker_quote_stale_or_invalid")
            available = _number(raw.get("available_quantity"), minimum=0.0)
            if available is None or available < quantity:
                blockers.append("broker_available_quantity_insufficient")
            price_field = "ask_price" if normalized_side == "buy" else "bid_price"
            quote_price = _number(raw.get(price_field), minimum=0.0)
            if quote_price is None or quote_price <= 0:
                blockers.append(f"broker_{price_field}_invalid")
            fee_bps = _number(raw.get("fee_bps"), minimum=0.0)
            if fee_bps is None:
                blockers.append("broker_fee_bps_invalid")
            latency_ms = _number(raw.get("latency_ms"), minimum=0.0)
            if latency_ms is None:
                blockers.append("broker_latency_invalid")
            observation = {
                "broker_id": broker_id or None,
                "health_status": raw.get("health_status"),
                "route_permission": raw.get("route_permission") is True,
                "quote_at": quote_at.isoformat() if quote_at else None,
                "quote_age_seconds": round(quote_age, 6) if quote_age is not None else None,
                "available_quantity": available,
                "quote_price": quote_price,
                "fee_bps": fee_bps,
                "latency_ms": latency_ms,
                "eligible": not blockers,
                "blockers": sorted(set(blockers)),
            }
            observations.append(observation)
            if not blockers:
                notional = float(quantity) * float(quote_price)
                fee = notional * float(fee_bps) / 10_000.0
                objective = notional + fee if normalized_side == "buy" else -(notional - fee)
                eligible.append(
                    {
                        **observation,
                        "notional": round(notional, 6),
                        "estimated_fee": round(fee, 6),
                        "objective": round(objective, 6),
                    }
                )

        selected = None
        if eligible:
            selected = sorted(
                eligible,
                key=lambda item: (item["objective"], item["latency_ms"], str(item["broker_id"])),
            )[0]
        blockers = [] if selected else ["no_eligible_broker_route"]
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "symbol": normalized_symbol,
            "side": normalized_side,
            "quantity": quantity,
            "as_of": normalized_as_of.isoformat(),
            "required_broker_ids": sorted(required),
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "candidate_count": len(observations),
            "eligible_candidate_count": len(eligible),
            "selected_broker_id": selected["broker_id"] if selected else None,
            "selected_quote_price": selected["quote_price"] if selected else None,
            "selected_estimated_fee": selected["estimated_fee"] if selected else None,
            "candidates": observations,
            "blockers": blockers,
            "routing_status": "routed" if selected else "withheld",
            "execution_authority": "none",
            "execution_boundary": "routing_plan_only_no_order_submission",
        }
        receipt["receipt_sha256"] = _sha256(receipt)
        verify_multi_broker_routing_receipt(receipt)
        return receipt
