from __future__ import annotations

import json
import sqlite3
from calendar import monthrange
from datetime import datetime, timezone

from .p71_schema import verify_p71_schema


LATEST_SCHEMA_VERSION = 47


class ManagedSQLiteConnection(sqlite3.Connection):
    """A sqlite connection whose ``with`` blocks also release the file handle.

    ``sqlite3.Connection.__exit__`` commits or rolls back but intentionally
    leaves the connection open.  The application uses short-lived connections
    for every API request, so retaining those descriptors eventually exhausts
    the desktop process and turns normal UI requests into database errors.
    """

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma busy_timeout = 5000")


def apply_migrations(conn: sqlite3.Connection) -> None:
    configure_connection(conn)
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma synchronous = normal")
    conn.execute(
        """
        create table if not exists schema_migrations (
            version integer primary key,
            name text not null,
            applied_at text not null default (datetime('now'))
        )
        """
    )

    applied_versions = {
        int(row[0]) for row in conn.execute("select version from schema_migrations")
    }
    current = max(applied_versions, default=0)
    starting_version = int(current)
    needs_entity_repair = 13 not in applied_versions
    needs_envelope_repair = 14 not in applied_versions
    needs_temporal_repair = 15 not in applied_versions
    needs_revision_trigger_repair = (
        19 not in applied_versions
        or needs_envelope_repair
        or needs_temporal_repair
    )
    needs_quality_repair = 20 not in applied_versions
    # Immutable revision triggers only need to move out of the way while the
    # migrations that introduced/backfilled those rows are still pending.
    # Dropping and recreating them on every connection turns a read-only schema
    # check into a schema write and causes severe WAL growth on large databases.
    if needs_revision_trigger_repair:
        _drop_revision_history_immutability(conn)
    if current < 1:
        _migration_1(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (1, "initial_ledgers_and_indexes"),
        )
        current = 1
    if current < 2:
        _migration_2(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (2, "paper_oms_accounts_orders_fills_positions_cash"),
        )
        current = 2
    if current < 3:
        _migration_3(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (3, "verified_market_marks_and_agent_learning_ledger"),
        )
        current = 3
    if current < 4:
        _migration_4(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (4, "broker_style_paper_orders_and_lifecycle_events"),
        )
        current = 4
    if current < 5:
        _migration_5(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (5, "durable_agent_runs_events_tools_approvals_and_schedules"),
        )
        current = 5
    if current < 6:
        _migration_6(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (6, "policy_proposals_evaluations_shadow_and_strategy_versions"),
        )
        current = 6
    if current < 7:
        _migration_7(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (7, "unified_agent_sessions_plans_checkpoints_memory_workers_and_workflows"),
        )
        current = 7
    if current < 8:
        _migration_8(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (8, "durable_ui_bridge_scheduler_leases_and_memory_search"),
        )
        current = 8
    if current < 9:
        _migration_9(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (9, "durable_runtime_event_inbox"),
        )
        current = 9
    if current < 10:
        _migration_10(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (10, "neutral_universes_model_invocations_provenance_and_validation"),
        )
        current = 10
    if current < 11:
        _migration_11(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (11, "unified_point_in_time_market_data_warehouse"),
        )
        current = 11
    if current < 12:
        _migration_12(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (12, "security_entity_lifecycle_events"),
        )
        current = 12
    if current < 13:
        _migration_13(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (13, "normalized_point_in_time_entity_registry"),
        )
        current = 13
    if current < 14:
        _migration_14(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (14, "field_level_data_envelope_provenance"),
        )
        current = 14
    if current < 15:
        _migration_15(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (15, "bitemporal_trade_and_fiscal_contract"),
        )
        current = 15
    if current < 16:
        _migration_16(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (16, "immutable_raw_data_lake_and_reprocessing_audit"),
        )
        current = 16
    if current < 17:
        _migration_17(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (17, "standard_market_warehouse_domain_tables"),
        )
        current = 17
    if current < 18:
        _migration_18(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (18, "incremental_ingestion_run_and_batch_history"),
        )
        current = 18
    if current < 19:
        _migration_19(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (19, "immutable_revision_history_and_point_in_time_snapshots"),
        )
        current = 19
    if current < 20:
        _migration_20(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (20, "daily_data_quality_reports_and_issue_details"),
        )
        current = 20
    if current < 21:
        _migration_21(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (21, "cache_policy_state_invalidation_and_refresh_leases"),
        )
        current = 21
    if current < 22:
        _migration_22(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (22, "source_failover_runs_attempts_and_provenance"),
        )
        current = 22
    if current < 23:
        _migration_23(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (23, "cross_source_reconciliation_runs_and_conflict_lifecycle"),
        )
        current = 23
    if current < 24:
        _migration_24(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (24, "complete_derived_artifact_data_lineage"),
        )
        current = 24
    if current < 25:
        _migration_25(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (25, "revisioned_intraday_one_minute_candles"),
        )
        current = 25
    if current < 26:
        _migration_26(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (26, "complete_daily_history_range_indexes"),
        )
        current = 26
    if current < 27:
        _migration_27(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (27, "revisioned_price_adjustment_indexes"),
        )
        current = 27
    if current < 28:
        _migration_28(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (28, "immutable_corporate_actions_and_paper_entitlements"),
        )
        current = 28
    if current < 29:
        _migration_29(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (29, "immutable_official_trading_restrictions"),
        )
        current = 29
    if current < 30:
        _migration_30(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (30, "official_share_revisions_and_liquidity_assessments"),
        )
        current = 30
    if current < 31:
        _migration_31(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (31, "immutable_trading_anomaly_events_and_tracking"),
        )
        current = 31
    if current < 32:
        _migration_32(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (32, "agent_dock_event_envelope_and_plan_step_projection"),
        )
        current = 32
    if current < 33:
        _migration_33(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (33, "monthly_revenue_history_range_and_checkpoint_indexes"),
        )
        current = 33
    if current < 34:
        _migration_34(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (34, "income_statement_history_range_and_checkpoint_indexes"),
        )
        current = 34
    if current < 35:
        _migration_35(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (35, "balance_sheet_history_comparison_and_checkpoint_indexes"),
        )
        current = 35
    if current < 36:
        _migration_36(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (36, "cash_flow_history_quality_and_checkpoint_indexes"),
        )
        current = 36
    if current < 37:
        _migration_37(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (37, "recursive_agent_forest_interaction_automation_memory_and_artifacts"),
        )
        current = 37
    if current < 38:
        _migration_38(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (38, "final_agent_runtime_store_schema_alignment"),
        )
        current = 38
    if current < 39:
        _migration_39(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (39, "immutable_research_model_versions_and_experiment_receipts"),
        )
        current = 39
    if current < 40:
        _migration_40(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (40, "immutable_revision_availability_contract_snapshots"),
        )
        current = 40
    if current < 41:
        _migration_41(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (41, "immutable_strategy_artifact_registry"),
        )
        current = 41
    if current < 42:
        _migration_42(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (42, "immutable_model_promotion_receipts"),
        )
        current = 42
    if current < 43:
        _migration_43(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (43, "paper_broker_ack_replace_and_expiry_lifecycle"),
        )
        current = 43
    if current < 44:
        _migration_44(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (44, "paper_t_plus_2_settlement_cash_ledger"),
        )
        current = 44
    if current < 45:
        _migration_45(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (45, "paper_short_borrow_locates_fees_and_recalls"),
        )
        current = 45
    if current < 46:
        _migration_46(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (46, "durable_artifact_rollback_and_change_management_receipts"),
        )
        current = 46
    if current < 47:
        _migration_47(conn)
        conn.execute(
            "insert or ignore into schema_migrations(version, name) values (?, ?)",
            (47, "durable_content_retention_projections_and_receipts"),
        )
        current = 47
    # The corresponding versioned migration already performs each data
    # backfill. Re-running these helpers on an up-to-date database scans every
    # historical revision and can add minutes to every runtime component
    # startup. Keep the repair path only for databases actually crossing the
    # relevant version boundary.
    if needs_entity_repair:
        _ensure_entity_registry_schema(conn)
    if needs_envelope_repair:
        _ensure_data_envelope_schema(conn)
    if needs_temporal_repair:
        _ensure_temporal_contract_schema(conn)
    if needs_quality_repair:
        _ensure_data_quality_schema(conn)
    if starting_version < LATEST_SCHEMA_VERSION or len(applied_versions) < LATEST_SCHEMA_VERSION:
        _ensure_indexes(conn)
    if needs_revision_trigger_repair:
        _ensure_revision_history_immutability(conn)
    # P71 tables are a shared persistence boundary.  Fail startup clearly if a
    # database advertises the latest migration version but is missing a table,
    # column or index required by one of the durable Agent stores.
    verify_p71_schema(conn)
    conn.execute(f"pragma user_version = {LATEST_SCHEMA_VERSION}")
    conn.commit()
    # This schema is shared by several runtime components, each of which
    # verifies migrations during startup.  Truncate the migration WAL while
    # no component transaction is active so repeated verification cannot grow
    # a multi-gigabyte WAL or starve interactive Agent/UI writes.
    try:
        conn.execute("pragma wal_checkpoint(truncate)")
    except sqlite3.OperationalError:
        # A concurrent reader can briefly prevent a truncate; SQLite's normal
        # autocheckpoint remains the fallback and schema migration is complete.
        pass


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists signals (
            id integer primary key autoincrement,
            created_at text not null,
            symbol text,
            market text,
            horizon text,
            action text,
            confidence real,
            risk_approved integer not null default 0,
            executed integer not null default 0,
            payload_json text not null
        );

        create table if not exists reports (
            id integer primary key autoincrement,
            created_at text not null,
            report_text text not null
        );

        create table if not exists trades (
            id integer primary key autoincrement,
            created_at text not null,
            symbol text,
            action text,
            payload_json text not null
        );

        create table if not exists decision_logs (
            id integer primary key autoincrement,
            created_at text not null,
            symbol text,
            market text,
            horizon text,
            rating text,
            trader_action text,
            signal_action text,
            confidence real,
            entry_price real,
            target_price real,
            stop_loss real,
            position_size_pct real,
            risk_approved integer not null default 0,
            executed integer not null default 0,
            lesson text,
            payload_json text not null
        );
        """
    )


def _migration_2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists paper_accounts (
            account_id text primary key,
            base_currency text not null,
            initial_cash real not null,
            cash_balance real not null,
            realized_pnl real not null default 0,
            created_at text not null,
            updated_at text not null
        );

        create table if not exists paper_orders (
            order_id text primary key,
            account_id text not null references paper_accounts(account_id),
            created_at text not null,
            updated_at text not null,
            symbol text not null,
            market text,
            action text not null,
            status text not null,
            requested_position_pct real,
            requested_quantity real,
            filled_quantity real not null default 0,
            average_fill_price real,
            rejection_reason text,
            payload_json text not null
        );

        create table if not exists paper_fills (
            fill_id text primary key,
            order_id text not null references paper_orders(order_id),
            account_id text not null references paper_accounts(account_id),
            created_at text not null,
            symbol text not null,
            side text not null,
            quantity real not null,
            reference_price real not null,
            fill_price real not null,
            gross_amount real not null,
            commission real not null default 0,
            tax real not null default 0,
            slippage_cost real not null default 0,
            net_cash_delta real not null
        );

        create table if not exists paper_positions (
            account_id text not null references paper_accounts(account_id),
            symbol text not null,
            market text,
            quantity real not null,
            average_cost real not null,
            last_price real not null,
            realized_pnl real not null default 0,
            updated_at text not null,
            primary key (account_id, symbol)
        );

        create table if not exists cash_ledger (
            id integer primary key autoincrement,
            account_id text not null references paper_accounts(account_id),
            order_id text,
            created_at text not null,
            entry_type text not null,
            amount real not null,
            balance_after real not null,
            currency text not null,
            metadata_json text not null
        );
        """
    )


def _migration_3(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists paper_price_marks (
            id integer primary key autoincrement,
            account_id text not null references paper_accounts(account_id),
            symbol text not null,
            market text,
            price real not null,
            price_source text not null,
            source_timestamp text,
            received_at text not null,
            is_realtime integer not null default 0,
            is_fallback integer not null default 0,
            metadata_json text not null
        );

        create table if not exists agent_learning_episodes (
            episode_id text primary key,
            account_id text not null references paper_accounts(account_id),
            created_at text not null,
            updated_at text not null,
            status text not null,
            actor text not null,
            objective text not null,
            starting_equity real not null,
            ending_equity real,
            total_reward real,
            metadata_json text not null
        );

        create table if not exists agent_learning_events (
            id integer primary key autoincrement,
            episode_id text not null references agent_learning_episodes(episode_id),
            account_id text not null references paper_accounts(account_id),
            created_at text not null,
            event_type text not null,
            symbol text,
            action text,
            price real,
            quantity real,
            equity real,
            reward real,
            rationale text,
            payload_json text not null
        );

        create table if not exists agent_reflections (
            id integer primary key autoincrement,
            episode_id text not null references agent_learning_episodes(episode_id),
            account_id text not null references paper_accounts(account_id),
            created_at text not null,
            summary text not null,
            lessons_json text not null,
            next_rules_json text not null,
            payload_json text not null
        );
        """
    )


def _migration_4(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists paper_broker_orders (
            order_id text primary key,
            account_id text not null references paper_accounts(account_id),
            created_at text not null,
            updated_at text not null,
            symbol text not null,
            market text,
            side text not null,
            order_type text not null,
            time_in_force text not null,
            lot_type text not null,
            session text not null,
            requested_lots real,
            requested_quantity real not null,
            limit_price real,
            stop_price real,
            status text not null,
            triggered_at text,
            filled_at text,
            canceled_at text,
            rejection_reason text,
            episode_id text,
            actor text not null,
            rationale text,
            last_market_price real,
            payload_json text not null
        );

        create table if not exists paper_broker_order_events (
            id integer primary key autoincrement,
            order_id text not null references paper_broker_orders(order_id),
            account_id text not null references paper_accounts(account_id),
            created_at text not null,
            event_type text not null,
            status text not null,
            market_price real,
            detail text,
            payload_json text not null
        );
        """
    )


