from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from stock_ai.n8n_runtime_hygiene import N8nExecutionHygiene, SCHEMA_VERSION


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            create table workflow_entity (id text primary key, name text not null, isArchived integer not null);
            create table execution_entity (id integer primary key, workflowId text not null, status text not null);
            create table execution_data (executionId integer primary key, workflowData text not null, data text not null);
            insert into workflow_entity values
                ('archived-stock-ai', 'Stock AI Automation AUT-old', 1),
                ('archived-other', 'Unrelated workflow', 1),
                ('active-stock-ai', 'Stock AI Automation AUT-active', 0);
            insert into execution_entity values
                (11, 'archived-stock-ai', 'error'),
                (12, 'archived-stock-ai', 'success'),
                (13, 'archived-other', 'error'),
                (14, 'active-stock-ai', 'success');
            insert into execution_data values
                (11, '{"workflow":"old"}', '{"callback_secret":"must not leave the database"}'),
                (12, '{"workflow":"old"}', '{"result":"safe"}'),
                (13, '{"workflow":"other"}', '{"secret":"unrelated"}'),
                (14, '{"workflow":"active"}', '{"result":"active"}');
            """
        )


def _hygiene(database: Path, receipts: Path) -> tuple[N8nExecutionHygiene, list[int]]:
    executions = {
        "archived-stock-ai": [
            {"id": 11, "workflowId": "archived-stock-ai", "status": "error"},
            {"id": 12, "workflowId": "archived-stock-ai", "status": "success"},
        ],
        "archived-other": [{"id": 13, "workflowId": "archived-other", "status": "error"}],
        "active-stock-ai": [{"id": 14, "workflowId": "active-stock-ai", "status": "success"}],
    }
    deleted: list[int] = []

    def delete(execution_id: int) -> dict[str, int]:
        deleted.append(execution_id)
        for workflow_id, values in executions.items():
            executions[workflow_id] = [item for item in values if item["id"] != execution_id]
        with sqlite3.connect(database) as connection:
            connection.execute("delete from execution_data where executionId=?", (execution_id,))
            connection.execute("delete from execution_entity where id=?", (execution_id,))
        return {"id": execution_id}

    return (
        N8nExecutionHygiene(
            database,
            list_workflows=lambda: [
                {"id": "archived-stock-ai", "name": "Stock AI Automation AUT-old", "isArchived": True},
                {"id": "archived-other", "name": "Unrelated workflow", "isArchived": True},
                {"id": "active-stock-ai", "name": "Stock AI Automation AUT-active", "isArchived": False},
            ],
            list_executions=lambda workflow_id: executions[workflow_id],
            delete_execution=delete,
            receipt_directory=receipts,
        ),
        deleted,
    )


def test_inspection_is_limited_to_archived_stock_ai_workflows_and_never_returns_payload(tmp_path):
    database = tmp_path / "database.sqlite"
    _database(database)
    hygiene, _ = _hygiene(database, tmp_path / "receipts")

    result = hygiene.inspect()

    assert result["schema_version"] == SCHEMA_VERSION
    assert result["scope"] == "archived_stock_ai_workflows_only"
    assert result["archived_workflow_count"] == 1
    assert result["execution_status_counts"] == {"error": 1, "success": 1}
    assert result["potential_secret_marker_rows"] == 1
    assert "execution_ids" not in result
    assert "must not leave" not in repr(result)
    assert "Unrelated workflow" not in repr(result)


def test_prune_uses_api_acknowledgements_and_persists_a_secret_free_receipt(tmp_path):
    database = tmp_path / "database.sqlite"
    _database(database)
    hygiene, deleted = _hygiene(database, tmp_path / "receipts")

    receipt = hygiene.prune_archived_stock_ai_executions()

    assert deleted == [11, 12]
    assert receipt["before"]["execution_count"] == 2
    assert receipt["before"]["potential_secret_marker_rows"] == 1
    assert receipt["after"]["execution_count"] == 0
    assert receipt["after"]["potential_secret_marker_rows"] == 0
    receipt_path = Path(receipt["receipt_path"])
    assert receipt_path.exists()
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["receipt_sha256"] == receipt["receipt_sha256"]
    assert "must not leave" not in receipt_path.read_text(encoding="utf-8")
    with sqlite3.connect(database) as connection:
        assert connection.execute("select count(*) from execution_entity where workflowId='archived-stock-ai'").fetchone()[0] == 0
        assert connection.execute("select count(*) from execution_entity where workflowId='archived-other'").fetchone()[0] == 1
        assert connection.execute("select count(*) from execution_entity where workflowId='active-stock-ai'").fetchone()[0] == 1


def test_prune_fails_closed_when_api_and_database_inventory_disagree(tmp_path):
    database = tmp_path / "database.sqlite"
    _database(database)
    hygiene, _ = _hygiene(database, tmp_path / "receipts")
    hygiene._list_executions = lambda _workflow_id: []  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="does not match"):
        hygiene.inspect()


def test_offline_compaction_reclaims_free_pages_and_refuses_a_live_listener(tmp_path):
    database = tmp_path / "database.sqlite"
    _database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("create table filler (value blob)")
        connection.execute("insert into filler values (zeroblob(1048576))")
        connection.execute("delete from filler")
    hygiene, _ = _hygiene(database, tmp_path / "receipts")
    hygiene._listener_present = lambda: True  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="still listening"):
        hygiene.compact_database_offline()

    hygiene._listener_present = lambda: False  # type: ignore[method-assign]
    receipt = hygiene.compact_database_offline()

    assert receipt["action"] == "compact_n8n_sqlite_offline"
    assert receipt["quick_check"] == "ok"
    assert receipt["free_pages_after"] == 0
    assert receipt["database_file_bytes_after"] < receipt["database_file_bytes_before"]
