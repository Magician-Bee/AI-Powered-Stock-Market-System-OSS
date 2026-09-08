#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from stock_ai.balance_sheet import backfill_balance_sheet_history
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.financial_ratios import (
    FINANCIAL_RATIO_SCHEMA_VERSION,
    query_financial_ratio_history,
)
from stock_ai.income_statement import backfill_income_statement_history


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fin005-") as directory:
        platform = MarketDataPlatform(
            database_path=Path(directory) / "verify.sqlite"
        )
        income = backfill_income_statement_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            max_workers=2,
            platform=platform,
        )
        for _ in range(4):
            if income["coverage"]["is_complete"]:
                break
            income = backfill_income_statement_history(
                "2330.TW",
                start_period="2013-Q1",
                end_period="2025-Q4",
                market_segment="sii",
                max_workers=2,
                platform=platform,
            )
        balance = backfill_balance_sheet_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            max_workers=2,
            platform=platform,
        )
        for _ in range(4):
            if balance["coverage"]["is_complete"]:
                break
            balance = backfill_balance_sheet_history(
                "2330.TW",
                start_period="2013-Q1",
                end_period="2025-Q4",
                market_segment="sii",
                max_workers=2,
                platform=platform,
            )
        ratios = query_financial_ratio_history(
            "2330.TW",
            start_period="2013-Q1",
            end_period="2025-Q4",
            market_segment="sii",
            platform=platform,
        )
        assert income["coverage"]["is_complete"] is True
        assert balance["coverage"]["is_complete"] is True
        assert ratios["coverage"]["is_complete"] is True
        assert ratios["count"] == 52
        latest = ratios["items"][0]
        assert latest["period"] == "2025-Q4"
        for field in (
            "gross_margin_percent",
            "operating_margin_percent",
            "net_margin_percent",
            "roe_percent",
            "roa_percent",
            "debt_ratio_percent",
        ):
            assert latest[field] is not None, (field, latest)
        assert latest["calculation_status"] == "complete"
        sources = latest["source_comparison"]
        assert sources["periods_match"] is True
        assert sources["income_statement"]["source_url"]
        assert sources["balance_sheet"]["source_url"]
        inputs = sources["income_statement"]["inputs"]
        balances = sources["balance_sheet"]["inputs"]
        assert latest["gross_margin_percent"] == (
            inputs["gross_profit"] / inputs["revenue"] * 100
        )
        assert latest["debt_ratio_percent"] == (
            balances["total_liabilities"] / balances["total_assets"] * 100
        )
        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.fin005_verification.v1",
                    "contract": FINANCIAL_RATIO_SCHEMA_VERSION,
                    "symbol": ratios["symbol"],
                    "period_range": [
                        ratios["start_period"],
                        ratios["end_period"],
                    ],
                    "ratio_period_count": ratios["count"],
                    "coverage_complete": ratios["coverage"]["is_complete"],
                    "latest_ratios_percent": {
                        field: latest[field]
                        for field in (
                            "gross_margin_percent",
                            "operating_margin_percent",
                            "net_margin_percent",
                            "roe_percent",
                            "roa_percent",
                            "debt_ratio_percent",
                        )
                    },
                    "calculation_contract": latest["calculation_contract"],
                    "source_urls": {
                        "income_statement": sources["income_statement"][
                            "source_url"
                        ],
                        "balance_sheet": sources["balance_sheet"]["source_url"],
                        "previous_balance_sheet": sources[
                            "previous_balance_sheet"
                        ]["source_url"],
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