def _migration_5(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists agent_runs (
            run_id text primary key,
            created_at text not null,
            updated_at text not null,
            started_at text,
            completed_at text,
            status text not null,
            objective text not null,
            driver text,
            autonomy text not null,
            symbols_json text not null,
            max_steps integer not null,
            current_step integer not null default 0,
            cancel_requested integer not null default 0,
            request_json text not null,
            result_json text,
            error_json text
        );

        create table if not exists agent_steps (
            id integer primary key autoincrement,
            run_id text not null references agent_runs(run_id) on delete cascade,
            step integer not null,
            status text not null,
            started_at text not null,
            completed_at text,
            summary text,
            payload_json text not null default '{}',
            unique(run_id, step)
        );

        create table if not exists agent_tool_calls (
            id integer primary key autoincrement,
            run_id text not null references agent_runs(run_id) on delete cascade,
            step integer not null,
            call_id text not null,
            tool_name text not null,
            status text not null,
            started_at text not null,
            completed_at text,
            arguments_json text not null,
            result_json text,
            error_json text,
            unique(run_id, call_id)
        );

        create table if not exists agent_events (
            id integer primary key autoincrement,
            run_id text not null references agent_runs(run_id) on delete cascade,
            sequence integer not null,
            created_at text not null,
            event_type text not null,
            payload_json text not null,
            unique(run_id, sequence)
        );

        create table if not exists agent_approvals (
            approval_id text primary key,
            run_id text not null references agent_runs(run_id) on delete cascade,
            capability text not null,
            resource_scope_json text not null,
            status text not null,
            requested_at text not null,
            decided_at text,
            expires_at text,
            payload_json text not null
        );

        create table if not exists agent_schedules (
            schedule_id text primary key,
            run_id text references agent_runs(run_id) on delete set null,
            name text not null,
            cron_expression text,
            next_run_at text,
            enabled integer not null default 1,
            created_at text not null,
            updated_at text not null,
            payload_json text not null
        );
        """
    )


def _migration_6(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists policy_proposals (
            proposal_id text primary key,
            account_id text references paper_accounts(account_id) on delete cascade,
            reflection_id integer references agent_reflections(id) on delete set null,
            created_at text not null,
            updated_at text not null,
            status text not null,
            title text not null,
            rules_json text not null,
            evidence_json text not null,
            evaluation_json text,
            shadow_json text,
            strategy_version_hash text,
            data_version_hash text,
            approved_by text,
            promoted_at text,
            payload_json text not null
        );

        create table if not exists strategy_versions (
            version_id text primary key,
            proposal_id text not null references policy_proposals(proposal_id),
            created_at text not null,
            status text not null,
            strategy_hash text not null,
            data_hash text not null,
            rules_json text not null,
            approval_json text not null,
            unique(proposal_id, strategy_hash, data_hash)
        );
        """
    )


def _migration_7(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists agent_sessions (
            session_id text primary key,
            namespace text not null,
            title text not null,
            status text not null,
            created_at text not null,
            updated_at text not null,
            last_run_id text,
            metadata_json text not null,
            unique(namespace, session_id)
        );

        create table if not exists agent_messages (
            message_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete set null,
            role text not null,
            created_at text not null,
            content_json text not null,
            source_json text not null
        );

        create table if not exists agent_plans (
            plan_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            run_id text not null references agent_runs(run_id) on delete cascade,
            status text not null,
            objective text not null,
            current_revision integer not null,
            created_at text not null,
            updated_at text not null,
            plan_json text not null
        );

        create table if not exists agent_plan_revisions (
            id integer primary key autoincrement,
            plan_id text not null references agent_plans(plan_id) on delete cascade,
            run_id text not null references agent_runs(run_id) on delete cascade,
            revision integer not null,
            created_at text not null,
            reason_summary text not null,
            patch_json text not null,
            plan_json text not null,
            unique(plan_id, revision)
        );

        create table if not exists agent_checkpoints (
            checkpoint_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            run_id text not null references agent_runs(run_id) on delete cascade,
            plan_revision integer not null,
            sequence integer not null,
            status text not null,
            created_at text not null,
            snapshot_hash text not null,
            payload_json text not null,
            rollback_json text not null
        );

        create table if not exists agent_memory (
            memory_id text primary key,
            namespace text not null,
            session_id text references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete set null,
            kind text not null,
            fact_type text not null,
            content text not null,
            source_json text not null,
            created_at text not null,
            updated_at text not null,
            expires_at text,
            archived_at text
        );

        create table if not exists agent_artifacts (
            artifact_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete cascade,
            kind text not null,
            name text not null,
            path text,
            media_type text,
            sha256 text not null,
            created_at text not null,
            metadata_json text not null
        );

        create table if not exists agent_workflows (
            workflow_id text primary key,
            namespace text not null,
            name text not null,
            version integer not null,
            status text not null,
            created_at text not null,
            updated_at text not null,
            plan_json text not null,
            metadata_json text not null,
            unique(namespace, name, version)
        );

        create table if not exists agent_workers (
            worker_id text primary key,
            run_id text not null references agent_runs(run_id) on delete cascade,
            step_id text,
            worker_type text not null,
            status text not null,
            pid integer,
            started_at text not null,
            completed_at text,
            heartbeat_at text,
            error_json text,
            payload_json text not null
        );

        create table if not exists agent_control_messages (
            control_id text primary key,
            run_id text not null references agent_runs(run_id) on delete cascade,
            control_type text not null,
            status text not null,
            created_at text not null,
            consumed_at text,
            payload_json text not null
        );

        create table if not exists provider_configs (
            provider_id text primary key,
            enabled integer not null,
            is_primary integer not null,
            updated_at text not null,
            config_json text not null
        );
        """
    )
    _add_column(conn, "agent_runs", "session_id text")
    _add_column(conn, "agent_runs", "plan_id text")
    _add_column(conn, "agent_runs", "checkpoint_id text")
    _add_column(conn, "agent_runs", "parent_run_id text")
    _add_column(conn, "agent_runs", "resume_count integer not null default 0")
    _add_column(conn, "agent_runs", "environment_hash text")
    _add_column(conn, "agent_runs", "idempotency_key text")
    _add_column(conn, "agent_steps", "node_id text")
    _add_column(conn, "agent_steps", "attempt integer not null default 1")
    _add_column(conn, "agent_steps", "validator_status text")
    _add_column(conn, "agent_steps", "checkpoint_id text")
    _add_column(conn, "agent_tool_calls", "worker_id text")
    _add_column(conn, "agent_tool_calls", "argument_digest text")
    _add_column(conn, "agent_tool_calls", "risk_class text")
    _add_column(conn, "agent_tool_calls", "before_json text")
    _add_column(conn, "agent_tool_calls", "after_json text")
    _add_column(conn, "agent_tool_calls", "validation_json text")
    _add_column(conn, "agent_tool_calls", "rollback_token text")
    _add_column(conn, "agent_tool_calls", "idempotency_key text")
    _add_column(conn, "agent_tool_calls", "attempt integer not null default 1")
    _add_column(conn, "agent_approvals", "step_id text")
    _add_column(conn, "agent_approvals", "tool_name text")
    _add_column(conn, "agent_approvals", "argument_digest text")
    _add_column(conn, "agent_approvals", "risk_class text")
    _add_column(conn, "agent_approvals", "decided_by text")
    _add_column(conn, "agent_approvals", "result_json text")
    _add_column(conn, "agent_schedules", "session_id text")
    _add_column(conn, "agent_schedules", "workflow_id text")
    _add_column(conn, "agent_schedules", "trigger_type text not null default 'one_shot'")
    _add_column(conn, "agent_schedules", "misfire_policy text not null default 'run_once'")
    _add_column(conn, "agent_schedules", "last_fired_at text")


def _migration_8(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists agent_ui_state (
            singleton_id integer primary key check (singleton_id = 1),
            state_json text not null,
            last_seen text,
            updated_at text not null
        );

        create table if not exists agent_ui_commands (
            command_id integer primary key autoincrement,
            action text not null,
            arguments_json text not null,
            status text not null,
            created_at text not null,
            completed_at text,
            result_json text
        );
        """
    )
    _add_column(conn, "agent_schedules", "claim_owner text")
    _add_column(conn, "agent_schedules", "claim_expires_at text")
    # FTS5 is included in the supported Python runtimes. Keep migration
    # compatibility with minimal SQLite builds by retaining the deterministic
    # Han n-gram fallback in MemoryStore when this optional index is absent.
    try:
        conn.execute(
            """
            create virtual table if not exists agent_memory_fts using fts5(
                memory_id unindexed,
                namespace unindexed,
                content,
                tokenize='trigram'
            )
            """
        )
        conn.execute("delete from agent_memory_fts")
        conn.execute(
            """
            insert into agent_memory_fts(memory_id, namespace, content)
            select memory_id, namespace, content
              from agent_memory
             where archived_at is null
            """
        )
        conn.executescript(
            """
            create trigger if not exists agent_memory_fts_insert
            after insert on agent_memory
            when new.archived_at is null
            begin
                insert into agent_memory_fts(memory_id, namespace, content)
                values (new.memory_id, new.namespace, new.content);
            end;

            create trigger if not exists agent_memory_fts_update
            after update of namespace, content, archived_at on agent_memory
            begin
                delete from agent_memory_fts where memory_id = old.memory_id;
                insert into agent_memory_fts(memory_id, namespace, content)
                select new.memory_id, new.namespace, new.content
                 where new.archived_at is null;
            end;

            create trigger if not exists agent_memory_fts_delete
            after delete on agent_memory
            begin
                delete from agent_memory_fts where memory_id = old.memory_id;
            end;
            """
        )
    except sqlite3.OperationalError:
        pass


