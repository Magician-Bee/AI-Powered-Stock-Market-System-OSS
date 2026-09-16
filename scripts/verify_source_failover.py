#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import json

from stock_ai.data_platform import (
    FailoverObservation,
    MarketDataPlatform,
    SourceFetchError,
    SourceFetchPayload,
    TemporalCoordinates,
)


def main() -> None:
    with TemporaryDirectory(prefix="stock-ai-source-failover-") as temporary:
        database = Path(temporary) / "market-data.sqlite"
        platform = MarketDataPlatform(database_path=database, code_version="verify-data-012")
        platform.source_failover_service.sleeper = lambda _seconds: None
        calls: list[dict[str, Any]] = []

        def fetch(candidate, _url: str, attempt: int):
            calls.append(
                {
                    "dataset_id": candidate.dataset_id,
                    "source_id": candidate.source_id,
                    "attempt": attempt,
                }
            )
            if candidate.dataset_id == "twse_stock_day":
                raise SourceFetchError(
                    "injected primary outage",
                    http_status=503,
                    response_payload={"error": "injected primary outage"},
                )
            return SourceFetchPayload(
                payload={"data": [{"symbol": "VERIFY", "close": 42.5}]},
                metadata={"verification": "DATA-012"},
            )

        def normalize(payload: Any, provenance: dict[str, Any]):
            row = payload["data"][0]
            acquired_at = provenance["acquired_at"]
            return [
                FailoverObservation(
                    entity_id="ENT-source-failover-verification",
                    observation_key="2026-07-27",
                    temporal=TemporalCoordinates(
                        time_basis="trade_date",
                        trade_date="2026-07-27",
                        observed_at="2026-07-27",
                        available_at=acquired_at,
                        acquired_at=acquired_at,
                        effective_at="2026-07-27",
                    ),
                    payload={"symbol": row["symbol"], "close": row["close"]},
                )
            ]

        failover = platform.ingest_with_failover(
            dataset_id="twse_stock_day",
            normalized_dataset="prices_daily",
            fetch=fetch,
            normalize=normalize,
            acquired_at="2026-07-27T03:00:00+00:00",
        )
        failover_run = platform.source_failover_run(failover["run_id"])
        if failover_run is None:
            raise AssertionError("failover run was not persisted")
        revisions_after_failover = platform.query(
            dataset="prices_daily",
            entity_id="ENT-source-failover-verification",
            as_of="2026-07-27T04:00:00+00:00",
            limit=10,
        )
        if len(revisions_after_failover) != 1:
            raise AssertionError("expected one failover revision")
        fallback_revision = revisions_after_failover[0]
        if fallback_revision.source_id != "twse_openapi" or not fallback_revision.is_fallback:
            raise AssertionError("fallback revision lost its actual source identity")
        for attempt in failover_run["attempts"]:
            raw = platform.warehouse.raw_payload(attempt["raw_payload_id"])
            if raw is None or raw["source_id"] != attempt["source_id"]:
                raise AssertionError("raw attempt source identity mismatch")

        primary = platform.ingest_with_failover(
            dataset_id="twse_stock_day",
            normalized_dataset="prices_daily",
            fetch=lambda _candidate, _url, _attempt: SourceFetchPayload(
                payload={"data": [{"symbol": "VERIFY", "close": 42.0}]}
            ),
            normalize=normalize,
            acquired_at="2026-07-27T03:05:00+00:00",
        )
        all_revisions = platform.query(
            dataset="prices_daily",
            entity_id="ENT-source-failover-verification",
            as_of="2026-07-27T04:00:00+00:00",
            limit=10,
        )
        preferred = platform.preferred_query(
            dataset="prices_daily",
            entity_id="ENT-source-failover-verification",
            as_of="2026-07-27T04:00:00+00:00",
        )
        status = platform.source_failover_status()

        if {item.source_id for item in all_revisions} != {
            "twse_official_web",
            "twse_openapi",
        }:
            raise AssertionError("primary and backup revisions were not retained separately")
        if preferred["source_id"] != "twse_official_web" or preferred["fallback_used"]:
            raise AssertionError("recovered primary was not preferred over backup data")
        if status["failover_count"] != 1 or not status["source_identity_preserved"]:
            raise AssertionError("source failover status did not preserve provenance")

        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.source_failover_verification.v1",
                    "status": "passed",
                    "retry_and_failover": {
                        "requested_source_id": failover["requested_source_id"],
                        "selected_source_id": failover["selected_source_id"],
                        "attempt_count": failover["attempt_count"],
                        "calls": calls,
                    },
                    "fallback_revision": {
                        "revision_id": fallback_revision.revision_id,
                        "source_id": fallback_revision.source_id,
                        "is_fallback": fallback_revision.is_fallback,
                        "field_sources": sorted(
                            {
                                item.source_id
                                for item in fallback_revision.field_provenance.values()
                            }
                        ),
                    },
                    "primary_recovery": {
                        "selected_source_id": primary["selected_source_id"],
                        "retained_source_ids": sorted(
                            {item.source_id for item in all_revisions}
                        ),
                        "preferred_source_id": preferred["source_id"],
                        "fallback_used": preferred["fallback_used"],
                    },
                    "status_summary": status,
                    "database": str(database),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
