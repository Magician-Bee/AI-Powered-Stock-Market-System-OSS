#!/usr/bin/env python3
from __future__ import annotations

from argparse import ArgumentParser
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3

from fastapi.testclient import TestClient

import stock_ai.data_platform.api as data_api
import stock_ai.main as stock_main
from stock_ai.data_platform import MarketDataPlatform, TemporalCoordinates


def parser() -> ArgumentParser:
    result = ArgumentParser(description="Verify DATA-015 complete data lineage")
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
        else TemporaryDirectory(prefix="stock-ai-data-lineage-")
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
            code_version="verify-data-015",
        )
        revisions = []
        closes = [100.0, 105.0, 110.0]
        for index, close in enumerate(closes, start=1):
            day = 20 + index
            timestamp = f"2026-07-{day:02d}T08:00:00+00:00"
            raw_payload_id = platform.warehouse.record_raw_payload(
                source_id="twse_openapi",
                payload={"date": timestamp[:10], "close": close},
                request_url=f"https://openapi.twse.com.tw/lineage/{day}",
                requested_at=timestamp,
                received_at=timestamp,
                metadata={"dataset": "prices_daily", "fixture": "DATA-015"},
            )
            revisions.append(
                platform.warehouse.write_revision(
                    dataset="prices_daily",
                    entity_id="ENT-DATA015",
                    observation_key=timestamp[:10],
                    source_id="twse_openapi",
                    temporal=TemporalCoordinates(
                        available_at=timestamp,
                        acquired_at=timestamp,
                        effective_at=timestamp,
                    ),
                    payload={"close": close},
                    raw_payload_id=raw_payload_id,
                    transformation_id="stock_ai.price_normalizer.v1",
                    code_version="verify-data-015",
                )
            )

        indicator = platform.warehouse.record_lineage_artifact(
            artifact_type="indicator",
            name="sma_3",
            entity_id="ENT-DATA015",
            observation_key="2026-07-23",
            value={"value": 105.0, "window": 3},
            inputs=[
                {
                    "kind": "revision",
                    "id": revision.revision_id,
                    "role": f"close_{index}",
                    "fields": ["/close"],
                }
                for index, revision in enumerate(revisions, start=1)
            ],
            transformation_id="stock_ai.indicator.sma.v1",
            code_version="verify-data-015",
            parameters={"window": 3},
        )
        conclusion = platform.warehouse.record_lineage_artifact(
            artifact_type="conclusion",
            name="price_above_sma_3",
            entity_id="ENT-DATA015",
            observation_key="2026-07-23",
            value={"state": "above", "difference": 5.0},
            inputs=[
                {
                    "kind": "revision",
                    "id": revisions[-1].revision_id,
                    "role": "latest_close",
                    "fields": ["/close"],
                },
                {
                    "kind": "artifact",
                    "id": indicator["artifact_id"],
                    "role": "sma_baseline",
                    "fields": ["/value"],
                },
            ],
            transformation_id="stock_ai.conclusion.price_vs_sma.v1",
            code_version="verify-data-015",
            parameters={"comparison": "close > sma_3"},
        )
        graph = platform.warehouse.lineage_graph(conclusion["artifact_id"])
        expected_transformations = {
            "stock_ai.price_normalizer.v1",
            "stock_ai.indicator.sma.v1",
            "stock_ai.conclusion.price_vs_sma.v1",
        }
        if graph["completeness"]["status"] != "complete":
            raise AssertionError(graph["completeness"])
        if graph["completeness"]["raw_payload_count"] != 3:
            raise AssertionError("conclusion did not retain all three raw observations")
        if not expected_transformations.issubset(graph["transformation_ids"]):
            raise AssertionError("multi-stage transformation chain is incomplete")
        if graph["sources"] != ["twse_openapi"]:
            raise AssertionError("source identity did not reach the conclusion")

        with sqlite3.connect(database) as conn:
            try:
                conn.execute(
                    "update data_lineage_artifacts set name='mutated' where artifact_id=?",
                    (conclusion["artifact_id"],),
                )
            except sqlite3.IntegrityError as exc:
                if "immutable" not in str(exc):
                    raise
            else:
                raise AssertionError("derived lineage artifacts are mutable")

        data_api.get_market_data_platform = lambda: platform
        client = TestClient(stock_main.app)
        api_paths = [
            "/api/data/status",
            "/api/data/lineage/artifacts?limit=10",
            f"/api/data/lineage/{conclusion['artifact_id']}",
            f"/api/data/revisions/{revisions[-1].revision_id}/lineage",
        ]
        responses = {path: client.get(path) for path in api_paths}
        failures = {
            path: response.status_code
            for path, response in responses.items()
            if response.status_code != 200
        }
        if failures:
            raise AssertionError(f"lineage API smoke requests failed: {failures}")
        status = responses["/api/data/status"].json()["warehouse"]["data_lineage"]
        if status["status"] != "complete" or status["artifact_count"] != 2:
            raise AssertionError(f"unexpected lineage status: {status}")

        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.complete_data_lineage_verification.v1",
                    "status": "passed",
                    "database": str(database),
                    "indicator_artifact_id": indicator["artifact_id"],
                    "conclusion_artifact_id": conclusion["artifact_id"],
                    "graph": {
                        "sources": graph["sources"],
                        "raw_payload_count": graph["completeness"][
                            "raw_payload_count"
                        ],
                        "revision_count": sum(
                            1 for node in graph["nodes"] if node["type"] == "revision"
                        ),
                        "artifact_count": sum(
                            1 for node in graph["nodes"] if node["type"] == "artifact"
                        ),
                        "transformation_ids": graph["transformation_ids"],
                        "max_depth": graph["completeness"]["max_depth"],
                        "completeness": graph["completeness"]["status"],
                    },
                    "write_policy": status["write_policy"],
                    "immutability": "passed",
                    "api_requests": {
                        path: response.status_code
                        for path, response in responses.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