def _migration_9(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists agent_runtime_events (
            event_id text primary key,
            event_type text not null,
            status text not null,
            payload_json text not null,
            payload_hash text not null,
            dedup_key text,
            occurred_at text not null,
            created_at text not null,
            claim_owner text,
            claim_expires_at text,
            processed_at text,
            error_json text
        );
        """
    )


def _migration_10(conn: sqlite3.Connection) -> None:
    """Persist neutral input scope and each independent analysis layer."""

    conn.executescript(
        """
        create table if not exists user_watchlists (
            watchlist_id text primary key,
            user_id text not null,
            name text not null,
            created_at text not null,
            updated_at text not null,
            metadata_json text not null,
            unique(user_id, name)
        );

        create table if not exists user_watchlist_symbols (
            watchlist_id text not null references user_watchlists(watchlist_id) on delete cascade,
            symbol text not null,
            added_at text not null,
            provenance_json text not null,
            primary key (watchlist_id, symbol)
        );

        create table if not exists universe_snapshots (
            universe_id text primary key,
            source text not null,
            created_at text not null,
            filters_json text not null,
            symbols_json text not null,
            symbol_count integer not null,
            provider text,
            provenance_json text not null
        );

        create table if not exists analysis_runs (
            analysis_run_id text primary key,
            agent_run_id text references agent_runs(run_id) on delete set null,
            universe_id text references universe_snapshots(universe_id) on delete set null,
            mode text not null,
            status text not null,
            created_at text not null,
            completed_at text,
            request_json text not null
        );

        create table if not exists model_invocations (
            model_call_id text primary key,
            analysis_run_id text references analysis_runs(analysis_run_id) on delete cascade,
            provider text not null,
            model_id text not null,
            protocol text not null,
            status text not null,
            started_at text not null,
            completed_at text,
            error_type text,
            error_json text,
            receipt_json text not null
        );

        create table if not exists analysis_provenance (
            provenance_id text primary key,
            analysis_run_id text not null references analysis_runs(analysis_run_id) on delete cascade,
            origin text not null,
            model_call_id text references model_invocations(model_call_id) on delete set null,
            rule_set_id text,
            universe_source text not null,
            data_ready integer not null,
            fallback_used integer not null default 0,
            generated_at text not null,
            payload_json text not null
        );

        create table if not exists rule_strategy_results (
            rule_result_id text primary key,
            analysis_run_id text not null references analysis_runs(analysis_run_id) on delete cascade,
            symbol text,
            rule_set_id text not null,
            rule_score real,
            score_min real,
            score_max real,
            method_id text not null,
            created_at text not null,
            payload_json text not null
        );

        create table if not exists model_analysis_results (
            model_result_id text primary key,
            analysis_run_id text not null references analysis_runs(analysis_run_id) on delete cascade,
            model_call_id text not null references model_invocations(model_call_id) on delete cascade,
            symbol text,
            created_at text not null,
            payload_json text not null
        );

        create table if not exists validation_reports (
            validation_report_id text primary key,
            analysis_run_id text not null references analysis_runs(analysis_run_id) on delete cascade,
            model_call_id text references model_invocations(model_call_id) on delete set null,
            layer text not null,
            status text not null,
            error_code text,
            created_at text not null,
            payload_json text not null
        );
        """
    )


def _migration_11(conn: sqlite3.Connection) -> None:
    """Create the single point-in-time market-data truth and lineage ledger."""

    conn.executescript(
        """
        create table if not exists data_sources (
            source_id text primary key,
            display_name text not null,
            authority text not null,
            base_url text,
            license_status text not null,
            update_frequency_seconds integer not null,
            reliability_tier integer not null,
            priority integer not null,
            domains_json text not null,
            failover_source_ids_json text not null,
            field_contract_json text not null,
            active integer not null default 1,
            registered_at text not null,
            updated_at text not null
        );

        create table if not exists market_entities (
            entity_id text primary key,
            entity_type text not null,
            canonical_name text not null,
            market text not null,
            exchange text,
            currency text,
            sector text,
            industry text,
            lifecycle_status text not null,
            listed_at text,
            delisted_at text,
            metadata_json text not null,
            created_at text not null,
            updated_at text not null
        );

        create table if not exists entity_identifiers (
            source_id text not null references data_sources(source_id),
            identifier_type text not null,
            identifier_value text not null,
            entity_id text not null references market_entities(entity_id),
            valid_from text,
            valid_to text,
            metadata_json text not null,
            created_at text not null,
            primary key (source_id, identifier_type, identifier_value, valid_from)
        );

        create table if not exists raw_data_payloads (
            raw_payload_id text primary key,
            source_id text not null references data_sources(source_id),
            request_url text,
            requested_at text not null,
            received_at text not null,
            http_status integer,
            content_type text,
            payload_hash text not null,
            payload_json text not null,
            metadata_json text not null,
            unique(source_id, payload_hash)
        );

        create table if not exists data_revisions (
            revision_id text primary key,
            dataset text not null,
            entity_id text not null,
            observation_key text not null,
            source_id text not null references data_sources(source_id),
            revision integer not null,
            observed_at text,
            published_at text,
            available_at text not null,
            acquired_at text not null,
            effective_at text not null,
            expires_at text,
            payload_hash text not null,
            raw_payload_id text references raw_data_payloads(raw_payload_id),
            quality_status text not null,
            quality_flags_json text not null,
            is_fallback integer not null default 0,
            supersedes_revision_id text references data_revisions(revision_id),
            transformation_json text not null,
            payload_json text not null,
            created_at text not null,
            unique(dataset, entity_id, observation_key, source_id, revision)
        );

        create table if not exists data_lineage_edges (
            lineage_id text primary key,
            output_revision_id text not null references data_revisions(revision_id) on delete cascade,
            input_revision_id text references data_revisions(revision_id) on delete set null,
            raw_payload_id text references raw_data_payloads(raw_payload_id) on delete set null,
            transformation_id text not null,
            code_version text not null,
            parameters_json text not null,
            created_at text not null
        );

        create table if not exists data_ingestion_checkpoints (
            source_id text not null references data_sources(source_id),
            dataset text not null,
            partition_key text not null,
            cursor_value text,
            status text not null,
            last_attempt_at text not null,
            last_success_at text,
            error_json text,
            metadata_json text not null,
            primary key (source_id, dataset, partition_key)
        );

        create table if not exists data_quality_reports (
            report_id text primary key,
            dataset text not null,
            partition_key text not null,
            status text not null,
            missing_count integer not null,
            anomaly_count integer not null,
            duplicate_count integer not null,
            conflict_count integer not null,
            generated_at text not null,
            payload_json text not null
        );

        create table if not exists data_reconciliation_conflicts (
            conflict_id text primary key,
            dataset text not null,
            entity_id text not null,
            observation_key text not null,
            field_name text not null,
            left_revision_id text not null references data_revisions(revision_id),
            right_revision_id text not null references data_revisions(revision_id),
            left_value_json text not null,
            right_value_json text not null,
            tolerance real,
            status text not null,
            detected_at text not null,
            resolved_at text,
            resolution_json text
        );
        """
    )


def _migration_12(conn: sqlite3.Connection) -> None:
    """Preserve venue transitions, listings, delistings and expirations."""

    conn.executescript(
        """
        create table if not exists entity_lifecycle_events (
            event_id text primary key,
            entity_id text not null references market_entities(entity_id) on delete cascade,
            event_type text not null,
            venue text,
            listing_type text,
            effective_at text not null,
            published_at text,
            acquired_at text not null,
            source_id text not null references data_sources(source_id),
            raw_payload_id text references raw_data_payloads(raw_payload_id) on delete set null,
            metadata_json text not null,
            created_at text not null,
            unique(entity_id, event_type, venue, effective_at, source_id)
        );
        """
    )


def _migration_13(conn: sqlite3.Connection) -> None:
    """Make source identifiers deterministic and safely queryable."""

    _ensure_entity_registry_schema(conn)


def _payload_leaf_pointers(value, prefix: str = "") -> list[str]:
    if isinstance(value, dict) and value:
        pointers: list[str] = []
        for key in sorted(value):
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            pointers.extend(_payload_leaf_pointers(value[key], f"{prefix}/{escaped}"))
        return pointers
    if isinstance(value, list) and value:
        pointers = []
        for index, item in enumerate(value):
            pointers.extend(_payload_leaf_pointers(item, f"{prefix}/{index}"))
        return pointers
    return [prefix or "/"]


def _migration_14(conn: sqlite3.Connection) -> None:
    """Persist field-level source, temporal, quality and raw-value lineage."""

    _ensure_data_envelope_schema(conn)


def _ensure_data_envelope_schema(conn: sqlite3.Connection) -> None:
    _add_column(
        conn,
        "data_revisions",
        "field_provenance_json text not null default '{}'",
    )
    rows = conn.execute(
        """
        select revision_id, payload_json, source_id, observed_at, published_at,
               available_at, acquired_at, effective_at, expires_at,
               raw_payload_id, quality_status, quality_flags_json,
               transformation_json, created_at, field_provenance_json
          from data_revisions
         where field_provenance_json is null or field_provenance_json = '{}'
        """
    ).fetchall()
    for row in rows:
        payload = json.loads(row[1])
        quality_flags = json.loads(row[11])
        transformation = json.loads(row[12])
        temporal = {
            "observed_at": row[3],
            "published_at": row[4],
            "available_at": row[5],
            "acquired_at": row[6],
            "effective_at": row[7],
            "expires_at": row[8],
        }
        provenance = {
            pointer: {
                "source_id": row[2],
                "temporal": temporal,
                "updated_at": row[13],
                "raw_payload_id": row[9],
                "raw_json_pointer": pointer,
                "quality_status": row[10],
                "quality_flags": quality_flags,
                "transformation_id": transformation.get(
                    "transformation_id",
                    "stock_ai.legacy_envelope_backfill.v1",
                ),
                "input_fields": [pointer],
            }
            for pointer in _payload_leaf_pointers(payload)
        }
        conn.execute(
            "update data_revisions set field_provenance_json=? where revision_id=?",
            (
                json.dumps(
                    provenance,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                row[0],
            ),
        )


def _iso_date(value) -> str | None:
    text = str(value or "").strip()
    if len(text) >= 10:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc).isoformat()
    return None


def _temporal_dimensions(
    dataset: str,
    payload: dict,
    observation_key: str,
) -> dict[str, str | None]:
    if dataset in {"prices_daily", "institutional_flows", "margin_trading", "borrowed_short"}:
        trade_date = _iso_date(
            payload.get("trade_date")
            or payload.get("date")
            or observation_key
        )
        return {
            "time_basis": "trade_date" if trade_date else "snapshot",
            "trade_date": trade_date,
            "fiscal_period": None,
            "period_start": None,
            "period_end": None,
        }
    if dataset == "revenues_monthly":
        period = str(payload.get("period") or "").strip()
        if len(period) == 7 and period[4] == "-":
            try:
                year = int(period[:4])
                month = int(period[5:7])
                period_start = datetime(
                    year, month, 1, tzinfo=timezone.utc
                ).isoformat()
                period_end = datetime(
                    year,
                    month,
                    monthrange(year, month)[1],
                    tzinfo=timezone.utc,
                ).isoformat()
            except (ValueError, OverflowError):
                pass
            else:
                return {
                    "time_basis": "fiscal_period",
                    "trade_date": None,
                    "fiscal_period": period,
                    "period_start": period_start,
                    "period_end": period_end,
                }
    if dataset == "security_master":
        return {
            "time_basis": "lifecycle",
            "trade_date": None,
            "fiscal_period": None,
            "period_start": None,
            "period_end": None,
        }
    return {
        "time_basis": "snapshot",
        "trade_date": None,
        "fiscal_period": None,
        "period_start": None,
        "period_end": None,
    }


def _migration_15(conn: sqlite3.Connection) -> None:
    """Separate market/fiscal effective time from research knowledge time."""

    _ensure_temporal_contract_schema(conn)


def _ensure_temporal_contract_schema(conn: sqlite3.Connection) -> None:
    for definition in (
        "temporal_contract_version text not null default ''",
        "time_basis text not null default 'snapshot'",
        "trade_date text",
        "fiscal_period text",
        "period_start text",
        "period_end text",
    ):
        _add_column(conn, "data_revisions", definition)
    rows = conn.execute(
        """
        select revision_id, dataset, observation_key, payload_json,
               field_provenance_json, published_at, available_at,
               acquired_at, effective_at
          from data_revisions
         where temporal_contract_version != 'stock_ai.temporal_contract.v1'
        """
    ).fetchall()
    for row in rows:
        payload = json.loads(row[3])
        dimensions = _temporal_dimensions(row[1], payload, row[2])
        published_at = row[5]
        available_at = row[6]
        acquired_at = row[7]
        effective_at = row[8]
        if dimensions["time_basis"] == "fiscal_period":
            inferred_publication = _iso_date(payload.get("report_date"))
            if (
                published_at is None
                and inferred_publication is not None
                and inferred_publication <= acquired_at
            ):
                published_at = inferred_publication
            available_at = max(
                value
                for value in (available_at, published_at)
                if value is not None
            )
            effective_at = dimensions["period_end"] or effective_at
        provenance = json.loads(row[4])
        for item in provenance.values():
            temporal = item.setdefault("temporal", {})
            temporal.update(
                {
                    "schema_version": "stock_ai.temporal_contract.v1",
                    **dimensions,
                    "published_at": published_at,
                    "available_at": available_at,
                    "effective_at": effective_at,
                }
            )
        conn.execute(
            """
            update data_revisions
               set temporal_contract_version='stock_ai.temporal_contract.v1',
                   time_basis=?, trade_date=?, fiscal_period=?,
                   period_start=?, period_end=?, published_at=?,
                   available_at=?, effective_at=?, field_provenance_json=?
             where revision_id=?
            """,
            (
                dimensions["time_basis"],
                dimensions["trade_date"],
                dimensions["fiscal_period"],
                dimensions["period_start"],
                dimensions["period_end"],
                published_at,
                available_at,
                effective_at,
                json.dumps(
                    provenance,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                row[0],
            ),
        )


def _migration_16(conn: sqlite3.Connection) -> None:
    """Preserve exact source bytes and audit deterministic cleaning replays."""

    _ensure_raw_data_lake_schema(conn)


def _migration_17(conn: sqlite3.Connection) -> None:
    """Materialize normalized revisions into shared domain warehouse tables."""

    domain_tables = {
        "market_prices": ("prices_daily", "prices_intraday"),
        "financial_facts": (
            "fundamentals",
            "fundamentals_quarterly",
            "revenues_monthly",
            "valuation_metrics",
        ),
        "ownership_flows": (
            "institutional_flows",
            "margin_trading",
            "borrowed_short",
            "ownership",
            "tdcc_holding_distribution",
            "derivatives",
            "taifex_derivatives",
        ),
        "market_events": ("events", "company_events", "documents", "event_impacts"),
        "macro_observations": ("macro", "macro_series"),
    }
    for table_name, datasets in domain_tables.items():
        conn.execute(
            f"""
            create table if not exists {table_name} (
                revision_id text primary key
                    references data_revisions(revision_id) on delete restrict,
                dataset text not null,
                entity_id text not null,
                observation_key text not null,
                source_id text not null references data_sources(source_id),
                revision integer not null,
                observed_at text,
                published_at text,
                available_at text not null,
                acquired_at text not null,
                effective_at text not null,
                expires_at text,
                quality_status text not null,
                is_fallback integer not null default 0,
                record_json text not null,
                created_at text not null,
                unique(dataset, entity_id, observation_key, source_id, revision)
            )
            """
        )
        conn.executescript(
            f"""
            create trigger if not exists trg_{table_name}_immutable_update
            before update on {table_name}
            begin
                select raise(abort, 'standard warehouse revisions are immutable');
            end;
            create trigger if not exists trg_{table_name}_immutable_delete
            before delete on {table_name}
            begin
                select raise(abort, 'standard warehouse revisions are immutable');
            end;
            """
        )
        placeholders = ",".join("?" for _ in datasets)
        conn.execute(
            f"""
            insert or ignore into {table_name} (
                revision_id, dataset, entity_id, observation_key, source_id,
                revision, observed_at, published_at, available_at, acquired_at,
                effective_at, expires_at, quality_status, is_fallback,
                record_json, created_at
            )
            select revision_id, dataset, entity_id, observation_key, source_id,
                   revision, observed_at, published_at, available_at, acquired_at,
                   effective_at, expires_at, quality_status, is_fallback,
                   payload_json, created_at
              from data_revisions
             where dataset in ({placeholders})
            """,
            datasets,
        )


def _ensure_raw_data_lake_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists raw_data_objects (
            raw_object_id text primary key,
            source_id text not null references data_sources(source_id),
            request_url text,
            requested_at text not null,
            received_at text not null,
            http_status integer,
            media_type text not null,
            content_encoding text not null,
            wire_hash text not null,
            byte_length integer not null,
            body_blob blob not null,
            metadata_json text not null,
            created_at text not null,
            unique(source_id, wire_hash)
        );

        create table if not exists raw_payload_objects (
            raw_payload_id text not null references raw_data_payloads(raw_payload_id),
            raw_object_id text not null references raw_data_objects(raw_object_id),
            parser_id text not null,
            linked_at text not null,
            primary key (raw_payload_id, raw_object_id)
        );

        create table if not exists raw_reprocessing_runs (
            run_id text primary key,
            raw_payload_id text not null references raw_data_payloads(raw_payload_id),
            raw_object_id text not null references raw_data_objects(raw_object_id),
            dataset text,
            parser_id text not null,
            transformation_id text not null,
            code_version text not null,
            input_wire_hash text not null,
            output_payload_hash text,
            output_record_count integer,
            status text not null,
            error_json text not null,
            parameters_json text not null,
            started_at text not null,
            completed_at text
        );
        """
    )
    # Development databases can revisit the idempotent ensure path. Temporarily
    # remove the guards so legacy rows can be backfilled before restoring them.
    conn.executescript(
        """
        drop trigger if exists trg_raw_data_payloads_immutable_update;
        drop trigger if exists trg_raw_data_payloads_immutable_delete;
        drop trigger if exists trg_raw_data_objects_immutable_update;
        drop trigger if exists trg_raw_data_objects_immutable_delete;
        """
    )
    legacy_rows = conn.execute(
        """
        select p.raw_payload_id, p.source_id, p.request_url, p.requested_at,
               p.received_at, p.http_status, p.content_type, p.payload_json,
               p.metadata_json
          from raw_data_payloads p
         where not exists (
             select 1 from raw_payload_objects l
              where l.raw_payload_id=p.raw_payload_id
         )
        """
    ).fetchall()
    from hashlib import sha256

    for row in legacy_rows:
        body = str(row[7]).encode("utf-8")
        wire_hash = sha256(body).hexdigest()
        raw_object_id = "RDO-" + sha256(
            f"{row[1]}:{wire_hash}".encode("utf-8")
        ).hexdigest()[:32]
        conn.execute(
            """
            insert or ignore into raw_data_objects (
                raw_object_id, source_id, request_url, requested_at, received_at,
                http_status, media_type, content_encoding, wire_hash,
                byte_length, body_blob, metadata_json, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, 'utf-8', ?, ?, ?, ?, ?)
            """,
            (
                raw_object_id,
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6] or "application/json",
                wire_hash,
                len(body),
                body,
                row[8],
                row[4],
            ),
        )
        conn.execute(
            """
            insert or ignore into raw_payload_objects (
                raw_payload_id, raw_object_id, parser_id, linked_at
            ) values (?, ?, 'stock_ai.raw.json.v1', ?)
            """,
            (row[0], raw_object_id, row[4]),
        )
    conn.executescript(
        """
        create trigger if not exists trg_raw_data_payloads_immutable_update
        before update on raw_data_payloads
        begin
            select raise(abort, 'raw_data_payloads are immutable');
        end;
        create trigger if not exists trg_raw_data_payloads_immutable_delete
        before delete on raw_data_payloads
        begin
            select raise(abort, 'raw_data_payloads are immutable');
        end;
        create trigger if not exists trg_raw_data_objects_immutable_update
        before update on raw_data_objects
        begin
            select raise(abort, 'raw_data_objects are immutable');
        end;
        create trigger if not exists trg_raw_data_objects_immutable_delete
        before delete on raw_data_objects
        begin
            select raise(abort, 'raw_data_objects are immutable');
        end;
        """
    )


