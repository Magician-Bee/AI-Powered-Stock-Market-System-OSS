from __future__ import annotations

import copy
import sqlite3

import pytest
from fastapi.testclient import TestClient

from open_stock_ai.agent_runtime.session_archival import (
    MATERIALIZED_ARCHIVE_TABLE,
    load_materialized_session_archive,
    restore_session_archive,
    verify_session_archive,
)
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.durable_agent_runtime import DurableAgentRuntime
from stock_ai.main import app


def _runtime(tmp_path):
    database = tmp_path / "session-archive.sqlite"
    sessions = AgentSessionStore(database)
    sessions.create(
        session_id="AS-archive",
        namespace="stock-ai",
        title="Archive me",
        metadata={"api_key": "must-not-leak", "purpose": "lineage"},
    )
    runs = AgentRunStore(database)
    runs.create_run(
        "AR-archive",
        {
            "objective": "preserve the decision lineage",
            "driver_id": "codex",
            "autonomy": "advisory",
            "session_id": "AS-archive",
        },
    )
    sessions.add_message(
        session_id="AS-archive",
        role="user",
        content={"text": "Keep this decision trace", "token": "must-not-leak"},
        run_id="AR-archive",
    )
    return DurableAgentRuntime(service_provider=lambda: None, store=runs)


def test_session_archive_contains_full_run_snapshot_and_verifies_cold_restore(tmp_path):
    runtime = _runtime(tmp_path)

    archive = runtime.export_session_archive("AS-archive")
    assert archive is not None
    assert archive["schema_version"] == "open_stock_ai.session_archive.v1"
    assert archive["runs"][0]["run"]["run_id"] == "AR-archive"
    assert archive["runs"][0]["events"] == []
    assert archive["messages"][0]["content"]["token"] == "[redacted]"
    assert archive["session"]["metadata"]["api_key"] == "[redacted]"

    verified = verify_session_archive(archive, expected_session_id="AS-archive")
    restored = runtime.restore_session_archive(archive, expected_session_id="AS-archive")
    assert verified["lineage_sha256"] == archive["lineage"]["lineage_sha256"]
    assert restored["restore_mode"] == "cold_projection"
    assert restored["read_only"] is True
    assert restored["restored"] is True
    assert restored["run_ids"] == ["AR-archive"]


def test_session_archive_rejects_tampering_and_cross_session_restore(tmp_path):
    runtime = _runtime(tmp_path)
    archive = runtime.export_session_archive("AS-archive")
    assert archive is not None

    tampered = copy.deepcopy(archive)
    tampered["messages"][0]["content"]["text"] = "tampered"
    with pytest.raises(ValueError, match="content hash mismatch"):
        restore_session_archive(tampered)
    with pytest.raises(ValueError, match="different Session"):
        restore_session_archive(archive, expected_session_id="AS-other")


def test_session_archive_api_exports_and_restores_verified_projection(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    monkeypatch.setattr("stock_ai.agent_api.get_agent_run_runtime", lambda: runtime)
    client = TestClient(app)

    exported = client.get("/api/agents/sessions/AS-archive/archive")
    assert exported.status_code == 200
    archive = exported.json()

    restored = client.post("/api/agents/sessions/AS-archive/restore", json=archive)
    assert restored.status_code == 200
    assert restored.json()["restore_mode"] == "cold_projection"

    mismatch = client.post("/api/agents/sessions/AS-other/restore", json=archive)
    assert mismatch.status_code == 422


def test_session_archive_materializes_fresh_authoritative_database_without_overwrite(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.store.append_event(
        "AR-archive",
        {"sequence": 1, "type": "decision.completed", "payload": {"artifact_id": "AA-lineage"}},
    )
    archive = runtime.export_session_archive("AS-archive")
    assert archive is not None

    target = tmp_path / "cold-restore.sqlite"
    receipt = runtime.materialize_session_archive(
        archive,
        str(target),
        expected_session_id="AS-archive",
    )
    assert receipt["schema_version"] == "open_stock_ai.session_materialize.v1"
    assert receipt["restore_mode"] == "fresh_sqlite_authoritative"
    assert AgentSessionStore(target).get("AS-archive")["session_id"] == "AS-archive"
    assert AgentSessionStore(target).messages("AS-archive")[0]["message_id"] == archive["messages"][0]["message_id"]
    assert AgentRunStore(target).get_run("AR-archive")["run_id"] == "AR-archive"
    assert receipt["archive_envelope"]["preserves_full_run_snapshots"] is True
    restored_archive = load_materialized_session_archive(target, session_id="AS-archive")
    assert restored_archive == archive
    assert restored_archive["runs"][0]["events"] == archive["runs"][0]["events"]

    with sqlite3.connect(target) as connection, pytest.raises(sqlite3.DatabaseError):
        connection.execute(f"update {MATERIALIZED_ARCHIVE_TABLE} set archive_json='{{}}'")

    with pytest.raises(ValueError, match="refuses to overwrite"):
        runtime.materialize_session_archive(archive, str(target), expected_session_id="AS-archive")
