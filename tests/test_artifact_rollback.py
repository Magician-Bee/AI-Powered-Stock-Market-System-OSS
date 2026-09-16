from datetime import datetime, timezone

import pytest

from open_stock_ai.governance.artifact_rollback import (
    ApprovedArtifactRollbackRegistry,
    ArtifactRollbackError,
    verify_rollback_receipt,
)
from open_stock_ai.governance.durable_store import SQLiteGovernanceStore


_T0 = datetime(2026, 8, 26, 4, 45, tzinfo=timezone.utc)


def test_rollback_restores_previous_approved_artifact_and_emits_receipt() -> None:
    registry = ApprovedArtifactRollbackRegistry()
    first = registry.approve("strategy-v1", "a" * 64, approved_by="owner", approved_at="2026-08-26T04:45:00+00:00")
    second = registry.approve("strategy-v2", "b" * 64, approved_by="owner", approved_at="2026-08-26T04:46:00+00:00")
    registry.activate(first["artifact_id"])
    registry.activate(second["artifact_id"])

    receipt = registry.rollback(reason="shadow_drawdown_breach", now=_T0)

    assert registry.current_artifact_id == "strategy-v1"
    assert receipt.from_artifact_id == "strategy-v2"
    assert receipt.to_artifact_id == "strategy-v1"
    assert receipt.verify() is True
    assert verify_rollback_receipt(receipt.as_dict()) is True


def test_rollback_requires_human_approval_and_previous_artifact() -> None:
    registry = ApprovedArtifactRollbackRegistry()
    with pytest.raises(ArtifactRollbackError, match="human_approval"):
        registry.approve("strategy-v1", "a" * 64, approved_by="agent", approved_at="2026-08-26T04:45:00+00:00")
    registry.approve("strategy-v1", "a" * 64, approved_by="owner", approved_at="2026-08-26T04:45:00+00:00")
    registry.activate("strategy-v1")
    with pytest.raises(ArtifactRollbackError, match="previous_approved"):
        registry.rollback(reason="provider_failure")


def test_tampered_rollback_receipt_is_rejected() -> None:
    registry = ApprovedArtifactRollbackRegistry()
    registry.approve("strategy-v1", "a" * 64, approved_by="owner", approved_at="2026-08-26T04:45:00+00:00")
    registry.approve("strategy-v2", "b" * 64, approved_by="owner", approved_at="2026-08-26T04:46:00+00:00")
    registry.activate("strategy-v1")
    registry.activate("strategy-v2")
    payload = registry.rollback(reason="operator_requested", now=_T0).as_dict()
    payload["to_artifact_id"] = "fake"
    assert verify_rollback_receipt(payload) is False


def test_sqlite_registry_survives_restart_and_keeps_receipts_immutable(tmp_path) -> None:
    store = SQLiteGovernanceStore(tmp_path / "governance.sqlite")
    registry = ApprovedArtifactRollbackRegistry(store)
    registry.approve("strategy-v1", "a" * 64, approved_by="owner", approved_at="2026-08-26T04:45:00+00:00")
    registry.approve("strategy-v2", "b" * 64, approved_by="owner", approved_at="2026-08-26T04:46:00+00:00")
    registry.activate("strategy-v1")
    registry.activate("strategy-v2")

    receipt = registry.rollback(reason="operator_requested", now=_T0)
    restarted = ApprovedArtifactRollbackRegistry(SQLiteGovernanceStore(tmp_path / "governance.sqlite"))

    assert restarted.current_artifact_id == "strategy-v1"
    assert len(restarted.receipts) == 1
    assert restarted.receipts[0].as_dict() == receipt.as_dict()
    assert verify_rollback_receipt(restarted.receipts[0].as_dict()) is True

    with pytest.raises(Exception, match="immutable"):
        with restarted.store._connect() as connection:
            connection.execute(
                "update governed_artifact_rollback_receipts set payload_json='{}'"
            )


def test_scoped_rollback_never_restores_an_artifact_from_another_deployment_lane() -> None:
    registry = ApprovedArtifactRollbackRegistry()
    for artifact_id, digest, metadata in (
        ("strategy-v1", "a" * 64, {"strategy_id": "rule-v1"}),
        ("strategy-v2", "b" * 64, {"strategy_id": "rule-v2"}),
        ("model-v1", "c" * 64, {"framework": "qlib"}),
        ("model-v2", "d" * 64, {"framework": "qlib"}),
    ):
        registry.approve(
            artifact_id,
            digest,
            approved_by="owner",
            approved_at="2026-08-26T04:45:00+00:00",
            metadata=metadata,
        )
        registry.activate(artifact_id)

    receipt = registry.rollback(
        artifact_scope="strategy",
        reason="strategy_shadow_drawdown_breach",
        approved_by="owner",
        now=_T0,
    )

    assert receipt.from_artifact_id == "strategy-v2"
    assert receipt.to_artifact_id == "strategy-v1"
    assert registry.current_artifact_id_for("strategy") == "strategy-v1"
    assert registry.current_artifact_id_for("model") == "model-v2"
    assert registry.status()["scopes"]["strategy"]["activation_count"] == 3
    with pytest.raises(ArtifactRollbackError, match="human_approval_required_for_artifact_rollback"):
        registry.rollback(artifact_scope="model", reason="not_human", approved_by="agent")
    with pytest.raises(ArtifactRollbackError, match="scope_invalid"):
        registry.rollback(artifact_scope="all", reason="invalid", approved_by="owner")
