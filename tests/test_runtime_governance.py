from __future__ import annotations

from dataclasses import replace

from open_stock_ai.config.settings import load_settings
from open_stock_ai.governance import build_runtime_governance
from open_stock_ai.main import build_engine


def test_runtime_governance_uses_one_durable_authority_across_restart(tmp_path) -> None:
    database = tmp_path / "runtime" / "open_stock_ai.sqlite"

    first = build_runtime_governance(database)
    assert first.durable is True
    assert first.store is not None
    assert first.artifact_rollback.store is first.store
    assert first.change_management.store is first.store
    assert first.retention.store is first.retention_store
    assert first.retention_maintenance.ledger is first.retention
    assert first.retention_store is not first.store
    assert first.retention_store.path == first.store.path
    assert first.status()["authorities"]["retention"]["store_wired"] is True
    assert first.status()["authorities"]["retention"]["maintenance"]["durable"] is True

    restarted = build_runtime_governance(database)
    assert restarted.durable is True
    assert restarted.store is not first.store
    assert restarted.artifact_rollback.store is restarted.store
    assert restarted.change_management.store is restarted.store
    assert restarted.retention.store is restarted.retention_store
    assert restarted.retention_maintenance.ledger is restarted.retention
    assert restarted.retention_store.path == restarted.store.path


def test_build_engine_exposes_runtime_governance_on_the_same_sqlite_path(tmp_path) -> None:
    database = tmp_path / "engine" / "open_stock_ai.sqlite"
    settings = replace(load_settings(), sqlite_path=str(database))

    engine = build_engine(settings=settings)

    assert engine.governance is not None
    assert engine.governance.durable is True
    assert engine.governance.database_path == str(database.resolve())
    assert engine.pipeline.trade_store is not None
    assert engine.pipeline.trade_store.store.path == database
    assert engine.pipeline.trade_store.paper_oms is not None
    assert engine.pipeline.trade_store.paper_oms.change_management is engine.governance.change_management


def test_build_engine_can_fail_closed_for_paper_orders_without_an_approved_change(tmp_path) -> None:
    database = tmp_path / "engine-change-binding" / "open_stock_ai.sqlite"
    settings = replace(
        load_settings(),
        sqlite_path=str(database),
        require_change_binding=True,
        active_change_id=None,
    )

    engine = build_engine(settings=settings)

    assert engine.pipeline.execution.active_change_id is None
    assert engine.pipeline.trade_store is not None
    assert engine.pipeline.trade_store.paper_oms is not None
    assert engine.pipeline.trade_store.paper_oms.require_change_binding is True
    assert engine.pipeline.trade_store.paper_oms.change_management is engine.governance.change_management


def test_memory_runtime_is_explicitly_non_durable() -> None:
    runtime = build_runtime_governance(":memory:")

    assert runtime.durable is False
    assert runtime.store is None
    assert runtime.retention_store is None
    assert runtime.artifact_rollback.store is None
    assert runtime.change_management.store is None
    assert runtime.retention.store is None
    assert runtime.retention_maintenance.ledger is runtime.retention
