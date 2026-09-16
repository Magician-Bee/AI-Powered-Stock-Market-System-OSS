from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any, Callable

from stock_ai.taiwan_official import (
    TPEX_COMPANIES,
    TPEX_DELISTED,
    TPEX_EMERGING_COMPANIES,
    TPEX_EMERGING_QUOTES,
    TPEX_QUOTES,
    TPEX_WARRANTS,
    TWSE_ALL_QUOTES,
    TWSE_COMPANIES,
    TWSE_DELISTED,
    TWSE_ETFS,
    TWSE_INDICES,
    TWSE_WARRANTS,
    tpex_companies,
    tpex_delisted,
    tpex_emerging_companies,
    tpex_emerging_quotes,
    tpex_indices,
    tpex_quotes,
    tpex_warrants,
    twse_companies,
    twse_delisted,
    twse_etfs,
    twse_indices,
    twse_quotes,
    twse_warrants,
)

from .incremental import IncrementalLoader
from .product_catalog import CATALOGUES, OfficialProductClassificationLoader
from .source_registry import source_endpoint
from .warehouse import content_hash


FetchPayloads = Callable[[], dict[str, list[dict[str, Any]]]]


class OfficialSecurityMasterLoader:
    """Checkpointed loader for every official Taiwan security lifecycle source."""

    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.incremental = IncrementalLoader(platform)

    @staticmethod
    def source_urls() -> list[str]:
        return [
            TWSE_COMPANIES,
            TWSE_ALL_QUOTES,
            TWSE_ETFS,
            TWSE_WARRANTS,
            TWSE_INDICES,
            TWSE_DELISTED,
            TPEX_COMPANIES,
            TPEX_QUOTES,
            TPEX_EMERGING_COMPANIES,
            TPEX_EMERGING_QUOTES,
            TPEX_WARRANTS,
            source_endpoint("tpex_index", path="tpex_index"),
            TPEX_DELISTED,
            *(source_endpoint(dataset_id) for dataset_id in CATALOGUES),
        ]

    @staticmethod
    def _parallel_payloads(
        loaders: dict[str, Callable[[], list[dict[str, Any]]]],
    ) -> dict[str, list[dict[str, Any]]]:
        # Keep aggregate concurrency bounded per official venue. Some TPEx
        # endpoints serve multi-megabyte snapshots and close overloaded full
        # responses early; individual clients can resume with verified byte
        # ranges without a burst of unrelated requests to the same host.
        with ThreadPoolExecutor(max_workers=min(2, len(loaders))) as pool:
            futures = {name: pool.submit(loader) for name, loader in loaders.items()}
            return {name: future.result() for name, future in futures.items()}

    def run(self, *, force: bool = False, as_of: str | None = None, stop_event: Event | None = None) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        # Capture and verify issuance evidence before lifecycle normalization.
        # A fresh skipped catalogue remains available through its retained
        # checkpoint; this does not require a second download.
        catalogue_results = OfficialProductClassificationLoader(self.platform).run(
            force=force, as_of=as_of, stop_event=stop_event)
        groups: tuple[tuple[str, str, FetchPayloads], ...] = (
            (
                "twse_openapi",
                "twse_official_master",
                lambda: self._parallel_payloads(
                    {
                        "twse_companies": twse_companies,
                        "twse_quotes": twse_quotes,
                        "twse_etfs": twse_etfs,
                        "twse_warrants": twse_warrants,
                        "twse_indices": twse_indices,
                        "twse_delisted": twse_delisted,
                    }
                ),
            ),
            (
                "tpex_openapi",
                "tpex_official_master",
                lambda: self._parallel_payloads(
                    {
                        "tpex_companies": tpex_companies,
                        "tpex_quotes": tpex_quotes,
                        "tpex_emerging_companies": tpex_emerging_companies,
                        "tpex_emerging_quotes": tpex_emerging_quotes,
                        "tpex_warrants": tpex_warrants,
                        "tpex_indices": tpex_indices,
                    }
                ),
            ),
            (
                "tpex_official_web",
                "tpex_delisted_history",
                lambda: {"tpex_delisted": tpex_delisted()},
            ),
        )
        for source_id, partition_key, fetch_payloads in groups:
            if stop_event is not None and stop_event.is_set():
                raise asyncio.CancelledError("security_master_refresh_cancelled")
            sync_result: dict[str, Any] = {}
            audit_metadata: dict[str, Any] = {}

            def fetch(_previous_cursor: str | None) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
                payloads = fetch_payloads()
                audit_metadata.update(payload_names=sorted(payloads),
                                      source_row_count=sum(len(rows) for rows in payloads.values()))
                return (
                    [{"payloads": payloads}],
                    content_hash(payloads),
                    audit_metadata,
                )

            def persist(records: list[dict[str, Any]], acquired_at: str) -> None:
                if len(records) != 1 or not isinstance(records[0].get("payloads"), dict):
                    raise ValueError("Security-master loader received an invalid snapshot batch")
                sync_result.update(
                    self.platform.sync_security_master_payloads(
                        **records[0]["payloads"],
                        acquired_at=acquired_at,
                    )
                )
                audit_metadata["identity_resolution"] = sync_result.get("identity_resolution")
                audit_metadata["raw_payload_ids"] = sync_result.get("raw_payload_ids", {})
                if sync_result.get("status") == "partial":
                    # Valid rows have been committed and each rejected row is
                    # retained in its source checkpoint. Do not certify or
                    # cache the complete master as successfully refreshed.
                    self.platform.cache_policy_service.invalidate(
                        source_id=source_id, dataset="security_master",
                        partition_key=partition_key, reason="identity_resolution_partial",
                        invalidated_at=acquired_at,
                        metadata={"unresolved_count": sync_result["identity_resolution"]["unresolved_count"]},
                    )
                    raise ValueError("security_master_identity_resolution_partial")

            try:
                load = self.incremental.run(
                    source_id=source_id,
                    dataset="security_master",
                    partition_key=partition_key,
                    fetch=fetch,
                    persist=persist,
                    force=force,
                    as_of=as_of,
                )
            except Exception as exc:
                # IncrementalLoader has already made the failure durable before
                # re-raising.  Preserve that exact run identity in the caller's
                # refresh receipt so a read-only audit can correlate source
                # latency, status and error without guessing by timestamp.
                checkpoint = self.platform.warehouse.get_checkpoint(
                    source_id=source_id,
                    dataset="security_master",
                    partition_key=partition_key,
                )
                metadata = checkpoint.get("metadata") if isinstance(checkpoint, dict) else {}
                metadata = metadata if isinstance(metadata, dict) else {}
                load = {
                    "status": "failed",
                    "run_id": metadata.get("run_id"),
                    "source_id": source_id,
                    "dataset": "security_master",
                    "partition_key": partition_key,
                    "cursor": checkpoint.get("cursor_value") if isinstance(checkpoint, dict) else None,
                    "record_count": int(metadata.get("committed_record_count") or 0),
                    "batch_count": int(metadata.get("committed_batch_count") or 0),
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            results.append({**load, "sync": sync_result or None})
        results.extend(catalogue_results)
        lifecycle = self.platform.warehouse.lifecycle_summary()
        partition_statuses = {str(item["status"]) for item in results}
        return {
            "schema_version": "stock_ai.official_security_master_load.v1",
            "status": (
                "partial" if ("partial" in partition_statuses or (("failed" in partition_statuses or "skipped_refresh_in_progress" in partition_statuses)
                              and len(partition_statuses) > 1)
                              )
                else "failed" if partition_statuses == {"failed"}
                else "succeeded"
                if "succeeded" in partition_statuses
                else "refresh_in_progress"
                if "skipped_refresh_in_progress" in partition_statuses
                else "skipped_fresh"
            ),
            "partitions": results,
            "lifecycle": lifecycle,
            "entity_count": sum(lifecycle["by_entity_type"].values()),
            "source_urls": self.source_urls(),
        }
