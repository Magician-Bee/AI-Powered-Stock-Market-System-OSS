from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from open_stock_ai.governance import CHAOS_SCENARIOS
from open_stock_ai.governance.hosted_chaos_gate import (
    build_hosted_chaos_gate_receipt,
    verify_hosted_chaos_gate_receipt,
)


def _observations() -> dict[str, dict[str, object]]:
    return {
        scenario: {
            "fault_observed": True,
            "invariants": {
                "durable_state_recovered": True,
                "new_orders_blocked_until_safe": True,
                "operator_receipt_written": True,
            },
            "fault_detail": f"observed-{scenario}",
        }
        for scenario in CHAOS_SCENARIOS
    }


def test_hosted_chaos_gate_binds_all_real_fault_observations() -> None:
    receipt = build_hosted_chaos_gate_receipt(
        _observations(),
        commit_sha="a" * 40,
        clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    assert receipt["passed"] is True
    assert receipt["blockers"] == []
    assert [item["scenario"] for item in receipt["scenarios"]] == list(CHAOS_SCENARIOS)
    assert verify_hosted_chaos_gate_receipt(receipt) is True

    receipt["scenarios"][0]["recovery"]["fault_observed"] = False
    assert verify_hosted_chaos_gate_receipt(receipt) is False


def test_hosted_chaos_gate_fails_closed_for_missing_fault_or_invariant() -> None:
    observations = _observations()
    observations["disk_full"]["fault_observed"] = False
    observations["timeout"]["invariants"]["durable_state_recovered"] = False  # type: ignore[index]
    receipt = build_hosted_chaos_gate_receipt(observations, commit_sha="b" * 40)
    assert receipt["passed"] is False
    assert "fault_not_observed:disk_full" in receipt["blockers"]
    assert "recovery_invariant_failed:timeout" in receipt["blockers"]
    assert verify_hosted_chaos_gate_receipt(receipt) is True


def test_hosted_workflow_injects_all_four_faults_and_retains_receipts() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/chaos-recovery.yml").read_text()
    assert "python scripts/run_hosted_chaos_gate.py" in workflow
    assert "--commit-sha \"${GITHUB_SHA}\"" in workflow
    assert "--allow-privileged" in workflow
    assert "verify_hosted_chaos_gate_receipt" in workflow
    assert "retention-days: 30" in workflow
    harness = (Path(__file__).parents[1] / "scripts/run_hosted_chaos_gate.py").read_text()
    for marker in ("SIGKILL", "iptables", "tmpfs", "TimeoutExpired"):
        assert marker in harness
