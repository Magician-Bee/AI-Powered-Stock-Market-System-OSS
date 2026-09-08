#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from stock_ai.cash_flow_statement import (
    CASH_FLOW_DATASET,
    CASH_FLOW_HISTORY_SCHEMA_VERSION,
    CASH_FLOW_SOURCE_ID,
    backfill_cash_flow_history,
    query_cash_flow_history,
)
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.income_statement import backfill_income_statement_history


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fin004-") as directory:
        database_path = Path(directory) / "verify.sqlite"
        platform = MarketDataPlatform(database_path=database_path)
        backfill_income_statement_history(
            "2330.TW",
            start_period="2025-Q4",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        first = backfill_cash_flow_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        second = backfill_cash_flow_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        history = query_cash_flow_history(
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
        latest = history["items"][0]
        assert latest["period"] == "2025-Q4"
        for field in (
            "operating_cash_flow",
            "investing_cash_flow",
            "financing_cash_flow",
            "capital_expenditure",
            "free_cash_flow",
        ):
            assert latest[field] is not None, (field, latest)
        assert latest["free_cash_flow"] == (
            latest["operating_cash_flow"] - abs(latest["capital_expenditure"])
        )
        assert latest["profit_quality"]["status"] != "insufficient_data"
        assert latest["profit_quality"]["operating_cash_flow_to_net_income"] is not None

        with sqlite3.connect(database_path) as conn:
            counts = {
                "cash_flow_revisions": conn.execute(
                    """
                    select count(*) from financial_facts
                     where dataset=? and source_id=?
                       and observation_key like 'cash-flow:%'
                    """,
                    (CASH_FLOW_DATASET, CASH_FLOW_SOURCE_ID),
                ).fetchone()[0],
                "cash_flow_checkpoints": conn.execute(
                    """
                    select count(*) from data_ingestion_checkpoints
                     where dataset=? and source_id=? and status='succeeded'
                       and partition_key like 'cash-flow:%:2330.TW'
                    """,
                    (CASH_FLOW_DATASET, CASH_FLOW_SOURCE_ID),
                ).fetchone()[0],
            }
        assert counts == {
            "cash_flow_revisions": 52,
            "cash_flow_checkpoints": 52,
        }
        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.fin004_verification.v1",
                    "contract": CASH_FLOW_HISTORY_SCHEMA_VERSION,
                    "symbol": history["symbol"],
                    "period_range": [
                        history["coverage"]["first_stored_period"],
                        history["coverage"]["last_stored_period"],
                    ],
                    "stored_quarter_count": history["count"],
                    "repeat_sync_fetched_period_count": second["sync"][
                        "fetched_period_count"
                    ],
                    "latest_official_values": {
                        field: latest[field]
                        for field in (
                            "period",
                            "operating_cash_flow",
                            "investing_cash_flow",
                            "financing_cash_flow",
                            "capital_expenditure",
                            "free_cash_flow",
                            "source_url",
                        )
                    },
                    "profit_quality": latest["profit_quality"],
                    "persistence": counts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