def _migration_18(conn: sqlite3.Connection) -> None:
    """Audit resumable incremental ingestion at run and committed-batch level."""

    conn.executescript(
        """
        create table if not exists data_ingestion_runs (
            run_id text primary key,
            source_id text not null references data_sources(source_id),
            dataset text not null,
            partition_key text not null,
            status text not null,
            starting_cursor text,
            committed_cursor text,
            batch_count integer not null default 0,
            record_count integer not null default 0,
            started_at text not null,
            updated_at text not null,
            completed_at text,
            error_json text,
            metadata_json text not null
        );

        create table if not exists data_ingestion_batches (
            run_id text not null references data_ingestion_runs(run_id) on delete cascade,
            batch_sequence integer not null,
            input_cursor text,
            output_cursor text,
            status text not null,
            record_count integer not null default 0,
            started_at text not null,
            completed_at text not null,
            error_json text,
            metadata_json text not null,
            primary key (run_id, batch_sequence)
        );

        create index if not exists idx_data_ingestion_runs_partition_started
            on data_ingestion_runs(source_id, dataset, partition_key, started_at desc);
        create index if not exists idx_data_ingestion_runs_status_updated
            on data_ingestion_runs(status, updated_at desc);
        create index if not exists idx_data_ingestion_batches_run_sequence
            on data_ingestion_batches(run_id, batch_sequence);
        """
    )


def _migration_19(conn: sqlite3.Connection) -> None:
    """Persist complete point-in-time manifests over immutable revisions."""

    conn.executescript(
        """
        create table if not exists data_revision_snapshots (
            snapshot_id text primary key,
            dataset text not null,
            knowledge_at text not null,
            effective_at text not null,
            entity_id text,
            source_id text,
            item_count integer not null,
            manifest_hash text not null unique,
            manifest_json text not null,
            created_at text not null
        );

        create table if not exists data_revision_snapshot_items (
            snapshot_id text not null
                references data_revision_snapshots(snapshot_id) on delete restrict,
            ordinal integer not null,
            revision_id text not null
                references data_revisions(revision_id) on delete restrict,
            payload_hash text not null,
            primary key (snapshot_id, ordinal),
            unique(snapshot_id, revision_id)
        );

        create index if not exists idx_data_revision_snapshots_dataset_time
            on data_revision_snapshots(dataset, knowledge_at, effective_at, created_at desc);
        create index if not exists idx_data_revision_snapshot_items_revision
            on data_revision_snapshot_items(revision_id, snapshot_id);
        """
    )


def _migration_20(conn: sqlite3.Connection) -> None:
    """Persist reproducible daily quality reports and issue-level evidence."""

    _ensure_data_quality_schema(conn)


def _migration_21(conn: sqlite3.Connection) -> None:
    """Persist cache freshness, invalidation history and single-flight leases."""

    conn.executescript(
        """
        create table if not exists data_cache_entries (
            source_id text not null references data_sources(source_id),
            dataset text not null,
            partition_key text not null,
            policy_json text not null,
            refreshed_at text,
            fresh_until text,
            stale_until text,
            invalidated_at text,
            invalidation_reason text,
            generation integer not null default 0,
            last_decision text not null default 'empty',
            last_decision_at text,
            hit_count integer not null default 0,
            miss_count integer not null default 0,
            stale_hit_count integer not null default 0,
            refresh_count integer not null default 0,
            primary key (source_id, dataset, partition_key)
        );

        create table if not exists data_cache_invalidations (
            invalidation_id text primary key,
            source_id text not null references data_sources(source_id),
            dataset text not null,
            partition_key text not null,
            reason text not null,
            metadata_json text not null,
            invalidated_at text not null
        );

        create table if not exists data_cache_refresh_leases (
            source_id text not null references data_sources(source_id),
            dataset text not null,
            partition_key text not null,
            lease_id text not null,
            owner_id text not null,
            acquired_at text not null,
            expires_at text not null,
            primary key (source_id, dataset, partition_key)
        );

        create index if not exists idx_data_cache_entries_dataset
            on data_cache_entries(dataset, last_decision, fresh_until, stale_until);
        create index if not exists idx_data_cache_invalidations_dataset
            on data_cache_invalidations(dataset, invalidated_at desc);
        create index if not exists idx_data_cache_refresh_leases_expiry
            on data_cache_refresh_leases(expires_at);
        """
    )


def _migration_22(conn: sqlite3.Connection) -> None:
    """Persist every primary/failover attempt without rewriting source identity."""

    conn.executescript(
        """
        create table if not exists data_source_failover_runs (
            run_id text primary key,
            requested_dataset_id text not null,
            requested_source_id text not null references data_sources(source_id),
            normalized_dataset text not null,
            status text not null,
            selected_dataset_id text,
            selected_source_id text references data_sources(source_id),
            is_failover integer not null default 0,
            attempt_count integer not null default 0,
            revision_ids_json text not null default '[]',
            policy_json text not null,
            error_json text,
            started_at text not null,
            completed_at text
        );

        create table if not exists data_source_failover_attempts (
            attempt_id text primary key,
            run_id text not null
                references data_source_failover_runs(run_id) on delete restrict,
            sequence integer not null,
            dataset_id text not null,
            source_id text not null references data_sources(source_id),
            failover_depth integer not null,
            attempt_number integer not null,
            request_url text,
            status text not null,
            http_status integer,
            raw_payload_id text references raw_data_payloads(raw_payload_id),
            revision_ids_json text not null default '[]',
            error_json text,
            started_at text not null,
            completed_at text not null,
            unique(run_id, sequence)
        );

        create index if not exists idx_data_source_failover_runs_started
            on data_source_failover_runs(started_at desc, run_id);
        create index if not exists idx_data_source_failover_runs_status
            on data_source_failover_runs(status, is_failover, started_at desc);
        create index if not exists idx_data_source_failover_attempts_run
            on data_source_failover_attempts(run_id, sequence);
        create index if not exists idx_data_source_failover_attempts_source
            on data_source_failover_attempts(source_id, dataset_id, status);

        create trigger if not exists trg_data_source_failover_attempts_immutable_update
        before update on data_source_failover_attempts
        begin
            select raise(abort, 'source failover attempts are immutable');
        end;

        create trigger if not exists trg_data_source_failover_attempts_immutable_delete
        before delete on data_source_failover_attempts
        begin
            select raise(abort, 'source failover attempts are immutable');
        end;
        """
    )


def _migration_23(conn: sqlite3.Connection) -> None:
    """Audit cross-source comparison runs and preserve conflict lifecycle."""

    conn.executescript(
        """
        create table if not exists data_reconciliation_runs (
            run_id text primary key,
            dataset text not null,
            entity_id text,
            observation_key text,
            status text not null,
            rules_version text not null,
            knowledge_at text not null,
            source_count integer not null default 0,
            observation_count integer not null default 0,
            comparison_count integer not null default 0,
            conflict_count integer not null default 0,
            resolved_count integer not null default 0,
            summary_json text not null,
            started_at text not null,
            completed_at text not null
        );
        """
    )
    for declaration in (
        "run_id text",
        "rule_id text",
        "comparison_method text",
        "left_source_id text",
        "right_source_id text",
        "absolute_difference real",
        "relative_difference real",
        "details_json text not null default '{}'",
    ):
        _add_column(conn, "data_reconciliation_conflicts", declaration)
    conn.executescript(
        """
        create index if not exists idx_data_reconciliation_runs_completed
            on data_reconciliation_runs(completed_at desc, run_id);
        create index if not exists idx_data_reconciliation_runs_dataset
            on data_reconciliation_runs(dataset, status, completed_at desc);
        create index if not exists idx_data_conflicts_identity
            on data_reconciliation_conflicts(
                dataset, entity_id, observation_key, field_name,
                left_source_id, right_source_id, status
            );
        """
    )


def _migration_24(conn: sqlite3.Connection) -> None:
    """Persist immutable revision-to-indicator-to-conclusion lineage."""

    conn.executescript(
        """
        create table if not exists data_lineage_artifacts (
            artifact_id text primary key,
            artifact_type text not null,
            name text not null,
            entity_id text,
            observation_key text,
            value_json text not null,
            quality_status text not null,
            artifact_hash text not null unique,
            metadata_json text not null,
            created_at text not null
        );

        create table if not exists data_artifact_lineage_edges (
            lineage_id text primary key,
            output_artifact_id text not null
                references data_lineage_artifacts(artifact_id) on delete restrict,
            input_artifact_id text
                references data_lineage_artifacts(artifact_id) on delete restrict,
            input_revision_id text
                references data_revisions(revision_id) on delete restrict,
            input_role text not null,
            input_fields_json text not null,
            transformation_id text not null,
            code_version text not null,
            parameters_json text not null,
            created_at text not null,
            check (
                (input_artifact_id is not null and input_revision_id is null)
                or
                (input_artifact_id is null and input_revision_id is not null)
            )
        );

        create index if not exists idx_data_lineage_artifacts_created
            on data_lineage_artifacts(created_at desc, artifact_id);
        create index if not exists idx_data_lineage_artifacts_entity
            on data_lineage_artifacts(entity_id, artifact_type, created_at desc);
        create index if not exists idx_data_artifact_lineage_output
            on data_artifact_lineage_edges(output_artifact_id, created_at);
        create index if not exists idx_data_artifact_lineage_input_artifact
            on data_artifact_lineage_edges(input_artifact_id);
        create index if not exists idx_data_artifact_lineage_input_revision
            on data_artifact_lineage_edges(input_revision_id);

        create trigger if not exists trg_data_lineage_artifacts_immutable_update
        before update on data_lineage_artifacts
        begin
            select raise(abort, 'data lineage artifacts are immutable');
        end;

        create trigger if not exists trg_data_lineage_artifacts_immutable_delete
        before delete on data_lineage_artifacts
        begin
            select raise(abort, 'data lineage artifacts are immutable');
        end;

        create trigger if not exists trg_data_artifact_lineage_edges_immutable_update
        before update on data_artifact_lineage_edges
        begin
            select raise(abort, 'data artifact lineage edges are immutable');
        end;

        create trigger if not exists trg_data_artifact_lineage_edges_immutable_delete
        before delete on data_artifact_lineage_edges
        begin
            select raise(abort, 'data artifact lineage edges are immutable');
        end;
        """
    )


def _migration_25(conn: sqlite3.Connection) -> None:
    """Persist revisioned one-minute candles and immutable import receipts."""

    conn.executescript(
        """
        create table if not exists intraday_candle_revisions (
            revision_id text primary key,
            symbol text not null,
            trading_date text not null,
            bucket_start text not null,
            bucket_end text not null,
            open real not null,
            high real not null,
            low real not null,
            close real not null,
            volume_lots real not null,
            turnover real,
            average real,
            opening_cumulative_volume_lots real,
            closing_cumulative_volume_lots real,
            source_id text not null,
            source_kind text not null,
            source_priority integer not null,
            authorized integer not null,
            is_final integer not null,
            sequence integer,
            observed_at text not null,
            ingested_at text not null,
            raw_payload_hash text not null,
            raw_payload_json text not null,
            quality_json text not null,
            unique(symbol, bucket_start, source_id, raw_payload_hash),
            check (high >= open and high >= close and high >= low),
            check (low <= open and low <= close and low <= high),
            check (volume_lots >= 0),
            check (authorized in (0, 1)),
            check (is_final in (0, 1))
        );

        create table if not exists intraday_candle_import_receipts (
            import_id text primary key,
            symbol text not null,
            trading_date text not null,
            source_id text not null,
            source_priority integer not null,
            source_timeframe_minutes integer not null,
            candle_count integer not null,
            is_complete integer not null,
            requested_at text not null,
            completed_at text not null,
            response_hash text not null,
            metadata_json text not null,
            check (source_timeframe_minutes = 1),
            check (candle_count >= 0),
            check (is_complete in (0, 1))
        );

        create index if not exists idx_intraday_candle_lookup
            on intraday_candle_revisions(
                symbol, trading_date, bucket_start,
                source_priority desc, is_final desc, observed_at desc
            );
        create index if not exists idx_intraday_candle_source
            on intraday_candle_revisions(
                source_id, trading_date, symbol, ingested_at desc
            );
        create index if not exists idx_intraday_import_lookup
            on intraday_candle_import_receipts(
                symbol, trading_date, source_priority desc, completed_at desc
            );

        create trigger if not exists trg_intraday_candle_revisions_immutable_update
        before update on intraday_candle_revisions
        begin
            select raise(abort, 'intraday candle revisions are immutable');
        end;

        create trigger if not exists trg_intraday_candle_revisions_immutable_delete
        before delete on intraday_candle_revisions
        begin
            select raise(abort, 'intraday candle revisions are immutable');
        end;

        create trigger if not exists trg_intraday_candle_imports_immutable_update
        before update on intraday_candle_import_receipts
        begin
            select raise(abort, 'intraday candle import receipts are immutable');
        end;

        create trigger if not exists trg_intraday_candle_imports_immutable_delete
        before delete on intraday_candle_import_receipts
        begin
            select raise(abort, 'intraday candle import receipts are immutable');
        end;
        """
    )


def _migration_26(conn: sqlite3.Connection) -> None:
    """Index immutable daily revisions for complete range reads and imports."""

    conn.executescript(
        """
        create index if not exists idx_market_prices_daily_history
            on market_prices(
                dataset, entity_id, observation_key, source_id, revision desc
            );
        create index if not exists idx_data_revisions_daily_history
            on data_revisions(
                dataset, entity_id, observation_key, source_id, revision desc
            );
        create index if not exists idx_daily_history_checkpoints
            on data_ingestion_checkpoints(
                source_id, dataset, partition_key, status
            );
        """
    )


def _migration_27(conn: sqlite3.Connection) -> None:
    """Index immutable official factors and adjusted daily projections."""

    conn.executescript(
        """
        create index if not exists idx_market_prices_adjusted_history
            on market_prices(
                dataset, entity_id, observation_key, source_id, revision desc
            );
        create index if not exists idx_data_revisions_adjustment_events
            on data_revisions(
                dataset, entity_id, effective_at, source_id, revision desc
            );
        create index if not exists idx_adjustment_factor_checkpoints
            on data_ingestion_checkpoints(
                dataset, source_id, partition_key, status
            );
        """
    )


