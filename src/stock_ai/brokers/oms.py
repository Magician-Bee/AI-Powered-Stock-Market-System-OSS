from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from open_stock_ai.governance.change_management import ChangeManagementError, ChangeManagementRegistry
from open_stock_ai.governance.content_retention import (
    ContentAddressedRetentionLedger,
)

from .contracts import (
    BrokerLiveTradingDisabled,
    BrokerOrderIntent,
    BrokerOrderReconciliationSnapshot,
    BrokerOrderReport,
    BrokerOrderReceipt,
    BrokerOrderState,
    BrokerRawEvent,
    BrokerReconciliationRequired,
)
from .event_bus import CanonicalFinancialEventBus
from .gateway import UnifiedBrokerGateway
from .risk import BrokerRiskDecision
from .order_store import BrokerOMSRecoveryReceipt, BrokerOMSStore, DurableOrderEntry
from .order_approval import BrokerHumanApprovalAuthority, BrokerHumanApprovalReceipt
from .live_activation import (
    BrokerRestrictedLiveActivationAuthority,
    BrokerRestrictedLiveActivationReceipt,
)


_TERMINAL_STATES = {
    BrokerOrderState.FILLED,
    BrokerOrderState.CANCELLED,
    BrokerOrderState.REJECTED,
}


@dataclass
class OrderLedgerEntry:
    intent: BrokerOrderIntent
    state: BrokerOrderState
    receipt: BrokerOrderReceipt | None = None
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    parent_intent_id: str | None = None
    replacement_intent_id: str | None = None
    report_ids: list[str] = field(default_factory=list)
    action_receipts: list[BrokerOrderReceipt] = field(default_factory=list)
    human_approval_receipt: BrokerHumanApprovalReceipt | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class BrokerOrderManagementGateway:
    """Host OMS with idempotency and unknown-state retry protection."""

    def __init__(
        self,
        gateway: UnifiedBrokerGateway | None = None,
        *,
        event_bus: CanonicalFinancialEventBus | None = None,
        store: BrokerOMSStore | None = None,
        human_approval_authority: BrokerHumanApprovalAuthority | None = None,
        live_activation_authority: BrokerRestrictedLiveActivationAuthority | None = None,
        change_management: ChangeManagementRegistry | None = None,
        require_change_binding: bool = False,
        retention_ledger: ContentAddressedRetentionLedger | None = None,
        require_critical_retention: bool = False,
    ) -> None:
        self.gateway = gateway or UnifiedBrokerGateway()
        self.event_bus = event_bus or CanonicalFinancialEventBus()
        self._by_intent: dict[str, OrderLedgerEntry] = {}
        self._intent_by_idempotency: dict[str, str] = {}
        self._intent_by_broker_order: dict[tuple[str, str], str] = {}
        self._intent_by_report: dict[str, str] = {}
        self.kill_switch_enabled = True
        self.store = store
        self.latest_recovery_receipt: BrokerOMSRecoveryReceipt | None = None
        self.human_approval_authority = human_approval_authority
        self.live_activation_authority = live_activation_authority
        if require_change_binding and change_management is None:
            raise ValueError("change management registry is required when change binding is mandatory")
        if require_change_binding and store is None:
            raise ValueError("change binding requires a durable Broker OMS store")
        governance_store = getattr(change_management, "store", None) if change_management is not None else None
        if require_change_binding and governance_store is None:
            raise ValueError("change binding requires a durable governance store")
        if change_management is not None and store is not None and governance_store is not None:
            oms_path = getattr(store, "path", None)
            governance_path = getattr(governance_store, "path", None)
            if (
                oms_path is None
                or governance_path is None
                or Path(oms_path).resolve() != Path(governance_path).resolve()
            ):
                raise ValueError("change binding must use the same durable database as the OMS")
        self.change_management = change_management
        self.require_change_binding = bool(require_change_binding)
        if require_critical_retention and retention_ledger is None:
            raise ValueError(
                "retention ledger is required when critical OMS retention is mandatory"
            )
        if require_critical_retention and store is None:
            raise ValueError(
                "critical OMS retention requires a durable Broker OMS store"
            )
        if (
            require_critical_retention
            and retention_ledger is not None
            and retention_ledger.store is None
        ):
            raise ValueError(
                "critical OMS retention requires a durable retention ledger store"
            )
        if require_critical_retention and retention_ledger is not None:
            oms_path = getattr(store, "path", None)
            retention_path = getattr(retention_ledger.store, "path", None)
            if oms_path is not None and retention_path is not None and oms_path != retention_path:
                raise ValueError(
                    "critical OMS retention must use the same durable database as the OMS"
                )
        self.retention_ledger = retention_ledger
        self.require_critical_retention = bool(require_critical_retention)
        if self.store is not None:
            self._restore_durable_entries()

    def register(self, intent: BrokerOrderIntent) -> OrderLedgerEntry:
        existing = self._load_by_idempotency_key(intent.idempotency_key)
        if existing is not None:
            if existing.intent.model_dump() != intent.model_dump():
                raise ValueError("idempotency key is already bound to different order arguments")
            return existing
        existing = self._load_by_intent_id(intent.intent_id)
        if existing is not None:
            if existing.intent.model_dump() != intent.model_dump():
                raise ValueError("intent ID is already bound to different order arguments")
            return existing
        if self.require_change_binding and not str(intent.change_id or "").strip():
            raise PermissionError("order requires a Host-approved change-set binding")
        if intent.change_id:
            if self.change_management is None:
                raise PermissionError("order change-set binding requires a governance registry")
            if self.store is not None and getattr(self.change_management, "store", None) is None:
                raise PermissionError("order change-set binding requires a durable governance registry")
            try:
                self.change_management.bind_order(intent.intent_id, change_id=intent.change_id)
            except ChangeManagementError as exc:
                raise PermissionError(f"order change-set binding rejected: {exc}") from exc
        if intent.environment == "live" and self.store is None:
            raise BrokerReconciliationRequired(
                "live order intents require a durable Broker OMS store before registration"
            )
        entry = OrderLedgerEntry(
            intent=intent,
            state=BrokerOrderState.CREATED,
            remaining_quantity=intent.quantity,
        )
        self._by_intent[intent.intent_id] = entry
        self._intent_by_idempotency[intent.idempotency_key] = intent.intent_id
        self._persist(entry)
        return entry

    def mark_unknown(self, intent_id: str) -> OrderLedgerEntry:
        entry = self._load_by_intent_id(intent_id)
        if entry is None:
            raise KeyError(intent_id)
        entry.state = BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED
        entry.updated_at = datetime.now(timezone.utc)
        self._persist(entry)
        return entry

    def register_replacement(
        self,
        original_intent_id: str,
        replacement: BrokerOrderIntent,
    ) -> OrderLedgerEntry:
        original = self._load_by_intent_id(original_intent_id)
        if original is None:
            raise KeyError(original_intent_id)
        if original.state not in {
            BrokerOrderState.ACKNOWLEDGED,
            BrokerOrderState.PARTIALLY_FILLED,
        }:
            raise ValueError("only an acknowledged open order can be replaced")
        if (
            replacement.broker_id != original.intent.broker_id
            or replacement.account_alias != original.intent.account_alias
            or replacement.instrument_id != original.intent.instrument_id
        ):
            raise ValueError(
                "replacement orders must keep broker, account and instrument isolation"
            )
        entry = self.register(replacement)
        entry.parent_intent_id = original_intent_id
        original.replacement_intent_id = replacement.intent_id
        original.updated_at = datetime.now(timezone.utc)
        self._persist(original)
        self._persist(entry)
        return entry

    async def submit(
        self,
        intent: BrokerOrderIntent,
        *,
        risk_decision: BrokerRiskDecision | None = None,
        human_approval_receipt: BrokerHumanApprovalReceipt | None = None,
        live_activation_receipt: BrokerRestrictedLiveActivationReceipt | None = None,
    ) -> OrderLedgerEntry:
        entry = self.register(intent)
        if entry.state == BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED:
            raise BrokerReconciliationRequired(
                "order state is unknown; reconcile with the broker before any retry"
            )
        if entry.receipt is not None or entry.state in _TERMINAL_STATES:
            return entry
        if self.kill_switch_enabled:
            raise BrokerLiveTradingDisabled("broker OMS kill switch is enabled")
        if risk_decision is None:
            raise PermissionError("host-issued broker risk approval is required")
        if risk_decision.risk_approval_id != intent.risk_approval_id:
            raise PermissionError("broker risk approval does not match the order intent")
        checked_at = risk_decision.checked_at
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - checked_at > timedelta(seconds=30):
            raise PermissionError("broker risk approval expired; re-evaluate the order")
        if risk_decision.approved is not True:
            raise PermissionError("central broker risk gate rejected the order")
        if intent.environment == "live":
            if not risk_decision.verify_pretrade_receipt(intent):
                raise PermissionError(
                    "live order requires an intact Host pre-trade risk receipt"
                )
            assert self.store is not None
            self.store.record_pretrade_risk_receipt(
                intent.intent_id, risk_decision.pretrade_receipt
            )
            activation_notional = self._require_restricted_live_activation(
                entry, live_activation_receipt
            )
            self._require_live_human_approval(entry, human_approval_receipt)
            self.store.record_restricted_live_activation_use(
                intent.intent_id,
                live_activation_receipt,
                notional=activation_notional,
            )
        entry.state = BrokerOrderState.SUBMITTING
        entry.updated_at = datetime.now(timezone.utc)
        # Write-ahead barrier: a process crash after this point must be
        # reconciled, never converted into a blind duplicate submission.
        self._persist(entry)
        try:
            receipt = await self.gateway.place_order(intent)
        except BaseException as exc:
            self.mark_unknown(intent.intent_id)
            raise BrokerReconciliationRequired(
                "broker submission outcome is not proven; reconcile before retry"
            ) from exc
        self._bind_broker_order(entry, receipt.broker_order_id)
        entry.receipt = receipt
        self._transition(entry, receipt.status)
        entry.remaining_quantity = receipt.accepted_quantity
        entry.updated_at = datetime.now(timezone.utc)
        self._persist(entry)
        return entry

    def apply_order_report(
        self,
        report: BrokerOrderReport,
        *,
        raw_event: BrokerRawEvent,
    ) -> OrderLedgerEntry:
        if report.raw_payload_hash != raw_event.payload_hash:
            raise ValueError("broker report must reference its retained raw payload")
        if report.broker_id != raw_event.broker_id:
            raise ValueError("broker report raw payload cannot cross broker boundaries")
        existing = self._load_by_report_id(report.report_id)
        if existing is not None:
            if existing.intent.intent_id != report.intent_id:
                raise ValueError("broker report ID is already bound to another Host intent")
            return existing
        entry = self._load_by_intent_id(report.intent_id)
        if entry is None:
            raise KeyError("broker report does not map to a Host order intent")
        if report.broker_id != entry.intent.broker_id:
            raise ValueError("a broker report cannot cross broker boundaries")
        if report.account_alias != entry.intent.account_alias:
            raise ValueError("a broker report cannot cross account boundaries")
        if report.filled_quantity + report.remaining_quantity != entry.intent.quantity:
            raise ValueError("broker report quantities must equal the Host order intent")
        if report.filled_quantity < entry.filled_quantity:
            raise ValueError("filled quantity cannot move backwards")
        self._bind_broker_order(entry, report.broker_order_id)
        self._transition(entry, report.status)
        entry.filled_quantity = report.filled_quantity
        entry.remaining_quantity = report.remaining_quantity
        entry.report_ids.append(report.report_id)
        entry.updated_at = report.received_at
        self.event_bus.publish_order_report(
            raw_event=raw_event,
            order_report=report,
        )
        self._intent_by_report[report.report_id] = report.intent_id
        self._persist(entry)
        return entry

    def reconcile_snapshot(
        self,
        snapshot: BrokerOrderReconciliationSnapshot,
        *,
        raw_event: BrokerRawEvent,
    ) -> dict:
        if snapshot.raw_payload_hash != raw_event.payload_hash:
            raise ValueError(
                "order reconciliation snapshot must reference its retained raw payload"
            )
        if snapshot.broker_id != raw_event.broker_id:
            raise ValueError("order reconciliation raw payload broker mismatch")
        observed_intents: set[str] = set()
        applied = 0
        untracked = 0
        for report in snapshot.reports:
            entry = self._load_by_intent_id(report.intent_id)
            if entry is None or entry.intent.account_alias != snapshot.account_alias:
                untracked += 1
                continue
            self.apply_order_report(report, raw_event=raw_event)
            observed_intents.add(report.intent_id)
            applied += 1
        marked_unknown: list[str] = []
        for entry in self._load_all_entries():
            intent_id = entry.intent.intent_id
            if (
                entry.intent.broker_id != snapshot.broker_id
                or entry.intent.account_alias != snapshot.account_alias
                or intent_id in observed_intents
                or entry.state
                not in {
                    BrokerOrderState.SUBMITTING,
                    BrokerOrderState.ACKNOWLEDGED,
                    BrokerOrderState.PARTIALLY_FILLED,
                    BrokerOrderState.CANCEL_PENDING,
                }
            ):
                continue
            self.mark_unknown(intent_id)
            marked_unknown.append(intent_id)
        return {
            "schema_version": "stock_ai.broker_order_reconciliation_result.v1",
            "broker_id": snapshot.broker_id,
            "account_alias": snapshot.account_alias,
            "as_of": snapshot.as_of.isoformat(),
            "applied_report_count": applied,
            "untracked_broker_report_count": untracked,
            "marked_unknown_intent_ids": marked_unknown,
            "trading_allowed": not marked_unknown,
        }

    def request_cancel(self, intent_id: str, *, user_requested: bool) -> OrderLedgerEntry:
        if not user_requested:
            raise PermissionError("cancelling a broker order requires explicit user action")
        entry = self._load_by_intent_id(intent_id)
        if entry is None:
            raise KeyError(intent_id)
        if entry.state not in {
            BrokerOrderState.ACKNOWLEDGED,
            BrokerOrderState.PARTIALLY_FILLED,
        }:
            raise ValueError("only an acknowledged open order can enter cancel pending")
        self._transition(entry, BrokerOrderState.CANCEL_PENDING)
        entry.updated_at = datetime.now(timezone.utc)
        self._persist(entry)
        return entry

    async def cancel(
        self,
        intent_id: str,
        *,
        user_requested: bool,
        human_authorization_receipt_id: str,
    ) -> OrderLedgerEntry:
        if not human_authorization_receipt_id:
            raise PermissionError("broker cancellation requires a Host authorization receipt")
        entry = self.request_cancel(intent_id, user_requested=user_requested)
        if entry.broker_order_id is None:
            self.mark_unknown(intent_id)
            raise BrokerReconciliationRequired(
                "broker order mapping is missing; reconcile before cancellation"
            )
        try:
            receipt = await self.gateway.cancel_order(
                entry.intent.broker_id,
                {
                    "intent_id": entry.intent.intent_id,
                    "account_alias": entry.intent.account_alias,
                    "broker_order_id": entry.broker_order_id,
                    "human_authorization_receipt_id": (
                        human_authorization_receipt_id
                    ),
                },
            )
            if (
                receipt.intent_id != entry.intent.intent_id
                or receipt.broker_id != entry.intent.broker_id
                or receipt.broker_order_id != entry.broker_order_id
                or receipt.status != BrokerOrderState.CANCELLED
            ):
                raise ValueError("broker cancellation receipt did not confirm this order")
        except BaseException as exc:
            self.mark_unknown(intent_id)
            raise BrokerReconciliationRequired(
                "broker cancellation outcome is not proven; reconcile before retry"
            ) from exc
        entry.action_receipts.append(receipt)
        self._transition(entry, BrokerOrderState.CANCELLED)
        entry.remaining_quantity = Decimal("0")
        entry.updated_at = receipt.submitted_at
        self._persist(entry)
        return entry

    async def activate_kill_switch_and_cancel_open_orders(
        self,
        *,
        user_requested: bool,
        human_authorization_receipt_id: str,
    ) -> dict[str, str]:
        if not user_requested:
            raise PermissionError(
                "cancelling all open orders requires explicit user action"
            )
        self.activate_kill_switch()
        results: dict[str, str] = {}
        for entry in self._load_all_entries():
            intent_id = entry.intent.intent_id
            if entry.state not in {
                BrokerOrderState.ACKNOWLEDGED,
                BrokerOrderState.PARTIALLY_FILLED,
            }:
                continue
            try:
                cancelled = await self.cancel(
                    intent_id,
                    user_requested=True,
                    human_authorization_receipt_id=human_authorization_receipt_id,
                )
                results[intent_id] = cancelled.state
            except BrokerReconciliationRequired:
                results[intent_id] = BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED
        return results

    def status(self, intent_id: str) -> OrderLedgerEntry:
        entry = self._load_by_intent_id(intent_id)
        if entry is None:
            raise KeyError(intent_id)
        return entry

    def activate_kill_switch(self) -> None:
        self.kill_switch_enabled = True

    def clear_kill_switch_for_sandbox(self) -> None:
        self.kill_switch_enabled = False

    def _restore_durable_entries(self) -> None:
        assert self.store is not None
        # Broker state may have changed while this process was down.  Persist
        # the UNKNOWN barrier before rebuilding indexes so a restart cannot
        # silently retry a possibly accepted order.
        self.latest_recovery_receipt = self.store.recover_open_orders()
        for stored in self.store.entries():
            self._cache_stored_entry(stored)

    def _load_by_intent_id(self, intent_id: str) -> OrderLedgerEntry | None:
        if self.store is not None:
            stored = self.store.by_intent_id(intent_id)
            if stored is None:
                return None
            return self._cache_stored_entry(stored)
        return self._by_intent.get(intent_id)

    def _load_by_idempotency_key(self, idempotency_key: str) -> OrderLedgerEntry | None:
        if self.store is not None:
            stored = self.store.by_idempotency_key(idempotency_key)
            if stored is None:
                return None
            return self._cache_stored_entry(stored)
        intent_id = self._intent_by_idempotency.get(idempotency_key)
        return self._by_intent.get(intent_id) if intent_id else None

    def _load_by_report_id(self, report_id: str) -> OrderLedgerEntry | None:
        if self.store is not None:
            stored = self.store.by_report_id(report_id)
            if stored is None:
                return None
            return self._cache_stored_entry(stored)
        intent_id = self._intent_by_report.get(report_id)
        return self._by_intent.get(intent_id) if intent_id else None

    def _load_all_entries(self) -> list[OrderLedgerEntry]:
        if self.store is not None:
            return [self._cache_stored_entry(item) for item in self.store.entries()]
        return list(self._by_intent.values())

    def _cache_stored_entry(self, stored: DurableOrderEntry) -> OrderLedgerEntry:
        entry = self._by_intent.get(stored.intent.intent_id)
        if entry is None:
            entry = OrderLedgerEntry(
                intent=stored.intent,
                state=stored.state,
                receipt=stored.receipt,
                broker_order_id=stored.broker_order_id,
                filled_quantity=stored.filled_quantity,
                remaining_quantity=stored.remaining_quantity,
                parent_intent_id=stored.parent_intent_id,
                replacement_intent_id=stored.replacement_intent_id,
                report_ids=list(stored.report_ids),
                action_receipts=list(stored.action_receipts),
                human_approval_receipt=stored.human_approval_receipt,
                updated_at=stored.updated_at,
            )
        else:
            # Keep object identity for callers while refreshing every field
            # from the durable authority before a read or state transition.
            entry.intent = stored.intent
            entry.state = stored.state
            entry.receipt = stored.receipt
            entry.broker_order_id = stored.broker_order_id
            entry.filled_quantity = stored.filled_quantity
            entry.remaining_quantity = stored.remaining_quantity
            entry.parent_intent_id = stored.parent_intent_id
            entry.replacement_intent_id = stored.replacement_intent_id
            entry.report_ids = list(stored.report_ids)
            entry.action_receipts = list(stored.action_receipts)
            entry.human_approval_receipt = stored.human_approval_receipt
            entry.updated_at = stored.updated_at
        self._by_intent[entry.intent.intent_id] = entry
        self._intent_by_idempotency[entry.intent.idempotency_key] = entry.intent.intent_id
        if entry.broker_order_id:
            self._intent_by_broker_order[(entry.intent.broker_id, entry.broker_order_id)] = entry.intent.intent_id
        for report_id in entry.report_ids:
            self._intent_by_report[report_id] = entry.intent.intent_id
        return entry

    def _persist(self, entry: OrderLedgerEntry) -> None:
        if self.retention_ledger is not None:
            payload = self._critical_retention_payload(entry)
            record_id = "oms-state-" + hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self.retention_ledger.append(
                record_id,
                payload,
                critical=True,
                kind="execution_oms_state",
                occurred_at=payload["updated_at"],
            )
        if self.store is None:
            return
        self.store.save(
            DurableOrderEntry(
                intent=entry.intent,
                state=entry.state,
                receipt=entry.receipt,
                broker_order_id=entry.broker_order_id,
                filled_quantity=entry.filled_quantity,
                remaining_quantity=entry.remaining_quantity,
                parent_intent_id=entry.parent_intent_id,
                replacement_intent_id=entry.replacement_intent_id,
                report_ids=tuple(entry.report_ids),
                action_receipts=tuple(entry.action_receipts),
                human_approval_receipt=entry.human_approval_receipt,
                updated_at=entry.updated_at,
            )
        )

    @staticmethod
    def _critical_retention_payload(entry: OrderLedgerEntry) -> dict:
        updated_at = entry.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return {
            "schema_version": "stock_ai.broker_oms_execution_state.v1",
            "intent": entry.intent.model_dump(mode="json"),
            "state": entry.state.value,
            "receipt": entry.receipt.model_dump(mode="json") if entry.receipt else None,
            "broker_order_id": entry.broker_order_id,
            "filled_quantity": str(entry.filled_quantity),
            "remaining_quantity": str(entry.remaining_quantity),
            "parent_intent_id": entry.parent_intent_id,
            "replacement_intent_id": entry.replacement_intent_id,
            "report_ids": list(entry.report_ids),
            "action_receipts": [
                receipt.model_dump(mode="json") for receipt in entry.action_receipts
            ],
            "human_approval_receipt": (
                entry.human_approval_receipt.model_dump(mode="json")
                if entry.human_approval_receipt
                else None
            ),
            "updated_at": updated_at.astimezone(timezone.utc).isoformat(),
        }

    def _require_live_human_approval(
        self,
        entry: OrderLedgerEntry,
        receipt: BrokerHumanApprovalReceipt | None,
    ) -> None:
        if self.human_approval_authority is None:
            raise PermissionError("live order submission requires a Host human approval authority")
        if receipt is None:
            raise PermissionError("live order submission requires a Host-signed human approval receipt")
        self.human_approval_authority.verify(entry.intent, receipt)
        if (
            entry.human_approval_receipt is not None
            and entry.human_approval_receipt.receipt_id != receipt.receipt_id
        ):
            raise PermissionError("a live order intent cannot replace its human approval receipt")
        entry.human_approval_receipt = receipt
        entry.updated_at = datetime.now(timezone.utc)
        # Persist the cryptographically verified approval before the write-ahead
        # submission barrier so restart/reconciliation has the full audit link.
        self._persist(entry)

    def _require_restricted_live_activation(
        self,
        entry: OrderLedgerEntry,
        receipt: BrokerRestrictedLiveActivationReceipt | None,
    ) -> Decimal:
        if self.live_activation_authority is None:
            raise PermissionError(
                "live order submission requires a Host restricted-live activation authority"
            )
        if receipt is None:
            raise PermissionError(
                "live order submission requires a Host restricted-live activation receipt"
            )
        assert self.store is not None
        expected_price = entry.intent.limit_price
        if expected_price is None:
            raise PermissionError("live order activation requires a bounded limit price")
        daily_notional = self.store.restricted_live_daily_notional(receipt)
        self.live_activation_authority.verify(
            entry.intent,
            receipt,
            daily_notional_before_order=daily_notional,
        )
        return entry.intent.quantity * expected_price

    def _bind_broker_order(
        self,
        entry: OrderLedgerEntry,
        broker_order_id: str,
    ) -> None:
        key = (entry.intent.broker_id, broker_order_id)
        if self.store is not None:
            stored = self.store.by_broker_order(*key)
            if stored is not None:
                mapped_intent = stored.intent.intent_id
                if mapped_intent != entry.intent.intent_id:
                    raise ValueError("broker order ID is already mapped to another Host intent")
                self._cache_stored_entry(stored)
        mapped_intent = self._intent_by_broker_order.get(key)
        if mapped_intent is not None and mapped_intent != entry.intent.intent_id:
            raise ValueError("broker order ID is already mapped to another Host intent")
        if entry.broker_order_id not in {None, broker_order_id}:
            raise ValueError("Host intent cannot change its broker order mapping")
        self._intent_by_broker_order[key] = entry.intent.intent_id
        entry.broker_order_id = broker_order_id

    @staticmethod
    def _transition(
        entry: OrderLedgerEntry,
        next_state: BrokerOrderState,
    ) -> None:
        if entry.state == next_state:
            return
        allowed = {
            BrokerOrderState.CREATED: {
                BrokerOrderState.VALIDATING,
                BrokerOrderState.RISK_PENDING,
                BrokerOrderState.USER_APPROVAL_PENDING,
                BrokerOrderState.SUBMITTING,
                BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED,
            },
            BrokerOrderState.SUBMITTING: {
                BrokerOrderState.ACKNOWLEDGED,
                BrokerOrderState.PARTIALLY_FILLED,
                BrokerOrderState.FILLED,
                BrokerOrderState.REJECTED,
                BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED,
            },
            BrokerOrderState.ACKNOWLEDGED: {
                BrokerOrderState.PARTIALLY_FILLED,
                BrokerOrderState.FILLED,
                BrokerOrderState.CANCEL_PENDING,
                BrokerOrderState.CANCELLED,
                BrokerOrderState.REJECTED,
                BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED,
            },
            BrokerOrderState.PARTIALLY_FILLED: {
                BrokerOrderState.PARTIALLY_FILLED,
                BrokerOrderState.FILLED,
                BrokerOrderState.CANCEL_PENDING,
                BrokerOrderState.CANCELLED,
                BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED,
            },
            BrokerOrderState.CANCEL_PENDING: {
                BrokerOrderState.PARTIALLY_FILLED,
                BrokerOrderState.FILLED,
                BrokerOrderState.CANCELLED,
                BrokerOrderState.REJECTED,
                BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED,
            },
            BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED: {
                BrokerOrderState.ACKNOWLEDGED,
                BrokerOrderState.PARTIALLY_FILLED,
                BrokerOrderState.FILLED,
                BrokerOrderState.CANCELLED,
                BrokerOrderState.REJECTED,
            },
        }
        if next_state not in allowed.get(entry.state, set()):
            raise ValueError(
                f"invalid broker OMS state transition: {entry.state} -> {next_state}"
            )
        entry.state = next_state
