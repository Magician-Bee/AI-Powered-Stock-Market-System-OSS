from __future__ import annotations

"""Shared, hash-verified data-quality evidence contracts.

This module intentionally has no dependency on the market-intelligence
package.  Screener, market-intelligence snapshots, and other decision
surfaces can therefore emit the same receipt without importing a service
package and creating a runtime cycle.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DataQuality(BaseModel):
    status: Literal["ready", "partial", "insufficient", "conflict"] = "insufficient"
    score: float = Field(default=0.0, ge=0, le=1)
    source: str = ""
    data_as_of: str | None = None
    acquired_at: str = Field(default_factory=utc_now)
    fallback: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    source_observation: dict[str, Any] = Field(default_factory=dict)


class DecisionDataQualityReceipt(BaseModel):
    """Immutable quality evidence bound to one decision surface result."""

    schema_version: Literal["stock_ai.decision_data_quality_receipt.v1"] = (
        "stock_ai.decision_data_quality_receipt.v1"
    )
    receipt_id: str
    snapshot_id: str
    symbol: str
    decision_at: str
    data_as_of: str | None = None
    source: str
    evidence_ids: list[str] = Field(default_factory=list)
    freshness_status: Literal["passed", "stale", "future", "unknown"]
    completeness_status: Literal["passed", "failed"]
    anomaly_status: Literal["passed", "warning", "failed"]
    source_disagreement_status: Literal["passed", "conflict", "not_observed"]
    confidence_status: Literal["passed", "warning", "failed"]
    certification_status: Literal["passed", "partial", "blocked"]
    data_quality: DataQuality
    receipt_sha256: str

    @classmethod
    def issue(
        cls,
        *,
        snapshot_id: str,
        symbol: str,
        decision_at: str,
        snapshot_data_as_of: str,
        data_quality: DataQuality,
        evidence_ids: list[str],
    ) -> "DecisionDataQualityReceipt":
        data_as_of = data_quality.data_as_of
        freshness = cls._freshness(data_as_of, snapshot_data_as_of)
        completeness = "passed" if not data_quality.missing_fields else "failed"
        anomaly = (
            "failed"
            if data_quality.status == "conflict"
            else "warning"
            if any(
                flag != "cross_source_observed"
                for flag in data_quality.quality_flags
            )
            else "passed"
        )
        disagreement = (
            "conflict"
            if data_quality.status == "conflict"
            else "passed"
            if "cross_source_observed" in data_quality.quality_flags
            else "not_observed"
        )
        confidence = (
            "passed"
            if data_quality.score >= 0.8
            else "warning"
            if data_quality.score >= 0.5
            else "failed"
        )
        certification = (
            "blocked"
            if (
                freshness in {"future", "unknown"}
                or completeness == "failed"
                or anomaly == "failed"
                or confidence == "failed"
            )
            else "partial"
            if freshness == "stale"
            or anomaly == "warning"
            or disagreement == "not_observed"
            or confidence == "warning"
            else "passed"
        )
        payload = {
            "schema_version": "stock_ai.decision_data_quality_receipt.v1",
            "snapshot_id": str(snapshot_id),
            "symbol": str(symbol).upper(),
            "decision_at": str(decision_at),
            "data_as_of": data_as_of,
            "source": data_quality.source,
            "evidence_ids": sorted(set(evidence_ids)),
            "freshness_status": freshness,
            "completeness_status": completeness,
            "anomaly_status": anomaly,
            "source_disagreement_status": disagreement,
            "confidence_status": confidence,
            "certification_status": certification,
            "data_quality": data_quality.model_dump(mode="json"),
        }
        digest = cls._digest(payload)
        return cls(
            receipt_id=f"DQDR-{digest[:32]}",
            receipt_sha256=digest,
            **payload,
        )

    @staticmethod
    def _freshness(data_as_of: str | None, snapshot_data_as_of: str) -> str:
        if not data_as_of:
            return "unknown"
        try:
            observed = datetime.fromisoformat(data_as_of.replace("Z", "+00:00")).date()
            snapshot = datetime.fromisoformat(snapshot_data_as_of.replace("Z", "+00:00")).date()
        except ValueError:
            return "unknown"
        if observed > snapshot:
            return "future"
        return "passed" if observed == snapshot else "stale"

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @model_validator(mode="after")
    def validate_receipt_hash(self) -> "DecisionDataQualityReceipt":
        payload = self.model_dump(mode="json", exclude={"receipt_id", "receipt_sha256"})
        digest = self._digest(payload)
        if self.receipt_id == f"DQDR-{digest[:32]}" and self.receipt_sha256 == digest:
            return self

        # Receipts written before ``source_observation`` was added are still
        # valid immutable v1 evidence.  Verify their original payload shape
        # without silently accepting any other hash mismatch.
        legacy_payload = dict(payload)
        legacy_quality = dict(legacy_payload.get("data_quality") or {})
        legacy_quality.pop("source_observation", None)
        legacy_payload["data_quality"] = legacy_quality
        legacy_digest = self._digest(legacy_payload)
        if self.receipt_id == f"DQDR-{legacy_digest[:32]}" and self.receipt_sha256 == legacy_digest:
            return self
        raise ValueError("decision data quality receipt hash mismatch")