def _migration_28(conn: sqlite3.Connection) -> None:
    """Canonical corporate actions plus exactly-once paper-account application."""

    conn.executescript(
        """
        create table if not exists corporate_action_revisions (
            revision_id text primary key,
            action_id text not null,
            revision integer not null,
            symbol text not null,
            market text,
            effective_date text not null,
            record_date text,
            payment_date text,
            action_type text not null,
            status text not null,
            official_verified integer not null default 0,
            source_id text not null,
            source_url text not null,
            acquired_at text not null,
            payload_hash text not null,
            supersedes_revision_id text
                references corporate_action_revisions(revision_id) on delete restrict,
            terms_json text not null,
            source_payload_json text not null,
            created_at text not null,
            unique(action_id, revision),
            unique(action_id, payload_hash)
        );

        create table if not exists paper_corporate_action_applications (
            application_id text primary key,
            account_id text not null references paper_accounts(account_id),
            action_id text not null,
            action_revision_id text not null
                references corporate_action_revisions(revision_id) on delete restrict,
            symbol text not null,
            action_type text not null,
            effective_date text not null,
            outcome text not null,
            cash_delta real not null default 0,
            before_json text not null,
            after_json text not null,
            notes_json text not null,
            applied_at text not null,
            unique(account_id, action_id)
        );

        create table if not exists paper_corporate_action_entitlements (
            entitlement_id text primary key,
            application_id text not null
                references paper_corporate_action_applications(application_id) on delete restrict,
            account_id text not null references paper_accounts(account_id),
            action_id text not null,
            entitlement_type text not null,
            symbol text not null,
            quantity real not null default 0,
            cash_amount real not null default 0,
            currency text not null,
            status text not null,
            metadata_json text not null,
            created_at text not null,
            unique(account_id, action_id, entitlement_type)
        );

        create index if not exists idx_corporate_actions_symbol_effective
            on corporate_action_revisions(symbol, effective_date desc, revision desc);
        create index if not exists idx_corporate_actions_type_effective
            on corporate_action_revisions(action_type, effective_date desc);
        create index if not exists idx_paper_corporate_actions_account_applied
            on paper_corporate_action_applications(account_id, applied_at desc);
        create index if not exists idx_paper_entitlements_account_status
            on paper_corporate_action_entitlements(account_id, status, created_at desc);

        create trigger if not exists trg_corporate_action_revisions_immutable_update
        before update on corporate_action_revisions
        begin
            select raise(abort, 'corporate action revisions are immutable');
        end;
        create trigger if not exists trg_corporate_action_revisions_immutable_delete
        before delete on corporate_action_revisions
        begin
            select raise(abort, 'corporate action revisions are immutable');
        end;
        create trigger if not exists trg_paper_corporate_action_applications_immutable_update
        before update on paper_corporate_action_applications
        begin
            select raise(abort, 'corporate action applications are immutable');
        end;
        create trigger if not exists trg_paper_corporate_action_applications_immutable_delete
        before delete on paper_corporate_action_applications
        begin
            select raise(abort, 'corporate action applications are immutable');
        end;
        create trigger if not exists trg_paper_corporate_action_entitlements_immutable_update
        before update on paper_corporate_action_entitlements
        begin
            select raise(abort, 'corporate action entitlements are immutable');
        end;
        create trigger if not exists trg_paper_corporate_action_entitlements_immutable_delete
        before delete on paper_corporate_action_entitlements
        begin
            select raise(abort, 'corporate action entitlements are immutable');
        end;
        """
    )


def _migration_29(conn: sqlite3.Connection) -> None:
    """Revisioned official trading restrictions used by paper execution gates."""

    conn.executescript(
        """
        create table if not exists trading_restriction_revisions (
            revision_id text primary key,
            restriction_id text not null,
            revision integer not null,
            symbol text not null,
            market text,
            restriction_type text not null,
            status text not null,
            effective_from text not null,
            effective_until text,
            official_verified integer not null default 0,
            source_id text not null,
            source_url text not null,
            acquired_at text not null,
            payload_hash text not null,
            supersedes_revision_id text
                references trading_restriction_revisions(revision_id) on delete restrict,
            terms_json text not null,
            source_payload_json text not null,
            created_at text not null,
            unique(restriction_id, revision),
            unique(restriction_id, payload_hash)
        );

        create index if not exists idx_trading_restrictions_symbol_effective
            on trading_restriction_revisions(symbol, effective_from desc, revision desc);
        create index if not exists idx_trading_restrictions_type_effective
            on trading_restriction_revisions(restriction_type, effective_from desc);

        create trigger if not exists trg_trading_restriction_revisions_immutable_update
        before update on trading_restriction_revisions
        begin
            select raise(abort, 'trading restriction revisions are immutable');
        end;
        create trigger if not exists trg_trading_restriction_revisions_immutable_delete
        before delete on trading_restriction_revisions
        begin
            select raise(abort, 'trading restriction revisions are immutable');
        end;
        """
    )


def _migration_30(conn: sqlite3.Connection) -> None:
    """Preserve official share counts and reproducible liquidity assessments."""

    conn.executescript(
        """
        create table if not exists official_share_revisions (
            revision_id text primary key,
            symbol text not null,
            revision integer not null,
            effective_date text not null,
            issued_common_shares integer not null check(issued_common_shares > 0),
            source_id text not null,
            source_url text not null,
            acquired_at text not null,
            payload_hash text not null,
            supersedes_revision_id text
                references official_share_revisions(revision_id) on delete restrict,
            source_payload_json text not null,
            created_at text not null,
            unique(symbol, revision),
            unique(symbol, payload_hash)
        );

        create table if not exists liquidity_assessments (
            assessment_id text primary key,
            symbol text not null,
            assessed_at text not null,
            history_start text,
            history_end text,
            window_sessions integer not null,
            order_quantity_shares integer,
            tradability_status text not null,
            estimated_slippage_bps real,
            history_source_ids_json text not null,
            quote_source_id text,
            share_revision_id text
                references official_share_revisions(revision_id) on delete restrict,
            input_hash text not null,
            assessment_json text not null,
            created_at text not null
        );

        create index if not exists idx_official_shares_symbol_effective
            on official_share_revisions(symbol, effective_date desc, revision desc);
        create index if not exists idx_liquidity_assessments_symbol_time
            on liquidity_assessments(symbol, assessed_at desc);

        create trigger if not exists trg_official_share_revisions_immutable_update
        before update on official_share_revisions
        begin
            select raise(abort, 'official share revisions are immutable');
        end;
        create trigger if not exists trg_official_share_revisions_immutable_delete
        before delete on official_share_revisions
        begin
            select raise(abort, 'official share revisions are immutable');
        end;
        create trigger if not exists trg_liquidity_assessments_immutable_update
        before update on liquidity_assessments
        begin
            select raise(abort, 'liquidity assessments are immutable');
        end;
        create trigger if not exists trg_liquidity_assessments_immutable_delete
        before delete on liquidity_assessments
        begin
            select raise(abort, 'liquidity assessments are immutable');
        end;
        """
    )


def _migration_31(conn: sqlite3.Connection) -> None:
    """Persist deterministic trading anomalies and append-only lifecycle actions."""

    conn.executescript(
        """
        create table if not exists trading_anomaly_events (
            event_id text primary key,
            fingerprint text not null unique,
            symbol text not null,
            trading_date text not null,
            event_type text not null,
            direction text not null,
            severity text not null,
            detector_version text not null,
            source_ids_json text not null,
            source_record_json text not null,
            metrics_json text not null,
            policy_json text not null,
            title text not null,
            summary text not null,
            detected_at text not null,
            created_at text not null
        );

        create table if not exists trading_anomaly_observations (
            observation_id text primary key,
            scan_id text not null,
            event_id text not null
                references trading_anomaly_events(event_id) on delete restrict,
            observed_at text not null,
            source_ids_json text not null,
            metrics_json text not null,
            unique(scan_id, event_id)
        );

        create table if not exists trading_anomaly_tracking_actions (
            action_id text primary key,
            event_id text not null
                references trading_anomaly_events(event_id) on delete restrict,
            status text not null,
            note text,
            actor text not null,
            acted_at text not null,
            check(status in ('open', 'acknowledged', 'resolved', 'reopened'))
        );

        create index if not exists idx_trading_anomalies_symbol_date
            on trading_anomaly_events(symbol, trading_date desc, severity);
        create index if not exists idx_trading_anomaly_observations_event
            on trading_anomaly_observations(event_id, observed_at desc);
        create index if not exists idx_trading_anomaly_tracking_event
            on trading_anomaly_tracking_actions(event_id, acted_at desc);

        create trigger if not exists trg_trading_anomaly_events_immutable_update
        before update on trading_anomaly_events
        begin
            select raise(abort, 'trading anomaly events are immutable');
        end;
        create trigger if not exists trg_trading_anomaly_events_immutable_delete
        before delete on trading_anomaly_events
        begin
            select raise(abort, 'trading anomaly events are immutable');
        end;
        create trigger if not exists trg_trading_anomaly_observations_immutable_update
        before update on trading_anomaly_observations
        begin
            select raise(abort, 'trading anomaly observations are immutable');
        end;
        create trigger if not exists trg_trading_anomaly_observations_immutable_delete
        before delete on trading_anomaly_observations
        begin
            select raise(abort, 'trading anomaly observations are immutable');
        end;
        create trigger if not exists trg_trading_anomaly_tracking_immutable_update
        before update on trading_anomaly_tracking_actions
        begin
            select raise(abort, 'trading anomaly tracking actions are immutable');
        end;
        create trigger if not exists trg_trading_anomaly_tracking_immutable_delete
        before delete on trading_anomaly_tracking_actions
        begin
            select raise(abort, 'trading anomaly tracking actions are immutable');
        end;
        """
    )


def _migration_32(conn: sqlite3.Connection) -> None:
    """Add the normalized projections required to rebuild Agent Dock state."""

    conn.executescript(
        """
        create table if not exists agent_plan_steps (
            run_id text not null references agent_runs(run_id) on delete cascade,
            node_id text not null,
            plan_id text,
            parent_node_id text,
            node_type text not null,
            title text not null,
            description text,
            status text not null,
            order_index integer not null default 0,
            dependency_ids_json text not null default '[]',
            assigned_agent text,
            capability text,
            skill_ids_json text not null default '[]',
            tool_call_ids_json text not null default '[]',
            reason_summary text,
            result_summary text,
            error_summary text,
            started_at text,
            completed_at text,
            payload_json text not null,
            primary key(run_id, node_id)
        );
        create table if not exists agent_environment_snapshots (
            snapshot_id text primary key,
            run_id text not null references agent_runs(run_id) on delete cascade,
            snapshot_hash text not null,
            created_at text not null,
            expired_at text,
            payload_json text not null
        );
        create index if not exists idx_agent_plan_steps_run_order
            on agent_plan_steps(run_id, order_index, node_id);
        create index if not exists idx_agent_environment_run_created
            on agent_environment_snapshots(run_id, created_at desc);
        create index if not exists idx_agent_events_event_id
            on agent_events(event_type, run_id, sequence);
        """
    )
    _add_column(conn, "agent_messages", "kind text not null default 'text'")
    _add_column(conn, "agent_messages", "status text not null default 'completed'")
    _add_column(conn, "agent_sessions", "archived integer not null default 0")
    _add_column(conn, "agent_tool_calls", "node_id text")


def _migration_33(conn: sqlite3.Connection) -> None:
    """Index official monthly-revenue periods and incremental archive coverage."""

    conn.executescript(
        """
        create index if not exists idx_financial_facts_monthly_revenue_period
            on financial_facts(
                entity_id, observation_key desc, source_id, revision desc
            )
            where dataset='revenues_monthly';
        create index if not exists idx_monthly_revenue_history_checkpoints
            on data_ingestion_checkpoints(
                source_id, dataset, partition_key, status, last_success_at desc
            )
            where dataset='revenues_monthly';
        """
    )


def _migration_34(conn: sqlite3.Connection) -> None:
    """Index official quarterly income statements and archive coverage."""

    conn.executescript(
        """
        create index if not exists idx_financial_facts_income_statement_period
            on financial_facts(
                entity_id, observation_key desc, source_id, revision desc
            )
            where dataset='fundamentals_quarterly';
        create index if not exists idx_income_statement_history_checkpoints
            on data_ingestion_checkpoints(
                source_id, dataset, partition_key, status, last_success_at desc
            )
            where dataset='fundamentals_quarterly';
        """
    )


def _migration_35(conn: sqlite3.Connection) -> None:
    """Index official quarterly balance sheets and their incremental partitions."""

    conn.executescript(
        """
        create index if not exists idx_financial_facts_balance_sheet_period
            on financial_facts(
                entity_id, observation_key desc, source_id, revision desc
            )
            where dataset='fundamentals_quarterly'
              and observation_key like 'balance-sheet:%';
        create index if not exists idx_balance_sheet_history_checkpoints
            on data_ingestion_checkpoints(
                source_id, dataset, partition_key, status, last_success_at desc
            )
            where dataset='fundamentals_quarterly'
              and partition_key like 'balance-sheet:%';
        """
    )


def _migration_36(conn: sqlite3.Connection) -> None:
    """Index cash-flow statements and their incremental archive partitions."""

    conn.executescript(
        """
        create index if not exists idx_financial_facts_cash_flow_period
            on financial_facts(
                entity_id, observation_key desc, source_id, revision desc
            )
            where dataset='fundamentals_quarterly'
              and observation_key like 'cash-flow:%';
        create index if not exists idx_cash_flow_history_checkpoints
            on data_ingestion_checkpoints(
                source_id, dataset, partition_key, status, last_success_at desc
            )
            where dataset='fundamentals_quarterly'
              and partition_key like 'cash-flow:%';
        """
    )


