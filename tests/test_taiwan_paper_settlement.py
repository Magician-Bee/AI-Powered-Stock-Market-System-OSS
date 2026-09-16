from __future__ import annotations

from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.taiwan_settlement import settlement_timestamp
from open_stock_ai.governance.content_retention import ContentAddressedRetentionLedger
from open_stock_ai.governance.durable_store import SQLiteRetentionStore
from open_stock_ai.storage.sqlite_store import SQLiteStore


def _broker(tmp_path) -> PaperBrokerSimulator:
    store = SQLiteStore(db_path=tmp_path / "settlement.sqlite")
    oms = PaperOMS(
        store=store,
        account_id="settlement-test",
        initial_cash=10_000.0,
        commission_bps=0.0,
        sell_tax_bps=0.0,
        slippage_bps=0.0,
        lot_size=1.0,
    )
    return PaperBrokerSimulator(store=store, oms=oms)


def _market(*, price: float, at: str) -> dict[str, object]:
    return {
        "symbol": "2330.TW",
        "market": "TW",
        "price": price,
        "source_timestamp": at,
        "is_realtime": True,
        "is_fallback": False,
        "settlement_rules_enforced": True,
    }


def _ticket(order_id: str, *, side: str, quantity: int) -> dict[str, object]:
    return {
        "order_id": order_id,
        "symbol": "2330.TW",
        "market": "TW",
        "side": side,
        "order_type": "market",
        "time_in_force": "rod",
        "lot_type": "odd_lot",
        "session": "regular",
        "quantity_shares": quantity,
        "actor": "user",
    }


def test_taiwan_t_plus_2_calendar_skips_official_holidays() -> None:
    due = settlement_timestamp("2026-02-11T10:00:00+08:00")

    # 2/12 and the following Lunar New Year dates are official closures; the
    # two next trading days are 2/23 and 2/24.
    assert due.isoformat() == "2026-02-24T11:00:00+08:00"


def test_exchange_paper_cash_is_held_until_t_plus_2_and_sale_receivable_is_not_spendable(tmp_path) -> None:
    broker = _broker(tmp_path)
    buy = broker.submit(
        _ticket("PB-SETTLE-BUY", side="buy", quantity=10),
        _market(price=100.0, at="2026-08-19T02:00:00+00:00"),
    )

    after_buy = buy["portfolio"]
    assert after_buy["cash_balance"] == 9_000.0
    assert after_buy["settled_cash_balance"] == 10_000.0
    assert after_buy["available_cash"] == 9_000.0
    assert after_buy["unsettled_payable"] == 1_000.0
    assert after_buy["unsettled_receivable"] == 0.0
    assert after_buy["settlements"][0]["settlement_due_at"] == "2026-08-21T11:00:00+08:00"
    assert after_buy["settlements"][0]["status"] == "pending"

    before_cutoff = broker.oms.settle_due(as_of="2026-08-21T10:59:59+08:00")
    assert before_cutoff["settled_count"] == 0
    at_cutoff = broker.oms.settle_due(as_of="2026-08-21T11:00:00+08:00")
    assert at_cutoff["settled_count"] == 1
    assert at_cutoff["account"]["settled_cash_balance"] == 9_000.0
    assert at_cutoff["account"]["available_cash"] == 9_000.0
    assert at_cutoff["account"]["pending_settlement_count"] == 0

    sell = broker.submit(
        _ticket("PB-SETTLE-SELL", side="sell", quantity=5),
        _market(price=110.0, at="2026-08-24T02:00:00+00:00"),
    )
    after_sell = sell["portfolio"]
    assert after_sell["cash_balance"] == 9_550.0
    assert after_sell["settled_cash_balance"] == 9_000.0
    assert after_sell["available_cash"] == 9_000.0
    assert after_sell["unsettled_receivable"] == 550.0

    preview = broker.preview(
        _ticket("PB-SETTLE-REUSE", side="buy", quantity=95),
        _market(price=100.0, at="2026-08-24T02:00:00+00:00"),
    )
    assert preview["validation"]["valid"] is False
    assert preview["validation"]["reason"] == "insufficient_settled_cash"
    assert preview["validation"]["available_cash"] == 9_000.0

    tick = broker.process_market_tick(
        "2330.TW",
        _market(price=110.0, at="2026-08-26T03:00:00+00:00"),
    )
    assert tick["settlements"]["settled_count"] == 1
    after_sale_settlement = tick["settlements"]["account"]
    assert after_sale_settlement["cash_balance"] == 9_550.0
    assert after_sale_settlement["settled_cash_balance"] == 9_550.0
    assert after_sale_settlement["available_cash"] == 9_550.0
    assert after_sale_settlement["pending_settlement_count"] == 0

    restarted = PaperOMS(store=broker.store, account_id="settlement-test")
    persisted = restarted.portfolio_summary()
    assert persisted["settled_cash_balance"] == 9_550.0
    assert [item["status"] for item in persisted["settlements"]] == ["settled", "settled"]
    with broker.store._connect() as conn:
        assert conn.execute("select count(*) from paper_settlement_events").fetchone()[0] == 4


def test_due_settlement_balance_change_has_durable_critical_receipt(tmp_path) -> None:
    database = tmp_path / "settlement-retention.sqlite"
    store = SQLiteStore(db_path=database)
    retention = ContentAddressedRetentionLedger(store=SQLiteRetentionStore(database))
    oms = PaperOMS(
        store=store,
        account_id="settlement-retention",
        initial_cash=10_000.0,
        retention_ledger=retention,
    )
    broker = PaperBrokerSimulator(store=store, oms=oms, retention_ledger=retention)
    broker.submit(
        _ticket("PB-SETTLE-RETAIN", side="buy", quantity=10),
        _market(price=100.0, at="2026-08-19T02:00:00+00:00"),
    )

    settled = oms.settle_due(as_of="2026-08-21T11:00:00+08:00")

    assert settled["settled_count"] == 1
    restarted = ContentAddressedRetentionLedger(store=SQLiteRetentionStore(database))
    records = [
        item for item in restarted._records.values()
        if item["kind"] == "execution_settlement"
    ]
    assert len(records) == 1
    assert records[0]["critical"] is True
    assert records[0]["payload"] == settled


def test_legacy_paper_fill_keeps_immediate_cash_contract_without_settlement_claim(tmp_path) -> None:
    store = SQLiteStore(db_path=tmp_path / "legacy.sqlite")
    oms = PaperOMS(store=store, account_id="legacy", initial_cash=1_000.0)

    result = oms.submit_and_fill(
        {
            "order_id": "LEGACY-BUY",
            "symbol": "2330.TW",
            "market": "TW",
            "action": "buy",
            "entry_price": 100.0,
            "position_size_pct": 10.0,
            "risk_approved": True,
        }
    )

    assert result["portfolio"]["cash_balance"] == 900.0
    assert result["portfolio"]["available_cash"] == 900.0
    assert result["portfolio"]["settlements"] == []
