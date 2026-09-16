#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.growth_metrics import (
    GROWTH_HISTORY_SCHEMA_VERSION,
    query_growth_history,
)
from stock_ai.income_statement import backfill_income_statement_history
from stock_ai.monthly_revenue import backfill_monthly_revenue_history


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fin006-") as directory:
        platform = MarketDataPlatform(
            database_path=Path(directory) / "verify.sqlite"
        )
        monthly = backfill_monthly_revenue_history(
            "2330.TW",
            start_period="2024-01",
            end_period="2025-12",
            market_segment="sii",
            max_workers=2,
            platform=platform,
        )
        for _ in range(3):
            if monthly["coverage"]["is_complete"]:
                break
            monthly = backfill_monthly_revenue_history(
                "2330.TW",
                start_period="2024-01",
                end_period="2025-12",
                market_segment="sii",
                max_workers=2,
                platform=platform,
            )
        quarterly = backfill_income_statement_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            max_workers=2,
            platform=platform,
        )
        for _ in range(3):
            if quarterly["coverage"]["is_complete"]:
                break
            quarterly = backfill_income_statement_history(
                "2330.TW",
                start_period="2013-Q1",
                end_period="2025-Q4",
                market_segment="sii",
                max_workers=2,
                platform=platform,
            )
        growth = query_growth_history(
            "2330.TW",
            monthly_start_period="2024-01",
            monthly_end_period="2025-12",
            quarterly_start_period="2013-Q1",
            quarterly_end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        assert monthly["coverage"]["is_complete"] is True
        assert quarterly["coverage"]["is_complete"] is True
        assert growth["monthly"]["count"] == 24
        latest_month = growth["monthly"]["items"][0]
        for field in ("mom_percent", "yoy_percent", "ytd_yoy_percent"):
            assert latest_month[field] is not None, (field, latest_month)
        quarters = {
            item["period"]: item for item in growth["quarterly"]["items"]
        }
        assert quarters["2025-Q2"]["qoq_percent"] is not None
        assert quarters["2025-Q3"]["qoq_percent"] is not None
        assert quarters["2025-Q4"]["qoq_percent"] is None
        assert quarters["2025-Q4"]["comparison_status"] == (
            "official_single_quarter_value_missing"
        )
        assert growth["annual"]["count"] == 13
        latest_year = growth["annual"]["items"][0]
        for field in (
            "yoy_percent",
            "cagr_3y_percent",
            "cagr_5y_percent",
            "cagr_10y_percent",
        ):
            assert latest_year[field] is not None, (field, latest_year)
        available = growth["annual"]["available_range_cagr"]
        assert available["start_year"] == 2013
        assert available["end_year"] == 2025
        assert available["years"] == 12
        assert available["percent"] is not None
        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.fin006_verification.v1",
                    "contract": GROWTH_HISTORY_SCHEMA_VERSION,
                    "symbol": growth["symbol"],
                    "monthly_period_count": growth["monthly"]["count"],
                    "quarterly_period_count": growth["quarterly"]["count"],
                    "annual_period_count": growth["annual"]["count"],
                    "latest_month": latest_month,
                    "latest_q2_qoq_percent": quarters["2025-Q2"][
                        "qoq_percent"
                    ],
                    "latest_q3_qoq_percent": quarters["2025-Q3"][
                        "qoq_percent"
                    ],
                    "q4_comparison_status": quarters["2025-Q4"][
                        "comparison_status"
                    ],
                    "latest_annual_growth": latest_year,
                    "available_range_cagr": available,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
