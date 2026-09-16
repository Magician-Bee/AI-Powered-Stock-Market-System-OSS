from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from stock_ai.data_platform.contracts import DataQuery, TemporalCoordinates
from stock_ai.data_platform.service import MarketDataPlatform


def verify(database_path: Path) -> dict[str, object]:
    platform = MarketDataPlatform(
        database_path=database_path,
        code_version="standard-market-warehouse-v1-acceptance",
    )
    temporal = TemporalCoordinates(
        available_at="2026-07-25T08:00:00+00:00",
        acquired_at="2026-07-25T08:01:00+00:00",
        effective_at="2026-07-25T00:00:00+00:00",
    )
    samples = {
        "prices": (
            "prices_daily",
            "twse_openapi",
            {"date": "2026-07-25", "symbol": "2330.TW", "close": 1150.0},
        ),
        "financials": (
            "revenues_monthly",
            "mops",
            {
                "report_date": "2026-07-10",
                "period": "2026-06",
                "symbol": "2330.TW",
                "name": "台積電",
                "current_revenue": 1000.0,
                "previous_revenue": 950.0,
                "last_year_revenue": 900.0,
                "mom_change_percent": 5.26,
                "yoy_change_percent": 11.11,
                "ytd_revenue": 5800.0,
                "last_ytd_revenue": 5200.0,
                "ytd_change_percent": 11.54,
                "source": "mops",
            },
        ),
        "flows": (
            "institutional_flows",
            "twse_openapi",
            {
                "trade_date": "2026-07-25",
                "symbol": "2330.TW",
                "name": "台積電",
                "foreign_buy": 5000.0,
                "foreign_sell": 3800.0,
                "foreign_net": 1200.0,
                "foreign_dealer_buy": 50.0,
                "foreign_dealer_sell": 25.0,
                "foreign_dealer_net": 25.0,
                "trust_buy": 900.0,
                "trust_sell": 600.0,
                "trust_net": 300.0,
                "dealer_buy": 700.0,
                "dealer_sell": 500.0,
                "dealer_net": 200.0,
                "dealer_hedge_net": 75.0,
                "total_institutional_net": 1725.0,
                "source": "twse_openapi",
            },
        ),
        "events": (
            "events",
            "mops",
            {
                "event_id": "EV-DATA007",
                "event_time": "2026-07-25",
                "symbol": "2330.TW",
                "title": "標準事件",
            },
        ),
        "macro": (
            "macro_series",
            "yahoo_finance",
            {"series_id": "US10Y", "period": "2026-07-25", "value": 4.1},
        ),
    }
    evidence: dict[str, object] = {}
    for domain, (dataset, source_id, record) in samples.items():
        revision = platform.warehouse.write_revision(
            dataset=dataset,
            entity_id=f"ENT-DATA007-{domain}",
            observation_key="2026-07-25",
            source_id=source_id,
            temporal=temporal,
            payload=record,
            raw_payload_id=None,
            transformation_id="stock_ai.standard_market_warehouse_acceptance.v1",
        )
        generic = platform.warehouse.query(
            DataQuery(
                dataset=dataset,
                entity_id=revision.entity_id,
                knowledge_at="2026-07-25T09:00:00+00:00",
                effective_at="2026-07-25T09:00:00+00:00",
            )
        )
        standard = platform.warehouse.standard_records(
            domain,
            dataset=dataset,
            entity_id=revision.entity_id,
            knowledge_at="2026-07-25T09:00:00+00:00",
            effective_at="2026-07-25T09:00:00+00:00",
        )
        if (
            len(generic) != 1
            or len(standard) != 1
            or generic[0].revision_id != standard[0]["revision_id"]
            or generic[0].payload != standard[0]["record"]
        ):
            raise RuntimeError(f"{domain} did not resolve to the shared warehouse revision")
        evidence[domain] = {
            "table": platform.warehouse.status()["standard_warehouse"]["domains"][domain][
                "table"
            ],
            "dataset": dataset,
            "revision_id": revision.revision_id,
            "shared_payload": True,
        }
    status = platform.warehouse.status()["standard_warehouse"]
    if status["record_count"] != 5:
        raise RuntimeError("Expected one standard record in every research domain")
    return {
        "schema_version": "stock_ai.standard_market_warehouse_acceptance.v1",
        "status": "passed",
        "read_path": status["read_path"],
        "record_count": status["record_count"],
        "domains": evidence,
        "database": str(database_path),
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    if args.database:
        result = verify(args.database.expanduser().resolve())
    else:
        with TemporaryDirectory(prefix="stock-ai-standard-warehouse-") as temp_dir:
            result = verify(Path(temp_dir) / "market-data.sqlite")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
