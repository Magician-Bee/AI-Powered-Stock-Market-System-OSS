#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any

from stock_ai.data_platform.contracts import TemporalCoordinates
from stock_ai.data_platform.service import MarketDataPlatform, stable_entity_id
from stock_ai.financial_revisions import query_financial_revision_history
from stock_ai.income_statement import query_income_statement_history


SYMBOL = "9000.TW"
PERIOD = "2025-Q4"
ENTITY_ID = stable_entity_id(
    market="taiwan",
    exchange="TWSE",
    source_code="9000",
)


def _record(revenue: float, acquired_at: str) -> dict[str, Any]:
    return {
        "statement_kind": "income_statement",
        "period": PERIOD,
        "fiscal_year": 2025,
        "quarter": 4,
        "symbol": SYMBOL,
        "name": "FIN-007 controlled restatement fixture",
        "statement_scope": "合併綜合損益表",
        "statement_semantics": "year_to_date_cumulative",
        "revenue": revenue,
        "gross_profit": 40.0,
        "operating_income": 30.0,
        "net_income": 20.0,
        "eps": 2.0,
        "current_quarter_revenue": None,
        "current_quarter_gross_profit": None,
        "current_quarter_operating_income": None,
        "current_quarter_net_income": None,
        "current_quarter_eps": None,
        "current_quarter_status": "not_disclosed_in_annual_summary",
        "unit": "thousand_twd",
        "eps_unit": "twd_per_share",
        "source": "MOPS-format controlled fixture",
        "source_id": "mops_archive",
        "source_url": "https://mops.twse.com.tw/controlled-fin007",
        "source_method": "POST",
        "source_market": "sii",
        "source_request_parameters": {
            "TYPEK": "sii",
            "co_id": "9000",
            "year": "114",
            "season": "04",
        },
        "raw_field_labels": {"revenue": "營業收入合計"},
        "acquired_at": acquired_at,
        "published_at": None,
        "available_at": acquired_at,
        "publication_time_status": "controlled_fixture_no_publication_time",
    }


def _write_revision(
    platform: MarketDataPlatform,
    *,
    revenue: float,
    acquired_at: str,
) -> str:
    revision = platform.warehouse.write_revision(
        dataset="fundamentals_quarterly",
        entity_id=ENTITY_ID,
        observation_key=PERIOD,
        source_id="mops_archive",
        temporal=TemporalCoordinates(
            time_basis="fiscal_period",
            fiscal_period="2025-12",
            period_start="2025-01-01",
            period_end="2025-12-31",
            observed_at="2025-12-31",
            published_at=None,
            available_at=acquired_at,
            acquired_at=acquired_at,
            effective_at="2025-12-31",
        ),
        payload=_record(revenue, acquired_at),
        raw_payload_id=None,
    )
    return revision.revision_id


def verify(database_path: Path) -> dict[str, Any]:
    platform = MarketDataPlatform(database_path=database_path)
    original_id = _write_revision(
        platform,
        revenue=100.0,
        acquired_at="2026-05-11T01:00:00+00:00",
    )
    restated_id = _write_revision(
        platform,
        revenue=105.0,
        acquired_at="2026-05-21T01:00:00+00:00",
    )
    early = query_financial_revision_history(
        SYMBOL,
        statement_kind="income_statement",
        period=PERIOD,
        knowledge_at="2026-05-12T00:00:00+00:00",
        platform=platform,
    )
    late = query_financial_revision_history(
        SYMBOL,
        statement_kind="income_statement",
        period=PERIOD,
        knowledge_at="2026-05-22T00:00:00+00:00",
        platform=platform,
    )
    early_statement = query_income_statement_history(
        SYMBOL,
        start_period=PERIOD,
        end_period=PERIOD,
        knowledge_at="2026-05-12T00:00:00+00:00",
        platform=platform,
    )
    late_statement = query_income_statement_history(
        SYMBOL,
        start_period=PERIOD,
        end_period=PERIOD,
        knowledge_at="2026-05-22T00:00:00+00:00",
        platform=platform,
    )
    restatement = next(
        item
        for item in late["versions"]
        if item["revision_id"] == restated_id
    )
    revenue_change = next(
        change
        for change in restatement["changes"]
        if change["field"] == "revenue"
    )
    checks = {
        "immutable_chain_has_two_versions": late["version_count"] == 2,
        "original_selected_before_restatement": (
            early["point_in_time"]["selected_revision_ids"] == [original_id]
        ),
        "restatement_selected_after_available": (
            late["point_in_time"]["selected_revision_ids"] == [restated_id]
        ),
        "same_source_restatement_linked": (
            restatement["supersedes_revision_id"] == original_id
        ),
        "before_after_difference_preserved": revenue_change == {
            "field": "revenue",
            "before": 100.0,
            "after": 105.0,
            "absolute_change": 5.0,
            "percent_change": 5.0,
        },
        "income_query_uses_original_as_known": (
            early_statement["items"][0]["revenue"] == 100.0
        ),
        "income_query_uses_restatement_when_known": (
            late_statement["items"][0]["revenue"] == 105.0
        ),
        "revision_chain_integrity_passed": late["status"] == "passed",
    }
    return {
        "schema_version": "stock_ai.fin007_acceptance.v1",
        "requirement_id": "FIN-007",
        "status": "passed" if all(checks.values()) else "failed",
        "fixture_origin": "controlled_restatement_acceptance_fixture",
        "fixture_is_claimed_as_real_mops_filing": False,
        "database_path": str(database_path.resolve()),
        "symbol": SYMBOL,
        "period": PERIOD,
        "original_revision_id": original_id,
        "restated_revision_id": restated_id,
        "checks": checks,
        "point_in_time": {
            "before_restatement": early["point_in_time"],
            "after_restatement": late["point_in_time"],
        },
        "difference": revenue_change,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify FIN-007 immutable financial restatements and point-in-time reads."
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        help="Persist the controlled fixture to this SQLite database.",
    )
    args = parser.parse_args()
    if args.database_path:
        args.database_path.parent.mkdir(parents=True, exist_ok=True)
        report = verify(args.database_path)
    else:
        with tempfile.TemporaryDirectory(prefix="stock-ai-fin007-") as directory:
            report = verify(Path(directory) / "financial-revisions.sqlite")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
