from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from stock_ai.data_platform.contracts import TemporalCoordinates
from stock_ai.data_platform.service import MarketDataPlatform


def verify(database_path: Path) -> dict[str, Any]:
    platform = MarketDataPlatform(database_path=database_path)
    original_payload = {
        "report_date": "2026-05-10",
        "period": "2026-03",
        "symbol": "9000.TW",
        "name": "版本驗證公司",
        "industry": "測試",
        "current_revenue": 100.0,
        "previous_revenue": 90.0,
        "last_year_revenue": 80.0,
        "mom_change_percent": 11.11,
        "yoy_change_percent": 25.0,
        "ytd_revenue": 300.0,
        "last_ytd_revenue": 240.0,
        "ytd_change_percent": 25.0,
        "note": None,
        "source": "MOPS revision fixture",
    }
    restated_payload = {
        **original_payload,
        "report_date": "2026-05-20",
        "current_revenue": 105.0,
        "mom_change_percent": 16.67,
        "yoy_change_percent": 31.25,
        "ytd_revenue": 305.0,
        "ytd_change_percent": 27.08,
    }
    first = platform.warehouse.write_revision(
        dataset="revenues_monthly",
        entity_id="ENT-revision-verification",
        observation_key="2026-03",
        source_id="mops",
        temporal=TemporalCoordinates(
            time_basis="fiscal_period",
            fiscal_period="2026-03",
            period_start="2026-03-01",
            period_end="2026-03-31",
            observed_at="2026-03-31",
            published_at="2026-05-10T08:00:00+00:00",
            available_at="2026-05-10T08:00:00+00:00",
            acquired_at="2026-05-11T01:00:00+00:00",
            effective_at="2026-03-31",
        ),
        payload=original_payload,
        raw_payload_id=None,
    )
    second = platform.warehouse.write_revision(
        dataset="revenues_monthly",
        entity_id="ENT-revision-verification",
        observation_key="2026-03",
        source_id="mops",
        temporal=TemporalCoordinates(
            time_basis="fiscal_period",
            fiscal_period="2026-03",
            period_start="2026-03-01",
            period_end="2026-03-31",
            observed_at="2026-03-31",
            published_at="2026-05-20T08:00:00+00:00",
            available_at="2026-05-20T08:00:00+00:00",
            acquired_at="2026-05-21T01:00:00+00:00",
            effective_at="2026-03-31",
        ),
        payload=restated_payload,
        raw_payload_id=None,
    )
    early = platform.create_revision_snapshot(
        dataset="revenues_monthly",
        knowledge_at="2026-05-12T00:00:00+00:00",
        effective_at="2026-04-30",
    )
    late = platform.create_revision_snapshot(
        dataset="revenues_monthly",
        knowledge_at="2026-05-22T00:00:00+00:00",
        effective_at="2026-04-30",
    )
    repeated = platform.create_revision_snapshot(
        dataset="revenues_monthly",
        knowledge_at="2026-05-12T00:00:00+00:00",
        effective_at="2026-04-30",
    )
    early_state = platform.warehouse.revision_snapshot(early["snapshot_id"])
    late_state = platform.warehouse.revision_snapshot(late["snapshot_id"])
    history = platform.revision_history(
        dataset="revenues_monthly",
        entity_id="ENT-revision-verification",
        observation_key="2026-03",
        source_id="mops",
    )
    if early_state is None or late_state is None:
        raise RuntimeError("A persisted revision snapshot is missing")
    if early_state["items"][0]["revision_id"] != first.revision_id:
        raise RuntimeError("Early snapshot did not reconstruct revision one")
    if late_state["items"][0]["revision_id"] != second.revision_id:
        raise RuntimeError("Late snapshot did not reconstruct the restatement")
    if early_state["integrity"]["status"] != "passed":
        raise RuntimeError(f"Early snapshot integrity failed: {early_state}")
    if late_state["integrity"]["status"] != "passed":
        raise RuntimeError(f"Late snapshot integrity failed: {late_state}")
    if repeated["snapshot_id"] != early["snapshot_id"]:
        raise RuntimeError("An identical historical state produced a new snapshot ID")
    if history["status"] != "passed" or history["count"] != 2:
        raise RuntimeError(f"Revision chain integrity failed: {history}")

    immutable_errors: list[str] = []
    with sqlite3.connect(database_path) as conn:
        for statement, parameters in (
            (
                "update data_revisions set payload_json='{}' where revision_id=?",
                (first.revision_id,),
            ),
            (
                "delete from data_revisions where revision_id=?",
                (first.revision_id,),
            ),
            (
                "update data_revision_snapshots set item_count=0 where snapshot_id=?",
                (early["snapshot_id"],),
            ),
        ):
            try:
                conn.execute(statement, parameters)
            except sqlite3.IntegrityError as exc:
                immutable_errors.append(str(exc))
                conn.rollback()
            else:
                raise RuntimeError(f"Immutable revision storage accepted: {statement}")

    status = platform.status()["warehouse"]["revision_history"]
    if status["status"] != "passed":
        raise RuntimeError(f"Revision History status failed: {status}")
    return {
        "schema_version": "stock_ai.revision_history_verification.v1",
        "status": "passed",
        "revision_chain": {
            "revision_ids": [first.revision_id, second.revision_id],
            "supersedes": second.supersedes_revision_id,
            "integrity": history["status"],
        },
        "early_snapshot": {
            "snapshot_id": early["snapshot_id"],
            "manifest_hash": early["manifest_hash"],
            "revision_id": early_state["items"][0]["revision_id"],
            "revenue": early_state["items"][0]["payload"]["current_revenue"],
            "integrity": early_state["integrity"]["status"],
        },
        "late_snapshot": {
            "snapshot_id": late["snapshot_id"],
            "manifest_hash": late["manifest_hash"],
            "revision_id": late_state["items"][0]["revision_id"],
            "revenue": late_state["items"][0]["payload"]["current_revenue"],
            "integrity": late_state["integrity"]["status"],
        },
        "repeat_snapshot_id": repeated["snapshot_id"],
        "immutability_errors": immutable_errors,
        "warehouse_status": status,
        "database": str(database_path),
    }


def main() -> None:
    with TemporaryDirectory(prefix="stock-ai-revision-history-") as temp_dir:
        print(
            json.dumps(
                verify(Path(temp_dir) / "market-data.sqlite"),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
