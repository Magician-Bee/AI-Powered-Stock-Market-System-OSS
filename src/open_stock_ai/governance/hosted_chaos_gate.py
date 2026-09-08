"""Hash-verified campaign envelope for real hosted R-010 fault injection."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .chaos_recovery import CHAOS_SCENARIOS, ChaosRecoveryCatalog, ChaosRecoveryReceipt


HOSTED_CHAOS_GATE_SCHEMA = "open_stock_ai.hosted_chaos_gate_receipt.v1"
REQUIRED_INVARIANTS = (
    "durable_state_recovered",
    "new_orders_blocked_until_safe",
    "operator_receipt_written",
)


def build_hosted_chaos_gate_receipt(
    observations: Mapping[str, Mapping[str, Any]],
    *,
    commit_sha: str,
    seed: int = 105,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    commit = str(commit_sha).strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("hosted chaos gate requires the exact candidate commit SHA")
    captured_at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat()
    recoveries: dict[str, dict[str, Any]] = {}
    fault_observed: dict[str, bool] = {}
    for scenario in CHAOS_SCENARIOS:
        observation = dict(observations.get(scenario) or {})
        fault_observed[scenario] = observation.get("fault_observed") is True
        raw_invariants = observation.pop("invariants", {})
        invariants = {
            name: isinstance(raw_invariants, Mapping) and raw_invariants.get(name) is True
            for name in REQUIRED_INVARIANTS
        }
        recoveries[scenario] = {
            "invariants": invariants,
            "fault_observed": fault_observed[scenario],
            "evidence": observation,
            "candidate_commit": commit,
        }
    catalog = ChaosRecoveryCatalog(seed=seed, clock=lambda: captured_at)
    scenario_receipts = catalog.run_catalog(recoveries)
    blockers = [
        f"fault_not_observed:{scenario}"
        for scenario in CHAOS_SCENARIOS
        if not fault_observed[scenario]
    ]
    blockers.extend(
        f"recovery_invariant_failed:{receipt.scenario}"
        for receipt in scenario_receipts
        if receipt.status != "recovered"
    )
    payload = {
        "schema_version": HOSTED_CHAOS_GATE_SCHEMA,
        "commit_sha": commit,
        "captured_at": captured_at,
        "seed": seed,
        "scenarios": [receipt.as_dict() for receipt in scenario_receipts],
        "blockers": blockers,
        "passed": catalog.complete() and not blockers,
    }
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_hosted_chaos_gate_receipt(receipt: Mapping[str, Any]) -> bool:
    required = {
        "schema_version",
        "commit_sha",
        "captured_at",
        "seed",
        "scenarios",
        "blockers",
        "passed",
        "receipt_sha256",
    }
    if set(receipt) != required or receipt.get("schema_version") != HOSTED_CHAOS_GATE_SCHEMA:
        return False
    payload = {key: receipt[key] for key in required if key != "receipt_sha256"}
    if not hmac.compare_digest(_digest(payload), str(receipt.get("receipt_sha256") or "")):
        return False
    raw_scenarios = receipt.get("scenarios")
    if not isinstance(raw_scenarios, list) or len(raw_scenarios) != len(CHAOS_SCENARIOS):
        return False
    try:
        scenarios = [ChaosRecoveryReceipt.from_dict(item) for item in raw_scenarios]
    except (KeyError, TypeError, ValueError):
        return False
    if [item.scenario for item in scenarios] != list(CHAOS_SCENARIOS):
        return False
    faults = [item.recovery.get("fault_observed") is True for item in scenarios]
    invariants = [
        set(item.invariants) == set(REQUIRED_INVARIANTS) and all(item.invariants.values())
        for item in scenarios
    ]
    expected_passed = all(faults) and all(invariants) and all(item.status == "recovered" for item in scenarios)
    return receipt.get("passed") is expected_passed and bool(receipt.get("blockers")) is (not expected_passed)


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "HOSTED_CHAOS_GATE_SCHEMA",
    "REQUIRED_INVARIANTS",
    "build_hosted_chaos_gate_receipt",
    "verify_hosted_chaos_gate_receipt",
]
