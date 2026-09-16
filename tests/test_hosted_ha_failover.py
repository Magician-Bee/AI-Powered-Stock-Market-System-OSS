from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from open_stock_ai.governance import (
    HA_SERVICES,
    SQLiteLeaderLease,
    build_hosted_ha_failover_receipt,
    verify_hosted_ha_failover_receipt,
)


COMMIT = "a" * 40


def _receipt_dict(receipt) -> dict:
    return {**receipt.payload(), "receipt_sha256": receipt.receipt_sha256}


def _observations(tmp_path) -> dict:
    now = [datetime(2026, 9, 7, tzinfo=timezone.utc)]
    store = SQLiteLeaderLease(tmp_path / "ha.sqlite", ttl_seconds=1, clock=lambda: now[0])
    result = {}
    for index, service in enumerate(HA_SERVICES, start=1):
        primary = store.claim(service, f"{service}-primary")
        now[0] += timedelta(seconds=2)
        standby = store.claim(service, f"{service}-standby")
        result[service] = {
            "primary_pid": 1000 + index,
            "standby_pid": 2000 + index,
            "failover_seconds": 1.05,
            "primary_receipt": _receipt_dict(primary),
            "standby_receipt": _receipt_dict(standby),
            "separate_processes": True,
            "primary_killed": True,
            "standby_acquired": True,
            "epoch_advanced": True,
            "stale_owner_fenced": True,
            "durable_state_recovered": True,
        }
    return result


def test_hosted_ha_receipt_requires_every_service_and_verifies_hash(tmp_path) -> None:
    receipt = build_hosted_ha_failover_receipt(
        _observations(tmp_path),
        commit_sha=COMMIT,
        database_quick_check="ok",
        clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    assert receipt["passed"] is True
    assert receipt["blockers"] == []
    assert [entry["service"] for entry in receipt["failovers"]] == list(HA_SERVICES)
    assert verify_hosted_ha_failover_receipt(receipt) is True

    tampered = json.loads(json.dumps(receipt))
    tampered["failovers"][0]["stale_owner_fenced"] = False
    assert verify_hosted_ha_failover_receipt(tampered) is False


def test_hosted_ha_receipt_fails_closed_on_missing_service_or_bad_database(tmp_path) -> None:
    observations = _observations(tmp_path)
    observations.pop("scheduler")
    receipt = build_hosted_ha_failover_receipt(
        observations,
        commit_sha=COMMIT,
        database_quick_check="corrupt",
    )
    assert receipt["passed"] is False
    assert "failover_invariant_failed:scheduler" in receipt["blockers"]
    assert "invalid_lease_transition:scheduler" in receipt["blockers"]
    assert "ha_database_quick_check_failed" in receipt["blockers"]
    assert verify_hosted_ha_failover_receipt(receipt) is False


def test_hosted_ha_workflow_runs_real_multi_process_failover_and_retains_evidence() -> None:
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/ha-failover.yml").read_text(encoding="utf-8")
    assert "python scripts/run_hosted_ha_failover.py" in workflow
    assert "--commit-sha \"${GITHUB_SHA}\"" in workflow
    assert "--allow-process-kill" in workflow
    assert "verify_hosted_ha_failover_receipt" in workflow
    assert "retention-days: 30" in workflow
    harness = (root / "scripts/run_hosted_ha_failover.py").read_text(encoding="utf-8")
    for marker in ("SIGKILL", 'role="primary"', 'role="standby"', "stale_fenced", "PRAGMA quick_check"):
        assert marker in harness
