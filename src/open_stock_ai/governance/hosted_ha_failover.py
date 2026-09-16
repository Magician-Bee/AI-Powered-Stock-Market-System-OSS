"""Hash-verified evidence envelope for hosted multi-process HA failover."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .ha_leader_lease import HA_SERVICES, LeaseReceipt, ServiceTopology


HOSTED_HA_FAILOVER_SCHEMA = "open_stock_ai.hosted_ha_failover_receipt.v1"
REQUIRED_FAILOVER_INVARIANTS = (
    "separate_processes",
    "primary_killed",
    "standby_acquired",
    "epoch_advanced",
    "stale_owner_fenced",
    "durable_state_recovered",
)


def build_hosted_ha_failover_receipt(
    observations: Mapping[str, Mapping[str, Any]],
    *,
    commit_sha: str,
    database_quick_check: str,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha).strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted HA failover requires the exact candidate commit SHA")
    captured_at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()
    failovers: list[dict[str, Any]] = []
    blockers: list[str] = []
    for service in HA_SERVICES:
        raw = dict(observations.get(service) or {})
        invariants = {name: raw.get(name) is True for name in REQUIRED_FAILOVER_INVARIANTS}
        primary = dict(raw.get("primary_receipt") or {})
        standby = dict(raw.get("standby_receipt") or {})
        entry = {
            "service": service,
            "primary_pid": raw.get("primary_pid"),
            "standby_pid": raw.get("standby_pid"),
            "failover_seconds": raw.get("failover_seconds"),
            "primary_receipt": primary,
            "standby_receipt": standby,
            "invariants": invariants,
        }
        failovers.append(entry)
        if not all(invariants.values()):
            blockers.append(f"failover_invariant_failed:{service}")
        if not _valid_lease_transition(service, primary, standby):
            blockers.append(f"invalid_lease_transition:{service}")
    if str(database_quick_check) != "ok":
        blockers.append("ha_database_quick_check_failed")
    payload = {
        "schema_version": HOSTED_HA_FAILOVER_SCHEMA,
        "commit_sha": commit,
        "captured_at": captured_at,
        "topology": ServiceTopology().as_dict(),
        "database_quick_check": str(database_quick_check),
        "failovers": failovers,
        "blockers": blockers,
        "passed": not blockers,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_ha_failover_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version",
        "commit_sha",
        "captured_at",
        "topology",
        "database_quick_check",
        "failovers",
        "blockers",
        "passed",
        "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_HA_FAILOVER_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    topology = receipt.get("topology")
    if topology != ServiceTopology().as_dict() or receipt.get("database_quick_check") != "ok":
        return False
    failovers = receipt.get("failovers")
    if not isinstance(failovers, list) or [item.get("service") for item in failovers if isinstance(item, Mapping)] != list(HA_SERVICES):
        return False
    valid = True
    for item in failovers:
        if not isinstance(item, Mapping) or set(item.get("invariants") or {}) != set(REQUIRED_FAILOVER_INVARIANTS):
            return False
        valid = valid and all(value is True for value in item["invariants"].values())
        valid = valid and isinstance(item.get("primary_pid"), int) and isinstance(item.get("standby_pid"), int)
        valid = valid and item.get("primary_pid") != item.get("standby_pid")
        valid = valid and isinstance(item.get("failover_seconds"), (int, float)) and item["failover_seconds"] >= 0
        valid = valid and _valid_lease_transition(
            str(item.get("service")),
            item.get("primary_receipt") or {},
            item.get("standby_receipt") or {},
        )
    expected_passed = valid and not receipt.get("blockers")
    return receipt.get("passed") is expected_passed


def _valid_lease_transition(service: str, primary: Mapping[str, Any], standby: Mapping[str, Any]) -> bool:
    try:
        primary_receipt = _lease_receipt(primary)
        standby_receipt = _lease_receipt(standby)
    except (KeyError, TypeError, ValueError):
        return False
    return (
        primary_receipt.verify()
        and standby_receipt.verify()
        and primary_receipt.action == "claim"
        and standby_receipt.action == "claim"
        and primary_receipt.lease_name == service
        and standby_receipt.lease_name == service
        and primary_receipt.owner_id != standby_receipt.owner_id
        and standby_receipt.epoch == primary_receipt.epoch + 1
    )


def _lease_receipt(raw: Mapping[str, Any]) -> LeaseReceipt:
    required = {"schema_version", "action", "lease_name", "owner_id", "epoch", "expires_at", "receipt_sha256"}
    if set(raw) != required or raw.get("schema_version") != "open_stock_ai.ha_lease_receipt.v1":
        raise ValueError("invalid HA lease receipt shape")
    return LeaseReceipt(
        action=str(raw["action"]),
        lease_name=str(raw["lease_name"]),
        owner_id=str(raw["owner_id"]),
        epoch=int(raw["epoch"]),
        expires_at=str(raw["expires_at"]),
        receipt_sha256=str(raw["receipt_sha256"]),
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__: Sequence[str] = (
    "HOSTED_HA_FAILOVER_SCHEMA",
    "REQUIRED_FAILOVER_INVARIANTS",
    "build_hosted_ha_failover_receipt",
    "verify_hosted_ha_failover_receipt",
)
