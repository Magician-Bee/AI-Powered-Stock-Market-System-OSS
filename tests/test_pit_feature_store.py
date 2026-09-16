from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from stock_ai.data_platform.feature_store import PITFeatureRecord, PITFeatureStore


def _record(*, available_at: str = "2026-08-20T02:00:00+00:00", value: float = 15.0) -> PITFeatureRecord:
    return PITFeatureRecord.issue(
        entity_id="EQ-2330",
        feature_id="revenue_yoy",
        value=value,
        event_time="2026-08-19T00:00:00+00:00",
        published_at="2026-08-20T01:00:00+00:00",
        available_at=available_at,
        effective_at="2026-08-20T01:00:00+00:00",
        source_revision_id="REV-20260820-1",
        transformation_id="feature.revenue_yoy.v1",
        transformation_sha="a" * 64,
        dataset_version="dataset-2026-08-20",
    )


def test_feature_record_requires_full_temporal_and_provenance_contract() -> None:
    record = _record()
    assert record.as_dict()["feature_sha256"] == record.feature_sha256
    record.verify()
    with pytest.raises(ValueError, match="precedes_published"):
        PITFeatureRecord.issue(
            entity_id="EQ-2330", feature_id="bad", value=1,
            event_time="2026-08-19T00:00:00+00:00", published_at="2026-08-20T03:00:00+00:00",
            available_at="2026-08-20T02:00:00+00:00", effective_at="2026-08-20T01:00:00+00:00",
            source_revision_id="REV", transformation_id="feature.bad.v1", transformation_sha="a" * 64,
            dataset_version="dataset-1",
        )


def test_pit_replay_excludes_features_not_available_at_decision_time(tmp_path) -> None:
    store = PITFeatureStore(tmp_path / "features.sqlite")
    store.put(_record())
    future = PITFeatureRecord.issue(
        entity_id="EQ-2330", feature_id="future", value=99,
        event_time="2026-08-20T00:00:00+00:00", published_at="2026-08-21T01:00:00+00:00",
        available_at="2026-08-21T02:00:00+00:00", effective_at="2026-08-21T01:00:00+00:00",
        source_revision_id="REV-20260821-1", transformation_id="feature.future.v1", transformation_sha="b" * 64,
        dataset_version="dataset-2026-08-21",
    )
    store.put(future)
    replay = store.replay(entity_id="EQ-2330", decision_time="2026-08-20T03:00:00+00:00")
    assert replay["record_count"] == 1
    assert replay["records"][0]["feature_id"] == "revenue_yoy"
    assert replay["replay_eligible"] is True
    assert len(replay["replay_receipt_sha256"]) == 64


def test_pit_store_rejects_identity_rewrite_and_is_immutable(tmp_path) -> None:
    store = PITFeatureStore(tmp_path / "features.sqlite")
    record = _record()
    store.put(record)
    with pytest.raises(ValueError, match="different_evidence"):
        store.put(_record(value=16.0))
    with sqlite3.connect(store.path) as conn, pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("delete from pit_features where feature_sha256=?", (record.feature_sha256,))


def test_pit_replay_requires_timezone_aware_decision_time(tmp_path) -> None:
    store = PITFeatureStore(tmp_path / "features.sqlite")
    with pytest.raises(ValueError, match="timezone_required"):
        store.replay(entity_id="EQ-2330", decision_time="2026-08-20T03:00:00")
