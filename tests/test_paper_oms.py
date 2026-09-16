from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, settings, strategies as st
import pytest

from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.governance.change_management import ChangeManagementRegistry
from open_stock_ai.governance.content_retention import ContentAddressedRetentionLedger
from open_stock_ai.governance.durable_store import SQLiteGovernanceStore, SQLiteRetentionStore
from open_stock_ai.storage.migrations import LATEST_SCHEMA_VERSION
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.storage.trade_store import TradeStore


_PROPERTY_SETTINGS = settings(derandomize=True, max_examples=35, deadline=None)


@st.composite
def _paper_order_sequence(draw: st.DrawFn) -> list[dict[str, object]]:
    """Generate approved paper orders, including legal retry attempts.

    Buy/add and sell/reduce may be rejected by the live account state.  That is
    intentional: the property asserts that both filled and rejected state paths
    preserve the same ledger invariants.
    """

    return draw(
        st.lists(
            st.fixed_dictionaries(
                {
                    "action": st.sampled_from(["buy", "add", "sell", "reduce"]),
                    "price_cents": st.integers(min_value=500, max_value=250_000),
                    "position_size_pct": st.integers(min_value=1, max_value=100),
                    "retry": st.booleans(),
                }
            ),
            min_size=1,
            max_size=28,
        )
    )


def _assert_paper_accounting_invariants(store: SQLiteStore, account_id: str) -> None:
    """Check persistence-level conservation, not just the response payload."""

    with store._connect() as conn:
        conn.row_factory = sqlite3.Row
        account = conn.execute(
            "select * from paper_accounts where account_id = ?", (account_id,)
        ).fetchone()
        assert account is not None
        orders = conn.execute(
            "select * from paper_orders where account_id = ? order by order_id", (account_id,)
        ).fetchall()
        fills = conn.execute(
            "select * from paper_fills where account_id = ? order by fill_id", (account_id,)
        ).fetchall()
        positions = conn.execute(
            "select * from paper_positions where account_id = ? order by symbol", (account_id,)
        ).fetchall()
        cash_entries = conn.execute(
            "select * from cash_ledger where account_id = ? order by id", (account_id,)
        ).fetchall()

    assert cash_entries
    running_cash = 0.0
    for entry in cash_entries:
        running_cash += float(entry["amount"])
        assert float(entry["balance_after"]) == pytest.approx(running_cash, abs=1e-6)
    assert float(account["cash_balance"]) == pytest.approx(running_cash, abs=1e-6)
    assert float(account["cash_balance"]) >= -1e-6

    filled_orders = {row["order_id"]: row for row in orders if row["status"] == "filled"}
    rejected_orders = {row["order_id"]: row for row in orders if row["status"] == "rejected"}
    assert len(fills) == len(filled_orders)
    assert len(cash_entries) == len(fills) + 1  # one immutable initial deposit

    signed_quantity = 0.0
    realized_from_positions = 0.0
    for fill in fills:
        order = filled_orders[fill["order_id"]]
        quantity = float(fill["quantity"])
        assert quantity > 0
        assert float(order["filled_quantity"]) == pytest.approx(quantity, abs=1e-8)
        assert float(fill["gross_amount"]) == pytest.approx(
            quantity * float(fill["fill_price"]), abs=1e-6
        )
        expected_cash_delta = (
            -(float(fill["gross_amount"]) + float(fill["commission"]))
            if fill["side"] == "buy"
            else float(fill["gross_amount"]) - float(fill["commission"]) - float(fill["tax"])
        )
        assert float(fill["net_cash_delta"]) == pytest.approx(expected_cash_delta, abs=1e-6)
        matching_entries = [entry for entry in cash_entries if entry["order_id"] == fill["order_id"]]
        assert len(matching_entries) == 1
        assert float(matching_entries[0]["amount"]) == pytest.approx(expected_cash_delta, abs=1e-6)
        signed_quantity += quantity if fill["side"] == "buy" else -quantity

    assert all(float(row["quantity"]) >= -1e-8 for row in positions)
    assert sum(float(row["quantity"]) for row in positions) == pytest.approx(
        signed_quantity, abs=1e-6
    )
    for row in positions:
        realized_from_positions += float(row["realized_pnl"])
    assert float(account["realized_pnl"]) == pytest.approx(realized_from_positions, abs=1e-6)
    assert all(
        not any(fill["order_id"] == order_id for fill in fills)
        for order_id in rejected_orders
    )


