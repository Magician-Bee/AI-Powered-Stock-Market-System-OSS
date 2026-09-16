import pytest

from open_stock_ai.governance.change_management import (
    ChangeManagementError,
    ChangeManagementRegistry,
    verify_order_version_binding,
)
from open_stock_ai.governance.durable_store import SQLiteGovernanceStore


def _pin(registry: ChangeManagementRegistry):
    return registry.pin_change(
        "change-1",
        code_sha256="a" * 64,
        model_sha256="b" * 64,
        data_sha256="c" * 64,
        risk_policy_sha256="d" * 64,
        approved_by="owner",
        approved_at="2026-08-26T04:45:00+00:00",
    )


def test_order_is_bound_to_all_immutable_change_versions() -> None:
    registry = ChangeManagementRegistry()
    change = _pin(registry)
    assert change.verify() is True
    binding = registry.bind_order("order-1", change_id=change.change_id, bound_at="2026-08-26T04:46:00+00:00")
    assert binding["change_sha256"] == change.change_sha256
    assert binding["code_sha256"] == "a" * 64
    assert binding["model_sha256"] == "b" * 64
    assert binding["data_sha256"] == "c" * 64
    assert binding["risk_policy_sha256"] == "d" * 64
    assert verify_order_version_binding(binding) is True


def test_order_binding_retry_returns_the_original_immutable_receipt() -> None:
    registry = ChangeManagementRegistry()
    change = _pin(registry)
    first = registry.bind_order(
        "order-1",
        change_id=change.change_id,
        bound_at="2026-08-26T04:46:00+00:00",
    )
    retried = registry.bind_order(
        "order-1",
        change_id=change.change_id,
        bound_at="2026-08-26T04:47:00+00:00",
    )

    assert retried == first
    assert retried["bound_at"] == "2026-08-26T04:46:00+00:00"


def test_change_management_rejects_model_approval_and_rewrites() -> None:
    registry = ChangeManagementRegistry()
    with pytest.raises(ChangeManagementError, match="human_approval"):
        registry.pin_change(
            "change-1", code_sha256="a" * 64, model_sha256="b" * 64,
            data_sha256="c" * 64, risk_policy_sha256="d" * 64,
            approved_by="model", approved_at="2026-08-26T04:45:00+00:00",
        )
    _pin(registry)
    with pytest.raises(ChangeManagementError, match="immutable_conflict"):
        registry.pin_change(
            "change-1", code_sha256="e" * 64, model_sha256="b" * 64,
            data_sha256="c" * 64, risk_policy_sha256="d" * 64,
            approved_by="owner", approved_at="2026-08-26T04:47:00+00:00",
        )


def test_tampered_order_binding_is_rejected() -> None:
    registry = ChangeManagementRegistry()
    change = _pin(registry)
    binding = registry.bind_order("order-1", change_id=change.change_id)
    binding["risk_policy_sha256"] = "e" * 64
    assert verify_order_version_binding(binding) is False


def test_sqlite_change_set_and_order_binding_survive_restart(tmp_path) -> None:
    store = SQLiteGovernanceStore(tmp_path / "governance.sqlite")
    registry = ChangeManagementRegistry(store)
    change = _pin(registry)
    binding = registry.bind_order("order-1", change_id=change.change_id, bound_at="2026-08-26T04:46:00+00:00")

    restarted = ChangeManagementRegistry(SQLiteGovernanceStore(tmp_path / "governance.sqlite"))
    assert restarted._changes["change-1"].as_dict() == change.as_dict()
    assert restarted._orders["order-1"] == binding
    assert verify_order_version_binding(restarted._orders["order-1"]) is True
    assert restarted.bind_order("order-1", change_id=change.change_id) == binding

    with pytest.raises(Exception, match="immutable"):
        with restarted.store._connect() as connection:
            connection.execute("delete from governed_change_sets where change_id='change-1'")
