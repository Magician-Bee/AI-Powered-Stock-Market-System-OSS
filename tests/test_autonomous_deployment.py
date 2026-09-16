"""Deployment correlation receipts are Host observations, never M1 evidence alone."""
from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from open_stock_ai.execution.trading_plan import content_hash
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai import autonomous_deployment as deployment


def source_tree(tmp_path):
    for directory in ("src/open_stock_ai", "src/stock_ai", "config"):
        (tmp_path / directory).mkdir(parents=True)
    source = tmp_path / "src/stock_ai/runner.py"
    source.write_text("version = 1\n")
    (tmp_path / "config/public.yaml").write_text("mode: paper\n")
    (tmp_path / "config/.env").write_text("API_KEY=never-retain-this\n")
    return source


def test_snapshot_hashes_source_and_public_config_without_contents_or_paths(tmp_path, monkeypatch):
    source_tree(tmp_path)
    monkeypatch.setenv("STOCK_AI_BUILD_COMMIT", "declared-commit")
    monkeypatch.setenv("STOCK_AI_INSTANCE_ID", "isolated-instance")
    snapshot = deployment._source_snapshot(tmp_path, capture_stage="isolated_test")
    assert set(snapshot["files"]) == {"src/stock_ai/runner.py", "config/public.yaml"}
    assert snapshot["declared_build_commit"] == "declared-commit"
    assert snapshot["errors"] == []
    assert snapshot["files_sha256"] == content_hash(snapshot["files"])
    assert snapshot["receipt_sha256"] == content_hash({k: v for k, v in snapshot.items() if k != "receipt_sha256"})
    assert "not_loaded_code_attestation" in snapshot["assurance"]
    assert str(tmp_path) not in json.dumps(snapshot)
    assert "never-retain-this" not in json.dumps(snapshot)


def test_process_snapshot_is_not_rewritten_when_sources_or_returned_copy_change(tmp_path, monkeypatch):
    source = source_tree(tmp_path)
    # Redirect only this module's source root; no installed files are modified.
    monkeypatch.setattr(deployment, "__file__", str(tmp_path / "src/stock_ai/autonomous_deployment.py"))
    monkeypatch.setattr(deployment, "_SOURCE_SNAPSHOT", None)
    original = deployment.initialize_source_snapshot(capture_stage="asgi_lifespan_start")
    original_hash = original["receipt_sha256"]
    original["files"].clear()
    source.write_text("version = 2\n")
    second = deployment.initialize_source_snapshot()
    assert second["receipt_sha256"] == original_hash
    assert second["capture_stage"] == "asgi_lifespan_start"
    assert second["files"]["src/stock_ai/runner.py"] == hashlib.sha256(b"version = 1\n").hexdigest()
    assert deployment._source_snapshot(tmp_path, capture_stage="later_process")["files_sha256"] != second["files_sha256"]


def test_missing_source_scope_is_retained_as_an_explicit_gap(tmp_path):
    snapshot = deployment._source_snapshot(tmp_path, capture_stage="isolated_test")
    assert snapshot["files"] == {}
    assert len(snapshot["errors"]) == 3
    assert all(row["error"] == "missing_source_directory" for row in snapshot["errors"])


def test_same_account_in_different_databases_has_distinct_deployment_binding(tmp_path, monkeypatch):
    source_tree(tmp_path)
    snapshot = deployment._source_snapshot(tmp_path, capture_stage="isolated_test")
    monkeypatch.setattr(deployment, "_SOURCE_SNAPSHOT", snapshot)
    retained = {}
    def campaign(path):
        def retain(kind, payload):
            evidence_id = "AE-" + content_hash({"account_id": "same-account", "kind": kind, "payload": payload})
            retained[evidence_id] = deepcopy(payload)
            return evidence_id
        return SimpleNamespace(broker=SimpleNamespace(account_id="same-account", mode="paper"),
                               plans=SimpleNamespace(store=SimpleNamespace(path=path)), _retain=retain)
    first = deployment.bind_campaign_execution_context(campaign(tmp_path / "first.db"))
    second = deployment.bind_campaign_execution_context(campaign(tmp_path / "second.db"))
    assert first["deployment_receipt_id"] != second["deployment_receipt_id"]
    assert first["deployment_receipt"]["source_snapshot_id"] == second["deployment_receipt"]["source_snapshot_id"]
    assert len(retained) == 3
    assert first == deployment.bind_campaign_execution_context(campaign(tmp_path / "first.db"))
    assert not (tmp_path / "first.db").exists()
    assert str(tmp_path) not in json.dumps(first)