def _buy_order(order_id: str = "PAPER-BUY-1") -> dict:
    return {
        "order_id": order_id,
        "symbol": "2330.TW",
        "market": "TW",
        "action": "buy",
        "entry_price": 100.0,
        "position_size_pct": 10.0,
        "risk_approved": True,
    }


def test_paper_oms_creates_cash_fill_and_position_ledgers(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "100000")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_COMMISSION_BPS", "10")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SELL_TAX_BPS", "30")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SLIPPAGE_BPS", "5")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_LOT_SIZE", "1")
    store = SQLiteStore(db_path=tmp_path / "paper.sqlite")
    oms = PaperOMS(store=store)

    result = oms.submit_and_fill(_buy_order())

    assert result["filled"] is True
    assert result["order"]["status"] == "filled"
    assert result["fill"]["quantity"] == 100
    assert result["fill"]["fill_price"] == pytest.approx(100.05)
    portfolio = result["portfolio"]
    assert portfolio["is_simulated"] is True
    assert portfolio["position_count"] == 1
    assert portfolio["positions"][0]["symbol"] == "2330.TW"
    assert portfolio["positions"][0]["quantity"] == 100
    assert portfolio["cash_balance"] < 90000
    assert portfolio["valuation_basis"] == "cash_plus_latest_verified_market_marks_or_real_fills"

    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_orders").fetchone()[0] == 1
        assert conn.execute("select count(*) from paper_fills").fetchone()[0] == 1
        assert conn.execute("select count(*) from cash_ledger").fetchone()[0] == 2
        assert conn.execute("pragma user_version").fetchone()[0] == LATEST_SCHEMA_VERSION


def test_paper_oms_is_idempotent_by_order_id(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "100000")
    store = SQLiteStore(db_path=tmp_path / "paper.sqlite")
    oms = PaperOMS(store=store)

    first = oms.submit_and_fill(_buy_order())
    second = oms.submit_and_fill(_buy_order())

    assert first["filled"] is True
    assert second["filled"] is True
    assert second["idempotent"] is True
    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_orders").fetchone()[0] == 1
        assert conn.execute("select count(*) from paper_fills").fetchone()[0] == 1


def test_paper_oms_accumulates_partial_fills_with_fill_report_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "100000")
    store = SQLiteStore(db_path=tmp_path / "partial-paper.sqlite")
    oms = PaperOMS(store=store)
    order = _buy_order()

    first = oms.submit_partial_fill(order, fill_quantity=40, fill_id="FILL-partial-1")
    duplicate = oms.submit_partial_fill(order, fill_quantity=40, fill_id="FILL-partial-1")
    final = oms.submit_partial_fill(order, fill_quantity=60, fill_id="FILL-partial-2")

    assert first["filled"] is False
    assert first["partially_filled"] is True
    assert first["order"]["filled_quantity"] == 40
    assert first["order"]["remaining_quantity"] == 60
    assert duplicate["idempotent"] is True
    assert duplicate["order"]["filled_quantity"] == 40
    assert final["filled"] is True
    assert final["order"]["filled_quantity"] == 100
    assert final["order"]["remaining_quantity"] == 0
    assert final["order"]["average_fill_price"] == pytest.approx(first["fill"]["fill_price"])

    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_fills where order_id = ?", (order["order_id"],)).fetchone()[0] == 2
        assert conn.execute("select count(*) from cash_ledger where order_id = ?", (order["order_id"],)).fetchone()[0] == 2


