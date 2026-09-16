from __future__ import annotations

import sqlite3

import pytest

from open_stock_ai.governance import CHAOS_SCENARIOS, ChaosRecoveryCatalog
from open_stock_ai.agent_runtime.chaos_recovery_runtime import ChaosRecoveryRuntime


def test_chaos_catalog_requires_all_recovery_invariants_and_verifies_receipts() -> None:
    catalog = ChaosRecoveryCatalog(seed=20260826, clock=lambda: "2026-08-26T00:00:00+00:00")
    recoveries = {
        scenario: {
            "invariants": {
                "durable_state_recovered": True,
                "new_orders_blocked_until_safe": True,
                "operator_receipt_written": True,
            },
            "recovery_id": f"REC-{scenario}",
        }
        for scenario in CHAOS_SCENARIOS
    }

    receipts = catalog.run_catalog(recoveries)

    assert [receipt.scenario for receipt in receipts] == list(CHAOS_SCENARIOS)
    assert catalog.complete() is True
    assert all(receipt.verify() for receipt in receipts)


def test_chaos_catalog_fails_closed_for_missing_disk_full_recovery() -> None:
    catalog = ChaosRecoveryCatalog(seed=7)
    catalog.run_catalog({scenario: {"invariants": {"durable_state_recovered": True}} for scenario in CHAOS_SCENARIOS if scenario != "disk_full"})

    assert catalog.complete() is False
    disk = [receipt for receipt in catalog.receipts if receipt.scenario == "disk_full"][0]
    assert disk.status == "blocked"
    assert disk.invariants == {}


def test_durable_chaos_runtime_keeps_verified_recovery_evidence_across_restart(tmp_path) -> None:
    database = tmp_path / "agent-runtime.sqlite"
    recoveries = {
        scenario: {
            "invariants": {
                "durable_state_recovered": True,
                "new_orders_blocked_until_safe": True,
                "operator_receipt_written": True,
            },
            "recovery_id": f"REC-{scenario}",
        }
        for scenario in CHAOS_SCENARIOS
    }
    runtime = ChaosRecoveryRuntime(database)

    receipts = runtime.record_catalog(recoveries, seed=20260829, campaign_id="local-safe-r010")

    assert len(receipts) == 4
    assert all(receipt.verify() for receipt in receipts)
    first = runtime.dashboard()
    second = ChaosRecoveryRuntime(database).dashboard()
    assert first["status"] == second["status"] == "recovered"
    assert first["triggered"] is False
    assert first["scenario_count"] == 4
    assert {item["scenario"] for item in second["receipts"]} == set(CHAOS_SCENARIOS)

    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.DatabaseError):
        connection.execute("update chaos_recovery_receipts set status='recovered'")


def test_durable_chaos_runtime_blocks_incomplete_campaign_without_executing_faults(tmp_path) -> None:
    runtime = ChaosRecoveryRuntime(tmp_path / "agent-runtime.sqlite")
    runtime.record_catalog(
        {
            "process_kill": {
                "invariants": {
                    "durable_state_recovered": True,
                    "new_orders_blocked_until_safe": True,
                    "operator_receipt_written": True,
                }
            },
        },
        seed=7,
        campaign_id="incomplete-catalog",
    )

    dashboard = runtime.dashboard()

    assert dashboard["status"] == "blocked"
    assert dashboard["triggered"] is True
    assert "missing_invariant:durable_state_recovered" in str(dashboard["reason"])
    assert "recovery_blocked" in str(dashboard["reason"])
    assert dashboard["scenario_count"] == 4
