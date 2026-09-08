from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from open_stock_ai.governance.content_retention import (
    ContentAddressedRetentionLedger,
    RetentionError,
)
from open_stock_ai.governance.durable_store import SQLiteRetentionStore
from open_stock_ai.governance.hosted_retention_gate import (
    REQUIRED_CRITICAL_SCHEMAS,
    verify_hosted_retention_gate_receipt,
)
from open_stock_ai.governance.retention_archive import (
    build_critical_retention_archive,
    restore_critical_retention_archive,
    verify_critical_retention_archive,
    verify_critical_retention_restore,
)
from scripts.run_hosted_retention_archive_gate import _create, _verify


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "7" * 40


def _ledger(path: Path) -> ContentAddressedRetentionLedger:
    return ContentAddressedRetentionLedger(store=SQLiteRetentionStore(path))


def test_critical_archive_excludes_bounded_signals_and_restores_without_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    ledger = _ledger(source)
    ledger.append(
        "signal-1",
        {"score": 1},
        critical=False,
        occurred_at="2026-09-07T00:00:00+00:00",
    )
    retained = ledger.append(
        "order-1",
        {"schema_version": "stock_ai.paper_oms_execution_snapshot.v1", "order_id": "O-1"},
        critical=True,
        kind="execution_oms_state",
        occurred_at="2026-09-07T00:01:00+00:00",
    )
    maintenance = ledger.prune_signals()

    archive = build_critical_retention_archive(
        SQLiteRetentionStore(source),
        source_authority="runtime:test",
        created_at="2026-09-07T00:02:00+00:00",
    )

    assert verify_critical_retention_archive(archive) is True
    assert archive["critical_count"] == 1
    assert archive["records"] == [retained]
    assert archive["retention_receipts"] == [maintenance.as_dict()]
    assert "signal-1" not in json.dumps(archive)

    target = tmp_path / "restored.sqlite"
    receipt = restore_critical_retention_archive(
        archive, target, restored_at="2026-09-07T00:03:00+00:00"
    )
    assert verify_critical_retention_restore(receipt, archive) is True
    assert _ledger(target).get("order-1") == retained
    with sqlite3.connect(target) as connection:
        assert connection.execute("pragma quick_check").fetchone()[0] == "ok"
    with pytest.raises(RetentionError, match="target_exists"):
        restore_critical_retention_archive(archive, target)


def test_archive_and_restore_verifiers_reject_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    ledger = _ledger(source)
    ledger.append(
        "order-1",
        {"schema_version": "stock_ai.paper_oms_execution_snapshot.v1", "order_id": "O-1"},
        critical=True,
        kind="execution_oms_state",
        occurred_at="2026-09-07T00:01:00+00:00",
    )
    archive = build_critical_retention_archive(
        SQLiteRetentionStore(source), source_authority="runtime:test"
    )
    receipt = restore_critical_retention_archive(archive, tmp_path / "restored.sqlite")

    archive["records"][0]["payload"]["order_id"] = "tampered"
    assert verify_critical_retention_archive(archive) is False
    assert verify_critical_retention_restore(receipt, archive) is False


def test_hosted_retention_archive_round_trip_is_explicitly_not_years_long(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact"
    verification = tmp_path / "verification"
    evidence = _create(tmp_path / "source.sqlite", artifact, COMMIT, "501")

    assert evidence["source_signal_count_after_prune"] == 2
    gate = _verify(artifact, verification, COMMIT, "501")

    assert gate["passed"] is True
    assert gate["production_years_satisfied"] is False
    assert gate["scope"] == "hosted_drill_only"
    assert gate["artifact_retention_days"] == 90
    assert verify_hosted_retention_gate_receipt(gate) is True
    assert set(REQUIRED_CRITICAL_SCHEMAS).issubset(
        {item["payload"]["schema_version"] for item in gate["archive"]["records"]}
    )
    gate["production_years_satisfied"] = True
    assert verify_hosted_retention_gate_receipt(gate) is False


def test_clean_runner_rejects_tampered_archive_file(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    _create(tmp_path / "source.sqlite", artifact, COMMIT, "501")
    archive = json.loads((artifact / "critical-retention-archive.json").read_text())
    archive["records"][0]["payload"]["order_id"] = "tampered"
    (artifact / "critical-retention-archive.json").write_text(json.dumps(archive))

    with pytest.raises(SystemExit, match="archive verification failed"):
        _verify(artifact, tmp_path / "verification", COMMIT, "501")


def test_hosted_retention_workflow_uses_fresh_runner_and_truthful_retention() -> None:
    workflow = (ROOT / ".github/workflows/critical-retention-archive.yml").read_text()
    script = (ROOT / "scripts/run_hosted_retention_archive_gate.py").read_text()

    assert "export-critical-retention:" in workflow
    assert "clean-runner-restore:" in workflow
    assert "needs: export-critical-retention" in workflow
    assert 'cron: "23 5 * * 0"' in workflow
    assert workflow.count("retention-days: 90") == 2
    assert "actions/upload-artifact@v4" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "production_years_satisfied" in workflow
    assert "--mode create" in workflow and "--mode verify" in workflow
    assert "GITHUB_ACTIONS" in script
    assert "--allow-retention-archive-drill" in script
    assert "ollama" not in script.lower()
    assert "openai" not in script.lower()
