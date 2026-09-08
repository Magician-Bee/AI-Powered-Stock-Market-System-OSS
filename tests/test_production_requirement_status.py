from __future__ import annotations

from pathlib import Path

from open_stock_ai.governance import capability_status, production_requirement_status


ROOT = Path(__file__).parents[1]


def test_p0_production_requirement_ledger_is_complete_unique_and_evidence_backed() -> None:
    status = production_requirement_status()
    requirements = status["requirements"]

    assert status["schema_version"] == "stock_ai.production_requirement_status.v1"
    assert len(requirements) == 46
    assert len({item["id"] for item in requirements}) == 46
    assert {item["id"] for item in requirements} == {
        *(f"Q-{index:03d}" for index in range(1, 10)),
        *(f"D-{index:03d}" for index in range(1, 5)),
        "M-001",
        "M-002",
        "M-006",
        "S-001",
        "S-002",
        "S-003",
        *(f"E-{index:03d}" for index in range(1, 12)),
        *(f"A-{index:03d}" for index in range(1, 5)),
        "N-001",
        "N-002",
        "N-007",
        "SEC-001",
        "SEC-002",
        "R-001",
        "T-001",
        "T-003",
        "T-004",
        "T-005",
        "GOV-001",
        "GOV-002",
    }
    assert len(status["ledger_sha256"]) == 64
    assert all(item["evidence"] for item in requirements if item["status"] != "unverified")
    assert all(not item["evidence"] for item in requirements if item["status"] == "unverified")
    assert all(not item["blockers"] for item in requirements if item["status"] == "complete")
    assert set(status["gates"]["master_p0"]["requirement_ids"]) == {item["id"] for item in requirements}
    assert status["master_p0_complete_count"] == 37
    assert status["master_p0_total"] == 46
    master_by_id = {item["id"]: item for item in requirements}
    assert master_by_id["SEC-001"]["status"] == "partial"
    assert master_by_id["SEC-001"]["blockers"] == [
        "credential_rotation_and_history_rewrite_are_not_evidenced",
    ]
    assert master_by_id["T-001"]["status"] == "partial"
    assert master_by_id["T-001"]["blockers"] == [
        "github_plan_does_not_allow_private_repository_branch_protection",
        "github_branch_protection_and_required_core_safety_statuses_are_not_enabled",
    ]
    branch_protection_audit = (ROOT / "docs" / "security" / "github-branch-protection.md").read_text(
        encoding="utf-8"
    )
    assert "Recorded remote audit — 2026-08-28" in branch_protection_audit
    assert "HTTP 403" in branch_protection_audit
    assert "GitHub Pro" in branch_protection_audit
    assert master_by_id["Q-008"]["status"] == "complete"
    assert master_by_id["Q-008"]["blockers"] == []
    assert master_by_id["D-004"]["status"] == "complete"
    assert master_by_id["D-004"]["blockers"] == []
    release_requirements = status["release_requirements"]
    assert len(release_requirements) == 78
    assert {item["id"] for item in release_requirements} == {
        *(f"Q-{index:03d}" for index in range(10, 17)),
        *(f"D-{index:03d}" for index in range(5, 13)),
        "M-003", "M-004", "M-005", *(f"M-{index:03d}" for index in range(7, 13)),
        *(f"S-{index:03d}" for index in range(4, 13)),
        *(f"E-{index:03d}" for index in range(12, 19)),
        *(f"A-{index:03d}" for index in range(5, 12)),
        *(f"N-{index:03d}" for index in range(3, 7)),
        *(f"SEC-{index:03d}" for index in range(3, 11)),
        *(f"R-{index:03d}" for index in range(2, 12)),
        "T-002", *(f"T-{index:03d}" for index in range(6, 10)),
        *(f"GOV-{index:03d}" for index in range(3, 7)),
    }
    by_id = {item["id"]: item for item in release_requirements}
    for requirement_id in ("Q-010", "Q-011", "Q-012", "Q-013", "Q-014", "Q-015", "Q-016"):
        assert by_id[requirement_id]["status"] == "complete"
        assert by_id[requirement_id]["blockers"] == []
    for requirement_id in ("M-004", "M-005", "M-007", "M-008", "M-009", "M-010", "M-011", "M-012"):
        assert by_id[requirement_id]["status"] == "complete"
        assert by_id[requirement_id]["blockers"] == []
    assert by_id["M-003"]["status"] == "complete"
    assert by_id["M-003"]["blockers"] == []
    assert by_id["E-014"]["status"] == "partial"
    assert by_id["E-014"]["blockers"] == [
        "available_quantity_queue_and_participation_inputs_are_explicit_simulator_inputs_not_a_historical_order_book_or_exchange_queue_replay",
        "fill_probability_market_impact_and_participation_caps_are_explicit_scenario_inputs_not_empirically_calibrated_against_broker_or_exchange_execution_data",
    ]
    assert by_id["E-015"]["status"] == "complete"
    assert by_id["E-015"]["blockers"] == []
    assert by_id["E-016"]["status"] == "partial"
    assert by_id["E-016"]["blockers"] == [
        "no_account_owner_verified_broker_or_twse_locate_rate_recall_and_fee_feed_is_connected",
        "paper_borrow_receipts_are_explicit_simulation_inputs_and_not_historical_pit_borrow_availability",
    ]
    assert by_id["S-004"]["status"] == "partial"
    assert by_id["S-004"]["blockers"] == [
        "versioned_point_in_time_risk_inputs_are_not_yet_materialized_for_live_portfolio_sizing",
    ]
    assert by_id["S-005"]["status"] == "partial"
    assert by_id["S-005"]["blockers"] == [
        "production_point_in_time_covariance_factor_and_account_risk_context_is_not_yet_materialized",
    ]
    assert by_id["D-005"]["status"] == "complete"
    assert by_id["D-005"]["blockers"] == []
    assert by_id["D-006"]["status"] == "complete"
    assert by_id["D-006"]["blockers"] == []
    assert by_id["D-007"]["status"] == "complete"
    assert by_id["D-007"]["blockers"] == []
    assert by_id["D-008"]["status"] == "complete"
    assert by_id["D-008"]["blockers"] == []
    assert by_id["D-009"]["status"] == "complete"
    assert by_id["D-009"]["blockers"] == []
    assert by_id["D-010"]["status"] == "partial"
    assert by_id["D-010"]["blockers"] == [
        "historical_level_1_level_5_or_depth_snapshot_corpus_not_ingested",
        "depth_impact_calibration_not_empirically_validated",
    ]
    assert by_id["D-011"]["status"] == "partial"
    assert by_id["D-011"]["blockers"] == [
        "borrow_availability_rate_recall_and_fee_history_not_ingested",
    ]
    assert by_id["D-012"]["status"] == "complete"
    assert by_id["D-012"]["blockers"] == []
    assert by_id["T-002"]["status"] == "complete"
    assert by_id["T-002"]["blockers"] == []
    assert by_id["N-003"]["status"] == "complete"
    assert by_id["N-003"]["blockers"] == []
    assert by_id["N-004"]["status"] == "complete"
    assert by_id["N-004"]["blockers"] == []
    assert by_id["N-005"]["status"] == "complete"
    assert by_id["N-005"]["blockers"] == []
    assert by_id["N-006"]["status"] == "complete"
    assert by_id["N-006"]["blockers"] == []
    assert by_id["A-005"]["status"] == "partial"
    assert by_id["A-005"]["blockers"] == [
        "live_provider_runtime_receipt_and_independent_hostile_corpus_review_signoff_are_not_yet_recorded",
    ]
    assert by_id["A-006"]["status"] == "partial"
    assert by_id["A-006"]["blockers"] == [
        "reviewed_malicious_memory_corpus_and_live_provider_to_memory_receipts_are_not_yet_recorded",
    ]
    assert by_id["A-007"]["status"] == "partial"
    assert by_id["A-007"]["blockers"] == [
        "live_provider_side_effect_receipts_and_owner_verified_external_execution_evidence_are_not_yet_recorded",
    ]
    assert by_id["A-008"]["status"] == "partial"
    assert by_id["A-008"]["blockers"] == [
        "reviewed_provider_cost_schedule_and_real_provider_runaway_chaos_receipts_are_not_yet_recorded",
    ]
    assert by_id["A-009"]["status"] == "partial"
    assert by_id["A-009"]["blockers"] == [
        "real_primary_outage_and_alternate_provider_runtime_receipts_are_not_yet_recorded",
        "provider_fallback_matrix_has_not_been_accepted_against_each_production_provider",
    ]
    assert by_id["A-010"]["status"] == "complete"
    assert by_id["A-010"]["blockers"] == []
    assert by_id["A-011"]["status"] == "partial"
    assert by_id["A-011"]["blockers"] == [
        "production_clean_machine_restore_receipt_and_off_host_archive_retention_are_not_yet_recorded",
    ]
    assert by_id["R-002"]["status"] == "complete"
    assert by_id["R-002"]["blockers"] == []
    assert by_id["R-003"]["status"] == "complete"
    assert by_id["R-003"]["blockers"] == []
    assert by_id["R-005"]["status"] == "partial"
    assert by_id["R-005"]["blockers"] == [
        "production_slo_targets_require_owner_acceptance",
    ]
    assert by_id["R-007"]["status"] == "complete"
    assert by_id["R-007"]["blockers"] == []
    assert by_id["R-008"]["status"] == "complete"
    assert by_id["R-008"]["blockers"] == []
    assert by_id["R-004"]["status"] == "complete"
    assert by_id["R-004"]["blockers"] == []
    assert by_id["R-006"]["status"] == "partial"
    assert by_id["R-006"]["blockers"] == [
        "production_alert_sink_on_call_routing_and_chaos_recovery_receipts_are_not_yet_configured",
    ]
    assert by_id["R-009"]["status"] == "complete"
    assert by_id["R-009"]["blockers"] == []
    assert by_id["R-010"]["status"] == "complete"
    assert by_id["R-010"]["blockers"] == []
    assert by_id["R-011"]["status"] == "complete"
    assert by_id["R-011"]["blockers"] == []
    assert by_id["E-018"]["status"] == "partial"
    assert by_id["E-018"]["blockers"] == [
        "production_broker_scheduler_and_provider_snapshot_receipts_are_not_yet_wired_to_every_supported_account",
    ]
    assert by_id["GOV-003"]["status"] == "partial"
    assert by_id["GOV-003"]["blockers"] == [
        "staging_acceptance_receipts_are_not_yet_recorded_for_each_capability_promotion",
    ]
    assert by_id["GOV-004"]["blockers"] == []
    assert by_id["T-006"]["status"] == "complete"
    assert by_id["T-006"]["blockers"] == []
    assert by_id["T-007"]["status"] == "partial"
    assert by_id["T-007"]["blockers"] == [
        "official_sandbox_provider_receipts_are_not_yet_recorded_for_each_supported_broker",
    ]
    assert by_id["T-008"]["status"] == "partial"
    assert by_id["T-008"]["blockers"] == [
        "model_runtime_baseline_and_candidate_samples_are_deferred_while_model_execution_is_disabled",
    ]
    assert by_id["T-009"]["status"] == "complete"
    assert by_id["T-009"]["blockers"] == []
    assert by_id["GOV-004"]["status"] == "complete"
    assert by_id["GOV-005"]["status"] == "partial"
    assert by_id["GOV-005"]["blockers"] == [
        "off_host_archive_and_all_execution_critical_writers_are_not_yet_connected",
    ]
    assert by_id["GOV-006"]["status"] == "partial"
    assert by_id["GOV-006"]["blockers"] == [
        "owner_approved_active_change_sets_are_not_yet_provisioned_for_default_paper_runtime_execution",
    ]
    assert all(
        item["status"] == "unverified"
        for item in release_requirements
        if item["id"] not in {
            "Q-010", "Q-011", "Q-012", "Q-013", "Q-014", "Q-015", "Q-016", "M-003", "M-004", "M-005", "M-007", "M-008", "M-009", "M-010", "M-011", "M-012", "S-004", "S-005", "E-012", "E-013", "E-014", "E-015", "E-016", "E-017",
                "S-006", "S-007", "S-008", "S-009", "S-010", "S-011", "S-012", "D-005", "D-006", "D-007", "D-008", "D-009", "D-010", "D-011", "D-012", "N-003", "N-004", "N-005", "N-006", "SEC-003", "SEC-004", "SEC-005", "SEC-006", "SEC-007", "SEC-008", "SEC-009", "SEC-010", "T-002", "A-005", "A-006", "A-007", "A-008", "A-009", "A-010", "A-011", "R-002", "R-003", "R-004", "R-005", "R-006", "R-007", "R-008", "R-009", "R-010", "R-011", "T-006", "T-007", "T-008", "GOV-003", "GOV-004", "GOV-005", "GOV-006", "T-009", "E-018",
        }
    )
    for requirement_id in ("S-006", "S-007", "S-008", "S-009", "S-010"):
        assert by_id[requirement_id]["status"] == "partial"
        assert by_id[requirement_id]["evidence"]
        assert by_id[requirement_id]["blockers"]
    for requirement_id in ("S-011", "S-012"):
        assert by_id[requirement_id]["status"] == "partial"
        assert by_id[requirement_id]["evidence"]
        assert by_id[requirement_id]["blockers"]
    for requirement_id in ("SEC-003", "SEC-004", "SEC-005", "SEC-006"):
        assert by_id[requirement_id]["status"] == "complete"
        assert by_id[requirement_id]["blockers"] == []
    assert status["full_release_complete_count"] == 83
    assert status["full_release_total"] == 124


def test_quant_execution_evidence_gate_is_derived_and_fail_closed() -> None:
    status = production_requirement_status()
    master_gate = status["gates"]["master_p0"]
    gate = status["gates"]["quant_foundation"]
    research_gate = status["gates"]["research_execution_evidence"]
    execution_gate = status["gates"]["sizing_and_execution_truth"]

    assert master_gate["passed"] is False
    assert len(master_gate["incomplete_requirement_ids"]) == 9
    assert status["gates"]["full_release"]["passed"] is False
    assert len(status["gates"]["full_release"]["incomplete_requirement_ids"]) == 41
    assert gate["passed"] is False
    assert gate["incomplete_requirement_ids"] == ["Q-003"]
    assert research_gate["passed"] is False
    assert execution_gate["passed"] is True
    assert status["research_execution_evidence_enabled"] is False
    assert capability_status()["research_execution_evidence_enabled"] is False


def test_release_test_policy_does_not_hide_obsolete_failures() -> None:
    conftest = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")

    assert "pytest_collection_modifyitems" not in conftest
    assert "pytest.mark.xfail" not in conftest
