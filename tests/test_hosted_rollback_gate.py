from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from open_stock_ai.governance import (
    ApprovedArtifactRollbackRegistry,
    build_hosted_rollback_gate_receipt,
    verify_hosted_rollback_gate_receipt,
)


T0 = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _observations() -> dict:
    results = {}
    for scope, first_hash, second_hash in (("strategy", "a" * 64, "b" * 64), ("model", "c" * 64, "d" * 64)):
        registry = ApprovedArtifactRollbackRegistry()
        for version, digest in ((1, first_hash), (2, second_hash)):
            registry.approve(
                f"{scope}-v{version}",
                digest,
                approved_by="owner",
                approved_at=T0.isoformat(),
                metadata={"artifact_scope": scope},
            )
            registry.activate(f"{scope}-v{version}")
        receipt = registry.rollback(
            artifact_scope=scope,
            reason=f"{scope}_rollback_drill",
            approved_by="owner",
            now=T0,
        )
        results[scope] = {
            "before_artifact_id": f"{scope}-v2",
            "after_artifact_id": f"{scope}-v1",
            "durable_after_restart": f"{scope}-v1",
            "other_lane_unchanged": True,
            "http_status": 200,
            "rollback_receipt": receipt.as_dict(),
        }
    return {
        "immutable_conflict_rejected": True,
        "unapproved_activation_rejected": True,
        "agent_rollback_rejected": True,
        "database_quick_check": "ok",
        "scope_results": results,
    }


def test_hosted_rollback_gate_verifies_strategy_and_model_receipts() -> None:
    receipt = build_hosted_rollback_gate_receipt(
        _observations(),
        commit_sha="a" * 40,
        server_pid=1234,
        clock=lambda: T0,
    )
    assert receipt["passed"] is True
    assert receipt["blockers"] == []
    assert verify_hosted_rollback_gate_receipt(receipt) is True

    tampered = json.loads(json.dumps(receipt))
    tampered["scope_results"][0]["after_artifact_id"] = "strategy-v2"
    assert verify_hosted_rollback_gate_receipt(tampered) is False


def test_hosted_rollback_gate_fails_closed_without_a_guard() -> None:
    observations = _observations()
    observations["agent_rollback_rejected"] = False
    receipt = build_hosted_rollback_gate_receipt(observations, commit_sha="b" * 40, server_pid=1234)
    assert receipt["passed"] is False
    assert receipt["blockers"] == ["rollback_guard_failed:agent_rollback_rejected"]
    assert verify_hosted_rollback_gate_receipt(receipt) is True


def test_hosted_rollback_workflow_runs_real_api_and_retains_evidence() -> None:
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/artifact-rollback-drill.yml").read_text(encoding="utf-8")
    assert "python scripts/run_hosted_rollback_gate.py" in workflow
    assert "--commit-sha \"${GITHUB_SHA}\"" in workflow
    assert "--allow-rollback-drill" in workflow
    assert "verify_hosted_rollback_gate_receipt" in workflow
    assert "retention-days: 30" in workflow
    harness = (root / "scripts/run_hosted_rollback_gate.py").read_text(encoding="utf-8")
    for marker in ("uvicorn", 'for scope in ("strategy", "model")', "PRAGMA quick_check", "approved_by"):
        assert marker in harness
