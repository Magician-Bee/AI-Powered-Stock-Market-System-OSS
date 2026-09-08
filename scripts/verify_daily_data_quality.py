from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zoneinfo import ZoneInfo
import json

from stock_ai.data_platform.contracts import TemporalCoordinates
from stock_ai.data_platform.service import MarketDataPlatform


def verify(database_path: Path) -> dict[str, Any]:
    platform = MarketDataPlatform(database_path=database_path)

    def write_price(
        *,
        entity_id: str,
        close: float | None,
        source_id: str = "twse_openapi",
        payload_trade_date: str = "2026-07-20",
    ) -> None:
        payload: dict[str, Any] = {"trade_date": payload_trade_date}
        if close is not None:
            payload["close"] = close
        platform.warehouse.write_revision(
            dataset="prices_daily",
            entity_id=entity_id,
            observation_key="2026-07-20",
            source_id=source_id,
            temporal=TemporalCoordinates(
                time_basis="trade_date",
                trade_date="2026-07-20",
                observed_at="2026-07-20",
                published_at="2026-07-21T01:00:00+00:00",
                available_at="2026-07-21T01:00:00+00:00",
                acquired_at="2026-07-21T02:00:00+00:00",
                effective_at="2026-07-20",
            ),
            payload=payload,
            raw_payload_id=None,
        )

    for index, close in enumerate((100.0, 101.0, 99.0, 100.0, 102.0, 1000.0)):
        write_price(entity_id=f"ENT-quality-{index}", close=close)
    write_price(entity_id="ENT-quality-missing", close=None)
    write_price(
        entity_id="ENT-quality-time",
        close=100.0,
        payload_trade_date="2026-07-19",
    )
    for close in (10.0, 11.0, 10.0):
        write_price(entity_id="ENT-quality-duplicate", close=close)
    write_price(entity_id="ENT-quality-conflict", close=50.0)
    write_price(
        entity_id="ENT-quality-conflict",
        close=70.0,
        source_id="yahoo_finance",
    )
    reconciliation = platform.warehouse.reconcile(
        dataset="prices_daily",
        entity_id="ENT-quality-conflict",
        observation_key="2026-07-20",
        field_names=["close"],
        tolerance=0.1,
        as_of="2099-01-01T00:00:00+00:00",
    )
    if reconciliation["status"] != "conflict":
        raise RuntimeError(f"Source conflict was not preserved: {reconciliation}")

    today = datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()
    report = platform.daily_quality_report(
        dataset="prices_daily",
        report_date=today,
    )
    repeated = platform.daily_quality_report(
        dataset="prices_daily",
        report_date=today,
    )
    detail = platform.quality_report_detail(report["report_id"])
    if detail is None:
        raise RuntimeError("Persisted daily quality report is missing")
    categories = {issue["category"] for issue in detail["issues"]}
    expected_categories = {
        "missing",
        "anomaly",
        "time_misalignment",
        "duplicate",
        "source_conflict",
    }
    if not expected_categories <= categories:
        raise RuntimeError(
            f"Daily report missed issue classes: {sorted(expected_categories - categories)}"
        )
    if repeated["report_id"] != report["report_id"]:
        raise RuntimeError("Unchanged daily state created a duplicate quality report")
    status = platform.status()["warehouse"]["data_quality"]
    if status["report_count"] != 1 or status["issue_count"] != report["issue_count"]:
        raise RuntimeError(f"Data quality status does not match persisted evidence: {status}")

    return {
        "schema_version": "stock_ai.daily_data_quality_verification.v1",
        "status": "passed",
        "report_id": report["report_id"],
        "report_date": report["report_date"],
        "report_status": report["status"],
        "row_count": report["row_count"],
        "issue_count": report["issue_count"],
        "counts": {
            "missing": report["missing_count"],
            "anomaly": report["anomaly_count"],
            "time_misalignment": report["time_misalignment_count"],
            "duplicate": report["duplicate_count"],
            "source_conflict": report["conflict_count"],
        },
        "categories": sorted(categories),
        "deterministic_reuse": repeated["report_id"] == report["report_id"],
        "persisted_status": status,
        "database": str(database_path),
    }


def main() -> None:
    with TemporaryDirectory(prefix="stock-ai-daily-quality-") as temp_dir:
        print(
            json.dumps(
                verify(Path(temp_dir) / "market-data.sqlite"),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
