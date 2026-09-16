from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from stock_ai.data_platform.incremental import IncrementalLoader
from stock_ai.data_platform.service import MarketDataPlatform


def verify(database_path: Path) -> dict[str, Any]:
    platform = MarketDataPlatform(database_path=database_path)
    loader = IncrementalLoader(platform)
    fetched_cursors: list[str | None] = []
    persisted_ids: list[str] = []
    pages = {
        None: ([{"id": "one"}], "cursor-1", {"has_more": True, "page": 1}),
        "cursor-1": (
            [{"id": "two"}],
            "cursor-2",
            {"has_more": True, "page": 2},
        ),
        "cursor-2": (
            [{"id": "three"}],
            "cursor-3",
            {"has_more": False, "page": 3},
        ),
    }

    def fetch(cursor: str | None):
        fetched_cursors.append(cursor)
        return pages[cursor]

    def persist(records: list[dict[str, Any]], _acquired_at: str) -> None:
        persisted_ids.extend(str(record["id"]) for record in records)

    paused = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="verification",
        fetch=fetch,
        persist=persist,
        max_batches=2,
    )
    resumed = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="verification",
        fetch=fetch,
        persist=persist,
    )
    fresh = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="verification",
        fetch=fetch,
        persist=persist,
    )

    if paused["status"] != "paused" or paused["cursor"] != "cursor-2":
        raise RuntimeError(f"Controlled pause did not commit page two: {paused}")
    if resumed["status"] != "succeeded" or resumed["previous_cursor"] != "cursor-2":
        raise RuntimeError(f"Resume did not start at the committed cursor: {resumed}")
    if fresh["status"] != "skipped_fresh":
        raise RuntimeError(f"Fresh data unexpectedly fetched again: {fresh}")
    if fetched_cursors != [None, "cursor-1", "cursor-2"]:
        raise RuntimeError(f"A committed page was fetched more than once: {fetched_cursors}")
    if persisted_ids != ["one", "two", "three"]:
        raise RuntimeError(f"Unexpected persisted delta sequence: {persisted_ids}")

    first_run = platform.warehouse.ingestion_run(paused["run_id"])
    second_run = platform.warehouse.ingestion_run(resumed["run_id"])
    if first_run is None or second_run is None:
        raise RuntimeError("Incremental run audit records are missing")
    if len(first_run["batches"]) != 2 or len(second_run["batches"]) != 1:
        raise RuntimeError("Committed batch audit history is incomplete")

    status = platform.status()["warehouse"]["incremental_loader"]
    return {
        "schema_version": "stock_ai.incremental_loader_verification.v1",
        "status": "passed",
        "fetch_cursors": fetched_cursors,
        "persisted_ids": persisted_ids,
        "pause": {
            "run_id": paused["run_id"],
            "cursor": paused["cursor"],
            "batch_count": paused["batch_count"],
        },
        "resume": {
            "run_id": resumed["run_id"],
            "starting_cursor": resumed["previous_cursor"],
            "cursor": resumed["cursor"],
            "batch_count": resumed["batch_count"],
        },
        "fresh_reload_status": fresh["status"],
        "warehouse_status": status,
        "database": str(database_path),
    }


def main() -> None:
    with TemporaryDirectory(prefix="stock-ai-incremental-verification-") as temp_dir:
        print(
            json.dumps(
                verify(Path(temp_dir) / "market-data.sqlite"),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
