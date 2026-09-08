from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
import json

from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.data_platform.security_loader import OfficialSecurityMasterLoader


def verify(database_path: Path) -> dict[str, object]:
    started = perf_counter()
    stage_started = started
    stage_seconds: dict[str, float] = {}
    platform = MarketDataPlatform(database_path=database_path)
    sync = OfficialSecurityMasterLoader(platform).run(force=True)
    stage_seconds["official_sync"] = round(perf_counter() - stage_started, 3)
    stage_started = perf_counter()
    revisions = platform.query(dataset="security_master", limit=1)
    if not revisions:
        raise RuntimeError("Official-source sync created no security-master revisions")
    lineage = platform.warehouse.lineage(revisions[0].revision_id)
    raw_payload = lineage.get("raw_payload") or {}
    if not raw_payload.get("payload_hash"):
        raise RuntimeError("Security-master revision has no immutable raw-payload hash")
    quality = platform.warehouse.quality_report("security_master")
    if quality["status"] != "passed":
        raise RuntimeError(f"Security lifecycle quality did not pass: {quality}")
    lifecycle = sync["lifecycle"]
    required_types = {"stock", "etf", "warrant", "index"}
    if not required_types.issubset(lifecycle["by_entity_type"]):
        raise RuntimeError(f"Missing security entity types: {lifecycle['by_entity_type']}")
    required_listings = {"listed", "otc", "emerging", "etf", "warrant", "index", "delisted"}
    if not required_listings.issubset(lifecycle["by_listing_type"]):
        raise RuntimeError(f"Missing listing lifecycle coverage: {lifecycle['by_listing_type']}")
    stage_seconds["lineage_and_quality"] = round(perf_counter() - stage_started, 3)
    stage_started = perf_counter()
    entity_registry = platform.entity_registry.status()
    if entity_registry["status"] != "passed":
        raise RuntimeError(f"Entity Registry quality did not pass: {entity_registry}")
    if entity_registry["cross_source_entity_count"] < 1:
        raise RuntimeError("Official data produced no cross-source canonical entity")
    with platform.warehouse._connect() as conn:
        cross_source_row = conn.execute(
            """
            select entity_id
              from entity_identifiers
             group by entity_id
            having count(distinct source_id) > 1
             order by count(distinct source_id) desc, entity_id
             limit 1
            """
        ).fetchone()
    if cross_source_row is None:
        raise RuntimeError("Cross-source Entity Registry sample is unavailable")
    cross_source_entity_id = str(cross_source_row["entity_id"])
    cross_source_profile = platform.warehouse.entity_profile(cross_source_entity_id)
    if cross_source_profile is None:
        raise RuntimeError("Cross-source Entity Registry sample has no profile")
    resolved_sources: set[str] = set()
    for identifier in cross_source_profile["identifiers"]:
        resolution = platform.resolve_entity(
            identifier["identifier_value"],
            source_id=identifier["source_id"],
            identifier_type=identifier["identifier_type"],
        )
        if (
            resolution["status"] == "resolved"
            and resolution["entity"]["entity_id"] == cross_source_entity_id
        ):
            resolved_sources.add(identifier["source_id"])
    if len(resolved_sources) < 2:
        raise RuntimeError(
            "Cross-source identifiers did not resolve to one canonical entity: "
            f"{cross_source_entity_id} / {sorted(resolved_sources)}"
        )
    stage_seconds["entity_registry"] = round(perf_counter() - stage_started, 3)
    stage_started = perf_counter()
    second = OfficialSecurityMasterLoader(platform).run()
    if any(item["status"] != "skipped_fresh" for item in second["partitions"]):
        raise RuntimeError("A fresh checkpoint unexpectedly fetched an official partition")
    status = platform.status()["warehouse"]
    stage_seconds["incremental_replay_and_status"] = round(
        perf_counter() - stage_started,
        3,
    )
    return {
        "schema_version": "stock_ai.unified_data_platform_verification.v3",
        "status": "passed",
        "partitions": [
            {
                "partition_key": item["partition_key"],
                "source_row_count": item["metadata"]["source_row_count"],
                "sync_entity_count": (item.get("sync") or {}).get("count"),
                "created_revision_count": (item.get("sync") or {}).get("revision_count"),
            }
            for item in sync["partitions"]
        ],
        "entity_count": sync["entity_count"],
        "lifecycle": lifecycle,
        "raw_payload_count": status["tables"]["raw_payloads"],
        "revision_count": status["tables"]["revisions"],
        "lifecycle_event_count": status["tables"]["lifecycle_events"],
        "entity_registry": entity_registry,
        "cross_source_resolution": {
            "entity_id": cross_source_entity_id,
            "resolved_source_ids": sorted(resolved_sources),
        },
        "lineage_raw_hash": raw_payload["payload_hash"],
        "quality_status": quality["status"],
        "quality": {
            key: quality.get(key)
            for key in (
                "schema_version",
                "report_id",
                "dataset",
                "row_count",
                "revision_history_count",
                "issue_count",
                "severity_counts",
                "state_hash",
            )
        },
        "fresh_reload_status": second["status"],
        "stage_seconds": stage_seconds,
        "elapsed_seconds": round(perf_counter() - started, 3),
        "database": str(database_path),
    }


def main() -> None:
    with TemporaryDirectory(prefix="stock-ai-data-verification-") as temp_dir:
        result = verify(Path(temp_dir) / "market-data.sqlite")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
