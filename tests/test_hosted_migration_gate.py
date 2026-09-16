from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_stock_ai.storage.hosted_migration_gate import verify_hosted_migration_gate_receipt
from open_stock_ai.storage.migration_safety import verify_migration_cleanup_report
from open_stock_ai.storage.sqlite_backup import SQLiteBackupError
from scripts.run_hosted_migration_gate import _create, _verify


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "2" * 40


def test_hosted_migration_drill_executes_cleanup_and_reverifies_off_host_pairs(tmp_path: Path) -> None:
    work = tmp_path / "work"
    artifact = tmp_path / "artifact"
    verification = tmp_path / "verification"

    _create(work, artifact, COMMIT, "84")
    gate = _verify(artifact, verification, COMMIT, "84")

    assert gate["passed"] is True
    assert gate["blockers"] == []
    assert gate["retained_completed_count"] == 2
    assert gate["prepared_pairs_preserved"] == 1
    assert gate["unknown_files_preserved"] == 1
    assert verify_hosted_migration_gate_receipt(gate) is True
    gate["passed"] = False
    assert verify_hosted_migration_gate_receipt(gate) is False
    assert {path.name for path in artifact.iterdir()} == {
        "migration-evidence.json",
        "migration-manifest.json",
        "retained-1.sqlite",
        "retained-1.receipt.json",
        "retained-2.sqlite",
        "retained-2.receipt.json",
        "retained-3.sqlite",
        "retained-3.receipt.json",
    }


def test_clean_runner_verification_rejects_tampered_retained_backup(tmp_path: Path) -> None:
    work = tmp_path / "work"
    artifact = tmp_path / "artifact"
    _create(work, artifact, COMMIT, "84")
    with (artifact / "retained-1.sqlite").open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(SQLiteBackupError, match="hash_mismatch"):
        _verify(artifact, tmp_path / "verification", COMMIT, "84")


def test_migration_cleanup_report_verifier_rejects_tampering(tmp_path: Path) -> None:
    work = tmp_path / "work"
    artifact = tmp_path / "artifact"
    _create(work, artifact, COMMIT, "84")
    evidence = json.loads((artifact / "migration-evidence.json").read_text(encoding="utf-8"))

    report = evidence["execution_report"]
    assert verify_migration_cleanup_report(report) is True
    report["retention_count"] = 1
    assert verify_migration_cleanup_report(report) is False


def test_hosted_migration_workflow_uses_two_runners_schedule_and_long_retention() -> None:
    workflow = (ROOT / ".github" / "workflows" / "migration-retention.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "run_hosted_migration_gate.py").read_text(encoding="utf-8")

    assert "migration-and-cleanup:" in workflow
    assert "clean-runner-verify:" in workflow
    assert "needs: migration-and-cleanup" in workflow
    assert 'cron: "41 4 * * 0"' in workflow
    assert workflow.count("retention-days: 90") == 2
    assert "actions/upload-artifact@v4" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "verify_hosted_migration_gate_receipt" in workflow
    assert "--mode create" in workflow
    assert "--mode verify" in workflow
    assert "GITHUB_ACTIONS" in script
    assert "--allow-migration-retention-drill" in script
