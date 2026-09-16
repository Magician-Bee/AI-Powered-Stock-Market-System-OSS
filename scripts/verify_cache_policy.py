from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Any
import json

from stock_ai.data_platform.incremental import IncrementalLoader
from stock_ai.data_platform.service import MarketDataPlatform


def verify(database_path: Path) -> dict[str, Any]:
    platform = MarketDataPlatform(database_path=database_path)
    loader = IncrementalLoader(platform)
    ttl_by_dataset = {
        dataset: platform.cache_ttl(dataset, source_id="twse_openapi")
        for dataset in ("prices_intraday", "prices_daily", "security_master")
    }
    if ttl_by_dataset != {
        "prices_intraday": 5,
        "prices_daily": 900,
        "security_master": 21600,
    }:
        raise RuntimeError(f"Dataset TTL policies are not distinct: {ttl_by_dataset}")

    platform.cache_policy_service.mark_refreshed(
        source_id="twse_mis",
        dataset="prices_intraday",
        partition_key="2330",
        refreshed_at="2026-07-27T01:00:00+00:00",
    )
    stale = platform.cache_policy_service.decision(
        source_id="twse_mis",
        dataset="prices_intraday",
        partition_key="2330",
        as_of="2026-07-27T01:00:06+00:00",
        record=False,
    )
    expired = platform.cache_policy_service.decision(
        source_id="twse_mis",
        dataset="prices_intraday",
        partition_key="2330",
        as_of="2026-07-27T01:00:16+00:00",
        record=False,
    )
    if stale["state"] != "stale_while_revalidate" or not stale["serve_stale"]:
        raise RuntimeError(f"Stale-while-revalidate window failed: {stale}")
    if expired["state"] != "expired" or expired["serve_stale"]:
        raise RuntimeError(f"Expired data was treated as stale-servable: {expired}")

    fetch_started = Event()
    release_fetch = Event()
    fetch_cursors: list[str | None] = []

    def fetch(cursor: str | None):
        fetch_cursors.append(cursor)
        fetch_started.set()
        if not release_fetch.wait(timeout=5):
            raise RuntimeError("Single-flight verifier timed out")
        return ([{"id": "v1"}], "cursor-v1", {})

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            loader.run,
            source_id="twse_openapi",
            dataset="security_master",
            partition_key="twse",
            fetch=fetch,
            persist=lambda _records, _at: None,
            as_of="2026-07-27T02:00:00+00:00",
        )
        if not fetch_started.wait(timeout=5):
            raise RuntimeError("First refresh never reached the source")
        duplicate_future = executor.submit(
            loader.run,
            source_id="twse_openapi",
            dataset="security_master",
            partition_key="twse",
            fetch=fetch,
            persist=lambda _records, _at: None,
            as_of="2026-07-27T02:00:00+00:00",
        )
        duplicate = duplicate_future.result(timeout=5)
        release_fetch.set()
        first = first_future.result(timeout=5)
    if first["status"] != "succeeded":
        raise RuntimeError(f"First refresh failed: {first}")
    if duplicate["status"] != "skipped_refresh_in_progress":
        raise RuntimeError(f"Concurrent duplicate was not suppressed: {duplicate}")
    if fetch_cursors != [None]:
        raise RuntimeError(f"Upstream source was called more than once: {fetch_cursors}")

    fresh = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="twse",
        fetch=fetch,
        persist=lambda _records, _at: None,
        as_of="2026-07-27T02:01:00+00:00",
    )
    if fresh["status"] != "skipped_fresh":
        raise RuntimeError(f"Fresh cache did not suppress another request: {fresh}")

    invalidation = platform.invalidate_cache(
        dataset="security_master",
        source_id="twse_openapi",
        partition_key="twse",
        reason="listing_event",
        invalidated_at="2026-07-27T02:02:00+00:00",
        metadata={"verification": True},
    )
    invalidated = platform.cache_status(
        dataset="security_master",
        as_of="2026-07-27T02:03:00+00:00",
    )
    if invalidation["count"] != 1 or invalidated["state_counts"].get("invalidated") != 1:
        raise RuntimeError(f"Reviewed invalidation did not take effect: {invalidated}")

    refreshed_cursors: list[str | None] = []
    refreshed = loader.run(
        source_id="twse_openapi",
        dataset="security_master",
        partition_key="twse",
        fetch=lambda cursor: (
            refreshed_cursors.append(cursor) or [{"id": "v2"}],
            "cursor-v2",
            {},
        ),
        persist=lambda _records, _at: None,
        as_of="2026-07-27T02:03:00+00:00",
    )
    if refreshed["status"] != "succeeded" or refreshed_cursors != ["cursor-v1"]:
        raise RuntimeError(f"Invalidated cache was not refreshed once: {refreshed}")
    final_status = platform.cache_status(as_of="2026-07-27T02:04:00+00:00")
    if final_status["active_refresh_count"] != 0:
        raise RuntimeError(f"Refresh lease was not released: {final_status}")

    return {
        "schema_version": "stock_ai.cache_policy_verification.v1",
        "status": "passed",
        "ttl_by_dataset": ttl_by_dataset,
        "stale_window": {
            "state": stale["state"],
            "serve_stale": stale["serve_stale"],
            "expired_state": expired["state"],
            "expired_servable": expired["serve_stale"],
        },
        "singleflight": {
            "first": first["status"],
            "concurrent": duplicate["status"],
            "fresh_repeat": fresh["status"],
            "source_call_count": len(fetch_cursors),
        },
        "invalidation": {
            "reason": invalidation["reason"],
            "count": invalidation["count"],
            "state_before_refresh": "invalidated",
            "refresh_status": refreshed["status"],
            "resume_cursor": refreshed_cursors[0],
        },
        "final_status": final_status,
        "database": str(database_path),
    }


def main() -> None:
    with TemporaryDirectory(prefix="stock-ai-cache-policy-") as temp_dir:
        print(
            json.dumps(
                verify(Path(temp_dir) / "market-data.sqlite"),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