def _migration_37(conn: sqlite3.Connection) -> None:
    """Create the final Session-first recursive Agent Runtime domain schema."""

    conn.executescript(
        """
        create table if not exists agent_objective_versions (
            objective_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            revision integer not null,
            objective text not null,
            added_requirements_json text not null default '[]',
            removed_requirements_json text not null default '[]',
            constraints_json text not null default '[]',
            supersedes text references agent_objective_versions(objective_id) on delete set null,
            created_from_message text references agent_messages(message_id) on delete set null,
            created_at text not null,
            payload_json text not null default '{}',
            unique(session_id, revision)
        );

        create table if not exists agent_task_forests (
            forest_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete set null,
            objective_id text not null references agent_objective_versions(objective_id) on delete restrict,
            root_branch_id text,
            status text not null,
            revision integer not null default 1,
            created_at text not null,
            updated_at text not null,
            payload_json text not null
        );

        create table if not exists agent_branches (
            branch_id text primary key,
            forest_id text not null references agent_task_forests(forest_id) on delete cascade,
            parent_branch_id text references agent_branches(branch_id) on delete cascade,
            objective_id text not null references agent_objective_versions(objective_id) on delete restrict,
            status text not null,
            execution_mode text not null,
            depth integer not null default 0,
            plan_revision integer not null default 1,
            result_json text,
            created_at text not null,
            updated_at text not null,
            payload_json text not null
        );

        create table if not exists agent_branch_plans (
            branch_plan_id text primary key,
            branch_id text not null references agent_branches(branch_id) on delete cascade,
            revision integer not null,
            status text not null,
            created_at text not null,
            payload_json text not null,
            unique(branch_id, revision)
        );

        create table if not exists agent_branch_steps (
            step_id text primary key,
            branch_id text not null references agent_branches(branch_id) on delete cascade,
            branch_plan_id text references agent_branch_plans(branch_plan_id) on delete cascade,
            position integer not null,
            status text not null,
            created_at text not null,
            updated_at text not null,
            payload_json text not null
        );

        create table if not exists agent_branch_dependencies (
            branch_id text not null references agent_branches(branch_id) on delete cascade,
            dependency_branch_id text not null references agent_branches(branch_id) on delete cascade,
            dependency_type text not null default 'completion',
            created_at text not null,
            payload_json text not null,
            primary key(branch_id, dependency_branch_id)
        );

        create table if not exists agent_join_nodes (
            join_id text primary key,
            forest_id text not null references agent_task_forests(forest_id) on delete cascade,
            parent_branch_id text references agent_branches(branch_id) on delete set null,
            status text not null,
            created_at text not null,
            updated_at text not null,
            payload_json text not null
        );

        create table if not exists agent_decision_checkpoints (
            interaction_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete set null,
            branch_id text references agent_branches(branch_id) on delete set null,
            status text not null,
            interaction_type text not null,
            created_at text not null,
            responded_at text,
            payload_json text not null,
            response_json text
        );

        create table if not exists agent_user_proposals (
            proposal_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            message_id text references agent_messages(message_id) on delete set null,
            branch_id text references agent_branches(branch_id) on delete set null,
            proposal_type text not null,
            status text not null,
            created_at text not null,
            payload_json text not null
        );

        create table if not exists agent_proposal_evaluations (
            evaluation_id text primary key,
            proposal_id text not null references agent_user_proposals(proposal_id) on delete cascade,
            decision text not null,
            created_at text not null,
            payload_json text not null
        );

        create table if not exists agent_failure_fingerprints (
            fingerprint text primary key,
            first_seen_at text not null,
            last_seen_at text not null,
            occurrence_count integer not null default 1,
            identical_retry_count integer not null default 0,
            payload_json text not null
        );

        create table if not exists agent_failure_ledger (
            error_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete set null,
            branch_id text references agent_branches(branch_id) on delete set null,
            step_id text,
            fingerprint text not null references agent_failure_fingerprints(fingerprint) on delete restrict,
            category text not null,
            status text not null,
            created_at text not null,
            resolved_at text,
            payload_json text not null
        );

        create table if not exists agent_repair_attempts (
            repair_id text primary key,
            error_id text not null references agent_failure_ledger(error_id) on delete cascade,
            level integer not null,
            strategy text not null,
            status text not null,
            output_hash text,
            arguments_hash text,
            started_at text not null,
            completed_at text,
            payload_json text not null
        );

        create table if not exists agent_memory_candidates (
            candidate_id text primary key,
            namespace text not null,
            session_id text references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete set null,
            kind text not null,
            status text not null,
            importance real not null,
            future_relevance real not null,
            confidence real not null,
            durability text not null,
            created_at text not null,
            decided_at text,
            payload_json text not null
        );

        create table if not exists agent_memory_conflicts (
            conflict_id text primary key,
            candidate_id text not null references agent_memory_candidates(candidate_id) on delete cascade,
            existing_memory_id text not null references agent_memory(memory_id) on delete cascade,
            status text not null,
            created_at text not null,
            resolved_at text,
            payload_json text not null
        );

        create table if not exists agent_memory_supersession (
            supersession_id text primary key,
            old_memory_id text not null references agent_memory(memory_id) on delete cascade,
            new_memory_id text not null references agent_memory(memory_id) on delete cascade,
            candidate_id text references agent_memory_candidates(candidate_id) on delete set null,
            created_at text not null,
            reason text not null,
            unique(old_memory_id, new_memory_id)
        );

        create table if not exists agent_procedural_lessons (
            lesson_id text primary key,
            namespace text not null,
            fingerprint text,
            status text not null,
            verified_by_host integer not null default 0,
            occurrence_count integer not null default 1,
            created_at text not null,
            updated_at text not null,
            payload_json text not null
        );

        create table if not exists agent_artifact_versions (
            artifact_version_id text primary key,
            artifact_id text not null references agent_artifacts(artifact_id) on delete cascade,
            version integer not null,
            parent_version integer,
            changed_by text not null,
            change_reason text,
            reason text not null,
            message_id text references agent_messages(message_id) on delete set null,
            base_version integer,
            affected_node_ids_json text not null default '[]',
            validation_result_json text not null default '{}',
            restored_from_version integer,
            content_json text not null,
            validation_status text not null default 'pending',
            sha256 text not null default '',
            created_at text not null,
            payload_json text not null default '{}',
            unique(artifact_id, version)
        );

        create table if not exists agent_artifact_selections (
            selection_id text primary key,
            artifact_id text not null references agent_artifacts(artifact_id) on delete cascade,
            artifact_version integer not null,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            branch_id text references agent_branches(branch_id) on delete set null,
            target_type text not null,
            node_id text,
            path text,
            created_at text not null,
            payload_json text not null
        );

        create table if not exists agent_automation_intents (
            intent_id text primary key,
            session_id text references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete set null,
            branch_id text references agent_branches(branch_id) on delete set null,
            user_id text not null,
            goal text not null,
            symbol text,
            kind text not null,
            status text not null default 'draft',
            semantic_fingerprint text not null,
            intent_json text not null,
            created_at text not null,
            updated_at text,
            payload_json text not null default '{}'
        );

        create table if not exists agent_automations (
            automation_id text primary key,
            intent_id text not null references agent_automation_intents(intent_id) on delete restrict,
            session_id text references agent_sessions(session_id) on delete cascade,
            user_id text not null,
            goal text not null,
            symbol text,
            kind text not null,
            state text not null,
            status text,
            backend text,
            backend_reference text,
            semantic_fingerprint text not null,
            current_version integer not null default 0,
            current_decision_json text,
            notification_state_json text not null default '{}',
            created_at text not null,
            updated_at text not null,
            expires_at text,
            payload_json text not null default '{}'
        );

        create table if not exists agent_automation_versions (
            automation_version_id text,
            automation_id text not null references agent_automations(automation_id) on delete cascade,
            version integer not null,
            status text not null default 'created',
            intent_json text not null,
            compiled_json text not null,
            artifact_json text not null,
            created_at text not null,
            payload_json text not null default '{}',
            unique(automation_id, version)
        );

        create table if not exists agent_automation_executions (
            execution_id text primary key,
            automation_id text not null references agent_automations(automation_id) on delete cascade,
            automation_version integer,
            run_id text references agent_runs(run_id) on delete set null,
            stage text not null,
            status text not null,
            input_json text not null default '{}',
            output_json text not null default '{}',
            meaningful_change integer,
            decision_changed integer,
            created_at text not null,
            started_at text,
            completed_at text,
            payload_json text not null default '{}'
        );

        create table if not exists agent_notification_deliveries (
            delivery_id text primary key,
            automation_id text references agent_automations(automation_id) on delete set null,
            execution_id text references agent_automation_executions(execution_id) on delete set null,
            user_id text not null,
            channel text not null,
            dedup_key text not null,
            status text not null,
            provider_receipt_json text not null default '{}',
            error text,
            created_at text not null,
            updated_at text not null,
            delivered_at text,
            acknowledged_at text,
            snoozed_until text,
            expires_at text,
            payload_json text not null default '{}'
        );

        create table if not exists agent_session_titles (
            session_id text primary key references agent_sessions(session_id) on delete cascade,
            current_revision integer not null,
            title text not null,
            updated_at text not null,
            payload_json text not null
        );

        create table if not exists agent_session_title_history (
            title_history_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            revision integer not null,
            title text not null,
            reason text not null,
            created_at text not null,
            payload_json text not null,
            unique(session_id, revision)
        );

        insert or ignore into agent_session_titles(
            session_id, current_revision, title, updated_at, payload_json
        )
        select session_id, 1, title, updated_at, '{"source":"migration_v37"}'
          from agent_sessions;

        insert or ignore into agent_session_title_history(
            title_history_id, session_id, revision, title, reason, created_at, payload_json
        )
        select 'ATH-legacy-' || session_id, session_id, 1, title,
               'migration_backfill', updated_at, '{"source":"migration_v37"}'
          from agent_sessions;

        create table if not exists agent_evidence (
            evidence_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete set null,
            branch_id text references agent_branches(branch_id) on delete set null,
            claim text not null,
            source_type text not null,
            observed_at text not null,
            payload_json text not null
        );

        create table if not exists agent_evidence_edges (
            edge_id text primary key,
            from_evidence_id text not null references agent_evidence(evidence_id) on delete cascade,
            to_evidence_id text references agent_evidence(evidence_id) on delete cascade,
            relation text not null,
            claim_key text,
            payload_json text not null
        );

        create table if not exists agent_kpi_events (
            kpi_event_id text primary key,
            session_id text references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete set null,
            metric text not null,
            value real not null,
            created_at text not null,
            payload_json text not null
        );

        create table if not exists agent_token_budgets (
            budget_id text primary key,
            session_id text not null references agent_sessions(session_id) on delete cascade,
            run_id text references agent_runs(run_id) on delete cascade,
            branch_id text references agent_branches(branch_id) on delete cascade,
            scope text not null,
            allocated integer not null,
            consumed integer not null default 0,
            updated_at text not null,
            payload_json text not null
        );

        create index if not exists idx_agent_objectives_session_revision
            on agent_objective_versions(session_id, revision desc);
        create index if not exists idx_agent_forests_session_updated
            on agent_task_forests(session_id, updated_at desc);
        create index if not exists idx_agent_branches_forest_status
            on agent_branches(forest_id, status, parent_branch_id);
        create index if not exists idx_agent_branch_steps_branch_position
            on agent_branch_steps(branch_id, position);
        create index if not exists idx_agent_interactions_session_status
            on agent_decision_checkpoints(session_id, status, created_at desc);
        create index if not exists idx_agent_failures_fingerprint_status
            on agent_failure_ledger(fingerprint, status, created_at desc);
        create index if not exists idx_agent_memory_candidates_status
            on agent_memory_candidates(namespace, status, created_at desc);
        create index if not exists idx_agent_artifact_versions_current
            on agent_artifact_versions(artifact_id, version desc);
        create index if not exists idx_agent_artifact_selections_session
            on agent_artifact_selections(session_id, created_at desc);
        create unique index if not exists idx_agent_automations_active_fingerprint
            on agent_automations(user_id, semantic_fingerprint)
            where state not in ('archived', 'deleted', 'expired');
        create index if not exists idx_agent_automation_executions_status
            on agent_automation_executions(automation_id, status, created_at desc);
        create index if not exists idx_agent_notifications_dedup
            on agent_notification_deliveries(dedup_key, status, created_at desc);
        create index if not exists idx_agent_kpi_metric_created
            on agent_kpi_events(metric, created_at desc);
        """
    )


def _migration_38(conn: sqlite3.Connection) -> None:
    """Align independently developed P71 stores without losing v37 data."""

    for definition in (
        "reason text not null default ''",
        "base_version integer",
        "affected_node_ids_json text not null default '[]'",
        "validation_result_json text not null default '{}'",
        "restored_from_version integer",
        "content_json text not null default '{}'",
    ):
        _add_column(conn, "agent_artifact_versions", definition)

    for definition in (
        "user_id text not null default ''",
        "goal text not null default ''",
        "symbol text",
        "kind text not null default 'condition_watch'",
        "intent_json text not null default '{}'",
    ):
        _add_column(conn, "agent_automation_intents", definition)

    for definition in (
        "user_id text not null default ''",
        "goal text not null default ''",
        "symbol text",
        "kind text not null default 'condition_watch'",
        "state text not null default 'draft'",
        "backend_reference text",
        "current_decision_json text",
        "notification_state_json text not null default '{}'",
    ):
        _add_column(conn, "agent_automations", definition)

    for definition in (
        "intent_json text not null default '{}'",
        "compiled_json text not null default '{}'",
        "artifact_json text not null default '{}'",
    ):
        _add_column(conn, "agent_automation_versions", definition)

    for definition in (
        "stage text not null default 'runtime'",
        "input_json text not null default '{}'",
        "output_json text not null default '{}'",
        "meaningful_change integer",
        "created_at text not null default ''",
    ):
        _add_column(conn, "agent_automation_executions", definition)

    for definition in (
        "user_id text not null default ''",
        "provider_receipt_json text not null default '{}'",
        "error text",
        "delivered_at text",
    ):
        _add_column(conn, "agent_notification_deliveries", definition)

    conn.executescript(
        """
        update agent_artifact_versions
           set reason=coalesce(nullif(reason, ''), change_reason, 'Artifact revision'),
               content_json=case
                   when content_json='{}' then coalesce(json_extract(payload_json, '$.content'), payload_json)
                   else content_json
               end;

        update agent_automation_intents
           set user_id=coalesce(nullif(user_id, ''), json_extract(payload_json, '$.user_id'), session_id, ''),
               goal=coalesce(nullif(goal, ''), json_extract(payload_json, '$.goal'), ''),
               symbol=coalesce(symbol, json_extract(payload_json, '$.symbol')),
               kind=coalesce(nullif(kind, ''), json_extract(payload_json, '$.kind'), 'condition_watch'),
               intent_json=case when intent_json='{}' then payload_json else intent_json end;

        update agent_automations
           set user_id=coalesce(nullif(user_id, ''), json_extract(payload_json, '$.user_id'), session_id, ''),
               goal=coalesce(nullif(goal, ''), json_extract(payload_json, '$.goal'), ''),
               symbol=coalesce(symbol, json_extract(payload_json, '$.symbol')),
               kind=coalesce(nullif(kind, ''), json_extract(payload_json, '$.kind'), 'condition_watch'),
               state=coalesce(nullif(state, ''), status, 'draft'),
               backend_reference=coalesce(backend_reference, json_extract(payload_json, '$.backend_reference'));

        update agent_automation_versions
           set intent_json=case when intent_json='{}' then coalesce(json_extract(payload_json, '$.intent'), '{}') else intent_json end,
               compiled_json=case when compiled_json='{}' then coalesce(json_extract(payload_json, '$.compiled'), '{}') else compiled_json end,
               artifact_json=case when artifact_json='{}' then coalesce(json_extract(payload_json, '$.artifact'), '{}') else artifact_json end;

        update agent_automation_executions
           set stage=coalesce(nullif(stage, ''), json_extract(payload_json, '$.stage'), 'runtime'),
               input_json=case when input_json='{}' then coalesce(json_extract(payload_json, '$.input'), '{}') else input_json end,
               output_json=case when output_json='{}' then coalesce(json_extract(payload_json, '$.output'), '{}') else output_json end,
               meaningful_change=coalesce(meaningful_change, decision_changed),
               created_at=coalesce(nullif(created_at, ''), started_at, completed_at, datetime('now'));

        update agent_notification_deliveries
           set user_id=coalesce(nullif(user_id, ''), json_extract(payload_json, '$.user_id'), ''),
               provider_receipt_json=case
                   when provider_receipt_json='{}' then coalesce(json_extract(payload_json, '$.provider_receipt'), '{}')
                   else provider_receipt_json
               end,
               error=coalesce(error, json_extract(payload_json, '$.error')),
               delivered_at=coalesce(delivered_at, json_extract(payload_json, '$.delivered_at'));

        drop index if exists idx_agent_automations_active_fingerprint;
        create unique index if not exists idx_agent_automations_active_fingerprint
            on agent_automations(user_id, semantic_fingerprint)
            where state not in ('archived', 'deleted', 'expired');
        """
    )


