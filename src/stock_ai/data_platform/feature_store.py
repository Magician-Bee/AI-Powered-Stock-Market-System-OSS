"""Point-in-time feature records and an immutable replay store.

Every research feature carries both the time it describes and the time it
became knowable.  Replay queries are therefore constrained by ``available_at``
and never silently substitute a later observation for an earlier decision.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


FEATURE_SCHEMA = "stock_ai.pit_feature_record.v1"
REPLAY_SCHEMA = "stock_ai.pit_feature_replay.v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _timestamp(value: str, field: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"feature_{field}_required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"feature_{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"feature_{field}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class PITFeatureRecord:
    entity_id: str
    feature_id: str
    value: Any
    event_time: str
    published_at: str
    available_at: str
    effective_at: str
    source_revision_id: str
    transformation_id: str
    transformation_sha: str
    dataset_version: str
    feature_sha256: str
    schema_version: str = FEATURE_SCHEMA

    @classmethod
    def issue(
        cls,
        *,
        entity_id: str,
        feature_id: str,
        value: Any,
        event_time: str,
        published_at: str,
        available_at: str,
        effective_at: str,
        source_revision_id: str,
        transformation_id: str,
        transformation_sha: str,
        dataset_version: str,
    ) -> "PITFeatureRecord":
        record = cls(
            entity_id=str(entity_id).strip(),
            feature_id=str(feature_id).strip(),
            value=value,
            event_time=_timestamp(event_time, "event_time"),
            published_at=_timestamp(published_at, "published_at"),
            available_at=_timestamp(available_at, "available_at"),
            effective_at=_timestamp(effective_at, "effective_at"),
            source_revision_id=str(source_revision_id).strip(),
            transformation_id=str(transformation_id).strip(),
            transformation_sha=str(transformation_sha).lower().strip(),
            dataset_version=str(dataset_version).strip(),
            feature_sha256="",
        )
        record.verify(require_hash=False)
        return replace(record, feature_sha256=_sha(record.payload()))

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entity_id": self.entity_id,
            "feature_id": self.feature_id,
            "value": self.value,
            "event_time": self.event_time,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "effective_at": self.effective_at,
            "source_revision_id": self.source_revision_id,
            "transformation_id": self.transformation_id,
            "transformation_sha": self.transformation_sha,
            "dataset_version": self.dataset_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "feature_sha256": self.feature_sha256}

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.entity_id, self.feature_id, self.event_time, self.dataset_version)

    def verify(self, *, require_hash: bool = True) -> None:
        if self.schema_version != FEATURE_SCHEMA:
            raise ValueError("feature_schema_invalid")
        if not all((self.entity_id, self.feature_id, self.source_revision_id, self.transformation_id, self.dataset_version)):
            raise ValueError("feature_identity_fields_required")
        for field in ("event_time", "published_at", "available_at", "effective_at"):
            _timestamp(getattr(self, field), field)
        if self.published_at > self.available_at:
            raise ValueError("feature_available_at_precedes_published_at")
        if len(self.transformation_sha) != 64 or any(c not in "0123456789abcdef" for c in self.transformation_sha):
            raise ValueError("feature_transformation_sha_invalid")
        if require_hash and (len(self.feature_sha256) != 64 or self.feature_sha256 != _sha(self.payload())):
            raise ValueError("feature_hash_mismatch")


class PITFeatureStore:
    """SQLite-backed immutable feature storage with fail-closed replay."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists pit_features (
                    feature_sha256 text primary key,
                    entity_id text not null,
                    feature_id text not null,
                    event_time text not null,
                    published_at text not null,
                    available_at text not null,
                    effective_at text not null,
                    source_revision_id text not null,
                    transformation_id text not null,
                    transformation_sha text not null,
                    dataset_version text not null,
                    value_json text not null,
                    payload_json text not null,
                    unique(entity_id, feature_id, event_time, dataset_version)
                );
                create trigger if not exists pit_features_immutable_update
                before update on pit_features begin
                    select raise(abort, 'pit features are immutable');
                end;
                create trigger if not exists pit_features_immutable_delete
                before delete on pit_features begin
                    select raise(abort, 'pit features are immutable');
                end;
                """
            )

    def put(self, record: PITFeatureRecord) -> PITFeatureRecord:
        record.verify()
        with self._connect() as conn:
            existing = conn.execute(
                "select feature_sha256, payload_json from pit_features where entity_id=? and feature_id=? and event_time=? and dataset_version=?",
                record.identity,
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != record.feature_sha256 or str(existing[1]) != _json(record.payload()):
                    raise ValueError("pit_feature_identity_is_bound_to_different_evidence")
                return record
            conn.execute(
                """insert into pit_features(
                    feature_sha256, entity_id, feature_id, event_time, published_at,
                    available_at, effective_at, source_revision_id, transformation_id,
                    transformation_sha, dataset_version, value_json, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.feature_sha256, record.entity_id, record.feature_id, record.event_time,
                    record.published_at, record.available_at, record.effective_at,
                    record.source_revision_id, record.transformation_id, record.transformation_sha,
                    record.dataset_version, _json(record.value), _json(record.payload()),
                ),
            )
        return record

    def put_many(self, records: Iterable[PITFeatureRecord]) -> list[PITFeatureRecord]:
        return [self.put(record) for record in records]

    def replay(self, *, entity_id: str, decision_time: str) -> dict[str, Any]:
        cutoff = _timestamp(decision_time, "decision_time")
        with self._connect() as conn:
            rows = conn.execute(
                """select payload_json, feature_sha256 from pit_features
                   where entity_id=? and available_at<=? and effective_at<=? and event_time<=?
                   order by event_time, feature_id, feature_sha256""",
                (str(entity_id).strip(), cutoff, cutoff, cutoff),
            ).fetchall()
        records = [json.loads(str(row[0])) | {"feature_sha256": str(row[1])} for row in rows]
        receipt_payload = {
            "schema_version": REPLAY_SCHEMA,
            "entity_id": str(entity_id).strip(),
            "decision_time": cutoff,
            "feature_hashes": [item["feature_sha256"] for item in records],
        }
        return {
            **receipt_payload,
            "records": records,
            "record_count": len(records),
            "replay_eligible": True,
            "replay_receipt_sha256": _sha(receipt_payload),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn


__all__ = ["FEATURE_SCHEMA", "REPLAY_SCHEMA", "PITFeatureRecord", "PITFeatureStore"]
