from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from open_stock_ai.storage.backup_policy import (
    POLICY_SCHEMA,
    backup_policy_from_mapping,
    evaluate_backup_schedule,
)


ROOT = Path(__file__).resolve().parents[1]


def test_declared_backup_policy_is_off_host_and_schedule_is_fail_closed() -> None:
    mapping = yaml.safe_load((ROOT / "config" / "sqlite_backup_policy.yaml").read_text(encoding="utf-8"))
    policy = backup_policy_from_mapping(mapping)
    assert policy.payload()["schema_version"] == POLICY_SCHEMA
    decision = evaluate_backup_schedule(
        policy,
        last_success_at="2026-08-26T00:00:00+00:00",
        now=datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc),
    )
    assert decision["due"] is True
    assert decision["destination_uri"].startswith("s3://")
    assert len(decision["policy_sha256"]) == 64


def test_backup_schedule_requires_first_backup_and_rejects_local_destination() -> None:
    policy = backup_policy_from_mapping(
        {
            "interval_hours": 24,
            "retention_count": 2,
            "destination_uri": "ssh://backup.example/stock-ai",
            "owner": "storage",
        }
    )
    decision = evaluate_backup_schedule(policy, last_success_at=None, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
    assert decision["due"] is True
    with pytest.raises(ValueError, match="off_host_backup_destination_required"):
        backup_policy_from_mapping(
            {"interval_hours": 24, "retention_count": 2, "destination_uri": "file:///tmp/backups", "owner": "storage"}
        )


def test_backup_schedule_rejects_invalid_last_success_timestamp() -> None:
    policy = backup_policy_from_mapping(
        {"interval_hours": 24, "retention_count": 2, "destination_uri": "s3://bucket/key", "owner": "storage"}
    )
    with pytest.raises(ValueError, match="timestamp_invalid"):
        evaluate_backup_schedule(policy, last_success_at="not-a-time")