def _migration_39(conn: sqlite3.Connection) -> None:
    """Persist content-addressed model versions and research execution receipts.

    A report path is useful to a human, but it is not model provenance: it can
    be regenerated or deleted.  These records instead bind the exact PIT
    inputs and external worker receipt to the trained/inferred artifact hashes.
    They are append-only so an earlier approval can always be audited against
    the model evidence that existed at the time.
    """

    conn.executescript(
        """
        create table if not exists research_model_versions (
            model_version_id text primary key,
            framework text not null,
            artifact_sha256 text not null,
            dataset_manifest_hash text not null,
            dataset_sha256 text not null,
            configuration_sha256 text not null,
            runtime_receipt_sha256 text not null,
            created_at text not null,
            payload_json text not null,
            unique(framework, artifact_sha256, dataset_sha256, configuration_sha256)
        );

        create table if not exists research_experiment_receipts (
            experiment_id text primary key,
            request_symbol text not null,
            request_market text not null,
            request_horizon text not null,
            dataset_manifest_hash text not null,
            dataset_sha256 text not null,
            runtime_receipt_sha256 text not null,
            model_version_ids_json text not null,
            reproducibility_json text not null,
            created_at text not null,
            payload_json text not null
        );

        create index if not exists idx_research_model_versions_dataset
            on research_model_versions(dataset_manifest_hash, created_at desc);
        create index if not exists idx_research_experiments_request
            on research_experiment_receipts(request_symbol, created_at desc);

        create trigger if not exists trg_research_model_versions_immutable_update
        before update on research_model_versions
        begin
            select raise(abort, 'research model versions are immutable');
        end;
        create trigger if not exists trg_research_model_versions_immutable_delete
        before delete on research_model_versions
        begin
            select raise(abort, 'research model versions are immutable');
        end;
        create trigger if not exists trg_research_experiment_receipts_immutable_update
        before update on research_experiment_receipts
        begin
            select raise(abort, 'research experiment receipts are immutable');
        end;
        create trigger if not exists trg_research_experiment_receipts_immutable_delete
        before delete on research_experiment_receipts
        begin
            select raise(abort, 'research experiment receipts are immutable');
        end;
        """
    )


def _migration_40(conn: sqlite3.Connection) -> None:
    """Freeze availability contracts with each immutable warehouse revision.

    Existing rows deliberately remain empty: recreating a past contract from
    today's registry would manufacture historical evidence. PIT consumers
    fail closed for those legacy revisions instead.
    """

    _add_column(
        conn,
        "data_revisions",
        "availability_contract_snapshot_json text not null default '{}'",
    )


def _migration_41(conn: sqlite3.Connection) -> None:
    """Record baseline and approved strategy artifacts without mutating history."""

    conn.executescript(
        """
        create table if not exists strategy_artifacts (
            artifact_id text primary key,
            strategy_id text not null,
            artifact_kind text not null check (artifact_kind in ('baseline_rule', 'approved_rule', 'model')),
            source_sha256 text not null,
            configuration_sha256 text not null,
            approval_status text not null,
            created_at text not null,
            payload_json text not null,
            unique(strategy_id, source_sha256, configuration_sha256)
        );

        create index if not exists idx_strategy_artifacts_strategy_created
            on strategy_artifacts(strategy_id, created_at desc);

        create trigger if not exists trg_strategy_artifacts_immutable_update
        before update on strategy_artifacts
        begin
            select raise(abort, 'strategy artifacts are immutable');
        end;
        create trigger if not exists trg_strategy_artifacts_immutable_delete
        before delete on strategy_artifacts
        begin
            select raise(abort, 'strategy artifacts are immutable');
        end;
        """
    )


def _migration_42(conn: sqlite3.Connection) -> None:
    """Keep model promotion decisions append-only and auditable.

    A model version remains research-only by default.  This separate receipt
    records the human decision that names a champion, its challengers and the
    deployment boundary without mutating the immutable training artifact.
    """

    conn.executescript(
        """
        create table if not exists model_promotion_receipts (
            promotion_id text primary key,
            champion_model_version_id text not null,
            challenger_model_version_ids_json text not null,
            deployment_mode text not null check (deployment_mode in ('shadow', 'production')),
            created_at text not null,
            payload_json text not null
        );

        create index if not exists idx_model_promotions_champion_created
            on model_promotion_receipts(champion_model_version_id, created_at desc);

        create trigger if not exists trg_model_promotions_immutable_update
        before update on model_promotion_receipts
        begin
            select raise(abort, 'model promotion receipts are immutable');
        end;
        create trigger if not exists trg_model_promotions_immutable_delete
        before delete on model_promotion_receipts
        begin
            select raise(abort, 'model promotion receipts are immutable');
        end;
        """
    )


def _migration_43(conn: sqlite3.Connection) -> None:
    """Add durable acknowledgement, replacement and expiry links to paper orders."""

    _add_column(conn, "paper_broker_orders", "acknowledged_at text")
    _add_column(conn, "paper_broker_orders", "expires_at text")
    _add_column(conn, "paper_broker_orders", "expired_at text")
    _add_column(conn, "paper_broker_orders", "replaced_at text")
    _add_column(conn, "paper_broker_orders", "replaces_order_id text")
    _add_column(conn, "paper_broker_orders", "replaced_by_order_id text")
    conn.executescript(
        """
        create index if not exists idx_paper_broker_orders_expiry
            on paper_broker_orders(account_id, status, expires_at);
        create index if not exists idx_paper_broker_orders_replacement
            on paper_broker_orders(replaces_order_id, replaced_by_order_id);
        """
    )


def _migration_44(conn: sqlite3.Connection) -> None:
    """Keep paper cash balances distinct from durable T+2 settlement claims."""

    _add_column(conn, "paper_accounts", "settled_cash_balance real")
    conn.execute(
        """
        update paper_accounts
           set settled_cash_balance = cash_balance
         where settled_cash_balance is null
        """
    )


def _migration_45(conn: sqlite3.Connection) -> None:
    """Persist borrow evidence and every paper short-lot lifecycle event.

    A short sale may only reserve inventory from an immutable locate receipt.
    This schema intentionally does not treat public short-interest activity as
    borrow availability: the receipt must carry the available quantity, rate,
    expiry and any recall deadline for this simulated account.
    """

    conn.executescript(
        """
        create table if not exists paper_borrow_locates (
            receipt_id text primary key,
            account_id text not null references paper_accounts(account_id) on delete cascade,
            symbol text not null,
            created_at text not null,
            verified_at text not null,
            expires_at text not null,
            recall_at text,
            available_quantity real not null check(available_quantity > 0),
            reserved_quantity real not null default 0 check(reserved_quantity >= 0),
            annual_fee_bps real not null check(annual_fee_bps >= 0 and annual_fee_bps <= 1600),
            source text not null,
            status text not null check(status in ('active', 'recalled', 'expired', 'exhausted')),
            receipt_json text not null
        );
        create table if not exists paper_short_borrows (
            short_borrow_id text primary key,
            receipt_id text not null references paper_borrow_locates(receipt_id) on delete restrict,
            account_id text not null references paper_accounts(account_id) on delete cascade,
            fill_id text not null unique references paper_fills(fill_id) on delete restrict,
            symbol text not null,
            opened_at text not null,
            original_quantity real not null check(original_quantity > 0),
            open_quantity real not null check(open_quantity >= 0),
            entry_price real not null check(entry_price > 0),
            annual_fee_bps real not null check(annual_fee_bps >= 0 and annual_fee_bps <= 1600),
            last_fee_accrual_date text not null,
            status text not null check(status in ('open', 'recall_due', 'closed')),
            closed_at text
        );
        create table if not exists paper_borrow_events (
            id integer primary key autoincrement,
            account_id text not null references paper_accounts(account_id) on delete cascade,
            receipt_id text references paper_borrow_locates(receipt_id) on delete restrict,
            short_borrow_id text references paper_short_borrows(short_borrow_id) on delete restrict,
            created_at text not null,
            event_type text not null check(event_type in ('locate_imported', 'short_opened', 'short_covered', 'fee_accrued', 'recall_due', 'locate_expired')),
            quantity real,
            amount real,
            payload_json text not null
        );
        create index if not exists idx_paper_borrow_locates_account_symbol_status
            on paper_borrow_locates(account_id, symbol, status, expires_at);
        create index if not exists idx_paper_short_borrows_account_symbol_status
            on paper_short_borrows(account_id, symbol, status, opened_at);
        create index if not exists idx_paper_borrow_events_account_created
            on paper_borrow_events(account_id, created_at desc);
        create trigger if not exists trg_paper_borrow_locates_immutable_update
        before update on paper_borrow_locates
        when old.receipt_json <> new.receipt_json
        begin
            select raise(abort, 'borrow locate receipt is immutable');
        end;
        """
    )
    conn.executescript(
        """
        create table if not exists paper_settlements (
            settlement_id text primary key,
            fill_id text not null unique references paper_fills(fill_id) on delete restrict,
            order_id text not null references paper_orders(order_id) on delete restrict,
            account_id text not null references paper_accounts(account_id) on delete cascade,
            created_at text not null,
            trade_at text not null,
            settlement_due_at text not null,
            settlement_date text not null,
            side text not null check(side in ('buy', 'sell')),
            currency text not null,
            payable real not null default 0 check(payable >= 0),
            receivable real not null default 0 check(receivable >= 0),
            status text not null check(status in ('pending', 'settled')),
            settled_at text,
            receipt_json text not null
        );
        create table if not exists paper_settlement_events (
            id integer primary key autoincrement,
            settlement_id text not null references paper_settlements(settlement_id) on delete cascade,
            account_id text not null references paper_accounts(account_id) on delete cascade,
            created_at text not null,
            event_type text not null check(event_type in ('pending', 'settled')),
            payload_json text not null
        );
        create index if not exists idx_paper_settlements_due
            on paper_settlements(account_id, status, settlement_due_at);
        create index if not exists idx_paper_settlement_events_settlement
            on paper_settlement_events(settlement_id, id);
        """
    )


def _migration_46(conn: sqlite3.Connection) -> None:
    """Persist release-governance approvals, rollbacks and order version pins."""

    conn.executescript(
        """
        create table if not exists governed_approved_artifacts (
            artifact_id text primary key,
            artifact_sha256 text not null,
            approved_by text not null,
            approved_at text not null,
            metadata_json text not null default '{}',
            payload_json text not null
        );
        create table if not exists governed_artifact_activations (
            activation_id integer primary key autoincrement,
            artifact_id text not null references governed_approved_artifacts(artifact_id) on delete restrict,
            activated_at text not null
        );
        create table if not exists governed_artifact_rollback_receipts (
            receipt_sha256 text primary key,
            occurred_at text not null,
            payload_json text not null
        );
        create table if not exists governed_change_sets (
            change_id text primary key,
            code_sha256 text not null,
            model_sha256 text not null,
            data_sha256 text not null,
            risk_policy_sha256 text not null,
            approved_by text not null,
            approved_at text not null,
            payload_json text not null
        );
        create table if not exists governed_order_version_bindings (
            order_id text primary key,
            change_id text not null references governed_change_sets(change_id) on delete restrict,
            change_sha256 text not null,
            code_sha256 text not null,
            model_sha256 text not null,
            data_sha256 text not null,
            risk_policy_sha256 text not null,
            bound_at text not null,
            payload_json text not null
        );
        create index if not exists idx_governed_artifact_activations_artifact
            on governed_artifact_activations(artifact_id, activation_id desc);
        create index if not exists idx_governed_artifact_rollbacks_occurred
            on governed_artifact_rollback_receipts(occurred_at desc);
        create trigger if not exists trg_governed_approved_artifacts_immutable_update
        before update on governed_approved_artifacts
        begin
            select raise(abort, 'approved artifacts are immutable');
        end;
        create trigger if not exists trg_governed_approved_artifacts_immutable_delete
        before delete on governed_approved_artifacts
        begin
            select raise(abort, 'approved artifacts are immutable');
        end;
        create trigger if not exists trg_governed_artifact_activations_immutable_update
        before update on governed_artifact_activations
        begin
            select raise(abort, 'artifact activations are append-only');
        end;
        create trigger if not exists trg_governed_artifact_activations_immutable_delete
        before delete on governed_artifact_activations
        begin
            select raise(abort, 'artifact activations are append-only');
        end;
        create trigger if not exists trg_governed_artifact_rollbacks_immutable_update
        before update on governed_artifact_rollback_receipts
        begin
            select raise(abort, 'artifact rollback receipts are immutable');
        end;
        create trigger if not exists trg_governed_artifact_rollbacks_immutable_delete
        before delete on governed_artifact_rollback_receipts
        begin
            select raise(abort, 'artifact rollback receipts are immutable');
        end;
        create trigger if not exists trg_governed_change_sets_immutable_update
        before update on governed_change_sets
        begin
            select raise(abort, 'change sets are immutable');
        end;
        create trigger if not exists trg_governed_change_sets_immutable_delete
        before delete on governed_change_sets
        begin
            select raise(abort, 'change sets are immutable');
        end;
        create trigger if not exists trg_governed_order_bindings_immutable_update
        before update on governed_order_version_bindings
        begin
            select raise(abort, 'order version bindings are immutable');
        end;
        create trigger if not exists trg_governed_order_bindings_immutable_delete
        before delete on governed_order_version_bindings
        begin
            select raise(abort, 'order version bindings are immutable');
        end;
        """
    )


