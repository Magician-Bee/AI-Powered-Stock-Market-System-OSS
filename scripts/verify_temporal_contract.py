from __future__ import annotations

from argparse import ArgumentParser
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import json

import httpx

from stock_ai.data_platform.contracts import TemporalCoordinates, utc_now
from stock_ai.data_platform.service import MarketDataPlatform, stable_entity_id


def _roc_date(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 7 and text.isdigit():
        return f"{int(text[:3]) + 1911:04d}-{text[3:5]}-{text[5:7]}"
    raise RuntimeError(f"Unexpected ROC date: {value!r}")


def _roc_period(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 5 and text.isdigit():
        return f"{int(text[:3]) + 1911:04d}-{text[3:5]}"
    raise RuntimeError(f"Unexpected ROC fiscal period: {value!r}")


def _compact_date(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    raise RuntimeError(f"Unexpected compact trade date: {value!r}")


def _day_before(value: str) -> str:
    return (
        datetime.fromisoformat(value).replace(tzinfo=timezone.utc) - timedelta(days=1)
    ).isoformat()


def _day_after(value: str) -> str:
    return (
        datetime.fromisoformat(value).replace(tzinfo=timezone.utc) + timedelta(days=1)
    ).isoformat()


def _fetch(client: httpx.Client, endpoint: str) -> list[dict[str, Any]]:
    response = client.get(endpoint)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError(f"Expected non-empty object rows from {endpoint}")
    return payload


def _number(value: Any) -> float:
    text = str(value or "").strip().replace(",", "")
    return float(text) if text not in {"", "-", "--"} else 0.0


def verify(database_path: Path) -> dict[str, Any]:
    platform = MarketDataPlatform(
        database_path=database_path,
        code_version="temporal-contract-v1-acceptance",
    )
    registry = platform.source_registry_service
    revenue_endpoint = registry.endpoint("twse_revenue")
    institutional_endpoint = registry.endpoint("taifex_futures_institutional")
    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "StockAI-Temporal-Acceptance/1.0"},
    ) as client:
        revenue_rows = _fetch(client, revenue_endpoint)
        institutional_rows = _fetch(client, institutional_endpoint)

    acquired_at = utc_now()
    revenue_raw_id = platform.warehouse.record_raw_payload(
        source_id="mops",
        payload=revenue_rows,
        request_url=revenue_endpoint,
        requested_at=acquired_at,
        received_at=acquired_at,
        metadata={"dataset": "twse_revenue", "acceptance": "DATA-005"},
    )
    revenue_source = revenue_rows[0]
    report_date = _roc_date(revenue_source["出表日期"])
    fiscal_period = _roc_period(revenue_source["資料年月"])
    fiscal_year, fiscal_month = (
        int(value) for value in fiscal_period.split("-", 1)
    )
    period_start = f"{fiscal_period}-01"
    period_end = (
        f"{fiscal_period}-{monthrange(fiscal_year, fiscal_month)[1]:02d}"
    )
    revenue_code = str(revenue_source["公司代號"]).strip()
    revenue_payload = {
        "report_date": report_date,
        "period": fiscal_period,
        "symbol": f"{revenue_code}.TW",
        "name": str(revenue_source["公司名稱"]).strip(),
        "industry": str(revenue_source.get("產業別") or "").strip() or None,
        "current_revenue": _number(revenue_source["營業收入-當月營收"]),
        "previous_revenue": _number(revenue_source["營業收入-上月營收"]),
        "last_year_revenue": _number(revenue_source["營業收入-去年當月營收"]),
        "mom_change_percent": _number(
            revenue_source["營業收入-上月比較增減(%)"]
        ),
        "yoy_change_percent": _number(
            revenue_source["營業收入-去年同月增減(%)"]
        ),
        "ytd_revenue": _number(revenue_source["累計營業收入-當月累計營收"]),
        "last_ytd_revenue": _number(
            revenue_source["累計營業收入-去年累計營收"]
        ),
        "ytd_change_percent": _number(
            revenue_source["累計營業收入-前期比較增減(%)"]
        ),
        "note": str(revenue_source.get("備註") or "").strip() or None,
        "source": "TWSE OpenAPI /opendata/t187ap05_L",
    }
    revenue_revision = platform.warehouse.write_revision(
        dataset="revenues_monthly",
        entity_id=stable_entity_id(
            market="taiwan",
            exchange="TWSE",
            source_code=revenue_code,
        ),
        observation_key=report_date,
        source_id="mops",
        temporal=TemporalCoordinates(
            time_basis="fiscal_period",
            fiscal_period=fiscal_period,
            period_start=period_start,
            period_end=period_end,
            observed_at=period_end,
            published_at=report_date,
            available_at=report_date,
            acquired_at=acquired_at,
            effective_at=period_end,
        ),
        payload=revenue_payload,
        raw_payload_id=revenue_raw_id,
        quality_flags=["live_official_source"],
        transformation_id="stock_ai.twse_revenue_temporal_normalizer.v1",
        code_version="temporal-contract-v1-acceptance",
    )

    query_args = {
        "dataset": "revenues_monthly",
        "entity_id": revenue_revision.entity_id,
    }
    before_publication = platform.query(
        **query_args,
        knowledge_at=_day_before(report_date),
        effective_at=_day_after(period_end),
    )
    after_publication_before_acquisition = platform.query(
        **query_args,
        knowledge_at=_day_after(report_date),
        effective_at=_day_after(period_end),
    )
    before_period_effective = platform.query(
        **query_args,
        knowledge_at=acquired_at,
        effective_at=_day_before(period_end),
    )
    visible_revenue = platform.query(
        **query_args,
        knowledge_at=acquired_at,
        effective_at=_day_after(period_end),
    )
    if before_publication:
        raise RuntimeError("Fiscal data was visible before publication")
    if after_publication_before_acquisition:
        raise RuntimeError("Fiscal data was visible before local acquisition")
    if before_period_effective:
        raise RuntimeError("Fiscal data was visible before its period was effective")
    if [row.revision_id for row in visible_revenue] != [
        revenue_revision.revision_id
    ]:
        raise RuntimeError("Fiscal data was not visible after both cutoffs passed")

    institutional_raw_id = platform.warehouse.record_raw_payload(
        source_id="taifex_openapi",
        payload=institutional_rows,
        request_url=institutional_endpoint,
        requested_at=acquired_at,
        received_at=acquired_at,
        metadata={
            "dataset": "taifex_futures_institutional",
            "acceptance": "DATA-005",
        },
    )
    institutional_source = institutional_rows[0]
    trade_date = _compact_date(institutional_source["Date"])
    contract = str(institutional_source["ContractCode"]).strip()
    investor = str(institutional_source["Item"]).strip()
    institutional_revision = platform.warehouse.write_revision(
        dataset="institutional_futures",
        entity_id=stable_entity_id(
            market="taiwan",
            exchange="TAIFEX",
            source_code=contract,
        ),
        observation_key=f"{trade_date}:{investor}",
        source_id="taifex_openapi",
        temporal=TemporalCoordinates(
            time_basis="trade_date",
            trade_date=trade_date,
            observed_at=trade_date,
            published_at=trade_date,
            available_at=trade_date,
            acquired_at=acquired_at,
            effective_at=trade_date,
        ),
        payload={
            "trade_date": trade_date,
            "contract": contract,
            "investor": investor,
            "long_open_interest": int(
                institutional_source["OpenInterest(Long)"].replace(",", "")
            ),
            "short_open_interest": int(
                institutional_source["OpenInterest(Short)"].replace(",", "")
            ),
        },
        raw_payload_id=institutional_raw_id,
        quality_flags=["live_official_source"],
        transformation_id="stock_ai.taifex_institutional_temporal_normalizer.v1",
        code_version="temporal-contract-v1-acceptance",
    )
    hidden_trade = platform.query(
        dataset="institutional_futures",
        entity_id=institutional_revision.entity_id,
        knowledge_at=acquired_at,
        effective_at=_day_before(trade_date),
    )
    visible_trade = platform.query(
        dataset="institutional_futures",
        entity_id=institutional_revision.entity_id,
        knowledge_at=acquired_at,
        effective_at=_day_after(trade_date),
    )
    if hidden_trade:
        raise RuntimeError("Trade-date data was visible before its effective date")
    if [row.revision_id for row in visible_trade] != [
        institutional_revision.revision_id
    ]:
        raise RuntimeError("Trade-date data was not visible after both cutoffs passed")

    status = platform.warehouse.status()["tables"]
    if status["temporal_contract_violations"] != 0:
        raise RuntimeError(
            f"Temporal contract violations: {status['temporal_contract_violations']}"
        )
    if status["fiscal_period_revisions"] != 1:
        raise RuntimeError("Expected exactly one fiscal-period acceptance revision")
    if status["trade_date_revisions"] != 1:
        raise RuntimeError("Expected exactly one trade-date acceptance revision")

    return {
        "schema_version": "stock_ai.temporal_contract_acceptance.v1",
        "status": "passed",
        "knowledge_time_guards": {
            "before_publication_hidden": not before_publication,
            "before_acquisition_hidden": not after_publication_before_acquisition,
        },
        "effective_time_guards": {
            "before_fiscal_period_end_hidden": not before_period_effective,
            "before_trade_date_hidden": not hidden_trade,
        },
        "twse_revenue": {
            "source_endpoint": revenue_endpoint,
            "source_row_count": len(revenue_rows),
            "fiscal_period": fiscal_period,
            "publication_date": report_date,
            "revision_id": revenue_revision.revision_id,
        },
        "taifex_institutional": {
            "source_endpoint": institutional_endpoint,
            "source_row_count": len(institutional_rows),
            "trade_date": trade_date,
            "revision_id": institutional_revision.revision_id,
        },
        "warehouse": {
            "temporal_contract_revisions": status["temporal_contract_revisions"],
            "trade_date_revisions": status["trade_date_revisions"],
            "fiscal_period_revisions": status["fiscal_period_revisions"],
            "temporal_contract_violations": status[
                "temporal_contract_violations"
            ],
        },
        "acquired_at": acquired_at,
        "database": str(database_path),
    }


def main() -> int:
    parser = ArgumentParser(
        description="Verify DATA-005 bitemporal guards against live official data"
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Persist the acceptance database for UI verification",
    )
    args = parser.parse_args()
    if args.database:
        result = verify(args.database.expanduser().resolve())
    else:
        with TemporaryDirectory(prefix="stock-ai-temporal-verification-") as temp_dir:
            result = verify(Path(temp_dir) / "market-data.sqlite")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
