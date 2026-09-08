"""Fail-closed policy contract for scheduled SQLite backups."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


POLICY_SCHEMA = "open_stock_ai.sqlite_backup_policy.v1"


@dataclass(frozen=True, slots=True)
class SQLiteBackupPolicy:
    interval_hours: int
    retention_count: int
    destination_uri: str
    off_host_required: bool = True
    owner: str = "platform-storage"

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA,
            "interval_hours": self.interval_hours,
            "retention_count": self.retention_count,
            "destination_uri": self.destination_uri,
            "off_host_required": self.off_host_required,
            "owner": self.owner,
        }

    @property
    def policy_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def backup_policy_from_mapping(mapping: Mapping[str, Any]) -> SQLiteBackupPolicy:
    policy = SQLiteBackupPolicy(
        interval_hours=int(mapping.get("interval_hours") or 0),
        retention_count=int(mapping.get("retention_count") or 0),
        destination_uri=str(mapping.get("destination_uri") or "").strip(),
        off_host_required=bool(mapping.get("off_host_required", True)),
        owner=str(mapping.get("owner") or "").strip(),
    )
    validate_backup_policy(policy)
    return policy


def validate_backup_policy(policy: SQLiteBackupPolicy) -> None:
    if policy.interval_hours < 1 or policy.interval_hours > 168:
        raise ValueError("backup_interval_hours_must_be_between_1_and_168")
    if policy.retention_count < 2:
        raise ValueError("backup_retention_count_must_keep_at_least_two_copies")
    if not policy.owner:
        raise ValueError("backup_policy_owner_required")
    if not policy.destination_uri or "://" not in policy.destination_uri:
        raise ValueError("backup_destination_uri_required")
    scheme = policy.destination_uri.split("://", 1)[0].lower()
    if policy.off_host_required and scheme in {"file", "local"}:
        raise ValueError("off_host_backup_destination_required")


def evaluate_backup_schedule(
    policy: SQLiteBackupPolicy,
    *,
    last_success_at: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic due/not-due decision with a hash-bound policy."""

    validate_backup_policy(policy)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if not last_success_at:
        due = True
        next_due_at = current
    else:
        try:
            last = datetime.fromisoformat(last_success_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("backup_last_success_timestamp_invalid") from exc
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        next_due_at = last + timedelta(hours=policy.interval_hours)
        due = current >= next_due_at
    return {
        **policy.payload(),
        "policy_sha256": policy.policy_sha256,
        "due": due,
        "evaluated_at": current.astimezone(timezone.utc).isoformat(),
        "next_due_at": next_due_at.astimezone(timezone.utc).isoformat(),
        "blockers": [],
    }


__all__ = [
    "POLICY_SCHEMA",
    "SQLiteBackupPolicy",
    "backup_policy_from_mapping",
    "evaluate_backup_schedule",
    "validate_backup_policy",
]
