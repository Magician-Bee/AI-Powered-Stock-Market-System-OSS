from __future__ import annotations

import json
import sqlite3

import pytest

import open_stock_ai.storage.migrations as migrations
from open_stock_ai.agent_runtime.artifacts.versioning import SQLiteArtifactVersionStore
from open_stock_ai.agent_runtime.automation.store import AutomationStore
from open_stock_ai.agent_runtime.session.objective_manager import SQLiteObjectiveStore
from open_stock_ai.storage.migration_safety import load_migration_safety_receipt
from open_stock_ai.storage.p71_schema import (
    P71_REQUIRED_COLUMNS,
    P71_REQUIRED_INDEXES,
    P71SchemaContractError,
    verify_p71_schema,
)
from open_stock_ai.storage.sqlite_store import SQLiteStore


def _build_v37_database(path) -> None:
    with sqlite3.connect(path) as conn:
        migrations.configure_connection(conn)
        conn.execute(
            """
            create table schema_migrations (
                version integer primary key,
                name text not null,
                applied_at text not null default (datetime('now'))
            )
            """
        )
        for version in range(1, 38):
            getattr(migrations, f"_migration_{version}")(conn)
            conn.execute(
                "insert into schema_migrations(version, name) values (?, ?)",
                (version, f"p71_fixture_{version}"),
            )
        conn.execute("pragma user_version = 37")
        conn.commit()


