#!/usr/bin/env python3
"""Export critical retention data off host and restore it on a fresh runner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.execution.paper_oms import PaperOMS  # noqa: E402
from open_stock_ai.governance.content_retention import (  # noqa: E402
    ContentAddressedRetentionLedger,
)
from open_stock_ai.governance.durable_store import SQLiteRetentionStore  # noqa: E402
from open_stock_ai.governance.hosted_retention_gate import (  # noqa: E402
    build_hosted_retention_gate_receipt,
    verify_hosted_retention_gate_receipt,
)
from open_stock_ai.governance.hosted_security_audit_gate import (  # noqa: E402
    build_hosted_security_audit_gate_receipt,
    verify_hosted_security_audit_gate_receipt,
)
from open_stock_ai.governance.retention_archive import (  # noqa: E402
    build_critical_retention_archive,
    restore_critical_retention_archive,
    verify_critical_retention_archive,
)
from open_stock_ai.governance.security_audit import (  # noqa: E402
    SecurityTradingAuditTrail,
    reconstruct_order_audit,
    verify_order_audit_reconstruction,
)
from open_stock_ai.risk.kill_switch import DurableRiskControlStore  # noqa: E402
from open_stock_ai.risk.settlement_pnl_feed import (  # noqa: E402
    SettlementPnLFeed,
    build_source_receipt,
)
from open_stock_ai.storage.sqlite_store import SQLiteStore  # noqa: E402
from stock_ai.brokers import (  # noqa: E402
    BrokerAccountReconciliationReceiptStore,
    BrokerAccountReconciliationService,
    BrokerAccountSnapshot,
    BrokerOMSStore,
    BrokerOrderIntent,
    BrokerOrderManagementGateway,
    BrokerOrderReceipt,
    BrokerOrderReport,
    BrokerOrderState,
    BrokerRawEvent,
    BrokerReconciliationService,
    HostAccountLedger,
    ReconciliationReceiptStore,
)
from stock_ai.brokers.risk import BrokerRiskDecision  # noqa: E402


def _encoded(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    encoded = _encoded(payload)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return hashlib.sha256(encoded).hexdigest()


def _exercise_connected_execution_writers(
    source_database: Path,
    ledger: ContentAddressedRetentionLedger,
) -> None:
    """Create deterministic paper/account mutations through production writers."""

    store = SQLiteStore(source_database)
    oms = PaperOMS(
        store=store,
        account_id="hosted-retention",
        initial_cash=1_000_000.0,
        retention_ledger=ledger,
    )
    buy = oms.submit_and_fill(
        {
            "order_id": "HOSTED-CORPORATE-ACTION-BUY",
            "symbol": "2330.TW",
            "market": "TW",
            "action": "buy",
            "entry_price": 100,
            "position_size_pct": 1,
            "risk_approved": True,
        }
    )
    if not buy.get("filled"):
        raise RuntimeError("hosted corporate-action fixture did not fill")
    oms.import_corporate_actions(
        [
            {
                "source_event_id": "HOSTED-CA-1",
                "symbol": "2330.TW",
                "market": "TW",
                "effective_date": "2026-09-07",
                "action_type": "cash_dividend",
                "status": "confirmed",
                "official_verified": True,
                "source_id": "twse_official_contract_fixture",
                "source_url": "https://www.twse.com.tw/zh/announcement/index.html",
                "acquired_at": "2026-09-07T00:00:00+00:00",
                "source_payload": {"scope": "hosted_contract_drill"},
                "cash_per_share": 2,
                "reference_price_before": 100,
                "reference_price_after": 98,
            }
        ]
    )
    corporate = oms.sync_corporate_actions(as_of="2026-09-30")
    if corporate.get("applied_count") != 1:
        raise RuntimeError("hosted corporate-action writer did not apply exactly once")

    short = oms.submit_and_fill(
        {
            "order_id": "HOSTED-BORROW-FEE",
            "symbol": "2317.TW",
            "market": "TW",
            "action": "short_sell",
            "entry_price": 100,
            "position_size_pct": 1,
            "risk_approved": True,
            "borrow_receipt": {
                "receipt_id": "HOSTED-LOCATE-1",
                "symbol": "2317.TW",
                "source": "hosted_contract_fixture",
                "verified_at": "2026-09-07T00:00:00+00:00",
                "expires_at": "2026-10-01T00:00:00+00:00",
                "available_quantity": 1000,
                "annual_fee_bps": 100,
            },
            "market_context": {"source_timestamp": "2026-09-07T00:00:00+00:00"},
        }
    )
    if not short.get("filled"):
        raise RuntimeError("hosted short-sale fixture did not fill")
    borrow_fee = oms.accrue_borrow_fees(as_of="2026-09-09T00:00:00+00:00")
    if not borrow_fee.get("events"):
        raise RuntimeError("hosted borrow-fee writer did not change cash")

    settlement_order = oms.submit_and_fill(
        {
            "order_id": "HOSTED-T2-SETTLEMENT",
            "symbol": "2454.TW",
            "market": "TW",
            "action": "buy",
            "entry_price": 100,
            "position_size_pct": 1,
            "risk_approved": True,
            "market_context": {
                "source_timestamp": "2026-09-07T00:00:00+00:00",
                "settlement_rules_enforced": True,
            },
        }
    )
    if not settlement_order.get("filled"):
        raise RuntimeError("hosted settlement fixture did not fill")
    settlement = oms.settle_due(as_of="2026-09-30T00:00:00+00:00")
    if settlement.get("settled_count") != 1:
        raise RuntimeError("hosted settlement writer did not settle exactly once")

    risk_control = DurableRiskControlStore(source_database)
    pnl_feed = SettlementPnLFeed(
        risk_control,
        retention_ledger=ledger,
        require_critical_retention=True,
    )
    pnl_source = build_source_receipt(
        {"source": "hosted_contract_fixture", "source_event_id": "HOSTED-PNL-1"}
    )
    pnl_feed.ingest(
        {
            "event_id": "HOSTED-PNL-1",
            "settled_at": "2026-09-09T00:00:00+00:00",
            "realized_pnl_pct": -0.1,
            "scopes": {"account": "hosted-retention"},
            "source_receipt": pnl_source,
        }
    )

    observed_at = datetime(2026, 9, 9, tzinfo=timezone.utc)
    broker_account = BrokerAccountSnapshot(
        broker_id="fubon",
        account_id_masked="hosted-***-1",
        account_type="cash",
        currency="TWD",
        available_cash="1000",
        settlement_due="0",
        positions=[],
        open_orders=[],
        fills=[],
        settlements=[],
        as_of=observed_at,
    )
    host_account = HostAccountLedger(
        broker_id="fubon",
        account_id_masked="hosted-***-1",
        account_type="cash",
        currency="TWD",
        available_cash="1000",
        settlement_due="0",
        positions=[],
        open_orders=[],
        fills=[],
        settlements=[],
        as_of=observed_at,
    )
    account_store = BrokerAccountReconciliationReceiptStore(
        source_database,
        retention_ledger=ledger,
        require_critical_retention=True,
    )
    account_service = BrokerAccountReconciliationService(
        account_store,
        lambda *_: (broker_account, host_account, "a" * 64),
        clock=lambda: observed_at,
    )
    account_service.run_startup([("fubon", "hosted-paper")])

    class _HostedOMS:
        @staticmethod
        def reconcile_snapshot(snapshot: object, *, raw_event: object) -> dict[str, Any]:
            del snapshot, raw_event
            return {
                "schema_version": "stock_ai.broker_order_reconciliation_result.v1",
                "trading_allowed": True,
                "applied_report_count": 0,
                "marked_unknown_intent_ids": [],
            }

    reconciliation_store = ReconciliationReceiptStore(
        source_database,
        retention_ledger=ledger,
        require_critical_retention=True,
    )
    BrokerReconciliationService(
        _HostedOMS(),
        lambda *_: (object(), object()),
        receipt_store=reconciliation_store,
        clock=lambda: "2026-09-09T00:00:00+00:00",
    ).run_startup([("fubon", "hosted-paper")])


def _exercise_broker_order_audit(
    source_database: Path,
    ledger: ContentAddressedRetentionLedger,
) -> str:
    """Write a complete sandbox order lifecycle through the production OMS."""

    intent = BrokerOrderIntent(
        intent_id="BOI-HOSTED-AUDIT-1",
        broker_id="fubon",
        account_alias="hosted-paper",
        instrument_id="TWSE:2330",
        side="buy",
        quantity=Decimal("1"),
        price_type="limit",
        limit_price=Decimal("1000"),
        time_in_force="ROD",
        session="regular_lot",
        order_purpose="hosted_security_audit_drill",
        environment="sandbox",
        user_approved=True,
        risk_approval_id="RISK-HOSTED-AUDIT-1",
        idempotency_key="hosted-security-audit-order-0001",
    )

    class _HostedSandboxGateway:
        @staticmethod
        async def place_order(submitted: BrokerOrderIntent) -> BrokerOrderReceipt:
            return BrokerOrderReceipt(
                intent_id=submitted.intent_id,
                broker_id=submitted.broker_id,
                broker_order_id="FUBON-HOSTED-AUDIT-1",
                submitted_at=datetime.now(timezone.utc),
                status=BrokerOrderState.ACKNOWLEDGED,
                accepted_quantity=submitted.quantity,
                raw_receipt_hash="b" * 64,
            )

    oms = BrokerOrderManagementGateway(
        gateway=_HostedSandboxGateway(),
        store=BrokerOMSStore(source_database),
        retention_ledger=ledger,
        require_critical_retention=True,
    )
    oms.clear_kill_switch_for_sandbox()
    oms.register(intent)
    intent_sha256 = hashlib.sha256(
        json.dumps(
            intent.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    SecurityTradingAuditTrail(ledger).record(
        event_id="SEC-HOSTED-PAPER-ORDER-1",
        category="trading_authorization",
        actor_id="host-paper-policy",
        subject_type="broker_order",
        subject_id=intent.intent_id,
        decision="allow_paper_order",
        outcome="allowed",
        reason="sandbox order passed the host paper-trading boundary",
        evidence_sha256=intent_sha256,
        details={"environment": "sandbox", "live_transport_used": False},
        occurred_at="2026-09-07T00:13:00+00:00",
    )
    risk = BrokerRiskDecision(
        risk_approval_id=intent.risk_approval_id,
        approved=True,
        checked_at=datetime.now(timezone.utc),
        checks={"paper_mode": True, "kill_switch_clear": True},
        reasons=[],
    )
    submitted = asyncio.run(oms.submit(intent, risk_decision=risk))
    if submitted.state != BrokerOrderState.ACKNOWLEDGED:
        raise RuntimeError("hosted audit order was not acknowledged")
    raw = BrokerRawEvent(
        raw_event_id="BRE-HOSTED-AUDIT-FILL-1",
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"fixture": "hosted_contract_drill", "status": "FILLED"},
    )
    filled = oms.apply_order_report(
        BrokerOrderReport(
            report_id="BOR-HOSTED-AUDIT-FILL-1",
            intent_id=intent.intent_id,
            broker_id="fubon",
            account_alias="hosted-paper",
            broker_order_id="FUBON-HOSTED-AUDIT-1",
            event_at=datetime.now(timezone.utc),
            received_at=datetime.now(timezone.utc),
            status=BrokerOrderState.FILLED,
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("0"),
            last_fill_price=Decimal("1000"),
            fee=Decimal("1"),
            tax=Decimal("0"),
            raw_payload_hash=str(raw.payload_hash),
        ),
        raw_event=raw,
    )
    if filled.state != BrokerOrderState.FILLED:
        raise RuntimeError("hosted audit order did not reach its terminal state")
    return intent.intent_id


def _create(source_database: Path, output: Path, commit_sha: str, run_id: str) -> dict[str, Any]:
    if source_database.exists() or output.exists():
        raise SystemExit("retention export requires fresh source and output paths")
    output.mkdir(parents=True)
    store = SQLiteRetentionStore(source_database)
    ledger = ContentAddressedRetentionLedger(max_signal_entries=2, store=store)
    for index in range(4):
        ledger.append(
            f"bounded-signal-{index}",
            {"symbol": "2330.TW", "score": index},
            critical=False,
            occurred_at=f"2026-09-07T00:0{index}:00+00:00",
        )
    ledger.append(
        "paper-oms-execution-PAPER-1",
        {
            "schema_version": "stock_ai.paper_oms_execution_snapshot.v1",
            "order_id": "PAPER-1",
            "source_of_truth": "paper_oms",
            "order": {"symbol": "2330.TW", "quantity": "1", "mode": "paper"},
            "oms": {"status": "filled", "fill_id": "FILL-1"},
        },
        critical=True,
        kind="execution_oms_state",
        occurred_at="2026-09-07T00:10:00+00:00",
    )
    ledger.append(
        "paper-broker-execution-PAPER-1-filled",
        {
            "schema_version": "stock_ai.paper_broker_execution_snapshot.v1",
            "order_id": "PAPER-1",
            "source_of_truth": "paper_broker_simulator",
            "result": {"order": {"status": "filled"}, "fill": {"fill_id": "FILL-1"}},
        },
        critical=True,
        kind="execution_broker_state",
        occurred_at="2026-09-07T00:11:00+00:00",
    )
    ledger.append(
        "oms-state-live-disabled-example",
        {
            "schema_version": "stock_ai.broker_oms_execution_state.v1",
            "intent": {"intent_id": "INTENT-1", "mode": "sandbox"},
            "state": "rejected",
            "receipt": None,
            "broker_order_id": None,
            "filled_quantity": "0",
            "remaining_quantity": "1",
            "parent_intent_id": None,
            "replacement_intent_id": None,
            "report_ids": [],
            "action_receipts": [],
            "human_approval_receipt": None,
            "updated_at": "2026-09-07T00:12:00+00:00",
        },
        critical=True,
        kind="execution_oms_state",
        occurred_at="2026-09-07T00:12:00+00:00",
    )
    _exercise_connected_execution_writers(source_database, ledger)
    reconstructed_intent_id = _exercise_broker_order_audit(source_database, ledger)
    ledger.prune_signals()
    archive = build_critical_retention_archive(
        store,
        source_authority=f"github-actions:{commit_sha}:{run_id}",
        created_at="2026-09-07T00:20:00+00:00",
    )
    archive_path = output / "critical-retention-archive.json"
    archive_file_sha256 = _write_json(archive_path, archive)
    evidence = {
        "commit_sha": commit_sha,
        "run_id": run_id,
        "export_job": "export-critical-retention",
        "restore_job": "clean-runner-restore",
        "off_host_artifact": f"stock-ai-critical-retention-{commit_sha}",
        "artifact_retention_days": 90,
        "production_years_satisfied": False,
        "scope": "hosted_drill_only",
        "archive_file_sha256": archive_file_sha256,
        "archive": archive,
        "source_signal_count_after_prune": ledger.retained_counts()["signals"],
        "reconstructed_intent_id": reconstructed_intent_id,
    }
    _write_json(output / "critical-retention-export-evidence.json", evidence)
    return evidence


def _verify(input_dir: Path, output: Path, commit_sha: str, run_id: str) -> dict[str, Any]:
    if output.exists():
        raise SystemExit("clean retention restore output must not exist")
    output.mkdir(parents=True)
    archive_path = input_dir / "critical-retention-archive.json"
    evidence = json.loads(
        (input_dir / "critical-retention-export-evidence.json").read_text(encoding="utf-8")
    )
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    archive_file_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if evidence.get("commit_sha") != commit_sha or evidence.get("run_id") != run_id:
        raise SystemExit("critical retention artifact identity mismatch")
    if archive != evidence.get("archive") or not verify_critical_retention_archive(archive):
        raise SystemExit("critical retention archive verification failed")
    if archive_file_sha256 != evidence.get("archive_file_sha256"):
        raise SystemExit("critical retention archive file hash mismatch")
    restore = restore_critical_retention_archive(
        archive,
        output / "restored-critical-retention.sqlite",
        restored_at="2026-09-07T00:30:00+00:00",
    )
    gate = build_hosted_retention_gate_receipt(
        {**evidence, "restore": restore}, commit_sha=commit_sha, run_id=run_id
    )
    if not gate["passed"] or not verify_hosted_retention_gate_receipt(gate):
        raise SystemExit(f"hosted critical retention gate failed: {gate['blockers']}")
    order_reconstruction = reconstruct_order_audit(
        archive,
        str(evidence.get("reconstructed_intent_id") or ""),
        reconstructed_at="2026-09-07T00:31:00+00:00",
    )
    if not order_reconstruction["complete"] or not verify_order_audit_reconstruction(
        order_reconstruction, archive
    ):
        raise SystemExit("hosted order reconstruction failed")
    security_gate = build_hosted_security_audit_gate_receipt(
        {**evidence, "restore": restore},
        order_reconstruction=order_reconstruction,
        commit_sha=commit_sha,
        run_id=run_id,
    )
    if not security_gate["passed"] or not verify_hosted_security_audit_gate_receipt(
        security_gate, archive, restore
    ):
        raise SystemExit(f"hosted security audit gate failed: {security_gate['blockers']}")
    _write_json(output / "critical-retention-restore-receipt.json", restore)
    _write_json(output / "critical-retention-gate-receipt.json", gate)
    _write_json(output / "order-audit-reconstruction-receipt.json", order_reconstruction)
    _write_json(output / "hosted-security-audit-gate-receipt.json", security_gate)
    return gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("create", "verify"))
    parser.add_argument("--source-database")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--allow-retention-archive-drill", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("hosted retention archive gate must run on a GitHub Actions Linux runner")
    if not args.allow_retention_archive_drill:
        raise SystemExit("refusing retention archive drill without explicit opt-in")
    output = Path(args.output_dir)
    if args.mode == "create":
        if not args.source_database:
            raise SystemExit("create mode requires --source-database")
        _create(Path(args.source_database), output, args.commit_sha, args.run_id)
        return 0
    if not args.input_dir:
        raise SystemExit("verify mode requires --input-dir")
    print(json.dumps(_verify(Path(args.input_dir), output, args.commit_sha, args.run_id), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
