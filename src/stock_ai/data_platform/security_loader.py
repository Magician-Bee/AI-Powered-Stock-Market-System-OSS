from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
        ]

    @staticmethod
    def _parallel_payloads(
        loaders: dict[str, Callable[[], list[dict[str, Any]]]],
    ) -> dict[str, list[dict[str, Any]]]:
        with ThreadPoolExecutor(max_workers=len(loaders)) as pool:
            futures = {name: pool.submit(loader) for name, loader in loaders.items()}
            return {name: future.result() for name, future in futures.items()}

    def run(self, *, force: bool = False, as_of: str | None = None) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
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
            sync_result: dict[str, Any] = {}

            def fetch(_previous_cursor: str | None) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
                payloads = fetch_payloads()
                return (
                    [{"payloads": payloads}],
                    content_hash(payloads),
                    {
                        "payload_names": sorted(payloads),
                        "source_row_count": sum(len(rows) for rows in payloads.values()),
                    },
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

            load = self.incremental.run(
                source_id=source_id,
                dataset="security_master",
                partition_key=partition_key,
                fetch=fetch,
                persist=persist,
                force=force,
                as_of=as_of,
            )
            results.append({**load, "sync": sync_result or None})
        lifecycle = self.platform.warehouse.lifecycle_summary()
        partition_statuses = {str(item["status"]) for item in results}
        return {
            "schema_version": "stock_ai.official_security_master_load.v1",
            "status": (
                "succeeded"
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
