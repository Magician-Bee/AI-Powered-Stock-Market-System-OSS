#!/usr/bin/env python3
"""Deterministic STOCK-005 corporate-action and paper-account verifier."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.storage.sqlite_store import SQLiteStore


OFFICIAL_URL = "https://www.twse.com.tw/zh/announcement/index.html"


def action(symbol: str, action_type: str, **terms) -> dict:
    return {
        "source_event_id": f"verify-{symbol}-{action_type}",
        "symbol": symbol,
        "market": "TW",
        "effective_date": "2026-01-15",
        "action_type": action_type,
        "status": "confirmed",
        "official_verified": True,
        "source_id": "twse_official",
        "source_url": OFFICIAL_URL,
        "acquired_at": "2026-01-10T08:00:00+00:00",
        "source_payload": {"verifier": "STOCK-005"},
        **terms,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="stock-ai-corporate-actions-") as directory:
        store = SQLiteStore(db_path=Path(directory) / "verify.sqlite")
        oms = PaperOMS(store=store, initial_cash=1_000_000)
        symbols = [
            "CASH.TW",
            "STOCK.TW",
            "REDUCE.TW",
            "RIGHTS.TW",
            "SPLIT.TW",
            "REVERSE.TW",
            "MERGE.TW",
        ]
        for symbol in symbols:
            result = oms.submit_and_fill(
                {
                    "order_id": f"VERIFY-BUY-{symbol}",
                    "symbol": symbol,
                    "market": "TW",
                    "action": "buy",
                    "entry_price": 100,
                    "position_size_pct": 1,
                }
            )
            assert result["filled"]

        events = [
            action(
                "CASH.TW",
                "cash_dividend",
                cash_per_share=2,
                reference_price_before=100,
                reference_price_after=98,
            ),
            action(
                "STOCK.TW",
                "stock_dividend",
                share_multiplier=1.1,
                reference_price_before=100,
                reference_price_after=90.90909091,
            ),
            action(
                "REDUCE.TW",
                "capital_reduction",
                share_multiplier=0.8,
                cash_per_share=0.5,
                reference_price_before=100,
                reference_price_after=124.375,
            ),
            action(
                "RIGHTS.TW",
                "capital_increase",
                subscription_ratio=0.2,
                subscription_price=50,
                reference_price_before=100,
                reference_price_after=90,
            ),
            action(
                "SPLIT.TW",
                "split",
                share_multiplier=2,
                reference_price_before=100,
                reference_price_after=50,
            ),
            action(
                "REVERSE.TW",
                "reverse_split",
                share_multiplier=0.5,
                reference_price_before=100,
                reference_price_after=200,
            ),
            action(
                "MERGE.TW",
                "merger",
                successor_symbol="SUCCESSOR.TW",
                exchange_ratio=0.5,
                price_multiplier=2,
                cash_boot_per_share=1,
            ),
            action("CASH.TW", "treasury_stock"),
        ]
        imported = oms.import_corporate_actions(events)
        first = oms.sync_corporate_actions(as_of="2026-12-31")
        portfolio = oms.portfolio_summary()
        second = oms.sync_corporate_actions(as_of="2026-12-31")
        positions = {item["symbol"]: item for item in portfolio["positions"]}

        assert imported["created_count"] == 8
        assert first["applied_count"] == 8
        assert second["applied_count"] == 0
        assert second["idempotent_count"] == 8
        assert positions["STOCK.TW"]["quantity"] == 110
        assert positions["REDUCE.TW"]["quantity"] == 80
        assert positions["REDUCE.TW"]["average_cost"] == 124.375
        assert positions["SPLIT.TW"]["quantity"] == 200
        assert positions["REVERSE.TW"]["quantity"] == 50
        assert positions["RIGHTS.TW"]["quantity"] == 100
        assert positions["SUCCESSOR.TW"]["quantity"] == 50
        assert "MERGE.TW" not in positions
        assert portfolio["pending_entitlement_count"] == 1

        print(
            json.dumps(
                {
                    "schema_version": "stock_ai.stock_005_verification.v1",
                    "passed": True,
                    "official_action_count": imported["created_count"],
                    "action_types": sorted(
                        {item["action_type"] for item in imported["items"]}
                    ),
                    "first_sync_applied": first["applied_count"],
                    "second_sync_idempotent": second["idempotent_count"],
                    "position_count": portfolio["position_count"],
                    "cash_balance": portfolio["cash_balance"],
                    "pending_entitlements": portfolio["pending_entitlement_count"],
                    "ledger_tables": [
                        "corporate_action_revisions",
                        "paper_corporate_action_applications",
                        "paper_corporate_action_entitlements",
                        "cash_ledger",
                        "paper_positions",
                    ],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
