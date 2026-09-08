#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai.liquidity import (
    OfficialShareLedger,
    _persist_assessment,
    build_liquidity_assessment,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="stock007-") as directory:
        store = SQLiteStore(db_path=Path(directory) / "verify.sqlite")
        shares = OfficialShareLedger(store).import_record(
            symbol="2330.TW",
            issued_common_shares=10_000_000_000,
            effective_date="2026-07-27",
            source_id="twse_openapi",
            source_url="https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
            source_payload={
                "出表日期": "1150727",
                "公司代號": "2330",
                "已發行普通股數或TDR原股發行股數": "10000000000",
            },
        )
        points = [
            {
                "date": f"2026-07-{index + 1:02d}",
                "open": 99,
                "high": 101,
                "low": 98,
                "close": 100,
                "volume": 2_000_000,
                "turnover": 200_000_000,
            }
            for index in range(20)
        ]
        quote = {
            "provider": "twse_mis",
            "received_at": "2026-07-28T05:00:00Z",
            "total_volume_shares": 1_500_000,
            "bids": [{"price": 100, "size": 50}],
            "asks": [{"price": 100.1, "size": 40}],
        }
        complete = build_liquidity_assessment(
            symbol="2330.TW",
            history_points=points,
            history_source_ids=["twse_official_web"],
            quote=quote,
            share_revision=shares,
            order_quantity_shares=10_000,
            assessed_at="2026-07-28T05:00:00Z",
        )
        missing = build_liquidity_assessment(
            symbol="2330.TW",
            history_points=points,
            history_source_ids=["twse_official_web"],
            quote=None,
            share_revision=None,
        )
        _persist_assessment(store, complete)
        assert complete["tradability"]["status"] == "highly_tradeable"
        assert complete["metrics"]["turnover_rate_percent"] == 0.015
        assert complete["order_assessment"]["estimated_slippage_bps"] > 0
        assert missing["metrics"]["turnover_rate_percent"] is None
        assert missing["order_assessment"]["estimated_slippage_bps"] is None
        assert missing["tradability"]["status"] == "insufficient_data"
        with store._connect() as conn:
            stored = conn.execute(
                "select count(*) from liquidity_assessments"
            ).fetchone()[0]
        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.stock007_verification.v1",
                    "share_revision": shares["revision"],
                    "average_volume_shares": complete["metrics"][
                        "average_daily_volume_shares"
                    ],
                    "average_turnover_twd": complete["metrics"][
                        "average_daily_turnover_twd"
                    ],
                    "spread_bps": complete["metrics"]["bid_ask_spread_bps"],
                    "turnover_rate_percent": complete["metrics"][
                        "turnover_rate_percent"
                    ],
                    "estimated_slippage_bps": complete["order_assessment"][
                        "estimated_slippage_bps"
                    ],
                    "tradability": complete["tradability"]["status"],
                    "missing_inputs_fail_closed": missing["tradability"]["status"],
                    "stored_assessments": stored,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