def test_paper_oms_will_not_mutate_an_existing_order_with_a_different_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "100000")
    store = SQLiteStore(db_path=tmp_path / "partial-paper-contract.sqlite")
    oms = PaperOMS(store=store)
    order = _buy_order("PAPER-IMMUTABLE-CONTRACT-1")

    first = oms.submit_partial_fill(order, fill_quantity=40, fill_id="FILL-contract-1")
    altered = oms.submit_partial_fill(
        {**order, "symbol": "2317.TW"},
        fill_quantity=60,
        fill_id="FILL-contract-2",
    )

    assert first["partially_filled"] is True
    assert altered["idempotent"] is False
    assert altered["order_contract_error"] == "order_contract_mismatch"
    assert altered["order"]["symbol"] == "2330.TW"
    assert altered["order"]["filled_quantity"] == 40
    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_fills").fetchone()[0] == 1


def test_paper_oms_retains_a_partial_fill_when_the_remaining_buy_is_unaffordable(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "10000")
    store = SQLiteStore(db_path=tmp_path / "partial-paper-cash.sqlite")
    oms = PaperOMS(store=store)
    order = {**_buy_order("PAPER-PARTIAL-CASH-1"), "position_size_pct": 100.0}

    first = oms.submit_partial_fill(order, fill_quantity=50, fill_id="FILL-cash-1")
    remaining = oms.submit_partial_fill(
        {**order, "entry_price": 200.0},
        fill_quantity=50,
        fill_id="FILL-cash-2",
    )

    assert first["partially_filled"] is True
    assert remaining["partially_filled"] is True
    assert remaining["filled"] is False
    assert remaining["order"]["filled_quantity"] == 50
    assert remaining["order"]["remaining_quantity"] == 50
    assert remaining["order"]["rejection_reason"] == "insufficient_cash_for_remaining_quantity"
    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_fills").fetchone()[0] == 1


@_PROPERTY_SETTINGS
@given(steps=_paper_order_sequence())
def test_paper_oms_generated_order_sequences_preserve_persistent_accounting_invariants(
    steps: list[dict[str, object]],
) -> None:
    """Every generated paper-OMS trace conserves cash, quantity and evidence.

    This exercises the production SQLite-backed PaperOMS path, including failed
    sells, insufficient-cash rejections, configured fees/tax/slippage, and a
    replay of randomly selected idempotency keys.
    """

    with TemporaryDirectory(prefix="open-stock-ai-paper-oms-") as directory:
        store = SQLiteStore(db_path=Path(directory) / "paper.sqlite")
        account_id = "property-account"
        oms = PaperOMS(
            store=store,
            account_id=account_id,
            initial_cash=50_000.0,
            commission_bps=8.55,
            sell_tax_bps=30.0,
            slippage_bps=5.0,
            lot_size=1.0,
        )

        for index, step in enumerate(steps):
            order = {
                "order_id": f"PROPERTY-{index:03d}",
                "symbol": "2330.TW",
                "market": "TW",
                "action": step["action"],
                "entry_price": int(step["price_cents"]) / 100.0,
                "position_size_pct": step["position_size_pct"],
                "risk_approved": True,
            }
            first = oms.submit_and_fill(order)
            if step["retry"]:
                retry = oms.submit_and_fill(order)
                assert retry["idempotent"] is True
                assert retry["filled"] is first["filled"]
                assert retry["order"] == first["order"]
                assert retry["fill"] == first["fill"]
            _assert_paper_accounting_invariants(store, account_id)


def test_paper_oms_sell_updates_cash_and_realized_pnl(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "100000")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_COMMISSION_BPS", "10")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SELL_TAX_BPS", "30")
    store = SQLiteStore(db_path=tmp_path / "paper.sqlite")
    oms = PaperOMS(store=store)
    buy = oms.submit_and_fill(_buy_order())
    cash_after_buy = buy["portfolio"]["cash_balance"]

    sell = oms.submit_and_fill(
        {
            "order_id": "PAPER-SELL-1",
            "symbol": "2330.TW",
            "market": "TW",
            "action": "sell",
            "entry_price": 110.0,
            "position_size_pct": 100.0,
            "risk_approved": True,
        }
    )

    assert sell["filled"] is True
    assert sell["fill"]["quantity"] == 100
    assert sell["portfolio"]["position_count"] == 0
    assert sell["portfolio"]["cash_balance"] > cash_after_buy
    assert sell["portfolio"]["realized_pnl"] > 0
    assert sell["portfolio"]["order_count"] == 2
    assert sell["portfolio"]["fill_count"] == 2


