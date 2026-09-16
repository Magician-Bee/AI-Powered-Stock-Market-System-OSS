from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


MODEL_VERSION = "open_stock_ai.paper_execution_model.v1"
HISTORICAL_ORDER_BOOK_SCHEMA = "open_stock_ai.historical_order_book_replay.v1"


def build_historical_order_book_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build a content-addressed PIT order-book replay receipt.

    The receipt is deliberately separate from a live quote.  A caller may
    use it to replay a historical queue only after the source has attested
    the snapshot and its availability time.
    """

    body = {"schema_version": HISTORICAL_ORDER_BOOK_SCHEMA, **dict(payload)}
    body["receipt_sha256"] = _hash_json(body)
    return body


def verify_historical_order_book_receipt(receipt: Mapping[str, Any]) -> bool:
    """Validate the immutable fields required for historical queue replay."""

    required = {
        "schema_version",
        "snapshot_id",
        "symbol",
        "venue",
        "captured_at",
        "available_at",
        "source",
        "source_revision",
        "provider_verified",
        "levels",
        "available_quantity",
        "matched_quantity",
        "queue_ahead_quantity",
        "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HISTORICAL_ORDER_BOOK_SCHEMA:
        raise ValueError("historical_order_book_receipt_schema_invalid")
    body = dict(receipt)
    actual = str(body.pop("receipt_sha256") or "")
    if len(actual) != 64 or actual != _hash_json(body):
        raise ValueError("historical_order_book_receipt_hash_mismatch")
    for field in ("snapshot_id", "symbol", "venue", "source", "source_revision"):
        if not str(body.get(field) or "").strip():
            raise ValueError(f"historical_order_book_{field}_required")
    for field in ("captured_at", "available_at"):
        raw = str(body.get(field) or "")
        try:
            timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"historical_order_book_{field}_invalid") from exc
        if timestamp.tzinfo is None:
            raise ValueError(f"historical_order_book_{field}_timezone_required")
    if body.get("provider_verified") not in {True, False}:
        raise ValueError("historical_order_book_provider_verified_invalid")
    levels = body.get("levels")
    if not isinstance(levels, list) or not levels:
        raise ValueError("historical_order_book_levels_required")
    level_total = 0.0
    for index, level in enumerate(levels):
        if not isinstance(level, Mapping):
            raise ValueError(f"historical_order_book_level_invalid:{index}")
        price = _finite_non_negative(level.get("price"), f"historical_order_book_level_price:{index}")
        size = _finite_non_negative(level.get("size"), f"historical_order_book_level_size:{index}")
        if price <= 0 or size <= 0:
            raise ValueError(f"historical_order_book_level_nonpositive:{index}")
        level_total += size
    available = _finite_non_negative(body.get("available_quantity"), "historical_order_book_available_quantity")
    if abs(level_total - available) > 1e-9:
        raise ValueError("historical_order_book_available_quantity_mismatch")
    _finite_non_negative(body.get("matched_quantity"), "historical_order_book_matched_quantity")
    _finite_non_negative(body.get("queue_ahead_quantity"), "historical_order_book_queue_ahead_quantity")
    return True


class HistoricalOrderBookReceiptStore:
    """Immutable SQLite storage for historical order-book replay receipts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists historical_order_book_receipts (
                    receipt_sha256 text primary key,
                    snapshot_id text not null unique,
                    symbol text not null,
                    venue text not null,
                    available_at text not null,
                    payload_json text not null
                );
                create trigger if not exists historical_order_book_receipts_immutable_update
                before update on historical_order_book_receipts begin
                    select raise(abort, 'historical order-book receipts are immutable');
                end;
                create trigger if not exists historical_order_book_receipts_immutable_delete
                before delete on historical_order_book_receipts begin
                    select raise(abort, 'historical order-book receipts are immutable');
                end;
                """
            )

    def save(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one verified snapshot exactly once."""

        normalized = dict(receipt)
        verify_historical_order_book_receipt(normalized)
        receipt_sha = str(normalized["receipt_sha256"]).lower()
        normalized["receipt_sha256"] = receipt_sha
        payload = _json(normalized)
        with self._connect() as connection:
            existing = connection.execute(
                "select payload_json from historical_order_book_receipts where receipt_sha256=?",
                (receipt_sha,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != payload:
                    raise ValueError("historical_order_book_receipt_is_bound_to_different_evidence")
                return normalized
            connection.execute(
                """insert into historical_order_book_receipts(
                    receipt_sha256, snapshot_id, symbol, venue, available_at, payload_json
                ) values (?, ?, ?, ?, ?, ?)""",
                (
                    receipt_sha,
                    normalized["snapshot_id"],
                    str(normalized["symbol"]).upper(),
                    str(normalized["venue"]).upper(),
                    normalized["available_at"],
                    payload,
                ),
            )
        return normalized

    put = save

    def by_receipt(self, receipt_sha256: str) -> dict[str, Any] | None:
        key = str(receipt_sha256 or "").lower().strip()
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "select payload_json from historical_order_book_receipts where receipt_sha256=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            receipt = json.loads(str(row[0]))
        except json.JSONDecodeError as exc:
            raise ValueError("historical_order_book_receipt_payload_invalid_json") from exc
        verify_historical_order_book_receipt(receipt)
        if str(receipt.get("receipt_sha256") or "").lower() != key:
            raise ValueError("historical_order_book_receipt_key_mismatch")
        return receipt

    def receipts(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        normalized_symbol = str(symbol or "").strip().upper()
        with self._connect() as connection:
            if normalized_symbol:
                rows = connection.execute(
                    "select payload_json from historical_order_book_receipts where symbol=? order by available_at, snapshot_id",
                    (normalized_symbol,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "select payload_json from historical_order_book_receipts order by available_at, snapshot_id"
                ).fetchall()
        results = []
        for row in rows:
            receipt = json.loads(str(row[0]))
            verify_historical_order_book_receipt(receipt)
            results.append(receipt)
        return results

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection


def resolve_execution(
    *,
    order_id: str,
    fill_sequence: int,
    remaining_quantity: float,
    market: dict[str, Any],
    prior_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one deterministic paper-execution opportunity.

    The simulator never invents a queue, probability, or participation limit.
    If a caller supplies those inputs, this function records and applies them;
    otherwise the legacy explicit ``available_quantity`` contract remains the
    only liquidity input.  ``market_volume`` + ``participation_rate`` and the
    optional direct ``volume_cap`` are scenario inputs, not inferred exchange
    depth.
    The returned receipt is JSON-safe and is intended to be embedded in the
    durable broker event payload.
    """

    remaining = _finite_non_negative(remaining_quantity, "remaining_quantity")
    state = dict(prior_state or {})
    blockers: list[str] = []

    receipt_reference = str(market.get("historical_order_book_receipt_id") or "").lower().strip()
    inline_receipt = market.get("historical_order_book_receipt")
    if receipt_reference:
        if not isinstance(inline_receipt, Mapping):
            blockers.append("historical_order_book_receipt_lookup_failed")
        elif str(inline_receipt.get("receipt_sha256") or "").lower() != receipt_reference:
            blockers.append("historical_order_book_receipt_reference_mismatch")

    historical_receipt = market.get("historical_order_book_receipt")
    evidence_blockers: list[str] = []
    historical_replay_verified = False
    if historical_receipt is None:
        evidence_blockers.append("historical_order_book_receipt_unavailable")
    else:
        if not isinstance(historical_receipt, Mapping):
            raise ValueError("historical_order_book_receipt_schema_invalid")
        verify_historical_order_book_receipt(historical_receipt)
        historical_replay_verified = historical_receipt["provider_verified"] is True
        if not historical_replay_verified:
            evidence_blockers.append("historical_order_book_provider_unverified")

    raw_available = market.get("available_quantity", market.get("available_volume"))
    if historical_receipt is not None:
        receipt_available = float(historical_receipt["available_quantity"])
        if raw_available is not None and abs(float(raw_available) - receipt_available) > 1e-9:
            blockers.append("historical_available_quantity_mismatch")
        raw_available = receipt_available
    available = remaining if raw_available is None else _finite_non_negative(raw_available, "available_quantity")

    market_volume_raw = market.get("market_volume")
    participation_rate_raw = market.get("participation_rate")
    volume_cap_raw = market.get("volume_cap")
    participation_model_configured = (
        market_volume_raw is not None
        or participation_rate_raw is not None
        or volume_cap_raw is not None
    )
    market_volume = (
        None
        if market_volume_raw is None
        else _finite_non_negative(market_volume_raw, "market_volume")
    )
    participation_rate = (
        None
        if participation_rate_raw is None
        else _finite_fraction(participation_rate_raw, "participation_rate")
    )
    volume_cap = (
        None
        if volume_cap_raw is None
        else _finite_non_negative(volume_cap_raw, "volume_cap")
    )
    if market_volume is None and participation_rate is not None:
        blockers.append("participation_model_requires_market_volume")
    if market_volume is not None and participation_rate is None:
        blockers.append("participation_model_requires_participation_rate")
    participation_quantity_cap = (
        None
        if market_volume is None or participation_rate is None
        else market_volume * participation_rate
    )

    if historical_receipt is not None:
        receipt_queue = float(historical_receipt["queue_ahead_quantity"])
        if "queue_ahead_quantity" in market and abs(float(market["queue_ahead_quantity"]) - receipt_queue) > 1e-9:
            blockers.append("historical_queue_quantity_mismatch")
        market = {**market, "queue_ahead_quantity": receipt_queue}
    queue_was_configured = "queue_ahead_quantity" in market or (
        "queue_ahead_remaining" in state
        and float(state.get("queue_ahead_remaining") or 0.0) > 0.0
    )
    queue_before = _finite_non_negative(
        state.get("queue_ahead_remaining", market.get("queue_ahead_quantity", 0.0)),
        "queue_ahead_quantity",
    )
    matched_raw = market.get("matched_quantity", market.get("executed_quantity"))
    if historical_receipt is not None:
        receipt_matched = float(historical_receipt["matched_quantity"])
        if matched_raw is not None and abs(float(matched_raw) - receipt_matched) > 1e-9:
            blockers.append("historical_matched_quantity_mismatch")
        matched_raw = receipt_matched
    matched = None if matched_raw is None else _finite_non_negative(matched_raw, "matched_quantity")
    if queue_was_configured and matched is None:
        blockers.append("queue_model_requires_matched_quantity")

    queue_consumed = min(queue_before, matched or 0.0)
    queue_after = max(0.0, queue_before - queue_consumed)
    queue_capacity = max(0.0, (matched or 0.0) - queue_before) if queue_was_configured else available

    probability = _finite_probability(market.get("fill_probability", 1.0))
    probability_seed = str(market.get("fill_probability_seed") or order_id)
    random_draw = _deterministic_draw(
        f"{probability_seed}|{order_id}|{int(fill_sequence)}"
    )
    if random_draw >= probability:
        blockers.append("fill_probability_not_met")

    latency_ms = _finite_non_negative(market.get("latency_ms", 0.0), "latency_ms")
    max_latency_raw = market.get("max_latency_ms")
    max_latency_ms = None if max_latency_raw is None else _finite_non_negative(max_latency_raw, "max_latency_ms")
    if max_latency_ms is not None and latency_ms > max_latency_ms + 1e-9:
        blockers.append("execution_latency_exceeded")

    market_impact_bps = _finite_non_negative(market.get("market_impact_bps", 0.0), "market_impact_bps")
    latency_impact_bps = _finite_non_negative(
        market.get("latency_impact_bps_per_100ms", 0.0),
        "latency_impact_bps_per_100ms",
    ) * latency_ms / 100.0
    total_impact_bps = market_impact_bps + latency_impact_bps

    calibration_artifact = market.get("impact_calibration_artifact")
    calibration_verified = False
    if calibration_artifact is None:
        evidence_blockers.append("empirical_impact_calibration_unavailable")
    else:
        if not isinstance(calibration_artifact, Mapping):
            raise ValueError("impact_calibration_artifact_invalid")
        from open_stock_ai.research.market_impact_calibration import verify_calibration_artifact

        verify_calibration_artifact(calibration_artifact)
        calibration_verified = calibration_artifact.get("execution_evidence_eligible") is True
        if not calibration_verified:
            evidence_blockers.append("empirical_impact_calibration_not_eligible")

    capacity = min(remaining, available)
    if queue_was_configured:
        capacity = min(capacity, queue_capacity)
    if participation_quantity_cap is not None:
        capacity = min(capacity, participation_quantity_cap)
    if volume_cap is not None:
        capacity = min(capacity, volume_cap)
    if blockers:
        capacity = 0.0

    return {
        "schema_version": MODEL_VERSION,
        "order_id": order_id,
        "fill_sequence": int(fill_sequence),
        "remaining_quantity": remaining,
        "available_quantity": available,
        "participation_model_enabled": participation_model_configured,
        "market_volume": market_volume,
        "participation_rate": participation_rate,
        "participation_quantity_cap": participation_quantity_cap,
        "volume_cap": volume_cap,
        "liquidity_quantity_cap": _minimum_optional(
            participation_quantity_cap,
            volume_cap,
        ),
        "matched_quantity": matched,
        "queue_model_enabled": queue_was_configured,
        "queue_ahead_before": queue_before,
        "queue_consumed": queue_consumed,
        "queue_ahead_remaining": queue_after,
        "fill_probability": probability,
        "fill_probability_seed": probability_seed,
        "deterministic_draw": random_draw,
        "latency_ms": latency_ms,
        "max_latency_ms": max_latency_ms,
        "market_impact_bps": market_impact_bps,
        "latency_impact_bps": latency_impact_bps,
        "total_impact_bps": total_impact_bps,
        "historical_order_book_replay_enabled": historical_receipt is not None,
        "historical_order_book_replay_verified": historical_replay_verified,
        "empirical_impact_calibration_verified": calibration_verified,
        "execution_evidence_eligible": historical_replay_verified and calibration_verified and not blockers,
        "evidence_blockers": list(dict.fromkeys(evidence_blockers)),
        "fill_quantity": capacity,
        "blockers": list(dict.fromkeys(blockers)),
        "execution_allowed": not blockers and capacity > 0.0,
    }


def _finite_non_negative(value: Any, field: str) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_finite_non_negative") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field}_must_be_finite_non_negative")
    return number


def _finite_probability(value: Any) -> float:
    try:
        number = float(1.0 if value is None else value)
    except (TypeError, ValueError) as exc:
        raise ValueError("fill_probability_must_be_between_zero_and_one") from exc
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ValueError("fill_probability_must_be_between_zero_and_one")
    return number


def _finite_fraction(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_must_be_between_zero_and_one") from exc
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ValueError(f"{field}_must_be_between_zero_and_one")
    return number


def _minimum_optional(*values: float | None) -> float | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _hash_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _deterministic_draw(value: str) -> float:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)
