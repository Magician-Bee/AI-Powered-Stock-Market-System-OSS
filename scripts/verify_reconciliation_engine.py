#!/usr/bin/env python3
from __future__ import annotations

from argparse import ArgumentParser
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import json

from stock_ai.data_platform import MarketDataPlatform, TemporalCoordinates


def parser() -> ArgumentParser:
    result = ArgumentParser(description="Verify DATA-013 reconciliation behavior")
    result.add_argument(
        "--database",
        type=Path,
        help="Persist the acceptance fixture in a new database for UI verification",
    )
    return result


def main() -> None:
    arguments = parser().parse_args()
    temporary = (
        nullcontext(None)
        if arguments.database is not None
        else TemporaryDirectory(prefix="stock-ai-reconciliation-")
    )
    with temporary as temporary_path:
        database = (
            arguments.database.expanduser().resolve()
            if arguments.database is not None
            else Path(str(temporary_path)) / "market-data.sqlite"
        )
        if database.exists():
            raise ValueError(f"verification database must not already exist: {database}")
        platform = MarketDataPlatform(
            database_path=database,
            code_version="verify-data-013",
        )

        def write(
            dataset: str,
            source_id: str,
            observation_key: str,
            payload: dict[str, Any],
            acquired_at: str,
        ) -> str:
            revision = platform.warehouse.write_revision(
                dataset=dataset,
                entity_id="ENT-reconciliation-verification",
                observation_key=observation_key,
                source_id=source_id,
                temporal=TemporalCoordinates(
                    observed_at="2026-07-26",
                    published_at="2026-07-26T01:00:00+00:00",
                    available_at="2026-07-26T01:00:00+00:00",
                    acquired_at=acquired_at,
                    effective_at="2026-07-26",
                ),
                payload=payload,
                raw_payload_id=None,
            )
            return revision.revision_id

        price_revision_ids = [
            write(
                "prices_daily",
                "twse_openapi",
                "2026-07-26",
                {"close": 100.0},
                "2026-07-26T02:00:00+00:00",
            ),
            write(
                "prices_daily",
                "yahoo_finance",
                "2026-07-26",
                {"close": 101.0},
                "2026-07-26T02:01:00+00:00",
            ),
        ]
        price = platform.reconcile_sources(
            dataset="prices_daily",
            entity_id="ENT-reconciliation-verification",
            observation_key="2026-07-26",
            knowledge_at="2026-07-26T03:00:00+00:00",
        )
        if price["status"] != "conflict" or price["conflict_count"] != 1:
            raise AssertionError("price discrepancy was not recorded as a conflict")

        for source_id, revenue in (
            ("twse_openapi", 1_000_000),
            ("yahoo_finance", 1_000_500),
        ):
            write(
                "revenues_monthly",
                source_id,
                "2026-06",
                {
                    "report_date": "2026-07-10",
                    "period": "2026-06",
                    "symbol": "VERIFY",
                    "name": "Reconciliation Verification",
                    "current_revenue": revenue,
                    "previous_revenue": 990_000,
                    "last_year_revenue": 900_000,
                    "mom_change_percent": 1.0,
                    "yoy_change_percent": 11.0,
                    "ytd_revenue": 5_900_000,
                    "last_ytd_revenue": 5_400_000,
                    "ytd_change_percent": 9.0,
                    "source": source_id,
                },
                "2026-07-26T03:10:00+00:00",
            )
        financial = platform.reconcile_sources(
            dataset="revenues_monthly",
            entity_id="ENT-reconciliation-verification",
            observation_key="2026-06",
            knowledge_at="2026-07-26T04:00:00+00:00",
        )
        if financial["status"] != "consistent":
            raise AssertionError("financial relative tolerance was not applied")

        for source_id, title, event_time in (
            ("twse_openapi", "重大訊息：董事會決議", "2026-07-26T06:00:00+00:00"),
            ("yahoo_finance", "重大訊息 董事會決議", "2026-07-26T06:03:00+00:00"),
        ):
            write(
                "events",
                source_id,
                "EVENT-VERIFY",
                {
                    "event_type": "material_information",
                    "title": title,
                    "event_time": event_time,
                },
                "2026-07-26T07:00:00+00:00",
            )
        event = platform.reconcile_sources(
            dataset="events",
            entity_id="ENT-reconciliation-verification",
            observation_key="EVENT-VERIFY",
            knowledge_at="2026-07-26T08:00:00+00:00",
        )
        if event["status"] != "consistent" or event["comparison_count"] != 3:
            raise AssertionError("event text/time equivalence rules were not applied")

        divergent_event_revision = write(
            "events",
            "yahoo_finance",
            "EVENT-VERIFY",
            {
                "event_type": "earnings",
                "title": "重大訊息 董事會決議",
                "event_time": "2026-07-26T06:03:00+00:00",
            },
            "2026-07-26T08:01:00+00:00",
        )
        event_conflict = platform.reconcile_sources(
            dataset="events",
            entity_id="ENT-reconciliation-verification",
            observation_key="EVENT-VERIFY",
            knowledge_at="2026-07-26T09:00:00+00:00",
        )
        if [item["field_name"] for item in event_conflict["conflicts"]] != [
            "event_type"
        ]:
            raise AssertionError("event-type source conflict was not isolated")

        converged_price_revision = write(
            "prices_daily",
            "yahoo_finance",
            "2026-07-26",
            {"close": 100.05},
            "2026-07-26T09:01:00+00:00",
        )
        resolved = platform.reconcile_sources(
            dataset="prices_daily",
            entity_id="ENT-reconciliation-verification",
            observation_key="2026-07-26",
            knowledge_at="2026-07-26T10:00:00+00:00",
        )
        if resolved["status"] != "consistent" or resolved["resolved_count"] != 1:
            raise AssertionError("converged source values did not resolve the conflict")

        history = platform.warehouse.revision_history(
            dataset="prices_daily",
            entity_id="ENT-reconciliation-verification",
            observation_key="2026-07-26",
        )
        if len(history["items"]) != 3:
            raise AssertionError("reconciliation overwrote source revision history")
        retained_ids = {item["revision_id"] for item in history["items"]}
        if not set(price_revision_ids + [converged_price_revision]) <= retained_ids:
            raise AssertionError("reconciliation lost source evidence")

        status = platform.reconciliation_status()
        open_conflicts = platform.reconciliation_conflicts(status="open", limit=20)
        resolved_conflicts = platform.reconciliation_conflicts(
            status="resolved",
            limit=20,
        )
        if status["open_conflict_count"] != 1 or status["resolved_conflict_count"] != 1:
            raise AssertionError("conflict lifecycle status is incorrect")
        if open_conflicts["items"][0]["right_revision_id"] != divergent_event_revision:
            raise AssertionError("open conflict does not reference latest source evidence")

        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.reconciliation_verification.v1",
                    "status": "passed",
                    "price": {
                        "initial_status": price["status"],
                        "absolute_difference": price["conflicts"][0][
                            "absolute_difference"
                        ],
                        "resolved_status": resolved["status"],
                        "retained_revision_count": len(history["items"]),
                    },
                    "financial": {
                        "status": financial["status"],
                        "comparison_count": financial["comparison_count"],
                    },
                    "event": {
                        "equivalent_status": event["status"],
                        "conflict_status": event_conflict["status"],
                        "conflicting_fields": [
                            item["field_name"]
                            for item in event_conflict["conflicts"]
                        ],
                    },
                    "lifecycle": {
                        "run_count": status["run_count"],
                        "open_conflicts": open_conflicts["count"],
                        "resolved_conflicts": resolved_conflicts["count"],
                        "source_data_preserved": status["source_data_preserved"],
                    },
                    "database": str(database),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
