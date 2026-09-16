from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from open_stock_ai.governance.content_retention import (
    ContentAddressedRetentionLedger,
    RetentionError,
)
from open_stock_ai.governance.durable_store import SQLiteRetentionStore
from open_stock_ai.governance.hosted_security_audit_gate import (
    verify_hosted_security_audit_gate_receipt,
)
from open_stock_ai.governance.retention_archive import build_critical_retention_archive
from open_stock_ai.governance.security_audit import (
    SecurityTradingAuditTrail,
    reconstruct_order_audit,
    verify_order_audit_reconstruction,
)
from scripts.run_hosted_retention_archive_gate import _create, _verify


COMMIT = "8" * 40


def _digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_security_audit_is_durable_immutable_and_rejects_sensitive_fields(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.sqlite"
    ledger = ContentAddressedRetentionLedger(store=SQLiteRetentionStore(database))
    audit = SecurityTradingAuditTrail(ledger)
    recorded = audit.record(
        event_id="SEC-1",
        category="authorization",
        actor_id="host-policy",
        subject_type="broker_order",
        subject_id="BOI-1",
        decision="allow",
        outcome="allowed",
        reason="paper policy passed",
        evidence_sha256="a" * 64,
        details={"environment": "sandbox"},
        occurred_at="2026-09-08T00:00:00+00:00",
    )

    reopened = ContentAddressedRetentionLedger(store=SQLiteRetentionStore(database))
    assert reopened.get(recorded["record_id"]) == recorded
    with pytest.raises(RetentionError, match="immutable_conflict"):
        audit.record(
            event_id="SEC-1",
            category="authorization",
            actor_id="host-policy",
            subject_type="broker_order",
            subject_id="BOI-1",
            decision="deny",
            outcome="blocked",
            reason="changed",
            evidence_sha256="a" * 64,
            occurred_at="2026-09-08T00:00:00+00:00",
        )
    with pytest.raises(RetentionError, match="sensitive_field"):
        audit.record(
            event_id="SEC-2",
            category="authorization",
            actor_id="host-policy",
            subject_type="broker_order",
            subject_id="BOI-2",
            decision="deny",
            outcome="blocked",
            reason="credential rejected",
            evidence_sha256="b" * 64,
            details={"token": "must-not-leak"},
        )


def test_order_reconstruction_is_hash_bound_and_rejects_missing_audit(
    tmp_path: Path,
) -> None:
    store = SQLiteRetentionStore(tmp_path / "source.sqlite")
    ledger = ContentAddressedRetentionLedger(store=store)
    intent = {
        "intent_id": "BOI-1",
        "broker_id": "fubon",
        "account_alias": "paper",
        "environment": "sandbox",
    }
    for index, state in enumerate(("CREATED", "SUBMITTING", "ACKNOWLEDGED", "FILLED")):
        payload = {
            "schema_version": "stock_ai.broker_oms_execution_state.v1",
            "intent": intent,
            "state": state,
            "receipt": None,
            "broker_order_id": None if index < 2 else "BROKER-1",
            "filled_quantity": "1" if state == "FILLED" else "0",
            "remaining_quantity": "0" if state == "FILLED" else "1",
            "parent_intent_id": None,
            "replacement_intent_id": None,
            "report_ids": ["REPORT-1"] if state == "FILLED" else [],
            "action_receipts": [],
            "human_approval_receipt": None,
            "updated_at": f"2026-09-08T00:0{index}:00+00:00",
        }
        ledger.append(
            f"oms-{index}",
            payload,
            critical=True,
            kind="execution_oms_state",
            occurred_at=payload["updated_at"],
        )
    SecurityTradingAuditTrail(ledger).record(
        event_id="SEC-ORDER-1",
        category="trading_authorization",
        actor_id="host-paper-policy",
        subject_type="broker_order",
        subject_id="BOI-1",
        decision="allow_paper_order",
        outcome="allowed",
        reason="paper policy passed",
        evidence_sha256=_digest(intent),
        occurred_at="2026-09-08T00:00:30+00:00",
    )
    archive = build_critical_retention_archive(
        store,
        source_authority="runtime:test",
        created_at="2026-09-08T00:10:00+00:00",
    )
    receipt = reconstruct_order_audit(
        archive, "BOI-1", reconstructed_at="2026-09-08T00:11:00+00:00"
    )

    assert receipt["complete"] is True
    assert [item["state"] for item in receipt["state_timeline"]] == [
        "CREATED",
        "SUBMITTING",
        "ACKNOWLEDGED",
        "FILLED",
    ]
    assert verify_order_audit_reconstruction(receipt, archive) is True
    altered = copy.deepcopy(receipt)
    altered["final_state"] = "CANCELLED"
    assert verify_order_audit_reconstruction(altered, archive) is False


def test_hosted_round_trip_proves_off_host_sink_and_order_reconstruction(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact"
    verification = tmp_path / "verification"
    evidence = _create(tmp_path / "source.sqlite", artifact, COMMIT, "901")
    _verify(artifact, verification, COMMIT, "901")

    reconstruction = json.loads(
        (verification / "order-audit-reconstruction-receipt.json").read_text()
    )
    security = json.loads(
        (verification / "hosted-security-audit-gate-receipt.json").read_text()
    )
    retention = json.loads(
        (verification / "critical-retention-gate-receipt.json").read_text()
    )
    assert evidence["reconstructed_intent_id"] == "BOI-HOSTED-AUDIT-1"
    assert reconstruction["complete"] is True
    assert reconstruction["final_state"] == "FILLED"
    assert any(
        item["category"] == "pretrade_risk"
        and item["decision"] == "approve_pretrade"
        for item in reconstruction["security_decisions"]
    )
    assert security["immutable_off_host_sink_verified"] is True
    assert security["order_reconstruction_verified"] is True
    assert verify_hosted_security_audit_gate_receipt(
        security, retention["archive"], retention["restore"]
    ) is True