def _migration_47(conn: sqlite3.Connection) -> None:
    """Persist bounded signal projections and critical retention receipts."""

    conn.executescript(
        """
        create table if not exists governed_retained_content (
            record_id text primary key,
            kind text not null,
            critical integer not null check(critical in (0, 1)),
            content_sha256 text not null,
            occurred_at text not null,
            payload_json text not null
        );
        create table if not exists governed_retention_receipts (
            receipt_sha256 text primary key,
            occurred_at text not null,
            payload_json text not null
        );
        create index if not exists idx_governed_retained_content_kind_occurred
            on governed_retained_content(kind, occurred_at, record_id);
        create index if not exists idx_governed_retention_receipts_occurred
            on governed_retention_receipts(occurred_at desc);
        create trigger if not exists trg_governed_retained_content_immutable_update
        before update on governed_retained_content
        begin
            select raise(abort, 'retained content is immutable');
        end;
        create trigger if not exists trg_governed_retained_critical_immutable_delete
        before delete on governed_retained_content
        when old.critical = 1
        begin
            select raise(abort, 'critical retained content cannot be deleted');
        end;
        create trigger if not exists trg_governed_retention_receipts_immutable_update
        before update on governed_retention_receipts
        begin
            select raise(abort, 'retention receipts are immutable');
        end;
        create trigger if not exists trg_governed_retention_receipts_immutable_delete
        before delete on governed_retention_receipts
        begin
            select raise(abort, 'retention receipts are immutable');
        end;
        """
    )


def _ensure_data_quality_schema(conn: sqlite3.Connection) -> None:
    _add_column(conn, "data_quality_reports", "report_date text not null default ''")
    _add_column(conn, "data_quality_reports", "knowledge_at text not null default ''")
    _add_column(
        conn,
        "data_quality_reports",
        "time_misalignment_count integer not null default 0",
    )
    _add_column(conn, "data_quality_reports", "issue_count integer not null default 0")
    _add_column(
        conn,
        "data_quality_reports",
        "rules_version text not null default 'legacy'",
    )
    _add_column(conn, "data_quality_reports", "state_hash text not null default ''")
    conn.execute(
        """
        update data_quality_reports
           set report_date=substr(generated_at, 1, 10)
         where report_date=''
        """
    )
    conn.execute(
        """
        update data_quality_reports
           set knowledge_at=generated_at
         where knowledge_at=''
        """
    )
    conn.executescript(
        """
        create table if not exists data_quality_issues (
            issue_id text primary key,
            report_id text not null
                references data_quality_reports(report_id) on delete cascade,
            category text not null,
            severity text not null,
            code text not null,
            revision_id text references data_revisions(revision_id) on delete restrict,
            entity_id text,
            observation_key text,
            source_id text,
            field_name text,
            expected_json text,
            actual_json text,
            details_json text not null,
            detected_at text not null
        );

        create unique index if not exists idx_data_quality_report_state
            on data_quality_reports(state_hash) where state_hash != '';
        create index if not exists idx_data_quality_daily
            on data_quality_reports(report_date, dataset, generated_at desc);
        create index if not exists idx_data_quality_issues_report
            on data_quality_issues(report_id, category, severity);
        create index if not exists idx_data_quality_issues_revision
            on data_quality_issues(revision_id, detected_at desc);
        """
    )


def _drop_revision_history_immutability(conn: sqlite3.Connection) -> None:
    """Allow idempotent migration backfills before restoring runtime guards."""

    conn.executescript(
        """
        drop trigger if exists trg_data_revisions_immutable_update;
        drop trigger if exists trg_data_revisions_immutable_delete;
        drop trigger if exists trg_data_revision_snapshots_immutable_update;
        drop trigger if exists trg_data_revision_snapshots_immutable_delete;
        drop trigger if exists trg_data_revision_snapshot_items_immutable_update;
        drop trigger if exists trg_data_revision_snapshot_items_immutable_delete;
        """
    )


def _ensure_revision_history_immutability(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create trigger if not exists trg_data_revisions_immutable_update
        before update on data_revisions
        begin
            select raise(abort, 'data revisions are immutable');
        end;
        create trigger if not exists trg_data_revisions_immutable_delete
        before delete on data_revisions
        begin
            select raise(abort, 'data revisions are immutable');
        end;
        create trigger if not exists trg_data_revision_snapshots_immutable_update
        before update on data_revision_snapshots
        begin
            select raise(abort, 'data revision snapshots are immutable');
        end;
        create trigger if not exists trg_data_revision_snapshots_immutable_delete
        before delete on data_revision_snapshots
        begin
            select raise(abort, 'data revision snapshots are immutable');
        end;
        create trigger if not exists trg_data_revision_snapshot_items_immutable_update
        before update on data_revision_snapshot_items
        begin
            select raise(abort, 'data revision snapshot items are immutable');
        end;
        create trigger if not exists trg_data_revision_snapshot_items_immutable_delete
        before delete on data_revision_snapshot_items
        begin
            select raise(abort, 'data revision snapshot items are immutable');
        end;
        """
    )


def _ensure_entity_registry_schema(conn: sqlite3.Connection) -> None:
    """Idempotent v13 shape, including development databases already at v13."""

    _add_column(conn, "entity_identifiers", "normalized_value text")
    _add_column(conn, "entity_identifiers", "confidence real not null default 1.0")
    _add_column(conn, "entity_identifiers", "is_primary integer not null default 0")
    _add_column(conn, "entity_identifiers", "superseded_by_entity_id text")
    _add_column(conn, "entity_identifiers", "superseded_at text")
    conn.execute(
        """
        create table if not exists entity_identity_merges (
            merge_id text primary key,
            from_entity_id text not null references market_entities(entity_id),
            to_entity_id text not null references market_entities(entity_id),
            reason text not null,
            effective_at text not null,
            metadata_json text not null,
            created_at text not null,
            unique(from_entity_id, to_entity_id, reason)
        )
        """
    )
    conn.execute(
        """
        update entity_identifiers
           set normalized_value =
               case identifier_type
                 when 'unified_business_no'
                   then replace(replace(replace(trim(identifier_value), '-', ''), ' ', ''), '.', '')
                 else upper(replace(trim(identifier_value), ' ', ''))
               end
         where normalized_value is null or normalized_value = ''
        """
    )


def _add_column(conn: sqlite3.Connection, table: str, declaration: str) -> None:
    column = declaration.split()[0]
    existing = {str(row[1]) for row in conn.execute(f"pragma table_info({table})")}
    if column not in existing:
        conn.execute(f"alter table {table} add column {declaration}")


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create index if not exists idx_signals_created_at on signals(created_at desc);
        create index if not exists idx_signals_symbol_created_at on signals(symbol, created_at desc);
        create index if not exists idx_reports_created_at on reports(created_at desc);
        create index if not exists idx_trades_symbol_created_at on trades(symbol, created_at desc);
        create index if not exists idx_decision_logs_created_at on decision_logs(created_at desc);
        create index if not exists idx_decision_logs_symbol_created_at on decision_logs(symbol, created_at desc);
        create index if not exists idx_paper_orders_account_created_at on paper_orders(account_id, created_at desc);
        create index if not exists idx_paper_orders_symbol_created_at on paper_orders(symbol, created_at desc);
        create index if not exists idx_paper_fills_order_id on paper_fills(order_id);
        create index if not exists idx_paper_fills_symbol_created_at on paper_fills(symbol, created_at desc);
        create index if not exists idx_cash_ledger_account_created_at on cash_ledger(account_id, created_at desc);
        create index if not exists idx_paper_marks_account_symbol on paper_price_marks(account_id, symbol, id desc);
        create index if not exists idx_learning_episodes_account_created on agent_learning_episodes(account_id, created_at desc);
        create index if not exists idx_learning_events_episode_created on agent_learning_events(episode_id, created_at desc);
        create index if not exists idx_learning_events_symbol_created on agent_learning_events(symbol, created_at desc);
        create index if not exists idx_reflections_episode_created on agent_reflections(episode_id, created_at desc);
        create index if not exists idx_broker_orders_account_status_created on paper_broker_orders(account_id, status, created_at desc);
        create index if not exists idx_broker_orders_symbol_status on paper_broker_orders(symbol, status, created_at desc);
        create index if not exists idx_broker_events_order_created on paper_broker_order_events(order_id, created_at desc);
        create index if not exists idx_agent_runs_status_updated on agent_runs(status, updated_at desc);
        create index if not exists idx_agent_steps_run_step on agent_steps(run_id, step);
        create index if not exists idx_agent_tool_calls_run_step on agent_tool_calls(run_id, step, id);
        create index if not exists idx_agent_events_run_sequence on agent_events(run_id, sequence);
        create index if not exists idx_agent_approvals_run_status on agent_approvals(run_id, status, requested_at desc);
        create index if not exists idx_agent_schedules_enabled_next on agent_schedules(enabled, next_run_at);
        create index if not exists idx_agent_sessions_namespace_updated on agent_sessions(namespace, updated_at desc);
        create index if not exists idx_agent_messages_session_created on agent_messages(session_id, created_at);
        create index if not exists idx_agent_runs_session_created on agent_runs(session_id, created_at desc);
        create index if not exists idx_raw_objects_source_received
            on raw_data_objects(source_id, received_at desc);
        create index if not exists idx_raw_payload_objects_object
            on raw_payload_objects(raw_object_id);
        create index if not exists idx_raw_reprocessing_payload_started
            on raw_reprocessing_runs(raw_payload_id, started_at desc);
        create index if not exists idx_market_prices_lookup
            on market_prices(dataset, entity_id, effective_at, available_at, revision desc);
        create index if not exists idx_financial_facts_lookup
            on financial_facts(dataset, entity_id, effective_at, available_at, revision desc);
        create index if not exists idx_ownership_flows_lookup
            on ownership_flows(dataset, entity_id, effective_at, available_at, revision desc);
        create index if not exists idx_market_events_lookup
            on market_events(dataset, entity_id, effective_at, available_at, revision desc);
        create index if not exists idx_macro_observations_lookup
            on macro_observations(dataset, entity_id, effective_at, available_at, revision desc);
        create unique index if not exists idx_agent_runs_idempotency on agent_runs(idempotency_key)
            where idempotency_key is not null;
        create index if not exists idx_agent_plans_run_revision on agent_plans(run_id, current_revision);
        create index if not exists idx_agent_plan_revisions_plan_revision
            on agent_plan_revisions(plan_id, revision);
        create index if not exists idx_agent_checkpoints_run_sequence
            on agent_checkpoints(run_id, sequence desc);
        create index if not exists idx_agent_memory_namespace_kind_updated
            on agent_memory(namespace, kind, updated_at desc);
        create index if not exists idx_agent_artifacts_run_created
            on agent_artifacts(run_id, created_at desc);
        create index if not exists idx_agent_workflows_namespace_name
            on agent_workflows(namespace, name, version desc);
        create index if not exists idx_agent_workers_run_status
            on agent_workers(run_id, status, started_at desc);
        create index if not exists idx_agent_control_run_status_created
            on agent_control_messages(run_id, status, created_at);
        create index if not exists idx_agent_ui_commands_status_id
            on agent_ui_commands(status, command_id);
        create index if not exists idx_agent_schedules_claim
            on agent_schedules(enabled, next_run_at, claim_expires_at);
        create index if not exists idx_agent_runtime_events_status_created
            on agent_runtime_events(status, created_at, claim_expires_at);
        create unique index if not exists idx_agent_runtime_events_dedup
            on agent_runtime_events(dedup_key) where dedup_key is not null;
        create index if not exists idx_user_watchlists_user_updated
            on user_watchlists(user_id, updated_at desc);
        create index if not exists idx_universe_snapshots_source_created
            on universe_snapshots(source, created_at desc);
        create index if not exists idx_analysis_runs_status_created
            on analysis_runs(status, created_at desc);
        create index if not exists idx_model_invocations_analysis_status
            on model_invocations(analysis_run_id, status, started_at desc);
        create index if not exists idx_analysis_provenance_run
            on analysis_provenance(analysis_run_id, generated_at desc);
        create index if not exists idx_rule_results_run_symbol
            on rule_strategy_results(analysis_run_id, symbol);
        create index if not exists idx_model_results_run_symbol
            on model_analysis_results(analysis_run_id, symbol);
        create index if not exists idx_validation_reports_run_layer
            on validation_reports(analysis_run_id, layer, created_at);
        create index if not exists idx_entity_identifiers_entity
            on entity_identifiers(entity_id, valid_from, valid_to);
        create index if not exists idx_entity_identifiers_entity_type
            on entity_identifiers(entity_id, identifier_type, valid_from, valid_to);
        create index if not exists idx_entity_identifiers_normalized
            on entity_identifiers(identifier_type, normalized_value, valid_from, valid_to);
        create index if not exists idx_entity_identifiers_source_normalized
            on entity_identifiers(source_id, identifier_type, normalized_value, valid_from, valid_to);
        create index if not exists idx_entity_identifiers_current_normalized
            on entity_identifiers(identifier_type, normalized_value, superseded_at, valid_from, valid_to);
        create index if not exists idx_entity_identity_merges_from
            on entity_identity_merges(from_entity_id, effective_at);
        create index if not exists idx_entity_identity_merges_to
            on entity_identity_merges(to_entity_id, effective_at);
        create index if not exists idx_market_entities_type_status
            on market_entities(entity_type, lifecycle_status, exchange);
        create index if not exists idx_raw_data_source_received
            on raw_data_payloads(source_id, received_at desc);
        create index if not exists idx_data_revisions_point_in_time
            on data_revisions(dataset, entity_id, observation_key, available_at, acquired_at, revision desc);
        create index if not exists idx_data_revisions_bitemporal
            on data_revisions(
                dataset, entity_id, published_at, available_at, acquired_at,
                effective_at, expires_at, revision desc
            );
        create index if not exists idx_data_revisions_source_dataset
            on data_revisions(source_id, dataset, acquired_at desc);
        create index if not exists idx_data_revisions_quality_scan
            on data_revisions(
                dataset, entity_id, observation_key, source_id, payload_hash,
                quality_status, quality_flags_json, available_at, acquired_at
            );
        create index if not exists idx_data_lineage_output
            on data_lineage_edges(output_revision_id, created_at);
        create index if not exists idx_data_quality_dataset_generated
            on data_quality_reports(dataset, generated_at desc);
        create index if not exists idx_data_conflicts_dataset_status
            on data_reconciliation_conflicts(dataset, status, detected_at desc);
        create index if not exists idx_entity_lifecycle_entity_effective
            on entity_lifecycle_events(entity_id, effective_at, event_type);
        create index if not exists idx_entity_lifecycle_venue_type
            on entity_lifecycle_events(venue, listing_type, event_type, effective_at);
        create index if not exists idx_policy_proposals_account_status on policy_proposals(account_id, status, updated_at desc);
        create index if not exists idx_strategy_versions_status_created on strategy_versions(status, created_at desc);
        """
    )