def _seed_v37_p71_rows(path) -> None:
    timestamp = "2026-09-08T12:00:00+00:00"
    intent_payload = {
        "user_id": "legacy-user",
        "goal": "watch TSMC",
        "symbol": "2330.TW",
        "kind": "condition_watch",
    }
    automation_payload = {
        **intent_payload,
        "backend_reference": "legacy-ref",
        "current_decision": {"action": "hold"},
    }
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            insert into agent_sessions(
                session_id, namespace, title, status, created_at, updated_at,
                last_run_id, metadata_json
            ) values (?, ?, ?, ?, ?, ?, null, ?)
            """,
            ("SES-p71", "default", "P71 upgrade", "idle", timestamp, timestamp, "{}"),
        )
        conn.execute(
            """
            insert into agent_artifacts(
                artifact_id, session_id, run_id, kind, name, path, media_type,
                sha256, created_at, metadata_json
            ) values (?, ?, null, ?, ?, null, ?, ?, ?, ?)
            """,
            (
                "ART-p71", "SES-p71", "analysis", "legacy artifact",
                "application/json", "legacy-sha", timestamp, "{}",
            ),
        )
        conn.execute(
            """
            insert into agent_objective_versions(
                objective_id, session_id, revision, objective,
                added_requirements_json, removed_requirements_json,
                constraints_json, supersedes, created_from_message, created_at,
                payload_json
            ) values (?, ?, 1, ?, '[]', '[]', '["paper-only"]', null, null, ?, '{}')
            """,
            ("OBJ-p71", "SES-p71", "preserve durable state", timestamp),
        )
        conn.execute(
            """
            insert into agent_artifact_versions(
                artifact_version_id, artifact_id, version, parent_version,
                changed_by, change_reason, reason, message_id, base_version,
                affected_node_ids_json, validation_result_json,
                restored_from_version, content_json, validation_status, sha256,
                created_at, payload_json
            ) values (?, ?, 1, null, ?, ?, '', null, null, '[]', '{}', null,
                      '{}', 'valid', ?, ?, ?)
            """,
            (
                "ARV-p71", "ART-p71", "legacy", "initial", "legacy-sha",
                timestamp, json.dumps({"content": {"signal": "hold"}}),
            ),
        )
        conn.execute(
            """
            insert into agent_automation_intents(
                intent_id, session_id, run_id, branch_id, user_id, goal, symbol,
                kind, status, semantic_fingerprint, intent_json, created_at,
                updated_at, payload_json
            ) values (?, ?, null, null, '', '', null, '', 'confirmed', ?, '{}', ?, ?, ?)
            """,
            (
                "AIT-p71", "SES-p71", "fingerprint-p71", timestamp,
                timestamp, json.dumps(intent_payload),
            ),
        )
        conn.execute(
            """
            insert into agent_automations(
                automation_id, intent_id, session_id, user_id, goal, symbol,
                kind, state, status, backend, backend_reference,
                semantic_fingerprint, current_version, current_decision_json,
                notification_state_json, created_at, updated_at, expires_at,
                payload_json
            ) values (?, ?, ?, '', '', null, '', '', 'active', 'local', null,
                      ?, 1, null, '{}', ?, ?, null, ?)
            """,
            (
                "AUT-p71", "AIT-p71", "SES-p71", "fingerprint-p71",
                timestamp, timestamp, json.dumps(automation_payload),
            ),
        )
        conn.execute(
            """
            insert into agent_automation_versions(
                automation_version_id, automation_id, version, status,
                intent_json, compiled_json, artifact_json, created_at, payload_json
            ) values (?, ?, 1, 'active', '{}', '{}', '{}', ?, ?)
            """,
            (
                "AVR-p71", "AUT-p71", timestamp,
                json.dumps(
                    {
                        "intent": intent_payload,
                        "compiled": {"rule": "close>0"},
                        "artifact": {"kind": "watch"},
                    }
                ),
            ),
        )
        conn.execute(
            """
            insert into agent_automation_executions(
                execution_id, automation_id, automation_version, run_id, stage,
                status, input_json, output_json, meaningful_change,
                decision_changed, created_at, started_at, completed_at,
                payload_json
            ) values (?, ?, 1, null, '', 'completed', '{}', '{}', null, 1, '', ?, ?, ?)
            """,
            (
                "AEX-p71", "AUT-p71", timestamp, timestamp,
                json.dumps(
                    {
                        "stage": "evaluation",
                        "input": {"price": 100},
                        "output": {"action": "hold"},
                    }
                ),
            ),
        )
        conn.execute(
            """
            insert into agent_notification_deliveries(
                delivery_id, automation_id, execution_id, user_id, channel,
                dedup_key, status, provider_receipt_json, error, created_at,
                updated_at, delivered_at, acknowledged_at, snoozed_until,
                expires_at, payload_json
            ) values (?, ?, ?, '', 'desktop', ?, 'delivered', '{}', null, ?, ?,
                      null, null, null, null, ?)
            """,
            (
                "NTF-p71", "AUT-p71", "AEX-p71", "dedup-p71", timestamp, timestamp,
                json.dumps(
                    {
                        "user_id": "legacy-user",
                        "provider_receipt": {"id": "receipt-p71"},
                        "delivered_at": timestamp,
                    }
                ),
            ),
        )
        conn.commit()


def test_fresh_database_satisfies_complete_p71_contract(tmp_path):
    path = tmp_path / "fresh.sqlite"
    SQLiteStore(db_path=path)

    with sqlite3.connect(path) as conn:
        report = verify_p71_schema(conn)

    assert report == {
        "valid": True,
        "table_count": len(P71_REQUIRED_COLUMNS),
        "index_count": len(P71_REQUIRED_INDEXES),
    }


def test_current_version_database_with_incomplete_p71_schema_fails_closed(tmp_path):
    path = tmp_path / "incomplete.sqlite"
    SQLiteStore(db_path=path)
    with sqlite3.connect(path) as conn:
        conn.execute("drop index idx_agent_notifications_dedup")
        conn.commit()

    with pytest.raises(P71SchemaContractError, match="idx_agent_notifications_dedup"):
        SQLiteStore(db_path=path)


def test_v37_p71_rows_survive_upgrade_and_restart_through_domain_stores(tmp_path):
    path = tmp_path / "p71-v37.sqlite"
    _build_v37_database(path)
    _seed_v37_p71_rows(path)

    SQLiteStore(db_path=path)
    SQLiteStore(db_path=path)  # a second startup proves the upgraded schema is idempotent

    objectives = SQLiteObjectiveStore(path).list_for_session("SES-p71")
    artifact = SQLiteArtifactVersionStore(path).get_version("ART-p71", 1)
    automations = AutomationStore(path)
    automation = automations.get_automation("AUT-p71")
    version = automations.latest_version("AUT-p71")
    executions = automations.list_executions("AUT-p71")

    assert [item.objective for item in objectives] == ["preserve durable state"]
    assert artifact is not None
    assert artifact.reason == "initial"
    assert artifact.content == {"signal": "hold"}
    assert automation is not None
    assert automation["user_id"] == "legacy-user"
    assert automation["goal"] == "watch TSMC"
    assert automation["symbol"] == "2330.TW"
    assert automation["state"] == "active"
    assert automation["backend_reference"] == "legacy-ref"
    assert version is not None
    assert version["compiled"] == {"rule": "close>0"}
    assert version["artifact"] == {"kind": "watch"}
    assert executions[0]["stage"] == "evaluation"
    assert executions[0]["input_json"] == json.dumps({"price": 100}, separators=(",", ":"))

    with sqlite3.connect(path) as conn:
        assert conn.execute("pragma user_version").fetchone()[0] == migrations.LATEST_SCHEMA_VERSION
        assert conn.execute("pragma quick_check").fetchone()[0] == "ok"
        assert conn.execute("pragma foreign_key_check").fetchall() == []
        notification = conn.execute(
            """
            select user_id, provider_receipt_json, delivered_at
              from agent_notification_deliveries
             where delivery_id='NTF-p71'
            """
        ).fetchone()
        assert notification == ("legacy-user", '{"id":"receipt-p71"}', "2026-09-08T12:00:00+00:00")

    receipt_path = next(tmp_path.glob(".p71-v37.sqlite.migration-v*.receipt.json"))
    receipt = load_migration_safety_receipt(receipt_path)
    assert receipt.status == "completed"
    assert receipt.from_version == 37
    assert receipt.to_version == migrations.LATEST_SCHEMA_VERSION
