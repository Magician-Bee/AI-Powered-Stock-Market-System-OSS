#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.trading_restrictions import TradingRestrictionLedger
from open_stock_ai.storage.sqlite_store import SQLiteStore


AS_OF = "2026-07-28T05:00:00Z"
SOURCE_URL = "https://openapi.twse.com.tw/v1/announcement/notice"


def payload(kind: str, *, key: str, start: str = "2026-07-01", end: str | None = "2026-08-31"):
    return {
        "restriction_id": key,
        "symbol": "2330.TW",
        "restriction_type": kind,
        "status": "active",
        "effective_from": start,
        "effective_until": end,
        "official_verified": True,
        "source_id": "twse_openapi",
        "source_url": SOURCE_URL,
        "acquired_at": "2026-07-28T04:00:00Z",
        "source_payload": {"Code": "2330", "kind": kind, "record": key},
    }


def ticket(order_id: str, order_type: str = "market", **extra):
    return {
        "order_id": order_id,
        "symbol": "2330.TW",
        "side": "buy",
        "order_type": order_type,
        "time_in_force": "rod",
        "lot_type": "odd_lot",
        "session": "regular",
        "quantity_shares": 10,
        "actor": "codex",
        **extra,
    }


def market(price: float = 100, **extra):
    return {
        "price": price,
        "restriction_as_of": AS_OF,
        "limit_up": 110,
        "limit_down": 90,
        "trading_state": "trading",
        **extra,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="stock006-") as directory:
        store = SQLiteStore(db_path=Path(directory) / "verify.sqlite")
        oms = PaperOMS(store=store)
        broker = PaperBrokerSimulator(store=store, oms=oms)
        ledger = TradingRestrictionLedger(store)

        attention = ledger.import_restrictions([payload("attention_stock", key="ATTENTION")])
        attention_preview = broker.preview(ticket("ATTENTION-ORDER"), market())
        assert attention_preview["can_submit"] is True
        assert attention_preview["trading_restrictions"]["warnings"][0]["code"] == "attention_stock"

        ledger.import_restrictions([payload("disposition_stock", key="DISPOSITION")])
        disposition_market = broker.preview(ticket("DISPOSITION-MARKET"), market())
        disposition_limit = broker.preview(
            ticket("DISPOSITION-LIMIT", "limit", limit_price=101),
            market(),
        )
        assert disposition_market["can_submit"] is False
        assert disposition_limit["can_submit"] is True

        resting = broker.submit(
            ticket("RESTING", "limit", limit_price=95),
            market(),
        )
        assert resting["order"]["status"] == "open"
        ledger.import_restrictions(
            [payload("halt_trading", key="HALT", end=None)]
        )
        halted = broker.process_market_tick("2330.TW", market(94))
        assert halted["results"][0]["order"]["status"] == "open"
        ledger.import_restrictions(
            [
                payload(
                    "resume_trading",
                    key="RESUME",
                    start="2026-07-28T04:30:00Z",
                    end=None,
                )
            ]
        )
        resumed = broker.process_market_tick("2330.TW", market(94))
        assert resumed["results"][0]["order"]["status"] == "filled"

        post_restriction_market = market(restriction_as_of="2026-09-01T05:00:00Z")
        outside = broker.preview(
            ticket("OUTSIDE", "limit", limit_price=111),
            post_restriction_market,
        )
        locked = broker.preview(
            ticket("LOCKED"),
            market(110, restriction_as_of="2026-09-01T05:00:00Z"),
        )
        liquid = broker.preview(
            ticket("LIQUID"),
            market(
                110,
                buy_liquidity_confirmed=True,
                restriction_as_of="2026-09-01T05:00:00Z",
            ),
        )
        assert outside["validation"]["reason"] == "limit_price_above_daily_limit"
        assert locked["validation"]["reason"] == "limit_up_liquidity_unverified"
        assert liquid["can_submit"] is True

        report = {
            "schema_version": "stock_ai.stock006_verification.v1",
            "official_revision_count": ledger.list_restrictions(symbol="2330.TW")["count"],
            "attention_warn_only": attention_preview["can_submit"],
            "disposition_market_reason": disposition_market["validation"]["reason"],
            "disposition_limit_allowed": disposition_limit["can_submit"],
            "halt_kept_resting_order_open": halted["results"][0]["order"]["status"] == "open",
            "resume_filled_resting_order": resumed["results"][0]["order"]["status"] == "filled",
            "outside_limit_reason": outside["validation"]["reason"],
            "locked_market_reason": locked["validation"]["reason"],
            "idempotent_reimport": ledger.import_restrictions(
                [payload("attention_stock", key="ATTENTION")]
            )["idempotent_count"],
            "initial_import_created": attention["created_count"],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
