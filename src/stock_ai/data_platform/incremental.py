from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .contracts import normalize_timestamp, utc_now


FetchBatch = Callable[[str | None], tuple[list[dict[str, Any]], str | None, dict[str, Any]]]
PersistBatch = Callable[[list[dict[str, Any]], str], Any]


class IncrementalLoader:
    """Checkpointed loader that skips fresh partitions and resumes from cursors."""

    def __init__(self, platform: Any) -> None:
        self.platform = platform

    def run(
        self,
        *,
        source_id: str,
        dataset: str,
        fetch: FetchBatch,
        persist: PersistBatch,
        partition_key: str = "all",
        force: bool = False,
        as_of: str | None = None,
        max_batches: int | None = None,
    ) -> dict[str, Any]:
        if source_id not in self.platform.sources:
            raise ValueError(f"Unknown source_id: {source_id}")
        if max_batches is not None and max_batches < 1:
            raise ValueError("max_batches must be at least 1")
        current = normalize_timestamp(as_of or utc_now(), required=True)
        assert current is not None
        checkpoint = self.platform.warehouse.get_checkpoint(
            source_id=source_id,
            dataset=dataset,
            partition_key=partition_key,
        )
        cache_decision = self.platform.cache_policy_service.decision(
            source_id=source_id,
            dataset=dataset,
            partition_key=partition_key,
            as_of=current,
            force=force,
        )
        ttl_seconds = int(cache_decision["policy"]["ttl_seconds"])
        if cache_decision["state"] == "fresh":
            return {
                "schema_version": "stock_ai.incremental_load.v2",
                "status": "skipped_fresh",
                "source_id": source_id,
                "dataset": dataset,
                "partition_key": partition_key,
                "cursor": checkpoint.get("cursor_value") if checkpoint else None,
                "ttl_seconds": ttl_seconds,
                "record_count": 0,
                "batch_count": 0,
                "cache": cache_decision,
            }
        lease = self.platform.cache_policy_service.acquire_refresh(
            source_id=source_id,
            dataset=dataset,
            partition_key=partition_key,
            as_of=current,
        )
        if lease is None:
            return {
                "schema_version": "stock_ai.incremental_load.v2",
                "status": "skipped_refresh_in_progress",
                "source_id": source_id,
                "dataset": dataset,
                "partition_key": partition_key,
                "cursor": checkpoint.get("cursor_value") if checkpoint else None,
                "ttl_seconds": ttl_seconds,
                "record_count": 0,
                "batch_count": 0,
                "serve_stale": cache_decision["serve_stale"],
                "cache": cache_decision,
            }
        starting_cursor = checkpoint.get("cursor_value") if checkpoint else None
        resumed_status = checkpoint.get("status") if checkpoint else None
        try:
            run_id = self.platform.warehouse.start_ingestion_run(
                source_id=source_id,
                dataset=dataset,
                partition_key=partition_key,
                starting_cursor=starting_cursor,
                metadata={
                    "forced": force,
                    "resumed": starting_cursor is not None,
                    "resumed_from_status": resumed_status,
                    "max_batches": max_batches,
                    "cache_state": cache_decision["state"],
                    "cache_lease_id": lease["lease_id"],
                },
            )
        except Exception:
            self.platform.cache_policy_service.release_refresh(lease["lease_id"])
            raise
        self.platform.warehouse.save_checkpoint(
            source_id=source_id,
            dataset=dataset,
            partition_key=partition_key,
            cursor_value=starting_cursor,
            status="running",
            metadata={
                "run_id": run_id,
                "resumed": starting_cursor is not None,
                "resumed_from_status": resumed_status,
                "forced": force,
                "committed_batch_count": 0,
                "committed_record_count": 0,
            },
        )
        committed_cursor = starting_cursor
        batch_count = 0
        record_count = 0
        latest_metadata: dict[str, Any] = {}

        while True:
            batch_sequence = batch_count + 1
            batch_started_at = utc_now()
            fetch_metadata: dict[str, Any] = {}
            try:
                records, next_cursor, fetch_metadata = fetch(committed_cursor)
                if not isinstance(records, list):
                    raise TypeError("incremental fetch must return a list of records")
                if not isinstance(fetch_metadata, dict):
                    raise TypeError("incremental fetch metadata must be a dict")
                has_more = bool(fetch_metadata.get("has_more", False))
                if has_more and next_cursor == committed_cursor:
                    raise ValueError(
                        "incremental fetch declared has_more without advancing its cursor"
                    )
                persist(records, current)
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc)}
                retained_metadata = fetch_metadata if isinstance(fetch_metadata, dict) else {}
                self.platform.warehouse.record_ingestion_batch(
                    run_id=run_id,
                    batch_sequence=batch_sequence,
                    input_cursor=committed_cursor,
                    output_cursor=None,
                    status="failed",
                    record_count=0,
                    started_at=batch_started_at,
                    error=error,
                    metadata={**retained_metadata, "resumable": True},
                )
                failure_metadata = {
                    **retained_metadata,
                    "run_id": run_id,
                    "resumable": True,
                    "committed_batch_count": batch_count,
                    "committed_record_count": record_count,
                }
                self.platform.warehouse.save_checkpoint(
                    source_id=source_id,
                    dataset=dataset,
                    partition_key=partition_key,
                    cursor_value=committed_cursor,
                    status="failed",
                    error=error,
                    metadata=failure_metadata,
                )
                self.platform.warehouse.finish_ingestion_run(
                    run_id=run_id,
                    status="failed",
                    committed_cursor=committed_cursor,
                    batch_count=batch_count,
                    record_count=record_count,
                    error=error,
                    metadata=failure_metadata,
                )
                self.platform.cache_policy_service.release_refresh(lease["lease_id"])
                raise

            input_cursor = committed_cursor
            committed_cursor = (
                next_cursor if next_cursor is not None else committed_cursor
            )
            batch_count += 1
            record_count += len(records)
            latest_metadata = dict(fetch_metadata)
            self.platform.warehouse.record_ingestion_batch(
                run_id=run_id,
                batch_sequence=batch_count,
                input_cursor=input_cursor,
                output_cursor=committed_cursor,
                status="succeeded",
                record_count=len(records),
                started_at=batch_started_at,
                metadata=latest_metadata,
            )

            has_more = bool(latest_metadata.get("has_more", False))
            paused = bool(has_more and max_batches and batch_count >= max_batches)
            terminal_status = "paused" if paused else "running" if has_more else "succeeded"
            checkpoint_metadata = {
                **latest_metadata,
                "run_id": run_id,
                "resumable": paused,
                "committed_batch_count": batch_count,
                "committed_record_count": record_count,
                "starting_cursor": starting_cursor,
            }
            self.platform.warehouse.save_checkpoint(
                source_id=source_id,
                dataset=dataset,
                partition_key=partition_key,
                cursor_value=committed_cursor,
                status=terminal_status,
                metadata=checkpoint_metadata,
            )
            if terminal_status == "running":
                continue

            self.platform.warehouse.finish_ingestion_run(
                run_id=run_id,
                status=terminal_status,
                committed_cursor=committed_cursor,
                batch_count=batch_count,
                record_count=record_count,
                metadata=checkpoint_metadata,
            )
            cache_entry = None
            try:
                if terminal_status == "succeeded":
                    cache_entry = self.platform.cache_policy_service.mark_refreshed(
                        source_id=source_id,
                        dataset=dataset,
                        partition_key=partition_key,
                        refreshed_at=current,
                    )
            finally:
                self.platform.cache_policy_service.release_refresh(lease["lease_id"])
            return {
                "schema_version": "stock_ai.incremental_load.v2",
                "status": terminal_status,
                "run_id": run_id,
                "source_id": source_id,
                "dataset": dataset,
                "partition_key": partition_key,
                "cursor": committed_cursor,
                "previous_cursor": starting_cursor,
                "ttl_seconds": ttl_seconds,
                "record_count": record_count,
                "batch_count": batch_count,
                "resumable": paused,
                "metadata": latest_metadata,
                "cache": {
                    "decision": cache_decision,
                    "entry": cache_entry,
                    "lease_id": lease["lease_id"],
                },
            }

    @staticmethod
    def _is_fresh(
        checkpoint: dict[str, Any] | None,
        as_of: str,
        ttl_seconds: int,
    ) -> bool:
        if not checkpoint or checkpoint.get("status") != "succeeded":
            return False
        last_success_at = checkpoint.get("last_success_at")
        if not last_success_at:
            return False
        current = datetime.fromisoformat(as_of).astimezone(timezone.utc)
        last_success = datetime.fromisoformat(str(last_success_at)).astimezone(timezone.utc)
        return current <= last_success + timedelta(seconds=ttl_seconds)
