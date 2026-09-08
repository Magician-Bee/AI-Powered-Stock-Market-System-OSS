from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from open_stock_ai.storage.backup_policy import backup_policy_from_mapping, evaluate_backup_schedule
from open_stock_ai.storage.hosted_backup_gate import (
    HOSTED_BACKUP_GATE_SCHEMA,
    build_hosted_backup_gate_receipt,
    verify_hosted_backup_gate_receipt,
)
from open_stock_ai.storage.sqlite_backup import (
    SQLiteBackupError,
    create_sqlite_backup,
    restore_sqlite_backup,
    verify_sqlite_backup,
)
from scripts.run_hosted_backup_gate import _create


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "1" * 40


def _content(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("select id, symbol from decisions order by id").fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _evidence(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "authoritative.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("create table decisions (id integer primary key, symbol text not null)")
        connection.executemany("insert into decisions(symbol) values (?)", [("2330.TW",), ("0050.TW",)])
    backup = tmp_path / "authoritative.backup.sqlite"
    backup_receipt = create_sqlite_backup(source, backup)
    policy = backup_policy_from_mapping(
        {
            "interval_hours": 24,
            "retention_count": 14,
            "destination_uri": f"github-actions-artifact://owner/repo/runs/42/stock-ai-sqlite-backup-{COMMIT}",
            "off_host_required": True,
            "owner": "platform-storage",
        }
    )
    policy_decision = evaluate_backup_schedule(policy, last_success_at=None)
    restored = tmp_path / "clean-runner" / "restored.sqlite"
    verification = verify_sqlite_backup(backup, backup_receipt)
    restoration = restore_sqlite_backup(backup, restored, backup_receipt)
    protected = tmp_path / "clean-runner" / "protected.sqlite"
    protected.write_bytes(b"caller-owned")
    refused = False
    try:
        restore_sqlite_backup(backup, protected, backup_receipt)
    except SQLiteBackupError as exc:
        refused = "target_already_exists" in str(exc)
    return {
        "backup_job": "create-backup",
        "restore_job": "clean-runner-restore",
        "off_host_artifact": f"stock-ai-sqlite-backup-{COMMIT}",
        "policy_decision": policy_decision,
        "backup_receipt": backup_receipt.as_dict(),
        "restoration": {**verification, **restoration},
        "source_content_sha256": _content(source),
        "restored_content_sha256": _content(restored),
        "existing_target_refused": refused,
    }


def test_hosted_backup_gate_binds_off_host_policy_clean_restore_and_content(tmp_path: Path) -> None:
    receipt = build_hosted_backup_gate_receipt(_evidence(tmp_path), commit_sha=COMMIT, run_id="42")

    assert receipt["schema_version"] == HOSTED_BACKUP_GATE_SCHEMA
    assert receipt["passed"] is True
    assert receipt["blockers"] == []
    assert verify_hosted_backup_gate_receipt(receipt) is True


def test_hosted_backup_gate_fails_closed_for_content_or_receipt_tampering(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    evidence["restored_content_sha256"] = "f" * 64
    receipt = build_hosted_backup_gate_receipt(evidence, commit_sha=COMMIT, run_id="42")
    assert receipt["passed"] is False
    assert receipt["blockers"] == ["off_host_backup_restore_invariant_failed"]
    assert verify_hosted_backup_gate_receipt(receipt) is True

    receipt["passed"] = True
    assert verify_hosted_backup_gate_receipt(receipt) is False


def test_hosted_backup_workflow_uses_two_jobs_and_retained_artifact() -> None:
    workflow = (ROOT / ".github" / "workflows" / "sqlite-backup-restore.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts" / "run_hosted_backup_gate.py").read_text(encoding="utf-8")

    assert "create-backup:" in workflow
    assert "clean-runner-restore:" in workflow
    assert 'cron: "17 3 * * *"' in workflow
    assert "needs: create-backup" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert workflow.count("retention-days: 30") == 2
    assert "test ! -e \"${RESTORE_DIR}\"" in workflow
    assert "verify_hosted_backup_gate_receipt" in workflow
    assert "--mode backup" in workflow
    assert "--mode restore" in workflow
    assert "GITHUB_ACTIONS" in script
    assert "--allow-off-host-drill" in script
    assert "off-host-artifact/authoritative.backup.sqlite" in workflow
    assert "off-host-artifact/authoritative.sqlite-wal" not in workflow


def test_backup_artifact_contains_only_verified_restore_inputs(tmp_path: Path) -> None:
    output = tmp_path / "artifact"
    _create(output, COMMIT, "42", "owner/repo")

    assert {path.name for path in output.iterdir()} == {
        "authoritative.backup.sqlite",
        "backup-receipt.json",
        "backup-policy-decision.json",
        "source-manifest.json",
    }
