from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from open_stock_ai.agent_runtime.hosted_session_archive_gate import (
    verify_hosted_session_archive_gate_receipt,
)
from open_stock_ai.agent_runtime.session_archival import (
    load_materialized_session_archive,
)
from scripts.run_hosted_session_archive_gate import _create, _verify


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "8" * 40


def test_hosted_session_archive_round_trip_materializes_full_lineage(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    restore_dir = tmp_path / "restore"
    evidence = _create(artifact, COMMIT, "601")

    assert "must-not-leak" not in json.dumps(evidence)
    gate = _verify(artifact, restore_dir, COMMIT, "601")

    assert gate["passed"] is True
    assert gate["quick_check"] == "ok"
    assert gate["overwrite_refused"] is True
    assert gate["materialized_archive_verified"] is True
    assert verify_hosted_session_archive_gate_receipt(gate) is True
    restored = load_materialized_session_archive(
        restore_dir / "restored-session.sqlite", session_id="AS-hosted-archive"
    )
    assert restored == evidence["archive"]
    assert restored["runs"][0]["events"][0]["event_id"] == "ARE-hosted-complete"
    with sqlite3.connect(restore_dir / "restored-session.sqlite") as connection:
        assert connection.execute("pragma quick_check").fetchone()[0] == "ok"


def test_clean_runner_rejects_tampered_session_archive(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    _create(artifact, COMMIT, "601")
    archive_path = artifact / "session-archive.json"
    archive = json.loads(archive_path.read_text())
    archive["messages"][0]["content"]["text"] = "tampered"
    archive_path.write_text(json.dumps(archive))

    with pytest.raises(SystemExit, match="evidence mismatch"):
        _verify(artifact, tmp_path / "restore", COMMIT, "601")


def test_hosted_session_workflow_uses_two_runners_and_no_model() -> None:
    workflow = (ROOT / ".github/workflows/session-archive-restore.yml").read_text()
    script = (ROOT / "scripts/run_hosted_session_archive_gate.py").read_text()

    assert "export-session-archive:" in workflow
    assert "clean-runner-session-restore:" in workflow
    assert "needs: export-session-archive" in workflow
    assert workflow.count("retention-days: 90") == 2
    assert "actions/upload-artifact@v4" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "--mode create" in workflow and "--mode verify" in workflow
    assert "GITHUB_ACTIONS" in script
    assert "--allow-session-archive-drill" in script
    assert "ollama" not in script.lower()
    assert "openai" not in script.lower()
