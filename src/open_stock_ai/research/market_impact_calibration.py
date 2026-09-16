from __future__ import annotations

"""Immutable empirical market-impact observations and calibration receipts.

The normal impact schedule is intentionally a research baseline until a
reviewed empirical artifact exists.  This module provides the missing durable
artifact boundary without silently promoting local scenarios or unverified
fills: every observation is hash-bound to a source receipt, and a calibration
is execution-eligible only when its minimum sample is provider-attested.
"""

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "open_stock_ai.market_impact_calibration.v1"
SOURCE_RECEIPT_SCHEMA_VERSION = "open_stock_ai.market_impact_source.v1"
_REVIEWED_SOURCE_DOCUMENTS = frozenset({"broker_reconciliation", "exchange_execution"})
_VENUES = {"TWSE", "TPEX"}
_PRODUCTS = {"stock", "etf", "bond_etf"}
_SIDES = {"buy", "sell"}
_BUCKETS = (
    ("0_to_0.001", 0.0, 0.001),
    ("0.001_to_0.005", 0.001, 0.005),
    ("0.005_to_0.02", 0.005, 0.02),
    ("0.02_to_0.10", 0.02, 0.10),
    ("0.10_plus", 0.10, float("inf")),
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("impact_observation_captured_at_required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("impact_observation_captured_at_invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("impact_observation_captured_at_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat()


def build_source_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create a hash-bound source receipt for a fill observation."""

    body = {"schema_version": SOURCE_RECEIPT_SCHEMA_VERSION, **dict(payload)}
    body["receipt_sha256"] = _hash(body)
    return body


def build_reviewed_source_receipt(
    payload: Mapping[str, Any],
    *,
    broker_reconciliation_receipt_id: str,
    exchange_execution_receipt_id: str,
    account_owner_review_receipt_sha256: str,
) -> dict[str, Any]:
    """Build the explicit reviewed chain required for execution evidence.

    A content hash and a caller-supplied ``provider_verified`` flag only make
    a local assertion immutable; they do not demonstrate that a broker fill
    and exchange execution were independently reviewed by the account owner.
    Keep that distinction in the persisted source receipt so generic local
    observations remain useful for research without being promoted to an
    empirical execution calibration.
    """

    return build_source_receipt(
        {
            **dict(payload),
            "provider_verified": True,
            "reviewed_provider_evidence": True,
            "review_document_receipt_ids": {
                "broker_reconciliation": str(broker_reconciliation_receipt_id).strip(),
                "exchange_execution": str(exchange_execution_receipt_id).strip(),
            },
            "account_owner_review_receipt_sha256": str(
                account_owner_review_receipt_sha256
            ).lower(),
        }
    )


def _is_sha256(value: Any) -> bool:
    raw = str(value or "").lower()
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


def reviewed_provider_evidence_eligible(receipt: Mapping[str, Any]) -> bool:
    """Return true only for a structurally complete reviewed evidence chain."""

    document_ids = receipt.get("review_document_receipt_ids")
    return (
        receipt.get("provider_verified") is True
        and receipt.get("reviewed_provider_evidence") is True
        and isinstance(document_ids, Mapping)
        and set(document_ids) == _REVIEWED_SOURCE_DOCUMENTS
        and all(str(document_ids[name] or "").strip() for name in _REVIEWED_SOURCE_DOCUMENTS)
        and _is_sha256(receipt.get("account_owner_review_receipt_sha256"))
    )


def verify_source_receipt(receipt: Mapping[str, Any], *, observation_id: str | None = None) -> bool:
    if receipt.get("schema_version") != SOURCE_RECEIPT_SCHEMA_VERSION:
        raise ValueError("impact_source_receipt_schema_invalid")
    body = dict(receipt)
    actual = str(body.pop("receipt_sha256", ""))
    if len(actual) != 64 or actual != _hash(body):
        raise ValueError("impact_source_receipt_hash_mismatch")
    if not str(body.get("source") or "").strip():
        raise ValueError("impact_source_receipt_source_required")
    if observation_id is not None and str(body.get("source_event_id") or "") != observation_id:
        raise ValueError("impact_source_receipt_event_id_mismatch")
    if body.get("reviewed_provider_evidence") is True and not reviewed_provider_evidence_eligible(body):
        raise ValueError("impact_source_receipt_review_chain_invalid")
    return True


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile_requires_values")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _participation_bucket(value: float) -> str | None:
    for label, lower, upper in _BUCKETS:
        if lower <= value < upper:
            return label
    return None


class MarketImpactCalibrationStore:
    """SQLite-backed immutable observation and calibration ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists market_impact_observations (
                    observation_id text primary key,
                    observation_sha256 text not null unique,
                    payload_json text not null,
                    captured_at text not null
                );
                create table if not exists market_impact_calibrations (
                    calibration_id text primary key,
                    artifact_sha256 text not null unique,
                    payload_json text not null,
                    created_at text not null
                );
                create trigger if not exists market_impact_observations_immutable_update
                    before update on market_impact_observations
                    begin select raise(abort, 'market impact observations are immutable'); end;
                create trigger if not exists market_impact_observations_immutable_delete
                    before delete on market_impact_observations
                    begin select raise(abort, 'market impact observations are immutable'); end;
                create trigger if not exists market_impact_calibrations_immutable_update
                    before update on market_impact_calibrations
                    begin select raise(abort, 'market impact calibrations are immutable'); end;
                create trigger if not exists market_impact_calibrations_immutable_delete
                    before delete on market_impact_calibrations
                    begin select raise(abort, 'market impact calibrations are immutable'); end;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_observation(observation)
        observation_id = normalized["observation_id"]
        body = dict(normalized)
        observation_sha = _hash(body)
        payload = {**body, "observation_sha256": observation_sha}
        serialized = _json(payload)
        with self._connect() as connection:
            existing = connection.execute(
                "select observation_sha256, payload_json from market_impact_observations where observation_id=?",
                (observation_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["observation_sha256"]) != observation_sha or str(existing["payload_json"]) != serialized:
                    raise ValueError("impact_observation_identity_is_bound_to_different_evidence")
                return payload
            connection.execute(
                "insert into market_impact_observations(observation_id, observation_sha256, payload_json, captured_at) values (?, ?, ?, ?)",
                (observation_id, observation_sha, serialized, normalized["captured_at"]),
            )
        return payload

    def record_many(self, observations: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [self.record(item) for item in observations]

    def observations(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from market_impact_observations order by captured_at, observation_id"
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def calibrate(
        self,
        *,
        calibration_id: str,
        venue: str,
        product_type: str,
        side: str,
        min_samples: int = 20,
        observation_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        normalized_venue = str(venue or "").upper().strip()
        normalized_product = str(product_type or "").lower().strip()
        normalized_side = str(side or "").lower().strip()
        if normalized_venue not in _VENUES or normalized_product not in _PRODUCTS or normalized_side not in _SIDES:
            raise ValueError("impact_calibration_scope_invalid")
        if int(min_samples) <= 0:
            raise ValueError("impact_calibration_min_samples_invalid")
        selected_ids = {str(value).strip() for value in observation_ids or () if str(value).strip()}
        rows = [
            item for item in self.observations()
            if item["venue"] == normalized_venue
            and item["product_type"] == normalized_product
            and item["side"] == normalized_side
            and (not selected_ids or item["observation_id"] in selected_ids)
        ]
        blockers: set[str] = set()
        if not rows:
            blockers.add("no_matching_impact_observations")
        elif any(item.get("participation_rate") is None for item in rows):
            blockers.add("participation_input_missing")
        rules: list[dict[str, Any]] = []
        used_hashes: list[str] = []
        for label, _, _ in _BUCKETS:
            bucket_rows: list[dict[str, Any]] = []
            for item in rows:
                participation = item.get("participation_rate")
                if participation is not None and _participation_bucket(float(participation)) == label:
                    bucket_rows.append(item)
            if not bucket_rows:
                continue
            reviewed_rows = [
                item
                for item in bucket_rows
                if reviewed_provider_evidence_eligible(item["source_receipt"])
            ]
            if len(reviewed_rows) < len(bucket_rows):
                blockers.add(f"reviewed_provider_evidence_missing:{label}")
            if len(bucket_rows) < int(min_samples):
                blockers.add(f"insufficient_samples:{label}")
            if len(reviewed_rows) < int(min_samples):
                eligible = False
            else:
                eligible = True
            calibration_rows = reviewed_rows if reviewed_rows else bucket_rows
            adverse = [float(item["adverse_impact_bps"]) for item in calibration_rows]
            rules.append(
                {
                    "participation_bucket": label,
                    "sample_count": len(bucket_rows),
                    "reviewed_sample_count": len(reviewed_rows),
                    "median_adverse_impact_bps": round(_percentile(adverse, 0.5), 8),
                    "p90_adverse_impact_bps": round(_percentile(adverse, 0.9), 8),
                    "execution_evidence_eligible": eligible,
                    "observation_sha256": sorted(item["observation_sha256"] for item in bucket_rows),
                }
            )
            used_hashes.extend(item["observation_sha256"] for item in bucket_rows)
        complete = bool(rules) and not blockers and all(item["execution_evidence_eligible"] for item in rules)
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "calibration_id": str(calibration_id).strip(),
            "scope": {
                "venue": normalized_venue,
                "product_type": normalized_product,
                "side": normalized_side,
            },
            "minimum_samples": int(min_samples),
            "observation_count": len(rows),
            "observation_manifest_sha256": _hash({"observation_sha256": sorted(set(used_hashes))}),
            "rules": rules,
            "calibration_status": "empirically_calibrated" if complete else "partial_unverified",
            "execution_evidence_eligible": complete,
            "blockers": sorted(blockers),
            "method": "adverse_fill_impact_by_pit_participation_bucket",
        }
        if not payload["calibration_id"]:
            raise ValueError("impact_calibration_id_required")
        artifact_hash = _hash(payload)
        artifact = {**payload, "artifact_sha256": artifact_hash}
        serialized = _json(artifact)
        with self._connect() as connection:
            existing = connection.execute(
                "select payload_json from market_impact_calibrations where calibration_id=?",
                (payload["calibration_id"],),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != serialized:
                    raise ValueError("impact_calibration_identity_is_bound_to_different_evidence")
            else:
                connection.execute(
                    "insert into market_impact_calibrations(calibration_id, artifact_sha256, payload_json, created_at) values (?, ?, ?, ?)",
                    (payload["calibration_id"], artifact_hash, serialized, datetime.now(timezone.utc).isoformat()),
                )
        return artifact

    def calibrations(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from market_impact_calibrations order by created_at, calibration_id"
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    @staticmethod
    def _normalize_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
        observation_id = str(observation.get("observation_id") or "").strip()
        if not observation_id:
            raise ValueError("impact_observation_id_required")
        venue = str(observation.get("venue") or "").upper().strip()
        product_type = str(observation.get("product_type") or "").lower().strip()
        side = str(observation.get("side") or "").lower().strip()
        if venue not in _VENUES or product_type not in _PRODUCTS or side not in _SIDES:
            raise ValueError("impact_observation_scope_invalid")
        try:
            reference_price = float(observation.get("reference_price"))
            fill_price = float(observation.get("fill_price"))
            quantity = float(observation.get("quantity"))
        except (TypeError, ValueError):
            raise ValueError("impact_observation_numeric_fields_required") from None
        if not all(math.isfinite(value) for value in (reference_price, fill_price, quantity)) or reference_price <= 0 or fill_price <= 0 or quantity <= 0:
            raise ValueError("impact_observation_numeric_fields_invalid")
        participation: float | None = None
        if observation.get("adv_volume_shares") is not None:
            try:
                adv = float(observation["adv_volume_shares"])
            except (TypeError, ValueError):
                raise ValueError("impact_observation_adv_invalid") from None
            if not math.isfinite(adv) or adv <= 0:
                raise ValueError("impact_observation_adv_invalid")
            participation = quantity / adv
        adverse = (fill_price - reference_price) if side == "buy" else (reference_price - fill_price)
        adverse_impact_bps = adverse / reference_price * 10_000.0
        source_receipt = observation.get("source_receipt")
        if not isinstance(source_receipt, Mapping):
            raise ValueError("impact_observation_source_receipt_required")
        verify_source_receipt(source_receipt, observation_id=observation_id)
        return {
            "observation_id": observation_id,
            "venue": venue,
            "product_type": product_type,
            "side": side,
            "reference_price": reference_price,
            "fill_price": fill_price,
            "quantity": quantity,
            "adv_volume_shares": float(observation["adv_volume_shares"]) if observation.get("adv_volume_shares") is not None else None,
            "participation_rate": participation,
            "adverse_impact_bps": adverse_impact_bps,
            "captured_at": _timestamp(observation.get("captured_at")),
            "source_receipt": dict(source_receipt),
        }


def verify_calibration_artifact(artifact: Mapping[str, Any]) -> bool:
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("impact_calibration_schema_invalid")
    body = dict(artifact)
    actual = str(body.pop("artifact_sha256", ""))
    if len(actual) != 64 or actual != _hash(body):
        raise ValueError("impact_calibration_artifact_hash_mismatch")
    if not isinstance(body.get("rules"), list):
        raise ValueError("impact_calibration_rules_invalid")
    if bool(body.get("execution_evidence_eligible")) and body.get("calibration_status") != "empirically_calibrated":
        raise ValueError("impact_calibration_eligibility_status_mismatch")
    return True


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE_RECEIPT_SCHEMA_VERSION",
    "MarketImpactCalibrationStore",
    "build_source_receipt",
    "build_reviewed_source_receipt",
    "reviewed_provider_evidence_eligible",
    "verify_calibration_artifact",
    "verify_source_receipt",
]
