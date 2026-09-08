from datetime import datetime, timezone

import pytest

from open_stock_ai.governance.content_retention import (
    ContentAddressedRetentionLedger,
    RetentionError,
    RetentionMaintenanceScheduler,
    verify_retention_receipt,
)
from open_stock_ai.governance.durable_store import SQLiteRetentionStore


_T0 = datetime(2026, 8, 26, 4, 45, tzinfo=timezone.utc)


def test_signal_projection_is_bounded_but_critical_artifact_is_retained() -> None:
    ledger = ContentAddressedRetentionLedger(max_signal_entries=500)
    for index in range(501):
        ledger.append(f"signal-{index}", {"value": index}, critical=False, occurred_at=f"2026-08-26T04:{index // 60:02d}:{index % 60:02d}+00:00")
    ledger.append("order-receipt-1", {"order_id": "O-1", "fill_id": "F-1"}, critical=True, kind="execution_receipt")

    receipt = ledger.prune_signals(now=_T0)

    assert ledger.retained_counts() == {"total": 501, "signals": 500, "critical": 1}
    assert ledger.get("signal-0") is None
    assert ledger.get("signal-1") is not None
    assert ledger.get("order-receipt-1")["payload"] == {"order_id": "O-1", "fill_id": "F-1"}
    assert receipt.verify() is True
    assert verify_retention_receipt(receipt.as_dict()) is True


def test_retention_records_are_immutable_and_tamper_receipt_fails() -> None:
    ledger = ContentAddressedRetentionLedger(max_signal_entries=1)
    ledger.append("signal-1", {"value": 1}, critical=False, occurred_at="2026-08-26T04:45:00+00:00")
    with pytest.raises(RetentionError, match="immutable_conflict"):
        ledger.append("signal-1", {"value": 2}, critical=False, occurred_at="2026-08-26T04:45:00+00:00")
    payload = ledger.prune_signals(now=_T0).as_dict()
    payload["operation"] = "fake"
    assert verify_retention_receipt(payload) is False


def test_sqlite_retention_survives_restart_and_protects_critical_content(tmp_path) -> None:
    database = tmp_path / "retention.sqlite"
    ledger = ContentAddressedRetentionLedger(
        max_signal_entries=2,
        store=SQLiteRetentionStore(database),
    )
    for index in range(3):
        ledger.append(
            f"signal-{index}",
            {"value": index},
            critical=False,
            occurred_at=f"2026-08-26T04:4{index}:00+00:00",
        )
    ledger.append(
        "execution-receipt-1",
        {"order_id": "O-1", "fill_id": "F-1"},
        critical=True,
        kind="execution_receipt",
        occurred_at="2026-08-26T04:50:00+00:00",
    )

    receipt = ledger.prune_signals(now=_T0)
    restarted = ContentAddressedRetentionLedger(
        max_signal_entries=2,
        store=SQLiteRetentionStore(database),
    )

    assert restarted.retained_counts() == {"total": 3, "signals": 2, "critical": 1}
    assert restarted.get("signal-0") is None
    assert restarted.get("signal-1") is not None
    assert restarted.get("execution-receipt-1")["payload"] == {"order_id": "O-1", "fill_id": "F-1"}
    assert [item.as_dict() for item in restarted.receipts] == [receipt.as_dict()]

    with pytest.raises(Exception, match="critical retained content"):
        with restarted.store._connect() as connection:
            connection.execute(
                "delete from governed_retained_content where record_id='execution-receipt-1'"
            )


def test_durable_maintenance_scheduler_is_due_once_and_survives_restart(tmp_path) -> None:
    database = tmp_path / "retention-maintenance.sqlite"
    first_ledger = ContentAddressedRetentionLedger(
        max_signal_entries=2,
        store=SQLiteRetentionStore(database),
    )
    for index in range(3):
        first_ledger.append(
            f"signal-{index}",
            {"value": index},
            critical=False,
            occurred_at=f"2026-08-26T04:4{index}:00+00:00",
        )
    first_ledger.append(
        "execution-receipt-1",
        {"order_id": "O-1"},
        critical=True,
        kind="execution_receipt",
        occurred_at="2026-08-26T04:50:00+00:00",
    )
    scheduler = RetentionMaintenanceScheduler(first_ledger, interval_seconds=3600)

    receipt = scheduler.run_due(now=_T0)

    assert receipt is not None
    assert first_ledger.retained_counts() == {"total": 3, "signals": 2, "critical": 1}
    assert scheduler.run_due(now=datetime(2026, 8, 26, 5, 44, tzinfo=timezone.utc)) is None

    restarted = ContentAddressedRetentionLedger(
        max_signal_entries=2,
        store=SQLiteRetentionStore(database),
    )
    restarted_scheduler = RetentionMaintenanceScheduler(restarted, interval_seconds=3600)
    status = restarted_scheduler.status(now=datetime(2026, 8, 26, 5, 44, tzinfo=timezone.utc))

    assert status["durable"] is True
    assert status["last_completed_at"] == _T0.isoformat()
    assert status["due"] is False
    assert restarted_scheduler.run_due(now=datetime(2026, 8, 26, 5, 45, tzinfo=timezone.utc)) is not None
    assert restarted.get("execution-receipt-1") is not None
