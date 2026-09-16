#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.monthly_revenue import (
    MONTHLY_REVENUE_DATASET,
    MONTHLY_REVENUE_HISTORY_SCHEMA_VERSION,
    MONTHLY_REVENUE_SOURCE_ID,
    backfill_monthly_revenue_history,
    query_monthly_revenue_history,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fin001-") as directory:
        database_path = Path(directory) / "verify.sqlite"
        platform = MarketDataPlatform(database_path=database_path)
        first = backfill_monthly_revenue_history(
            "2330.TW",
            start_period="2025-06",
            end_period="2026-06",
            market_segment="sii",
            platform=platform,
        )
        second = backfill_monthly_revenue_history(
            "2330.TW",
            start_period="2025-06",
            end_period="2026-06",
            market_segment="sii",
            platform=platform,
        )
        history = query_monthly_revenue_history(
            "2330.TW",
            start_period="2025-06",
            end_period="2026-06",
            market_segment="sii",
            platform=platform,
        )

        assert first["sync"]["status"] == "succeeded", first["sync"]
        assert first["sync"]["fetched_period_count"] == 13
        assert first["sync"]["record_period_count"] == 13
        assert first["coverage"]["is_complete"] is True
        assert second["sync"]["fetched_period_count"] == 0
        assert second["sync"]["skipped_period_count"] == 13
        assert history["count"] == 13
        assert history["coverage"]["first_stored_period"] == "2025-06"
        assert history["coverage"]["last_stored_period"] == "2026-06"
        assert history["source_ids"] == [MONTHLY_REVENUE_SOURCE_ID]
        latest = history["items"][0]
        assert latest["period"] == "2026-06"
        assert latest["current_revenue"] is not None
        assert latest["mom_change_percent"] is not None
        assert latest["yoy_change_percent"] is not None
        assert latest["ytd_revenue"] is not None
        assert latest["ytd_change_percent"] is not None
        assert latest["published_at"] is None
        assert latest["publication_time_status"] == "not_provided_by_archive"

        with sqlite3.connect(database_path) as conn:
            counts = {
                "financial_fact_revisions": conn.execute(
                    """
                    select count(*) from financial_facts
                     where dataset=? and source_id=?
                    """,
                    (MONTHLY_REVENUE_DATASET, MONTHLY_REVENUE_SOURCE_ID),
                ).fetchone()[0],
                "raw_archive_pages": conn.execute(
                    """
                    select count(*) from raw_data_objects
                     where source_id=?
                    """,
                    (MONTHLY_REVENUE_SOURCE_ID,),
                ).fetchone()[0],
                "successful_checkpoints": conn.execute(
                    """
                    select count(*) from data_ingestion_checkpoints
                     where source_id=? and dataset=? and status='succeeded'
                       and partition_key like 'sii:%:2330.TW'
                    """,
                    (MONTHLY_REVENUE_SOURCE_ID, MONTHLY_REVENUE_DATASET),
                ).fetchone()[0],
            }
        assert counts == {
            "financial_fact_revisions": 13,
            "raw_archive_pages": 13,
            "successful_checkpoints": 13,
        }, counts
        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.fin001_verification.v1",
                    "contract": MONTHLY_REVENUE_HISTORY_SCHEMA_VERSION,
                    "symbol": history["symbol"],
                    "period_range": [
                        history["coverage"]["first_stored_period"],
                        history["coverage"]["last_stored_period"],
                    ],
                    "stored_period_count": history["count"],
                    "coverage_complete": history["coverage"]["is_complete"],
                    "repeat_sync_fetched_period_count": second["sync"][
                        "fetched_period_count"
                    ],
                    "repeat_sync_skipped_period_count": second["sync"][
                        "skipped_period_count"
                    ],
                    "latest_official_values": {
                        "period": latest["period"],
                        "current_revenue_thousand_twd": latest["current_revenue"],
                        "mom_percent": latest["mom_change_percent"],
                        "yoy_percent": latest["yoy_change_percent"],
                        "ytd_revenue_thousand_twd": latest["ytd_revenue"],
                        "ytd_yoy_percent": latest["ytd_change_percent"],
                        "source_url": latest["source_url"],
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
