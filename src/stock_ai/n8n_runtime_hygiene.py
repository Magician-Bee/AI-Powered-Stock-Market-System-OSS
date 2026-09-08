"""Auditable hygiene for the project-owned local n8n execution history.

The n8n public API owns deletion of execution records.  This module never
edits n8n tables: it inspects a read-only local SQLite projection, asks the
authenticated n8n API to remove only records for archived Stock AI workflows,
then verifies that the projection is empty.  The resulting receipt contains
counts and hashes only; workflow payloads, callback tokens and execution data
never leave the n8n database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


SCHEMA_VERSION = "stock_ai.n8n_execution_hygiene_receipt.v1"
_STOCK_AI_WORKFLOW_PREFIX = "Stock AI Automation "
_SECRET_MARKERS = ("secret", "token", "password", "api_key", "authorization", "credential")


@dataclass(frozen=True, slots=True)
class N8nExecution:
    """The minimal, non-secret identity needed to delete one execution."""

    execution_id: int
    workflow_id: str
    status: str


class N8nExecutionHygiene:
    """Inspect and prune stale data using the official n8n public API."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        list_workflows: Callable[[], Iterable[Mapping[str, Any]]],
        list_executions: Callable[[str], Iterable[Mapping[str, Any]]],
        delete_execution: Callable[[int], Mapping[str, Any]],
        receipt_directory: str | Path | None = None,
        listener_present: Callable[[], bool] | None = None,
    ) -> None:
        self.database_path = Path(database_path).resolve()
        self._list_workflows = list_workflows
        self._list_executions = list_executions
        self._delete_execution = delete_execution
        self._listener_present = listener_present or _n8n_listener_present
        self.receipt_directory = (
            Path(receipt_directory).resolve()
            if receipt_directory is not None
            else self.database_path.parent.parent / "maintenance-receipts"
        )

    def inspect(self) -> dict[str, Any]:
        """Return a redacted inventory for archived Stock AI workflows only."""

        return _safe_projection(self._inventory())

    def _inventory(self) -> dict[str, Any]:
        """Build the internal deletion inventory; never return it to callers."""

        workflow_ids = sorted(
            str(item.get("id") or "")
            for item in self._list_workflows()
            if bool(item.get("isArchived"))
            and str(item.get("name") or "").startswith(_STOCK_AI_WORKFLOW_PREFIX)
            and str(item.get("id") or "")
        )
        api_executions = sorted(
            {
                N8nExecution(
                    execution_id=_positive_execution_id(item.get("id")),
                    workflow_id=workflow_id,
                    status=str(item.get("status") or "unknown"),
                )
                for workflow_id in workflow_ids
                for item in self._list_executions(workflow_id)
                if str(item.get("workflowId") or workflow_id) == workflow_id
            },
            key=lambda item: item.execution_id,
        )
        database = _database_projection(self.database_path, workflow_ids)
        api_ids = [item.execution_id for item in api_executions]
        if api_ids != database["execution_ids"]:
            raise RuntimeError(
                "n8n API execution inventory does not match the local read-only database projection"
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "scope": "archived_stock_ai_workflows_only",
            "archived_workflow_count": len(workflow_ids),
            "execution_count": len(api_executions),
            "execution_status_counts": _status_counts(api_executions),
            "execution_data_bytes": database["execution_data_bytes"],
            "potential_secret_marker_rows": database["potential_secret_marker_rows"],
            "database_file_bytes": self.database_path.stat().st_size,
            "execution_ids": api_ids,
        }

    def prune_archived_stock_ai_executions(self) -> dict[str, Any]:
        """Delete precisely the inspected execution IDs, then persist a receipt.

        All actual mutations go through n8n's authenticated public API.  A
        changed inventory causes a fail-closed error rather than deleting an
        execution that was not present in the preflight inspection.
        """

        before = self._inventory()
        for execution_id in before["execution_ids"]:
            response = dict(self._delete_execution(int(execution_id)))
            if _positive_execution_id(response.get("id")) != int(execution_id):
                raise RuntimeError("n8n did not acknowledge deletion of the requested execution")
        after = self._inventory()
        if after["execution_count"] or after["potential_secret_marker_rows"]:
            raise RuntimeError("n8n execution prune did not clear the inspected archived workflow history")
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "action": "prune_archived_stock_ai_executions",
            "scope": before["scope"],
            "performed_at": datetime.now(UTC).isoformat(),
            "before": _safe_projection(before),
            "after": _safe_projection(after),
        }
        receipt["receipt_sha256"] = _sha256(receipt)
        receipt_path = self._write_receipt(receipt)
        return {**receipt, "receipt_path": str(receipt_path)}

    def _write_receipt(self, receipt: Mapping[str, Any]) -> Path:
        self.receipt_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.receipt_directory / f"n8n-execution-hygiene-{timestamp}.json"
        suffix = 1
        while path.exists():
            path = self.receipt_directory / f"n8n-execution-hygiene-{timestamp}-{suffix}.json"
            suffix += 1
        payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        path.write_text(payload, encoding="utf-8")
        path.chmod(0o600)
        return path

    def compact_database_offline(self) -> dict[str, Any]:
        """Reclaim SQLite free pages only after the local n8n listener stops."""

        if self._listener_present():
            raise RuntimeError("n8n is still listening on 127.0.0.1:5678; offline compaction is blocked")
        before_bytes = self.database_path.stat().st_size
        with sqlite3.connect(self.database_path, timeout=1.0) as connection:
            before_pages = int(connection.execute("pragma page_count").fetchone()[0])
            before_free_pages = int(connection.execute("pragma freelist_count").fetchone()[0])
            before_integrity = str(connection.execute("pragma quick_check").fetchone()[0])
            if before_integrity != "ok":
                raise RuntimeError("n8n SQLite quick_check failed before compaction")
            connection.execute("vacuum")
            after_pages = int(connection.execute("pragma page_count").fetchone()[0])
            after_free_pages = int(connection.execute("pragma freelist_count").fetchone()[0])
            after_integrity = str(connection.execute("pragma quick_check").fetchone()[0])
            if after_integrity != "ok":
                raise RuntimeError("n8n SQLite quick_check failed after compaction")
        # SQLite can retain the pre-VACUUM file size until its final connection
        # close/checkpoint. Read the physical result only after leaving the
        # connection context so the durable receipt cannot overstate usage.
        after_bytes = self.database_path.stat().st_size
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "action": "compact_n8n_sqlite_offline",
            "performed_at": datetime.now(UTC).isoformat(),
            "database_file_bytes_before": before_bytes,
            "database_file_bytes_after": after_bytes,
            "database_pages_before": before_pages,
            "database_pages_after": after_pages,
            "free_pages_before": before_free_pages,
            "free_pages_after": after_free_pages,
            "quick_check": after_integrity,
        }
        receipt["receipt_sha256"] = _sha256(receipt)
        receipt_path = self._write_receipt(receipt)
        return {**receipt, "receipt_path": str(receipt_path)}


