import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import RLock

import pytest

from open_stock_ai.storage.migrations import LATEST_SCHEMA_VERSION, ManagedSQLiteConnection
from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai.data_platform.warehouse import serialized_warehouse_write


def test_sqlite_store_enables_wal_busy_timeout_and_versioned_schema(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "open-stock-ai.sqlite")

    with store._connect() as conn:
        journal_mode = conn.execute("pragma journal_mode").fetchone()[0]
        busy_timeout = conn.execute("pragma busy_timeout").fetchone()[0]
        foreign_keys = conn.execute("pragma foreign_keys").fetchone()[0]
        user_version = conn.execute("pragma user_version").fetchone()[0]
        migration_version = conn.execute("select max(version) from schema_migrations").fetchone()[0]
        indexes = {
            row[1]
            for row in conn.execute("pragma index_list('signals')").fetchall()
        }
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'").fetchall()
        }
        broker_indexes = {
            row[1]
            for row in conn.execute("pragma index_list('paper_broker_orders')").fetchall()
        }
        daily_history_indexes = {
            row[1]
            for row in conn.execute("pragma index_list('market_prices')").fetchall()
        }
        adjustment_indexes = {
            row[1]
            for row in conn.execute("pragma index_list('data_revisions')").fetchall()
        }
        anomaly_indexes = {
            row[1]
            for row in conn.execute(
                "pragma index_list('trading_anomaly_events')"
            ).fetchall()
        }

    assert journal_mode.lower() == "wal"
    assert busy_timeout >= 5000
    assert foreign_keys == 1
    assert user_version == LATEST_SCHEMA_VERSION
    assert migration_version == LATEST_SCHEMA_VERSION
    assert "idx_signals_symbol_created_at" in indexes
    assert {"paper_broker_orders", "paper_broker_order_events"}.issubset(tables)
    assert {
        "intraday_candle_revisions",
        "intraday_candle_import_receipts",
    }.issubset(tables)
    assert {
        "agent_runs",
        "agent_steps",
        "agent_tool_calls",
        "agent_events",
        "agent_approvals",
        "agent_schedules",
    }.issubset(tables)
    assert {"policy_proposals", "strategy_versions"}.issubset(tables)
    assert "idx_broker_orders_account_status_created" in broker_indexes
    assert "idx_market_prices_daily_history" in daily_history_indexes
    assert "idx_market_prices_adjusted_history" in daily_history_indexes
    assert "idx_data_revisions_adjustment_events" in adjustment_indexes
    assert {
        "trading_anomaly_events",
        "trading_anomaly_observations",
        "trading_anomaly_tracking_actions",
    }.issubset(tables)
    assert "idx_trading_anomalies_symbol_date" in anomaly_indexes


def test_managed_connection_closes_after_transaction_context(tmp_path):
    path = tmp_path / "managed.sqlite"
    connection = sqlite3.connect(path, factory=ManagedSQLiteConnection)

    with connection as active:
        active.execute("create table sample (id integer primary key)")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("select 1")


def test_concurrent_store_initialization_serializes_schema_writes(tmp_path):
    path = tmp_path / "concurrent-schema.sqlite"

    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = list(executor.map(lambda _: SQLiteStore(db_path=path), range(8)))

    assert all(store.count("signals") == 0 for store in stores)


def test_serialized_warehouse_writer_retries_transient_sqlite_lock(monkeypatch):
    attempts = 0

    class Writer:
        _write_lock = RLock()

        @serialized_warehouse_write
        def write(self):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise sqlite3.OperationalError("database is locked")
            return "persisted"

    monkeypatch.setattr("stock_ai.data_platform.warehouse.time.sleep", lambda _delay: None)

    assert Writer().write() == "persisted"
    assert attempts == 3


def test_existing_unversioned_database_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "create table signals (id integer primary key autoincrement, created_at text not null, symbol text, market text, horizon text, action text, confidence real, risk_approved integer not null default 0, executed integer not null default 0, payload_json text not null)"
        )
        conn.execute(
            "insert into signals(created_at, symbol, payload_json) values ('2026-07-15T00:00:00+00:00', '2330.TW', '{}')"
        )
        conn.commit()

    store = SQLiteStore(db_path=path)

    assert store.count("signals") == 1
    with store._connect() as conn:
        assert conn.execute("select max(version) from schema_migrations").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert conn.execute(
            """
            select count(*) from sqlite_master
             where type='index'
               and name in (
                   'idx_financial_facts_monthly_revenue_period',
                   'idx_monthly_revenue_history_checkpoints'
               )
            """
        ).fetchone()[0] == 2
        assert conn.execute(
            "select count(*) from sqlite_master where type = 'table' and name = 'paper_broker_orders'"
        ).fetchone()[0] == 1
