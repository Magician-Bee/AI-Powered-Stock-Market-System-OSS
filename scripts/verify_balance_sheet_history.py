#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from stock_ai.balance_sheet import (
    BALANCE_SHEET_DATASET,
    BALANCE_SHEET_HISTORY_SCHEMA_VERSION,
    BALANCE_SHEET_SOURCE_ID,
    backfill_balance_sheet_history,
    query_balance_sheet_history,
)
from stock_ai.data_platform.service import MarketDataPlatform


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fin003-") as directory:
        database_path = Path(directory) / "verify.sqlite"
        platform = MarketDataPlatform(database_path=database_path)
        first = backfill_balance_sheet_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        second = backfill_balance_sheet_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        history = query_balance_sheet_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        assert first["sync"]["status"] == "succeeded", first["sync"]
        assert first["sync"]["fetched_period_count"] == 52
        assert first["coverage"]["is_complete"] is True
        assert second["sync"]["fetched_period_count"] == 0
        assert second["sync"]["skipped_period_count"] == 52
        assert history["count"] == 52
        latest = history["items"][0]
        assert latest["period"] == "2025-Q4"
        for field in (
            "cash_and_cash_equivalents",
            "total_assets",
            "total_liabilities",
            "total_equity",
            "inventory",
            "accounts_receivable",
        ):
            assert latest[field] is not None, (field, latest)
            assert latest["quarter_comparison"]["fields"][field]["change"] is not None
        assert latest["quarter_comparison"]["previous_period"] == "2025-Q3"
        assert latest["published_at"] is None

        with sqlite3.connect(database_path) as conn:
            counts = {
                "financial_fact_revisions": conn.execute(
                    """
                    select count(*) from financial_facts
                     where dataset=? and source_id=?
                       and observation_key like 'balance-sheet:%'
                    """,
                    (BALANCE_SHEET_DATASET, BALANCE_SHEET_SOURCE_ID),
                ).fetchone()[0],
                "raw_archive_pages": conn.execute(
                    "select count(*) from raw_data_objects where source_id=?",
                    (BALANCE_SHEET_SOURCE_ID,),
                ).fetchone()[0],
                "successful_symbol_quarter_checkpoints": conn.execute(
                    """
                    select count(*) from data_ingestion_checkpoints
                     where source_id=? and dataset=? and status='succeeded'
                       and partition_key like 'balance-sheet:%:2330.TW'
                    """,
                    (BALANCE_SHEET_SOURCE_ID, BALANCE_SHEET_DATASET),
                ).fetchone()[0],
            }
        assert set(counts.values()) == {52}, counts
        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.fin003_verification.v1",
                    "contract": BALANCE_SHEET_HISTORY_SCHEMA_VERSION,
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
                    "latest_official_values": {
                        field: latest[field]
                        for field in (
                            "period",
                            "cash_and_cash_equivalents",
                            "total_assets",
                            "total_liabilities",
                            "total_equity",
                            "inventory",
                            "accounts_receivable",
                            "source_url",
                        )
                    },
                    "previous_period": latest["quarter_comparison"][
                        "previous_period"
                    ],
                    "persistence": counts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
