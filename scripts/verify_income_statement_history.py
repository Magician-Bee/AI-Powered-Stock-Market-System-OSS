#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.income_statement import (
    INCOME_STATEMENT_DATASET,
    INCOME_STATEMENT_HISTORY_SCHEMA_VERSION,
    INCOME_STATEMENT_SOURCE_ID,
    backfill_income_statement_history,
    query_income_statement_history,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fin002-") as directory:
        database_path = Path(directory) / "verify.sqlite"
        platform = MarketDataPlatform(database_path=database_path)
        first = backfill_income_statement_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        second = backfill_income_statement_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        history = query_income_statement_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )

        assert first["sync"]["status"] == "succeeded", first["sync"]
        assert first["sync"]["fetched_period_count"] == 52
        assert first["coverage"]["is_complete"] is True
        assert first["coverage"]["covered_years"] == 13
        assert second["sync"]["fetched_period_count"] == 0
        assert second["sync"]["skipped_period_count"] == 52
        assert history["count"] == 52
        assert history["coverage"]["first_stored_period"] == "2013-Q1"
        assert history["coverage"]["last_stored_period"] == "2025-Q4"
        assert history["source_ids"] == [INCOME_STATEMENT_SOURCE_ID]

        latest = history["items"][0]
        for field in (
            "revenue",
            "gross_profit",
            "operating_income",
            "net_income",
            "eps",
        ):
            assert latest[field] is not None, (field, latest)
        assert latest["period"] == "2025-Q4"
        assert latest["current_quarter_revenue"] is None
        assert latest["current_quarter_eps"] is None
        assert latest["published_at"] is None
        assert latest["publication_time_status"] == "not_provided_by_archive"

        disclosed_q3 = next(
            item for item in history["items"] if item["period"] == "2025-Q3"
        )
        for field in (
            "current_quarter_revenue",
            "current_quarter_gross_profit",
            "current_quarter_operating_income",
            "current_quarter_net_income",
            "current_quarter_eps",
        ):
            assert disclosed_q3[field] is not None, (field, disclosed_q3)

        with sqlite3.connect(database_path) as conn:
            counts = {
                "financial_fact_revisions": conn.execute(
                    """
                    select count(*) from financial_facts
                     where dataset=? and source_id=?
                    """,
                    (INCOME_STATEMENT_DATASET, INCOME_STATEMENT_SOURCE_ID),
                ).fetchone()[0],
                "raw_archive_pages": conn.execute(
                    "select count(*) from raw_data_objects where source_id=?",
                    (INCOME_STATEMENT_SOURCE_ID,),
                ).fetchone()[0],
                "successful_symbol_quarter_checkpoints": conn.execute(
                    """
                    select count(*) from data_ingestion_checkpoints
                     where source_id=? and dataset=? and status='succeeded'
                       and partition_key like 'sii:%:2330.TW'
                    """,
                    (INCOME_STATEMENT_SOURCE_ID, INCOME_STATEMENT_DATASET),
                ).fetchone()[0],
            }
        assert counts == {
            "financial_fact_revisions": 52,
            "raw_archive_pages": 52,
            "successful_symbol_quarter_checkpoints": 52,
        }, counts
        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.fin002_verification.v1",
                    "contract": INCOME_STATEMENT_HISTORY_SCHEMA_VERSION,
                    "symbol": history["symbol"],
                    "period_range": [
                        history["coverage"]["first_stored_period"],
                        history["coverage"]["last_stored_period"],
                    ],
                    "stored_quarter_count": history["count"],
                    "covered_years": history["coverage"]["covered_years"],
                    "coverage_complete": history["coverage"]["is_complete"],
                    "repeat_sync_fetched_period_count": second["sync"][
                        "fetched_period_count"
                    ],
                    "repeat_sync_skipped_period_count": second["sync"][
                        "skipped_period_count"
                    ],
                    "latest_official_annual_values": {
                        "period": latest["period"],
                        "revenue_thousand_twd": latest["revenue"],
                        "gross_profit_thousand_twd": latest["gross_profit"],
                        "operating_income_thousand_twd": latest[
                            "operating_income"
                        ],
                        "net_income_thousand_twd": latest["net_income"],
                        "eps_twd_per_share": latest["eps"],
                        "source_url": latest["source_url"],
                    },
                    "q3_official_single_quarter_values": {
                        "period": disclosed_q3["period"],
                        "revenue_thousand_twd": disclosed_q3[
                            "current_quarter_revenue"
                        ],
                        "gross_profit_thousand_twd": disclosed_q3[
                            "current_quarter_gross_profit"
                        ],
                        "operating_income_thousand_twd": disclosed_q3[
                            "current_quarter_operating_income"
                        ],
                        "net_income_thousand_twd": disclosed_q3[
                            "current_quarter_net_income"
                        ],
                        "eps_twd_per_share": disclosed_q3[
                            "current_quarter_eps"
                        ],
                    },
                    "publication_time_status": latest[
                        "publication_time_status"
                    ],
                    "persistence": counts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