def test_paper_oms_rejects_sell_without_position(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "paper.sqlite")
    oms = PaperOMS(store=store)

    result = oms.submit_and_fill(
        {
            "order_id": "PAPER-SELL-MISSING",
            "symbol": "2330.TW",
            "market": "TW",
            "action": "sell",
            "entry_price": 100.0,
            "position_size_pct": 100.0,
            "risk_approved": True,
        }
    )

    assert result["filled"] is False
    assert result["order"]["status"] == "rejected"
    assert result["order"]["rejection_reason"] == "insufficient_position"
    assert result["portfolio"]["position_count"] == 0


@pytest.mark.parametrize("action", ["buy", "add", "sell", "reduce"])
@pytest.mark.parametrize("unsafe_size", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_paper_oms_rejects_invalid_sizing_before_persisting_any_order(tmp_path, action, unsafe_size):
    store = SQLiteStore(db_path=tmp_path / f"paper-{action}.sqlite")
    oms = PaperOMS(store=store)
    result = oms.submit_and_fill(
        {
            "order_id": f"PAPER-{action}-{unsafe_size}",
            "symbol": "2330.TW",
            "market": "TW",
            "action": action,
            "entry_price": 100.0,
            "position_size_pct": unsafe_size,
            "risk_approved": True,
        }
    )

    assert result["filled"] is False
    assert result["order"]["rejection_reason"] == "missing_position_size"
    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_orders").fetchone()[0] == 0
        assert conn.execute("select count(*) from paper_fills").fetchone()[0] == 0


def test_paper_oms_rejects_unapproved_risk_before_persisting_order(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "paper.sqlite")
    oms = PaperOMS(store=store)

    result = oms.submit_and_fill({**_buy_order(), "risk_approved": False})

    assert result["filled"] is False
    assert result["order"]["rejection_reason"] == "risk_not_approved"
    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_orders").fetchone()[0] == 0


def test_paper_oms_requires_and_persists_an_immutable_change_set_binding(tmp_path) -> None:
    database = tmp_path / "paper-change-binding.sqlite"
    store = SQLiteStore(db_path=database)
    registry = ChangeManagementRegistry(SQLiteGovernanceStore(database))
    change = registry.pin_change(
        "paper-change-1",
        code_sha256="a" * 64,
        model_sha256="b" * 64,
        data_sha256="c" * 64,
        risk_policy_sha256="d" * 64,
        approved_by="owner",
        approved_at="2026-08-26T10:00:00+00:00",
    )
    oms = PaperOMS(
        store=store,
        change_management=registry,
        require_change_binding=True,
    )

    missing = oms.submit_and_fill(_buy_order("PAPER-NO-CHANGE"))
    assert missing["order"]["rejection_reason"] == "missing_host_approved_change_set_binding"
    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_orders").fetchone()[0] == 0

    filled = oms.submit_and_fill({**_buy_order("PAPER-WITH-CHANGE"), "change_id": change.change_id})
    binding = filled["order"]["change_binding"]
    assert filled["filled"] is True
    assert binding["change_id"] == change.change_id
    assert binding["change_sha256"] == change.change_sha256
    assert binding["risk_policy_sha256"] == "d" * 64

    restarted_registry = ChangeManagementRegistry(SQLiteGovernanceStore(database))
    restarted = PaperOMS(
        store=SQLiteStore(db_path=database),
        change_management=restarted_registry,
        require_change_binding=True,
    )
    retry = restarted.submit_and_fill({**_buy_order("PAPER-WITH-CHANGE"), "change_id": change.change_id})
    assert retry["idempotent"] is True
    assert retry["order"]["change_binding"] == binding

    with pytest.raises(ValueError, match="same durable database"):
        PaperOMS(
            store=store,
            change_management=ChangeManagementRegistry(SQLiteGovernanceStore(tmp_path / "other.sqlite")),
            require_change_binding=True,
        )


def test_trade_store_cannot_bypass_risk_rejection_to_reach_oms(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "paper.sqlite")
    trade_store = TradeStore(store=store)
    receipt = trade_store.save_paper_order(
        {
            "request": {"symbol": "2330.TW", "market": "TW", "horizon": "swing"},
            "signal": {
                "symbol": "2330.TW",
                "market": "TW",
                "action": "buy",
                "entry_price": 100.0,
                "position_size_pct": 10.0,
            },
            "risk": {"approved": False, "adjusted_position_size_pct": 10.0},
            "execution": {"order_id": "PAPER-BYPASS", "mode": "paper_pending_oms"},
        }
    )

    assert receipt["saved"] is False
    assert receipt["filled"] is False
    assert receipt["oms"]["order"]["rejection_reason"] == "risk_not_approved"
    with store._connect() as conn:
        assert conn.execute("select count(*) from paper_orders").fetchone()[0] == 0
        assert conn.execute("select count(*) from paper_fills").fetchone()[0] == 0


def test_trade_store_retains_immutable_critical_oms_snapshot_and_reuses_it(tmp_path):
    path = tmp_path / "paper-retention.sqlite"
    store = SQLiteStore(db_path=path)
    retention = ContentAddressedRetentionLedger(store=SQLiteRetentionStore(path))
    trade_store = TradeStore(store=store, retention_ledger=retention)
    payload = {
        "request": {"symbol": "2330.TW", "market": "TW", "horizon": "swing"},
        "signal": {
            "symbol": "2330.TW",
            "market": "TW",
            "action": "buy",
            "entry_price": 100.0,
            "position_size_pct": 10.0,
        },
        "risk": {"approved": True, "adjusted_position_size_pct": 10.0},
        "execution": {"order_id": "PAPER-RETENTION-1", "mode": "paper_pending_oms"},
    }

    first = trade_store.save_paper_order(payload)
    second = trade_store.save_paper_order(payload)

    assert first["retention"]["critical"] is True
    assert first["retention"]["kind"] == "execution_oms_state"
    assert second["retention"] == first["retention"]
    restarted = ContentAddressedRetentionLedger(store=SQLiteRetentionStore(path))
    assert restarted.get("paper-oms-execution-PAPER-RETENTION-1") == first["retention"]


def test_paper_oms_migrates_existing_version_one_database(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "create table schema_migrations (version integer primary key, name text not null, applied_at text not null default (datetime('now')))"
        )
        conn.execute("insert into schema_migrations(version, name) values (1, 'initial_ledgers_and_indexes')")
        conn.execute(
            "create table signals (id integer primary key autoincrement, created_at text not null, symbol text, market text, horizon text, action text, confidence real, risk_approved integer not null default 0, executed integer not null default 0, payload_json text not null)"
        )
        conn.execute("create table reports (id integer primary key autoincrement, created_at text not null, report_text text not null)")
        conn.execute("create table trades (id integer primary key autoincrement, created_at text not null, symbol text, action text, payload_json text not null)")
        conn.execute(
            "create table decision_logs (id integer primary key autoincrement, created_at text not null, symbol text, market text, horizon text, rating text, trader_action text, signal_action text, confidence real, entry_price real, target_price real, stop_loss real, position_size_pct real, risk_approved integer not null default 0, executed integer not null default 0, lesson text, payload_json text not null)"
        )
        conn.commit()

    store = SQLiteStore(db_path=path)
    PaperOMS(store=store)

    with store._connect() as conn:
        assert conn.execute("select max(version) from schema_migrations").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert conn.execute("select count(*) from paper_accounts").fetchone()[0] == 1
        assert conn.execute("select count(*) from paper_price_marks").fetchone()[0] == 0
        assert conn.execute("select count(*) from agent_learning_episodes").fetchone()[0] == 0