def test_campaign_wiring_retains_and_binds_actual_account_database(tmp_path, monkeypatch):
    from stock_ai import autonomous_trading_service as wiring
    source_tree(tmp_path)
    monkeypatch.setattr(deployment, "_SOURCE_SNAPSHOT", deployment._source_snapshot(tmp_path, capture_stage="isolated_test"))
    store = SQLiteStore(tmp_path / "trading.sqlite")
    engine = SimpleNamespace(pipeline=SimpleNamespace(trade_store=SimpleNamespace(store=store), risk=RiskEngine()))
    monkeypatch.setattr(wiring, "_SERVICE", None)
    monkeypatch.setattr(wiring, "get_runtime_engine", lambda: engine)
    monkeypatch.setattr(wiring, "default_taiwan_market_calendar", lambda: SimpleNamespace())
    campaign = wiring.get_autonomous_campaign()
    context = campaign.execution_context
    assert campaign.broker.broker.host_execution_context == context
    receipt = campaign._evidence(context["deployment_receipt_id"], "deployment_context")
    assert receipt == context["deployment_receipt"]
    assert receipt["account_id"] == "autonomous-paper-v1"
    assert receipt["database_bindings"]["trading_database_path_sha256"] == deployment.database_path_sha256(store.path)
    snapshot = campaign._evidence(receipt["source_snapshot_id"], "deployment_source_snapshot")
    assert receipt["source_snapshot_sha256"] == snapshot["receipt_sha256"]
    assert campaign.status()["enabled"] is False
    assert wiring.get_autonomous_campaign() is campaign
    with store._connect() as conn:
        assert conn.execute("select count(*) from autonomous_evidence").fetchone()[0] == 2
        assert conn.execute("select count(*) from paper_fills").fetchone()[0] == 0


def test_campaign_initialization_retries_after_transient_retention_failure(tmp_path, monkeypatch):
    from stock_ai import autonomous_trading_service as wiring
    source_tree(tmp_path)
    monkeypatch.setattr(deployment, "_SOURCE_SNAPSHOT", deployment._source_snapshot(tmp_path, capture_stage="isolated_test"))
    store = SQLiteStore(tmp_path / "trading.sqlite")
    engine = SimpleNamespace(pipeline=SimpleNamespace(trade_store=SimpleNamespace(store=store), risk=RiskEngine()))
    monkeypatch.setattr(wiring, "_SERVICE", None)
    monkeypatch.setattr(wiring, "get_runtime_engine", lambda: engine)
    monkeypatch.setattr(wiring, "default_taiwan_market_calendar", lambda: SimpleNamespace())
    original = deployment.bind_campaign_execution_context
    calls = []
    def fail_once(campaign):
        calls.append(campaign)
        if len(calls) == 1:
            raise sqlite3.OperationalError("isolated transient database failure")
        return original(campaign)
    monkeypatch.setattr(deployment, "bind_campaign_execution_context", fail_once)
    with pytest.raises(sqlite3.OperationalError, match="transient"):
        wiring.get_autonomous_campaign()
    assert wiring._SERVICE is None
    campaign = wiring.get_autonomous_campaign()
    assert len(calls) == 2 and campaign is calls[1]
    assert campaign.execution_context["deployment_receipt_id"]
    assert campaign.forward_monitor is not None
    assert campaign.broker.broker.host_execution_context == campaign.execution_context
    assert campaign.status()["enabled"] is False