def _database_projection(database_path: Path, workflow_ids: list[str]) -> dict[str, Any]:
    if not workflow_ids:
        return {
            "execution_ids": [],
            "execution_data_bytes": 0,
            "potential_secret_marker_rows": 0,
        }
    placeholders = ",".join("?" for _ in workflow_ids)
    uri = f"file:{database_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            f"""
            select e.id, coalesce(length(d.workflowData), 0) + coalesce(length(d.data), 0) as data_bytes,
                   lower(coalesce(d.workflowData, '') || char(10) || coalesce(d.data, '')) as payload
              from execution_entity e
              join workflow_entity w on w.id=e.workflowId
              left join execution_data d on d.executionId=e.id
             where w.id in ({placeholders})
             order by e.id
            """,
            workflow_ids,
        ).fetchall()
    return {
        "execution_ids": [int(row[0]) for row in rows],
        "execution_data_bytes": sum(int(row[1]) for row in rows),
        "potential_secret_marker_rows": sum(
            any(marker in str(row[2]) for marker in _SECRET_MARKERS) for row in rows
        ),
    }


def _positive_execution_id(value: Any) -> int:
    try:
        execution_id = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("n8n returned an invalid execution identifier") from exc
    if execution_id <= 0:
        raise RuntimeError("n8n returned an invalid execution identifier")
    return execution_id


def _status_counts(executions: Iterable[N8nExecution]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for execution in executions:
        counts[execution.status] = counts.get(execution.status, 0) + 1
    return dict(sorted(counts.items()))


def _safe_projection(projection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in projection.items()
        if key not in {"execution_ids", "database_file_bytes"}
    }


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _n8n_listener_present() -> bool:
    """Check the project-local n8n control-plane port without exposing PIDs."""

    result = subprocess.run(
        ["lsof", "-nP", "-iTCP:5678", "-sTCP:LISTEN"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0
