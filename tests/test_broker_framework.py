from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
import yaml

from open_stock_ai.agent_runtime.context_broker import ContextBroker
from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.agent_runtime.mutation_receipts import verify_mutation_receipt
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from open_stock_ai.agent_runtime.workers import WorkerSupervisor
from open_stock_ai.governance.change_management import ChangeManagementRegistry
from open_stock_ai.governance.content_retention import ContentAddressedRetentionLedger
from open_stock_ai.governance.durable_store import SQLiteGovernanceStore, SQLiteRetentionStore
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.broker_api import router as broker_router
from stock_ai.broker_tools import BrokerAgentToolProvider
from stock_ai.brokers import (
    BrokerAccountReconciliationEngine,
    BrokerCapabilityRegistry,
    BrokerEvidenceContextBuilder,
    BrokerFeedMetrics,
    BrokerFeedScoreEngine,
    BrokerHumanApprovalAuthority,
    BrokerRestrictedLiveActivationAuthority,
    BrokerObservabilityMonitor,
    BrokerOrderManagementGateway,
    BrokerOMSStore,
    BrokerRateLimitGovernor,
    BrokerRateLimitPolicy,
    BrokerReconnectPlanner,
    BrokerRuntimeMetrics,
    BrokerRuntimeStatusRegistry,
    BrokerSdkProvisioner,
    BrokerSequenceTracker,
    BrokerSourceSelectionLedger,
    BrokerSubscription,
    BrokerSubscriptionManager,
    BrokerWorkerInterface,
    BrokerWorkerOperation,
    BrokerWorkerRequest,
    CanonicalFinancialEventBus,
    HostAccountLedger,
    HumanAuthorizationWorkflow,
    MarketDataReconciliationEngine,
)
from stock_ai.brokers.contracts import (
    BrokerAccountSnapshot,
    BrokerAuthProfile,
    BrokerCapabilities,
    BrokerCapabilityProfile,
    BrokerFill,
    BrokerLiveTradingDisabled,
    BrokerOpenOrder,
    BrokerOrderIntent,
    BrokerOrderReceipt,
    BrokerOrderReconciliationSnapshot,
    BrokerOrderReport,
    BrokerOrderState,
    BrokerPosition,
    BrokerReconciliationRequired,
    BrokerRawEvent,
    BrokerSettlementAmount,
    CanonicalMarketEvent,
    PriceLevel,
)
from stock_ai.brokers.risk import BrokerPreTradeRiskContext, CentralBrokerRiskGate
from stock_ai.brokers.secret_store import SecretReference


def _event(
    broker_id: str,
    *,
    price: str,
    exchange_time: str,
    sequence: int,
    received_time: str,
) -> CanonicalMarketEvent:
    return CanonicalMarketEvent(
        broker_id=broker_id,
        instrument_id="TWSE:2330",
        exchange="TWSE",
        market_session="regular_lot",
        channel="trade",
        exchange_timestamp=exchange_time,
        received_at=received_time,
        sequence=sequence,
        price=Decimal(price),
        size=1000,
        trading_status="trading",
        raw_payload_hash="a" * 64,
    )


def _intent(
    *,
    intent_id: str = "BOI-one",
    idempotency_key: str = "broker-order-validation-0001",
    environment: str = "sandbox",
) -> BrokerOrderIntent:
    return BrokerOrderIntent(
        intent_id=intent_id,
        broker_id="fubon",
        account_alias="paper-validation",
        instrument_id="TWSE:2330",
        side="buy",
        quantity=Decimal("1"),
        price_type="limit",
        limit_price=Decimal("1000"),
        time_in_force="ROD",
        session="regular_lot",
        order_purpose="integration_validation",
        environment=environment,
        user_approved=True,
        risk_approval_id="RISK-validation",
        idempotency_key=idempotency_key,
    )


def _pretrade(**overrides) -> BrokerPreTradeRiskContext:
    payload = {
        "reference_price": Decimal("1000"),
        "quote_observed_at": datetime.now(timezone.utc),
        "max_quote_age_seconds": 30,
        "market_phase": "continuous",
        "max_quantity": Decimal("100"),
        "max_order_notional": Decimal("50000"),
        "price_collar_bps": Decimal("100"),
        "adv_quantity": Decimal("10000"),
        "max_adv_participation_pct": Decimal("2"),
        "current_symbol_notional": Decimal("10000"),
        "max_symbol_notional": Decimal("30000"),
        "current_account_notional": Decimal("40000"),
        "max_account_notional": Decimal("100000"),
        "daily_loss_notional": Decimal("0"),
        "max_daily_loss_notional": Decimal("5000"),
        "strategy_execution_authorized": True,
        "model_execution_eligible": True,
    }
    payload.update(overrides)
    return BrokerPreTradeRiskContext(**payload)


def _approved_live_risk(intent: BrokerOrderIntent):
    return CentralBrokerRiskGate().evaluate(
        intent,
        account_exists=True,
        api_permission_enabled=True,
        certificate_valid=True,
        market_data_fresh=True,
        broker_healthy=True,
        instrument_tradable=True,
        buying_power=Decimal("100000"),
        estimated_notional=Decimal("1000"),
        duplicate_order_exists=False,
        kill_switch_enabled=False,
        live_trading_enabled=True,
        pretrade=_pretrade(),
    )


def _restricted_live_activation(
    authority: BrokerRestrictedLiveActivationAuthority,
    *,
    user_requested: bool = True,
    **overrides,
):
    payload = {
        "broker_id": "fubon",
        "account_alias": "paper-validation",
        "allowed_instruments": ("TWSE:2330",),
        "max_order_notional": Decimal("1000"),
        "max_daily_notional": Decimal("2000"),
        "user_requested": user_requested,
    }
    payload.update(overrides)
    return authority.issue(**payload)


def test_broker_source_lock_is_official_and_never_claims_unverified_sdk_hashes() -> None:
    path = Path(__file__).parents[1] / "config" / "broker_sources.lock.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert payload["policy"]["official_sources_only"] is True
    assert payload["policy"]["live_trading_enabled"] is False
    assert set(payload["brokers"]) == {
        "taishin",
        "fubon",
        "sinopac",
        "yuanta",
        "masterlink",
    }
    for broker in payload["brokers"].values():
        assert broker["official_documentation"].startswith("https://")
        assert len(broker["documentation_sha256"]) == 64
        assert broker["sdk_artifact_sha256"] is None
        assert broker["sdk_status"].startswith("requires_user_")


def test_compliance_registry_keeps_unverified_permissions_unknown_and_live_off() -> None:
    path = Path(__file__).parents[1] / "config" / "broker_compliance.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert payload["policy"]["official_broker_terms_are_authoritative"] is True
    assert payload["policy"]["live_trading_enabled"] is False
    assert payload["policy"]["model_direct_submit"] is False
    assert set(payload["brokers"]) == {
        "taishin",
        "fubon",
        "sinopac",
        "yuanta",
        "masterlink",
    }
    for broker in payload["brokers"].values():
        assert broker["official_terms_url"].startswith("https://")
        assert broker["account_owner_review_status"] == "pending"
        assert broker["market_data_authorization_scope"] is None
        assert broker["market_data_redistribution_allowed"] is None
        assert broker["automation_authorization_scope"] is None
        assert broker["sandbox_permission"] is None
        assert broker["live_permission_recorded"] is False


def test_sdk_provenance_never_installs_unverified_or_in_project_artifacts(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    artifact = tmp_path / "broker-sdk.bin"
    artifact.write_bytes(b"official-sdk-test-fixture")
    digest = sha256(artifact.read_bytes()).hexdigest()
    provisioner = BrokerSdkProvisioner(project_root)

    recorded_only = provisioner.record_artifact(
        broker_id="fubon",
        artifact_path=artifact,
        source_url="https://www.fbs.com.tw/TradeAPI/",
        sdk_version="test-fixture",
        account_owner_confirmed_official_download=True,
    )
    verified = provisioner.record_artifact(
        broker_id="fubon",
        artifact_path=artifact,
        source_url="https://www.fbs.com.tw/TradeAPI/",
        sdk_version="test-fixture",
        account_owner_confirmed_official_download=True,
        official_sha256=digest,
    )

    assert recorded_only.artifact_sha256 == digest
    assert recorded_only.install_allowed is False
    assert verified.official_checksum_verified is True
    assert verified.install_allowed is True
    with pytest.raises(ValueError, match="outside the project"):
        provisioner.record_artifact(
            broker_id="fubon",
            artifact_path=project_root / "pyproject.toml",
            source_url="https://www.fbs.com.tw/TradeAPI/",
            sdk_version="invalid-location",
            account_owner_confirmed_official_download=True,
            official_sha256="0" * 64,
        )


def test_certificate_health_uses_host_metadata_and_secure_references_only(
    tmp_path: Path,
) -> None:
    provisioner = BrokerSdkProvisioner(Path(__file__).parents[1])
    report = provisioner.certificate_health_from_host_metadata(
        broker_id="sinopac",
        certificate_reference="keychain://stock-ai/sinopac/certificate",
        exists=True,
        readable_by_worker=True,
        permissions_restricted=True,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )

    assert report.status == "healthy"
    assert report.install_or_login_allowed is True
    with pytest.raises(ValidationError, match="secure-store references"):
        provisioner.certificate_health_from_host_metadata(
            broker_id="sinopac",
            certificate_reference=str(tmp_path / "certificate.pfx"),
            exists=True,
            readable_by_worker=True,
            permissions_restricted=True,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )


def test_declared_brokers_publish_unverified_capabilities_and_owner_checkpoints() -> None:
    registry = BrokerCapabilityRegistry()

    profiles = asyncio.run(registry.probe_all())
    authorization = asyncio.run(registry.authorization_matrix())

    assert registry.list_broker_ids() == [
        "taishin",
        "fubon",
        "sinopac",
        "yuanta",
        "masterlink",
    ]
    assert all(
        value is None
        for profile in profiles
        for value in profile["capabilities"].values()
    )
    assert all(item["state"] == "requires_user_action" for item in authorization)
    assert all(item["safe_to_continue_automatically"] is False for item in authorization)


def test_capability_cannot_be_true_without_a_real_probe_receipt() -> None:
    with pytest.raises(ValidationError, match="probe or official-document receipts"):
        BrokerCapabilityProfile(
            broker_id="fubon",
            adapter_version="1.0.0",
            sdk_version="2.2.8",
            platform="macos-arm64",
            capabilities=BrokerCapabilities(stock_quotes=True),
            verified_at=datetime.now(timezone.utc),
        )


def test_capability_cannot_be_false_without_official_evidence() -> None:
    with pytest.raises(ValidationError, match="probe or official-document receipts"):
        BrokerCapabilityProfile(
            broker_id="taishin",
            adapter_version="1.0.0",
            sdk_version="unverified",
            platform="macos-arm64",
            capabilities=BrokerCapabilities(stock_quotes=False),
            verified_at=datetime.now(timezone.utc),
        )


def test_auth_contract_rejects_plaintext_secrets_and_account_snapshot_requires_masking() -> None:
    with pytest.raises(ValidationError, match="secure-store references"):
        BrokerAuthProfile(
            broker_id="sinopac",
            account_alias="main",
            auth_method="api_key",
            secret_reference="actual-secret",
        )
    auth = BrokerAuthProfile(
        broker_id="sinopac",
        account_alias="main",
        auth_method="api_key",
        secret_reference="keychain://stock-ai/sinopac/main",
    )
    assert auth.secret_reference.startswith("keychain://")

    with pytest.raises(ValidationError, match="must be masked"):
        BrokerAccountSnapshot(
            broker_id="fubon",
            account_id_masked="1234567",
            account_type="stock",
            currency="TWD",
            as_of=datetime.now(timezone.utc),
        )
    assert (
        SecretReference(
            backend="credential-manager",
            locator="stock-ai/fubon/main",
        ).uri()
        == "credential-manager://stock-ai/fubon/main"
    )


def _agent_context(
    task_kind: str,
    *,
    account_approved: bool = False,
) -> AgentRunContext:
    return AgentRunContext(
        run_id="AR-broker-test",
        session_id="AR-broker-test",
        autonomy="advisory",
        symbols=("2330.TW",),
        state={
            "task_kind": task_kind,
            "broker_account_access_approved": account_approved,
        },
    )


def test_broker_agent_manifest_is_scoped_and_never_exposes_forbidden_operations() -> None:
    provider = BrokerAgentToolProvider()
    names = {item["name"] for item in provider.manifest()}
    forbidden = {
        "broker.login",
        "broker.secret.read",
        "broker.certificate.read",
        "broker.order.submit",
        "broker.bank.transfer",
    }

    assert forbidden.isdisjoint(names)
    assert "broker.order.propose" in names
    assert "broker.account.positions" in names

    broker = ContextBroker()
    project_names = {
        item["name"]
        for item in broker.filter_capabilities(provider.manifest(), task_kind="project_task")
    }
    market_names = {
        item["name"]
        for item in broker.filter_capabilities(
            provider.manifest(),
            task_kind="market_information",
        )
    }
    decision_names = {
        item["name"]
        for item in broker.filter_capabilities(
            provider.manifest(),
            task_kind="market_decision",
        )
    }

    assert not project_names
    assert "broker.market.snapshot" in market_names
    assert "broker.account.positions" not in market_names
    assert "broker.account.positions" in decision_names
    assert "broker.order.propose" in decision_names


def test_broker_network_tools_use_the_dedicated_broker_worker() -> None:
    manifest = BrokerAgentToolProvider().manifest()
    external = {
        item["name"]: item["execution_backend"]
        for item in manifest
        if item["name"].startswith(("broker.market.", "broker.account."))
    }

    assert external
    assert set(external.values()) == {"broker"}


def test_broker_worker_executes_through_a_real_child_process(tmp_path: Path) -> None:
    async def exercise() -> tuple[dict[str, object], int | None]:
        db_path = tmp_path / "agent.db"
        sessions = AgentSessionStore(db_path)
        sessions.create(
            session_id="AS-broker-worker",
            namespace="test",
            title="broker worker",
        )
        runs = AgentRunStore(db_path)
        runs.create_run(
            "AR-broker-worker",
            {
                "objective": "verify broker process isolation",
                "symbols": [],
                "driver_id": "codex",
                "autonomy": "advisory",
                "max_steps": 2,
                "session_id": "AS-broker-worker",
            },
        )
        supervisor = WorkerSupervisor(
            db_path,
            socket_path=tmp_path / "agent.sock",
            project_root=Path(__file__).parents[1],
        )
        await supervisor.start()
        try:
            _, result = await supervisor.execute(
                run_id="AR-broker-worker",
                step_id="STEP-broker-worker",
                worker_type="broker",
                timeout_seconds=30,
                payload={
                    "tool": "broker.list_connections",
                    "arguments": {},
                    "audit_arguments": {},
                    "context": {
                        "run_id": "AR-broker-worker",
                        "session_id": "AR-broker-worker",
                        "autonomy": "advisory",
                        "state": {"task_kind": "market_information"},
                    },
                },
                handler=lambda _request: asyncio.sleep(
                    0,
                    result={"unexpected": "in-process fallback"},
                ),
            )
            channel = supervisor._processes[("AR-broker-worker", "broker")]
            return result, channel.process.pid
        finally:
            await supervisor.close()

    result, worker_pid = asyncio.run(exercise())

    assert "unexpected" not in result
    assert result["live_trading_enabled"] is False
    assert worker_pid is not None


def test_broker_agent_tools_enforce_project_and_account_isolation() -> None:
    provider = BrokerAgentToolProvider()

    with pytest.raises(PermissionError, match="isolated"):
        asyncio.run(
            provider.execute(
                "broker.market.snapshot",
                {"broker_id": "fubon", "instrument_id": "TWSE:2330"},
                _agent_context("project_task"),
            )
        )
    with pytest.raises(PermissionError, match="explicit host-issued approval"):
        asyncio.run(
            provider.execute(
                "broker.account.positions",
                {"broker_id": "fubon", "account_alias": "main"},
                _agent_context("market_decision"),
            )
        )


def test_broker_order_proposal_is_local_unapproved_and_never_submitted() -> None:
    provider = BrokerAgentToolProvider()
    context = _agent_context("market_decision")
    result = asyncio.run(
        provider.execute(
            "broker.order.propose",
            {
                "broker_id": "fubon",
                "account_alias": "paper",
                "instrument_id": "TWSE:2330",
                "side": "buy",
                "quantity": 1,
                "price_type": "limit",
                "limit_price": 1000,
                "time_in_force": "ROD",
                "session": "regular_lot",
                "order_purpose": "test",
                "risk_approval_id": "RISK-test",
                "idempotency_key": "broker-agent-proposal-0001",
            },
            context,
        )
    )

    assert result["state"] == "USER_APPROVAL_PENDING"
    assert result["submitted"] is False
    assert result["mutation_performed"] is True
    assert result["mutation_receipt"] == result["receipt"]
    assert verify_mutation_receipt(result["receipt"])
    assert not verify_mutation_receipt({**result["receipt"], "accepted": False})
    stored = context.state["broker_order_proposals"][result["intent_id"]]
    assert stored["user_approved"] is False
    assert stored["submitted"] is False


def test_broker_order_cancel_emits_accepted_receipt_only_for_existing_local_proposal() -> None:
    provider = BrokerAgentToolProvider()
    context = _agent_context("market_decision")
    proposal = asyncio.run(
        provider.execute(
            "broker.order.propose",
            {
                "broker_id": "fubon",
                "account_alias": "paper",
                "instrument_id": "TWSE:2330",
                "side": "buy",
                "quantity": 1,
                "price_type": "limit",
                "limit_price": 1000,
                "time_in_force": "ROD",
                "session": "regular_lot",
                "order_purpose": "test",
                "risk_approval_id": "RISK-test",
                "idempotency_key": "broker-agent-proposal-0002",
            },
            context,
        )
    )

    cancelled = asyncio.run(
        provider.execute(
            "broker.order.cancel_proposal",
            {"intent_id": proposal["intent_id"]},
            context,
        )
    )
    assert cancelled["cancelled"] is True
    assert cancelled["mutation_performed"] is True
    assert cancelled["receipt"]["accepted"] is True
    assert verify_mutation_receipt(cancelled["receipt"])

    missing = asyncio.run(
        provider.execute(
            "broker.order.cancel_proposal",
            {"intent_id": proposal["intent_id"]},
            context,
        )
    )
    assert missing["cancelled"] is False
    assert missing["mutation_performed"] is False
    assert missing["receipt"]["accepted"] is False
    assert verify_mutation_receipt(missing["receipt"])


def test_event_bus_retains_raw_payload_hash_and_rejects_unrelated_payload() -> None:
    raw = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"symbol": "2330", "price": 1000},
    )
    canonical = _event(
        "fubon",
        price="1000",
        exchange_time="2026-07-26T01:00:00+00:00",
        sequence=10,
        received_time="2026-07-26T01:00:00.100000+00:00",
    ).model_copy(update={"raw_payload_hash": raw.payload_hash})
    bus = CanonicalFinancialEventBus()

    bus.publish(raw_event=raw, canonical_event=canonical)

    assert bus.events(instrument_id="TWSE:2330") == [canonical]
    assert bus.raw_event(raw.payload_hash).payload == raw.payload


def test_reconciliation_uses_exchange_time_and_never_averages_prices() -> None:
    earlier_but_fast = _event(
        "taishin",
        price="999",
        exchange_time="2026-07-26T01:00:00+00:00",
        sequence=10,
        received_time="2026-07-26T01:00:00.010000+00:00",
    )
    newer_but_slow = _event(
        "fubon",
        price="1001",
        exchange_time="2026-07-26T01:00:01+00:00",
        sequence=11,
        received_time="2026-07-26T01:00:03+00:00",
    )

    result = MarketDataReconciliationEngine().reconcile(
        [earlier_but_fast, newer_but_slow],
        feed_health={"taishin": 1.0, "fubon": 0.5},
    )

    assert result.selected_source == "fubon"
    assert next(
        item.price for item in result.observations if item.event_id == result.selected_event_id
    ) == Decimal("1001")
    assert result.consensus_status == "consistent"


def test_reconciliation_exposes_same_coordinate_conflicts_and_blocks_trading() -> None:
    first = _event(
        "taishin",
        price="999",
        exchange_time="2026-07-26T01:00:00+00:00",
        sequence=10,
        received_time="2026-07-26T01:00:00.010000+00:00",
    )
    second = _event(
        "fubon",
        price="1000",
        exchange_time="2026-07-26T01:00:00+00:00",
        sequence=10,
        received_time="2026-07-26T01:00:00.020000+00:00",
    )

    result = MarketDataReconciliationEngine().reconcile([first, second])

    assert result.consensus_status == "conflict"
    assert result.trading_allowed is False
    assert len(result.observations) == 2


def test_stock_evidence_pack_omits_raw_payload_and_private_account_by_default() -> None:
    market = MarketDataReconciliationEngine().reconcile(
        [
            _event(
                "fubon",
                price="1000",
                exchange_time="2026-07-26T01:00:00+00:00",
                sequence=10,
                received_time="2026-07-26T01:00:00.020000+00:00",
            )
        ]
    )
    account = BrokerAccountSnapshot(
        broker_id="fubon",
        account_id_masked="12****78",
        account_type="stock",
        currency="TWD",
        available_cash=Decimal("100000"),
        buying_power=Decimal("200000"),
        positions=[
            BrokerPosition(
                instrument_id="TWSE:2330",
                quantity=Decimal("1000"),
                sellable_quantity=Decimal("1000"),
                position_type="cash",
            )
        ],
        as_of=datetime.now(timezone.utc),
    )
    builder = BrokerEvidenceContextBuilder()

    public = builder.build(market, account=account).model_dump(mode="json")
    private = builder.build(
        market,
        account=account,
        account_access_approved=True,
    ).model_dump(mode="json")

    assert "raw_payload_hash" not in str(public)
    assert public["broker_account_context"]["enabled"] is False
    assert public["broker_account_context"]["positions"] == []
    assert private["broker_account_context"]["enabled"] is True
    assert private["broker_account_context"]["account_id_masked"] == "12****78"
    assert private["broker_account_context"]["positions"][0]["instrument_id"] == "TWSE:2330"


def test_reconciliation_detects_orderbook_or_size_conflicts_at_same_coordinate() -> None:
    first = _event(
        "taishin",
        price="1000",
        exchange_time="2026-07-26T01:00:00+00:00",
        sequence=10,
        received_time="2026-07-26T01:00:00.010000+00:00",
    ).model_copy(
        update={"size": 1000, "bids": [PriceLevel(price=999, size=2)]}
    )
    second = _event(
        "fubon",
        price="1000",
        exchange_time="2026-07-26T01:00:00+00:00",
        sequence=10,
        received_time="2026-07-26T01:00:00.020000+00:00",
    ).model_copy(
        update={"size": 2000, "bids": [PriceLevel(price=998, size=3)]}
    )

    result = MarketDataReconciliationEngine().reconcile([first, second])

    assert result.consensus_status == "conflict"
    assert result.trading_allowed is False


def test_verified_exchange_timestamp_has_priority_over_unverified_timestamp() -> None:
    verified = _event(
        "taishin",
        price="1000",
        exchange_time="2026-07-26T01:00:00+00:00",
        sequence=10,
        received_time="2026-07-26T01:00:00.010000+00:00",
    ).model_copy(update={"exchange_timestamp_verified": True})
    unverified_newer = _event(
        "fubon",
        price="1001",
        exchange_time="2026-07-26T01:00:01+00:00",
        sequence=11,
        received_time="2026-07-26T01:00:01.010000+00:00",
    )

    result = MarketDataReconciliationEngine().reconcile([verified, unverified_newer])

    assert result.selected_source == "taishin"


def test_oms_idempotency_and_unknown_state_prevent_duplicate_retry() -> None:
    oms = BrokerOrderManagementGateway()
    intent = _intent()

    first = oms.register(intent)
    second = oms.register(intent.model_copy())
    assert first is second

    with pytest.raises(ValueError, match="different order arguments"):
        oms.register(
            _intent(
                intent_id="BOI-two",
                idempotency_key=intent.idempotency_key,
            ).model_copy(update={"quantity": Decimal("2")})
        )

    oms.mark_unknown(intent.intent_id)
    oms.clear_kill_switch_for_sandbox()
    with pytest.raises(BrokerReconciliationRequired):
        asyncio.run(oms.submit(intent))


def test_live_oms_refuses_to_register_without_a_durable_write_ahead_store() -> None:
    oms = BrokerOrderManagementGateway()

    with pytest.raises(BrokerReconciliationRequired, match="durable Broker OMS store"):
        oms.register(_intent(environment="live"))


def test_oms_can_require_durable_change_set_binding_before_order_persistence(tmp_path) -> None:
    store = BrokerOMSStore(tmp_path / "broker-oms.sqlite")
    governance = SQLiteGovernanceStore(store.path)
    registry = ChangeManagementRegistry(governance)
    change = registry.pin_change(
        "change-oms-1",
        code_sha256="a" * 64,
        model_sha256="b" * 64,
        data_sha256="c" * 64,
        risk_policy_sha256="d" * 64,
        approved_by="owner",
        approved_at="2026-08-26T05:00:00+00:00",
    )
    intent = _intent().model_copy(update={"change_id": change.change_id})
    oms = BrokerOrderManagementGateway(
        store=store,
        change_management=registry,
        require_change_binding=True,
    )

    registered = oms.register(intent)
    assert registered.intent.change_id == change.change_id
    binding = ChangeManagementRegistry(SQLiteGovernanceStore(store.path))._orders[intent.intent_id]
    assert binding["change_sha256"] == change.change_sha256
    assert binding["code_sha256"] == "a" * 64
    assert BrokerOrderManagementGateway(store=BrokerOMSStore(store.path)).register(intent).intent.change_id == change.change_id

    with pytest.raises(PermissionError, match="change-set binding"):
        BrokerOrderManagementGateway(
            store=store,
            change_management=registry,
            require_change_binding=True,
        ).register(_intent(intent_id="BOI-no-change", idempotency_key="test-00000000000841"))
    with pytest.raises(ValueError, match="same durable database"):
        BrokerOrderManagementGateway(
            store=BrokerOMSStore(tmp_path / "other-oms.sqlite"),
            change_management=registry,
            require_change_binding=True,
        )
    with pytest.raises(ValueError, match="durable governance store"):
        BrokerOrderManagementGateway(
            store=store,
            change_management=ChangeManagementRegistry(),
            require_change_binding=True,
        )


def test_oms_persists_immutable_execution_state_to_critical_retention_ledger(tmp_path) -> None:
    database = tmp_path / "broker-oms-retention.sqlite"
    oms_store = BrokerOMSStore(database)
    ledger = ContentAddressedRetentionLedger(store=SQLiteRetentionStore(database))
    oms = BrokerOrderManagementGateway(
        store=oms_store,
        retention_ledger=ledger,
        require_critical_retention=True,
    )

    entry = oms.register(_intent())
    records = ledger.retained_counts()
    assert records == {"total": 1, "signals": 0, "critical": 1}
    retained = next(
        record for record_id in ledger._order if (record := ledger.get(record_id)) is not None
    )
    assert retained["kind"] == "execution_oms_state"
    assert retained["payload"]["intent"]["intent_id"] == entry.intent.intent_id
    assert retained["payload"]["state"] == BrokerOrderState.CREATED.value

    restarted_ledger = ContentAddressedRetentionLedger(
        store=SQLiteRetentionStore(database)
    )
    assert restarted_ledger.retained_counts() == records
    assert restarted_ledger.get(retained["record_id"]) == retained


def test_oms_rejects_mandatory_critical_retention_without_durable_wiring() -> None:
    with pytest.raises(ValueError, match="retention ledger"):
        BrokerOrderManagementGateway(require_critical_retention=True)


def test_oms_rejects_mandatory_retention_on_a_different_database(tmp_path) -> None:
    with pytest.raises(ValueError, match="same durable database"):
        BrokerOrderManagementGateway(
            store=BrokerOMSStore(tmp_path / "oms.sqlite"),
            retention_ledger=ContentAddressedRetentionLedger(
                store=SQLiteRetentionStore(tmp_path / "retention.sqlite")
            ),
            require_critical_retention=True,
        )


def test_durable_oms_restores_idempotency_and_forces_open_order_reconciliation(tmp_path) -> None:
    store = BrokerOMSStore(tmp_path / "broker-oms.sqlite")
    first = BrokerOrderManagementGateway(store=store)
    intent = _intent()
    entry = first.register(intent)
    entry.state = BrokerOrderState.SUBMITTING
    first._persist(entry)
    raw = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"order_id": "FUBON-DURABLE-1", "status": "ack"},
    )
    first.apply_order_report(
        _order_report(
            raw,
            report_id="BOR-durable-ack",
            status=BrokerOrderState.ACKNOWLEDGED,
            filled="0",
            remaining="1",
            broker_order_id="FUBON-DURABLE-1",
        ),
        raw_event=raw,
    )

    reopened = BrokerOrderManagementGateway(store=BrokerOMSStore(store.path))
    restored = reopened.register(intent.model_copy())

    assert reopened.latest_recovery_receipt is not None
    recovery = reopened.latest_recovery_receipt
    assert recovery.marked_unknown_intent_ids == (intent.intent_id,)
    assert recovery.open_orders_before_recovery == (
        {
            "intent_id": intent.intent_id,
            "state": BrokerOrderState.ACKNOWLEDGED.value,
            "updated_at": entry.updated_at.isoformat(),
        },
    )
    assert len(recovery.receipt_sha256) == 64
    assert BrokerOMSStore(store.path).recovery_receipts() == [recovery]
    assert restored.intent.intent_id == intent.intent_id
    assert restored.broker_order_id == "FUBON-DURABLE-1"
    assert restored.state == BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED
    assert restored.report_ids == ["BOR-durable-ack"]
    with pytest.raises(BrokerReconciliationRequired):
        asyncio.run(reopened.submit(intent))

    reconciliation_raw = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"order_id": "FUBON-DURABLE-1", "status": "ack-reconciled"},
    )
    resolved = reopened.apply_order_report(
        _order_report(
            reconciliation_raw,
            report_id="BOR-durable-reconciled",
            status=BrokerOrderState.ACKNOWLEDGED,
            filled="0",
            remaining="1",
            broker_order_id="FUBON-DURABLE-1",
        ),
        raw_event=reconciliation_raw,
    )
    assert resolved.state == BrokerOrderState.ACKNOWLEDGED
    assert BrokerOMSStore(store.path).entries()[0].state == BrokerOrderState.ACKNOWLEDGED


def test_durable_oms_uses_store_for_cross_instance_lookup_and_report_dedupe(tmp_path) -> None:
    store = BrokerOMSStore(tmp_path / "broker-oms-shared.sqlite")
    first = BrokerOrderManagementGateway(store=store)
    second = BrokerOrderManagementGateway(store=BrokerOMSStore(store.path))
    intent = _intent(
        intent_id="BOI-shared",
        idempotency_key="broker-shared-lookup-0001",
    )

    entry = first.register(intent)
    entry.state = BrokerOrderState.SUBMITTING
    first._persist(entry)

    assert second.register(intent.model_copy()).state == BrokerOrderState.SUBMITTING
    raw = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"order_id": "FUBON-SHARED-1", "status": "ack"},
    )
    report = _order_report(
        raw,
        report_id="BOR-shared-ack",
        status=BrokerOrderState.ACKNOWLEDGED,
        filled="0",
        remaining="1",
        broker_order_id="FUBON-SHARED-1",
        intent_id=intent.intent_id,
    )

    applied = second.apply_order_report(report, raw_event=raw)
    assert applied.state == BrokerOrderState.ACKNOWLEDGED
    assert first.apply_order_report(report, raw_event=raw).state == (
        BrokerOrderState.ACKNOWLEDGED
    )
    assert first.status(intent.intent_id).broker_order_id == "FUBON-SHARED-1"


@pytest.mark.parametrize(
    "crash_state",
    [
        BrokerOrderState.CREATED,
        BrokerOrderState.VALIDATING,
        BrokerOrderState.RISK_PENDING,
        BrokerOrderState.USER_APPROVAL_PENDING,
        BrokerOrderState.SUBMITTING,
        BrokerOrderState.ACKNOWLEDGED,
        BrokerOrderState.PARTIALLY_FILLED,
        BrokerOrderState.CANCEL_PENDING,
        BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED,
    ],
)
def test_durable_oms_restart_blocks_retry_for_every_nonterminal_lifecycle_state(
    tmp_path,
    crash_state: BrokerOrderState,
) -> None:
    store = BrokerOMSStore(tmp_path / f"crash-{crash_state.value}.sqlite")
    first = BrokerOrderManagementGateway(store=store)
    intent = _intent(
        intent_id=f"BOI-crash-{crash_state.value}",
        idempotency_key=f"broker-crash-recovery-{crash_state.value.lower()}",
    )
    entry = first.register(intent)
    entry.state = crash_state
    entry.updated_at = datetime.now(timezone.utc)
    first._persist(entry)

    reopened = BrokerOrderManagementGateway(store=BrokerOMSStore(store.path))

    assert reopened.latest_recovery_receipt is not None
    recovery = reopened.latest_recovery_receipt
    assert recovery.marked_unknown_intent_ids == (intent.intent_id,)
    assert recovery.open_orders_before_recovery[0]["state"] == crash_state.value
    assert reopened.status(intent.intent_id).state == (
        BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED
    )
    reopened.clear_kill_switch_for_sandbox()
    with pytest.raises(BrokerReconciliationRequired, match="reconcile"):
        asyncio.run(reopened.submit(intent.model_copy()))


def test_oms_commits_submission_barrier_before_gateway_call_and_recovers_after_crash(
    tmp_path,
) -> None:
    store = BrokerOMSStore(tmp_path / "write-ahead.sqlite")
    intent = _intent(
        intent_id="BOI-write-ahead",
        idempotency_key="broker-write-ahead-crash-0001",
    )

    class InterruptedGateway:
        observed_state: BrokerOrderState | None = None

        async def place_order(self, submitted: BrokerOrderIntent) -> BrokerOrderReceipt:
            persisted = BrokerOMSStore(store.path).entries()
            assert submitted.intent_id == intent.intent_id
            assert len(persisted) == 1
            self.observed_state = persisted[0].state
            raise RuntimeError("simulated process interruption after gateway dispatch")

    gateway = InterruptedGateway()
    oms = BrokerOrderManagementGateway(gateway=gateway, store=store)
    oms.clear_kill_switch_for_sandbox()
    risk = CentralBrokerRiskGate().evaluate(
        intent,
        account_exists=True,
        api_permission_enabled=True,
        certificate_valid=True,
        market_data_fresh=True,
        broker_healthy=True,
        instrument_tradable=True,
        buying_power=Decimal("100000"),
        estimated_notional=Decimal("1000"),
        duplicate_order_exists=False,
        kill_switch_enabled=False,
        live_trading_enabled=False,
    )
    assert risk.approved is True

    with pytest.raises(BrokerReconciliationRequired, match="outcome is not proven"):
        asyncio.run(oms.submit(intent, risk_decision=risk))

    assert gateway.observed_state == BrokerOrderState.SUBMITTING
    assert BrokerOMSStore(store.path).entries()[0].state == (
        BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED
    )

    reopened = BrokerOrderManagementGateway(store=BrokerOMSStore(store.path))
    assert reopened.latest_recovery_receipt is not None
    assert reopened.latest_recovery_receipt.open_orders_before_recovery[0]["state"] == (
        BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED.value
    )
    reopened.clear_kill_switch_for_sandbox()
    with pytest.raises(BrokerReconciliationRequired, match="reconcile"):
        asyncio.run(reopened.submit(intent.model_copy()))


def test_central_risk_gate_and_gateway_keep_live_trading_closed() -> None:
    intent = _intent(environment="live")
    risk = CentralBrokerRiskGate().evaluate(
        intent,
        account_exists=True,
        api_permission_enabled=True,
        certificate_valid=True,
        market_data_fresh=True,
        broker_healthy=True,
        instrument_tradable=True,
        buying_power=Decimal("100000"),
        estimated_notional=Decimal("1000"),
        duplicate_order_exists=False,
        kill_switch_enabled=True,
        live_trading_enabled=False,
    )
    assert risk.approved is False
    assert {"kill_switch_clear", "environment_allowed"} <= set(risk.reasons)

    oms = BrokerOrderManagementGateway()
    oms.clear_kill_switch_for_sandbox()
    with pytest.raises(BrokerLiveTradingDisabled):
        asyncio.run(oms.gateway.place_order(intent))


def test_live_pretrade_gate_requires_bounded_host_context_before_approval() -> None:
    intent = _intent(environment="live")
    common = {
        "account_exists": True,
        "api_permission_enabled": True,
        "certificate_valid": True,
        "market_data_fresh": True,
        "broker_healthy": True,
        "instrument_tradable": True,
        "buying_power": Decimal("100000"),
        "estimated_notional": Decimal("1000"),
        "duplicate_order_exists": False,
        "kill_switch_enabled": False,
        "live_trading_enabled": True,
    }

    missing = CentralBrokerRiskGate().evaluate(intent, **common)
    assert missing.approved is False
    assert missing.reasons == ["pretrade_context_present"]

    approved = CentralBrokerRiskGate().evaluate(intent, pretrade=_pretrade(), **common)
    assert approved.approved is True
    assert {
        "quote_within_freshness_window",
        "market_phase_allowed",
        "order_quantity_within_limit",
        "order_notional_within_limit",
        "price_within_collar",
        "adv_participation_within_limit",
        "symbol_exposure_within_limit",
        "account_exposure_within_limit",
        "daily_loss_within_limit",
        "strategy_execution_authorized",
        "model_execution_eligible",
    } <= set(approved.checks)


def test_live_pretrade_gate_rejects_adversarial_price_size_exposure_and_loss() -> None:
    intent = _intent(environment="live").model_copy(
        update={"quantity": Decimal("200"), "limit_price": Decimal("1200")}
    )
    risk = CentralBrokerRiskGate().evaluate(
        intent,
        account_exists=True,
        api_permission_enabled=True,
        certificate_valid=True,
        market_data_fresh=True,
        broker_healthy=True,
        instrument_tradable=True,
        buying_power=Decimal("500000"),
        estimated_notional=Decimal("240000"),
        duplicate_order_exists=False,
        kill_switch_enabled=False,
        live_trading_enabled=True,
        pretrade=_pretrade(
            quote_observed_at=datetime.now(timezone.utc) - timedelta(seconds=31),
            market_phase="closed",
            max_quantity=Decimal("100"),
            max_order_notional=Decimal("50000"),
            adv_quantity=Decimal("1000"),
            max_adv_participation_pct=Decimal("1"),
            current_symbol_notional=Decimal("25000"),
            max_symbol_notional=Decimal("30000"),
            current_account_notional=Decimal("90000"),
            max_account_notional=Decimal("100000"),
            daily_loss_notional=Decimal("5001"),
            strategy_execution_authorized=False,
            model_execution_eligible=False,
        ),
    )

    assert risk.approved is False
    assert {
        "quote_within_freshness_window",
        "market_phase_allowed",
        "order_quantity_within_limit",
        "order_notional_within_limit",
        "price_within_collar",
        "adv_participation_within_limit",
        "symbol_exposure_within_limit",
        "account_exposure_within_limit",
        "daily_loss_within_limit",
        "strategy_execution_authorized",
        "model_execution_eligible",
    } <= set(risk.reasons)


def test_live_pretrade_gate_requires_confirmed_sellable_inventory() -> None:
    intent = _intent(environment="live").model_copy(update={"side": "sell"})
    common = {
        "account_exists": True,
        "api_permission_enabled": True,
        "certificate_valid": True,
        "market_data_fresh": True,
        "broker_healthy": True,
        "instrument_tradable": True,
        "buying_power": Decimal("100000"),
        "estimated_notional": Decimal("1000"),
        "duplicate_order_exists": False,
        "kill_switch_enabled": False,
        "live_trading_enabled": True,
    }

    unknown = CentralBrokerRiskGate().evaluate(intent, pretrade=_pretrade(), **common)
    assert unknown.approved is False
    assert {"sellable_quantity_known", "sellable_quantity_sufficient"} <= set(unknown.reasons)

    approved = CentralBrokerRiskGate().evaluate(
        intent,
        pretrade=_pretrade(sellable_quantity=Decimal("1")),
        **common,
    )
    assert approved.approved is True


def test_live_pretrade_receipt_binds_exact_intent_context_and_calculations() -> None:
    intent = _intent(environment="live")
    evaluated_at = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    risk = CentralBrokerRiskGate().evaluate(
        intent,
        account_exists=True,
        api_permission_enabled=True,
        certificate_valid=True,
        market_data_fresh=True,
        broker_healthy=True,
        instrument_tradable=True,
        buying_power=Decimal("100000"),
        estimated_notional=Decimal("1000"),
        duplicate_order_exists=False,
        kill_switch_enabled=False,
        live_trading_enabled=True,
        pretrade=_pretrade(quote_observed_at=evaluated_at - timedelta(seconds=5)),
        evaluated_at=evaluated_at,
    )

    assert risk.approved is True
    assert risk.pretrade_receipt is not None
    assert risk.verify_pretrade_receipt(intent) is True
    assert risk.pretrade_receipt.observed == {
        "quote_age_seconds": "5",
        "expected_price": "1000",
        "estimated_notional": "1000",
        "price_deviation_bps": "0",
        "adv_participation_pct": "0.01",
        "proposed_symbol_notional": "11000",
        "proposed_account_notional": "41000",
    }

    changed_intent = intent.model_copy(update={"quantity": Decimal("2")})
    assert risk.verify_pretrade_receipt(changed_intent) is False
    tampered_receipt = risk.pretrade_receipt.model_copy(
        update={"observed": {**risk.pretrade_receipt.observed, "estimated_notional": "1"}}
    )
    assert risk.model_copy(update={"pretrade_receipt": tampered_receipt}).verify_pretrade_receipt(intent) is False


@pytest.mark.parametrize(
    ("check_name", "intent_update", "pretrade_overrides"),
    [
        ("quote_within_freshness_window", {}, {"quote_observed_at": datetime.now(timezone.utc) - timedelta(seconds=31)}),
        ("market_phase_allowed", {}, {"market_phase": "closed"}),
        ("order_quantity_within_limit", {"quantity": Decimal("101")}, {}),
        ("order_notional_within_limit", {"quantity": Decimal("51")}, {}),
        ("price_within_collar", {"limit_price": Decimal("1020")}, {}),
        ("adv_participation_within_limit", {"quantity": Decimal("201")}, {"max_quantity": Decimal("300")}),
        ("symbol_exposure_within_limit", {}, {"current_symbol_notional": Decimal("30000")}),
        ("account_exposure_within_limit", {}, {"current_account_notional": Decimal("100000")}),
        ("daily_loss_within_limit", {}, {"daily_loss_notional": Decimal("5001")}),
        ("strategy_execution_authorized", {}, {"strategy_execution_authorized": False}),
        ("model_execution_eligible", {}, {"model_execution_eligible": False}),
        ("sellable_quantity_sufficient", {"side": "sell"}, {"sellable_quantity": Decimal("0")}),
    ],
)
def test_live_pretrade_adversarial_matrix_fails_each_bounded_check(
    check_name: str,
    intent_update: dict,
    pretrade_overrides: dict,
) -> None:
    intent = _intent(environment="live").model_copy(update=intent_update)
    risk = CentralBrokerRiskGate().evaluate(
        intent,
        account_exists=True,
        api_permission_enabled=True,
        certificate_valid=True,
        market_data_fresh=True,
        broker_healthy=True,
        instrument_tradable=True,
        buying_power=Decimal("500000"),
        estimated_notional=Decimal("1000"),
        duplicate_order_exists=False,
        kill_switch_enabled=False,
        live_trading_enabled=True,
        pretrade=_pretrade(**pretrade_overrides),
    )

    assert risk.approved is False
    assert risk.checks[check_name] is False
    assert check_name in risk.reasons
    assert risk.pretrade_receipt is not None
    assert risk.verify_pretrade_receipt(intent) is True


def test_live_oms_requires_host_signed_activation_and_human_receipts(tmp_path) -> None:
    class AcceptingGateway:
        def __init__(self) -> None:
            self.submissions = 0

        async def place_order(self, intent: BrokerOrderIntent) -> BrokerOrderReceipt:
            self.submissions += 1
            return BrokerOrderReceipt(
                intent_id=intent.intent_id,
                broker_id=intent.broker_id,
                broker_order_id="FUBON-HUMAN-APPROVED-1",
                submitted_at=datetime.now(timezone.utc),
                status=BrokerOrderState.ACKNOWLEDGED,
                accepted_quantity=intent.quantity,
                raw_receipt_hash="d" * 64,
            )

    intent = _intent(environment="live")
    authority = BrokerHumanApprovalAuthority(b"host-only-human-approval-key-material-32b")
    activation_authority = BrokerRestrictedLiveActivationAuthority(
        b"host-only-restricted-live-activation-key-material-32b"
    )
    gateway = AcceptingGateway()
    store = BrokerOMSStore(tmp_path / "live-approval.sqlite")
    oms = BrokerOrderManagementGateway(
        gateway=gateway,
        store=store,
        human_approval_authority=authority,
        live_activation_authority=activation_authority,
    )
    oms.clear_kill_switch_for_sandbox()
    risk = _approved_live_risk(intent)
    assert risk.approved is True

    tampered_pretrade = risk.pretrade_receipt.model_copy(
        update={"receipt_sha256": "0" * 64}
    )
    with pytest.raises(PermissionError, match="intact Host pre-trade risk receipt"):
        asyncio.run(
            oms.submit(
                intent,
                risk_decision=risk.model_copy(update={"pretrade_receipt": tampered_pretrade}),
                human_approval_receipt=authority.issue(intent, user_requested=True),
            )
        )
    assert store.pretrade_risk_receipts() == []

    # An Agent proposal's `user_approved=True` cannot activate live access.
    with pytest.raises(PermissionError, match="Host restricted-live activation receipt"):
        asyncio.run(oms.submit(intent, risk_decision=risk))
    assert gateway.submissions == 0

    with pytest.raises(PermissionError, match="explicit user confirmation"):
        _restricted_live_activation(activation_authority, user_requested=False)
    activation = _restricted_live_activation(activation_authority)
    forged_activation = activation.model_copy(update={"signature": "0" * 64})
    with pytest.raises(PermissionError, match="activation receipt signature is invalid"):
        asyncio.run(
            oms.submit(
                intent,
                risk_decision=risk,
                live_activation_receipt=forged_activation,
            )
        )
    assert gateway.submissions == 0

    # An activation receipt is necessary but still never substitutes for the
    # per-order Host human receipt.
    with pytest.raises(PermissionError, match="Host-signed human approval receipt"):
        asyncio.run(
            oms.submit(
                intent,
                risk_decision=risk,
                live_activation_receipt=activation,
            )
        )
    assert gateway.submissions == 0

    receipt = authority.issue(intent, user_requested=True)
    forged = receipt.model_copy(update={"signature": "0" * 64})
    with pytest.raises(PermissionError, match="signature is invalid"):
        asyncio.run(
            oms.submit(
                intent,
                risk_decision=risk,
                human_approval_receipt=forged,
                live_activation_receipt=activation,
            )
        )
    assert gateway.submissions == 0

    submitted = asyncio.run(
        oms.submit(
            intent,
            risk_decision=risk,
            human_approval_receipt=receipt,
            live_activation_receipt=activation,
        )
    )
    assert submitted.state == BrokerOrderState.ACKNOWLEDGED
    assert gateway.submissions == 1
    persisted = BrokerOMSStore(store.path).entries()[0]
    assert persisted.human_approval_receipt is not None
    assert persisted.human_approval_receipt.receipt_id == receipt.receipt_id
    risk_receipts = BrokerOMSStore(store.path).pretrade_risk_receipts()
    assert len(risk_receipts) == 1
    assert risk_receipts[0].intent_id == intent.intent_id
    assert risk_receipts[0].receipt == risk.pretrade_receipt
    activation_uses = BrokerOMSStore(store.path).restricted_live_activation_uses()
    assert len(activation_uses) == 1
    assert activation_uses[0].intent_id == intent.intent_id
    assert activation_uses[0].receipt == activation
    assert activation_uses[0].notional == Decimal("1000")
    with BrokerOMSStore(store.path)._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "update broker_pretrade_risk_receipts set receipt_json='{}'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "update broker_restricted_live_activation_uses set notional='0'"
            )

    reopened = BrokerOrderManagementGateway(
        store=BrokerOMSStore(store.path),
        human_approval_authority=authority,
        live_activation_authority=activation_authority,
    )
    assert reopened.status(intent.intent_id).human_approval_receipt == receipt
    assert reopened.status(intent.intent_id).state == BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED


def test_human_approval_receipt_is_expiring_and_bound_to_exact_order() -> None:
    intent = _intent(environment="live")
    authority = BrokerHumanApprovalAuthority(b"host-only-human-approval-key-material-32b")
    issued_at = datetime.now(timezone.utc)

    with pytest.raises(PermissionError, match="explicit user confirmation"):
        authority.issue(intent, user_requested=False)

    receipt = authority.issue(intent, user_requested=True, issued_at=issued_at)
    changed_order = intent.model_copy(update={"quantity": Decimal("2")})
    with pytest.raises(PermissionError, match="does not match the submitted order"):
        authority.verify(changed_order, receipt, verified_at=issued_at + timedelta(seconds=1))

    expired = authority.issue(
        intent,
        user_requested=True,
        ttl_seconds=1,
        issued_at=issued_at,
    )
    with pytest.raises(PermissionError, match="has expired"):
        authority.verify(intent, expired, verified_at=issued_at + timedelta(seconds=2))


@pytest.mark.parametrize(
    ("intent_update", "daily_notional", "error"),
    [
        ({"side": "sell"}, Decimal("0"), "long-only"),
        ({"instrument_id": "TPEX:6488"}, Decimal("0"), "allowlist"),
        ({"quantity": Decimal("2")}, Decimal("0"), "notional limit"),
        ({}, Decimal("2000"), "daily notional limit"),
    ],
)
def test_restricted_live_activation_receipt_rejects_out_of_policy_orders(
    intent_update: dict,
    daily_notional: Decimal,
    error: str,
) -> None:
    authority = BrokerRestrictedLiveActivationAuthority(
        b"host-only-restricted-live-activation-key-material-32b"
    )
    receipt = _restricted_live_activation(authority)
    intent = _intent(environment="live").model_copy(update=intent_update)

    with pytest.raises(PermissionError, match=error):
        authority.verify(
            intent,
            receipt,
            daily_notional_before_order=daily_notional,
        )


def test_oms_requires_matching_fresh_host_risk_approval() -> None:
    intent = _intent()
    oms = BrokerOrderManagementGateway()
    oms.clear_kill_switch_for_sandbox()

    with pytest.raises(PermissionError, match="risk approval is required"):
        asyncio.run(oms.submit(intent))

    approved = CentralBrokerRiskGate().evaluate(
        intent,
        account_exists=True,
        api_permission_enabled=True,
        certificate_valid=True,
        market_data_fresh=True,
        broker_healthy=True,
        instrument_tradable=True,
        buying_power=Decimal("100000"),
        estimated_notional=Decimal("1000"),
        duplicate_order_exists=False,
        kill_switch_enabled=False,
        live_trading_enabled=False,
    )
    assert approved.approved is True
    mismatched = approved.model_copy(update={"risk_approval_id": "RISK-other"})
    with pytest.raises(PermissionError, match="does not match"):
        asyncio.run(oms.submit(intent, risk_decision=mismatched))


def test_broker_monitor_warns_on_feed_quality_and_stops_on_unknown_orders() -> None:
    oms = BrokerOrderManagementGateway()
    oms.clear_kill_switch_for_sandbox()
    monitor = BrokerObservabilityMonitor(oms, market_latency_warning_ms=1000)

    warnings = monitor.evaluate(
        [
            BrokerRuntimeMetrics(
                broker_id="fubon",
                login_connected=True,
                websocket_connected=True,
                market_latency_ms=1501,
                sequence_gap_count=2,
                observed_at=datetime.now(timezone.utc),
            )
        ]
    )

    assert [item.severity for item in warnings] == ["WARNING"]
    assert oms.kill_switch_enabled is False

    critical = monitor.evaluate(
        [
            BrokerRuntimeMetrics(
                broker_id="fubon",
                login_connected=True,
                unknown_order_count=1,
                observed_at=datetime.now(timezone.utc),
            )
        ]
    )

    assert [item.severity for item in critical] == ["CRITICAL"]
    assert critical[0].stop_new_orders is True
    assert oms.kill_switch_enabled is True


def test_broker_monitor_emergency_activates_global_kill_switch() -> None:
    oms = BrokerOrderManagementGateway()
    oms.clear_kill_switch_for_sandbox()
    monitor = BrokerObservabilityMonitor(oms)

    alerts = monitor.evaluate(
        [
            BrokerRuntimeMetrics(
                broker_id="taishin",
                login_connected=False,
                observed_at=datetime.now(timezone.utc),
            ),
            BrokerRuntimeMetrics(
                broker_id="fubon",
                login_connected=False,
                observed_at=datetime.now(timezone.utc),
            ),
        ]
    )

    assert alerts[0].severity == "EMERGENCY"
    assert alerts[0].kill_switch_activated is True
    assert oms.kill_switch_enabled is True


def test_human_authorization_does_not_open_browser_without_explicit_request(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: opened.append(url) or True)
    workflow = HumanAuthorizationWorkflow()

    result = asyncio.run(workflow.launch_official_page("fubon", user_requested=False))

    assert result["opened"] is False
    assert result["reason"] == "explicit_user_action_required"
    assert opened == []


def test_broker_api_exposes_readonly_status_without_secret_fields() -> None:
    app = FastAPI()
    app.include_router(broker_router)
    client = TestClient(app)

    responses = [
        client.get(path)
        for _ in range(3)
        for path in (
            "/api/brokers/connections",
            "/api/brokers/capabilities",
            "/api/brokers/health",
            "/api/brokers/runtime",
        )
    ]
    assert all(response.status_code == 200 for response in responses)

    connections = responses[0]
    capabilities = responses[1]
    health = responses[2]
    runtime = responses[3]

    payload = connections.json()
    assert payload["live_trading_enabled"] is False
    assert len(payload["brokers"]) == 5
    assert "secret" not in str(payload).casefold()
    workers = health.json()["workers"]
    assert all(item["process_running"] is False for item in workers)
    assert all(item["process_isolated"] is False for item in workers)
    assert all(item["sdk_installed"] is False for item in workers)
    assert runtime.json()["metrics"] == []
    assert runtime.json()["selected_sources"] == {}


def test_taishin_readonly_onboarding_api_is_explicitly_non_trading() -> None:
    app = FastAPI()
    app.include_router(broker_router)
    client = TestClient(app)

    response = client.get("/api/brokers/taishin/readonly-onboarding")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "stock_ai.taishin_readonly_onboarding.v1"
    assert payload["broker_id"] == "taishin"
    assert payload["integration_scope"] == "read_only_market_data"
    assert payload["live_trading_enabled"] is False
    assert payload["order_submission_enabled"] is False
    assert payload["account_snapshot_enabled"] is False
    assert payload["secret_entry_allowed_in_stock_ai"] is False
    assert payload["preferred_sdk_language"] == "python"
    assert payload["official_home_url"].startswith("https://mlapi.tssco.com.tw/")
    assert [step["step_id"] for step in payload["steps"]] == [
        "account_owner_authorization",
        "official_sdk_provenance",
        "secure_reference_setup",
        "isolated_quote_probe",
        "receipt_review",
    ]
    serialized = str(payload).casefold()
    assert "password" not in serialized
    assert "'otp':" not in serialized
    assert "'secret':" not in serialized
    assert "'certificate':" not in serialized


def test_broker_authorization_endpoint_requires_explicit_click(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: opened.append(url) or True)
    app = FastAPI()
    app.include_router(broker_router)
    client = TestClient(app)

    denied = client.post(
        "/api/brokers/fubon/authorization/open",
        json={"user_requested": False},
    )
    allowed = client.post(
        "/api/brokers/fubon/authorization/open",
        json={"user_requested": True},
    )

    assert denied.status_code == 409
    assert opened == ["https://www.fbs.com.tw/TradeAPI/docs/trading/prepare/"]
    assert allowed.status_code == 200
    assert allowed.json()["checkpoint"] == "waiting_for_account_owner"


def test_broker_settings_ui_lists_five_brokers_and_never_adds_direct_submit() -> None:
    static = Path(__file__).parents[1] / "src" / "stock_ai" / "ui" / "static"
    markup = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "js" / "features" / "broker-gateway.js").read_text(
        encoding="utf-8"
    )

    assert "五券商 API 與人工授權" in markup
    assert "真實交易保持關閉" in markup
    for name in ("台新證券", "富邦證券", "永豐金證券", "元大證券", "元富證券"):
        assert name in script
    assert "user_requested: true" in script
    assert "direct_submit" not in script
    assert "Feed／延遲" in script
    assert "查看台新唯讀接入步驟" in script
    assert "/api/brokers/taishin/readonly-onboarding" in script
    assert "不讀取帳務、不建立委託、不送單" in script


def test_runtime_status_never_fabricates_unobserved_feeds_or_latency() -> None:
    registry = BrokerRuntimeStatusRegistry()
    empty = registry.snapshot()

    assert empty.metrics == []
    assert empty.selected_sources == {}

    observed = BrokerRuntimeMetrics(
        broker_id="fubon",
        login_connected=True,
        websocket_connected=True,
        market_latency_ms=25,
        observed_at=datetime.now(timezone.utc),
    )
    registry.record_metrics(observed)
    snapshot = registry.snapshot()

    assert snapshot.metrics == [observed]
    assert snapshot.metrics[0].market_latency_ms == 25


def _order_report(
    raw: BrokerRawEvent,
    *,
    report_id: str,
    status: BrokerOrderState,
    filled: str,
    remaining: str,
    intent_id: str = "BOI-one",
    broker_id: str = "fubon",
    account_alias: str = "paper-validation",
    broker_order_id: str = "FUBON-ORDER-1",
) -> BrokerOrderReport:
    now = datetime.now(timezone.utc)
    return BrokerOrderReport(
        report_id=report_id,
        intent_id=intent_id,
        broker_id=broker_id,
        account_alias=account_alias,
        broker_order_id=broker_order_id,
        event_at=now,
        received_at=now,
        status=status,
        filled_quantity=Decimal(filled),
        remaining_quantity=Decimal(remaining),
        rejected_reason="broker rejected order" if status == BrokerOrderState.REJECTED else None,
        raw_payload_hash=raw.payload_hash,
    )


def test_order_reports_drive_partial_fill_to_fill_and_retain_raw_evidence() -> None:
    oms = BrokerOrderManagementGateway()
    entry = oms.register(_intent())
    entry.state = BrokerOrderState.SUBMITTING
    raw_ack = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"order_id": "FUBON-ORDER-1", "status": "ack"},
    )
    raw_partial = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"order_id": "FUBON-ORDER-1", "status": "partial"},
    )
    raw_filled = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"order_id": "FUBON-ORDER-1", "status": "filled"},
    )

    oms.apply_order_report(
        _order_report(
            raw_ack,
            report_id="BOR-ack",
            status=BrokerOrderState.ACKNOWLEDGED,
            filled="0",
            remaining="1",
        ),
        raw_event=raw_ack,
    )
    oms.apply_order_report(
        _order_report(
            raw_partial,
            report_id="BOR-partial",
            status=BrokerOrderState.PARTIALLY_FILLED,
            filled="0.5",
            remaining="0.5",
        ),
        raw_event=raw_partial,
    )
    final = oms.apply_order_report(
        _order_report(
            raw_filled,
            report_id="BOR-filled",
            status=BrokerOrderState.FILLED,
            filled="1",
            remaining="0",
        ),
        raw_event=raw_filled,
    )

    assert final.state == BrokerOrderState.FILLED
    assert final.filled_quantity == Decimal("1")
    assert final.remaining_quantity == 0
    assert final.report_ids == ["BOR-ack", "BOR-partial", "BOR-filled"]
    assert oms.event_bus.raw_event(raw_partial.payload_hash) == raw_partial
    assert [item.status for item in oms.event_bus.order_reports()] == [
        BrokerOrderState.ACKNOWLEDGED,
        BrokerOrderState.PARTIALLY_FILLED,
        BrokerOrderState.FILLED,
    ]


def test_order_report_isolation_deduplication_and_quantity_guards() -> None:
    oms = BrokerOrderManagementGateway()
    entry = oms.register(_intent())
    entry.state = BrokerOrderState.SUBMITTING
    raw = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"order_id": "FUBON-ORDER-1"},
    )
    report = _order_report(
        raw,
        report_id="BOR-dedupe",
        status=BrokerOrderState.ACKNOWLEDGED,
        filled="0",
        remaining="1",
    )

    first = oms.apply_order_report(report, raw_event=raw)
    assert oms.apply_order_report(report, raw_event=raw) is first
    with pytest.raises(ValueError, match="another Host intent"):
        oms.apply_order_report(
            report.model_copy(update={"intent_id": "BOI-other"}),
            raw_event=raw,
        )
    with pytest.raises(ValueError, match="account boundaries"):
        oms.apply_order_report(
            _order_report(
                raw,
                report_id="BOR-wrong-account",
                status=BrokerOrderState.ACKNOWLEDGED,
                filled="0",
                remaining="1",
                account_alias="another-account",
            ),
            raw_event=raw,
        )


def test_order_reconciliation_marks_missing_open_orders_unknown() -> None:
    oms = BrokerOrderManagementGateway()
    entry = oms.register(_intent())
    entry.state = BrokerOrderState.ACKNOWLEDGED
    raw = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"orders": []},
    )
    result = oms.reconcile_snapshot(
        BrokerOrderReconciliationSnapshot(
            broker_id="fubon",
            account_alias="paper-validation",
            as_of=datetime.now(timezone.utc),
            reports=[],
            raw_payload_hash=raw.payload_hash,
        ),
        raw_event=raw,
    )

    assert result["marked_unknown_intent_ids"] == ["BOI-one"]
    assert result["trading_allowed"] is False
    assert oms.status("BOI-one").state == BrokerOrderState.UNKNOWN_RECONCILIATION_REQUIRED


def _account_snapshot(*, cash: str | None = "100000") -> BrokerAccountSnapshot:
    now = datetime.now(timezone.utc)
    return BrokerAccountSnapshot(
        broker_id="fubon",
        account_id_masked="12****78",
        account_type="stock",
        currency="TWD",
        available_cash=Decimal(cash) if cash is not None else None,
        settlement_due=Decimal("2500"),
        positions=[
            BrokerPosition(
                instrument_id="TWSE:2330",
                quantity=Decimal("1000"),
                position_type="cash",
            )
        ],
        open_orders=[
            BrokerOpenOrder(
                broker_order_id_masked="ORD-****-1",
                instrument_id="TWSE:2330",
                side="buy",
                quantity=Decimal("100"),
                remaining_quantity=Decimal("100"),
                status="acknowledged",
                submitted_at=now,
            )
        ],
        fills=[
            BrokerFill(
                broker_fill_id_masked="FILL-****-1",
                broker_order_id_masked="ORD-****-0",
                instrument_id="TWSE:2330",
                side="buy",
                quantity=Decimal("100"),
                price=Decimal("1000"),
                fee=Decimal("20"),
                tax=Decimal("0"),
                filled_at=now,
            )
        ],
        settlements=[
            BrokerSettlementAmount(
                settlement_date="2026-07-28",
                currency="TWD",
                receivable=Decimal("0"),
                payable=Decimal("2500"),
            )
        ],
        as_of=now,
    )


def _host_ledger(snapshot: BrokerAccountSnapshot) -> HostAccountLedger:
    return HostAccountLedger(
        broker_id=snapshot.broker_id,
        account_id_masked=snapshot.account_id_masked,
        account_type=snapshot.account_type,
        currency=snapshot.currency,
        available_cash=snapshot.available_cash,
        settlement_due=snapshot.settlement_due,
        positions=snapshot.positions,
        open_orders=snapshot.open_orders,
        fills=snapshot.fills,
        settlements=snapshot.settlements,
        as_of=snapshot.as_of,
    )


def test_account_reconciliation_never_infers_missing_values_and_isolates_accounts() -> None:
    engine = BrokerAccountReconciliationEngine()
    exact = _account_snapshot()

    matched = engine.reconcile(exact, _host_ledger(exact))
    assert matched.status == "matched"
    assert matched.trading_allowed is True

    host_mismatch = _host_ledger(exact).model_copy(
        update={"available_cash": Decimal("99999")}
    )
    mismatch = engine.reconcile(exact, host_mismatch)
    assert mismatch.status == "mismatch"
    assert mismatch.trading_allowed is False

    missing = _account_snapshot(cash=None)
    incomplete = engine.reconcile(missing, _host_ledger(missing))
    assert incomplete.status == "incomplete"
    assert "available_cash" in incomplete.unverified_fields
    assert incomplete.trading_allowed is False

    with pytest.raises(ValueError, match="different brokers"):
        engine.reconcile(
            exact,
            _host_ledger(exact).model_copy(update={"broker_id": "taishin"}),
        )
    with pytest.raises(ValueError, match="account types"):
        engine.reconcile(
            exact,
            _host_ledger(exact).model_copy(update={"account_type": "futures"}),
        )


def test_feed_score_requires_observed_essentials_and_source_switches_are_audited() -> None:
    now = datetime.now(timezone.utc)
    engine = BrokerFeedScoreEngine()
    incomplete = engine.score(
        BrokerFeedMetrics(
            broker_id="fubon",
            connection_uptime_ratio=1,
            observed_at=now,
        )
    )
    complete = engine.score(
        BrokerFeedMetrics(
            broker_id="fubon",
            connection_uptime_ratio=0.999,
            message_latency_ms=50,
            sequence_gap_rate=0,
            duplicate_rate=0,
            parse_error_rate=0,
            reconnection_count=0,
            quote_staleness_ms=30,
            cross_broker_agreement=1,
            rate_limit_remaining_ratio=0.8,
            observed_at=now,
        )
    )

    assert incomplete.score is None
    assert incomplete.eligible_for_selection is False
    assert complete.score is not None
    assert complete.eligible_for_selection is True

    result = MarketDataReconciliationEngine().reconcile(
        [
            _event(
                "fubon",
                price="1000",
                exchange_time="2026-07-26T01:00:00+00:00",
                sequence=10,
                received_time="2026-07-26T01:00:00.020000+00:00",
            )
        ]
    )
    ledger = BrokerSourceSelectionLedger()
    record = ledger.observe(
        result,
        market_session="regular_lot",
        feed_scores={"fubon": complete.score},
    )
    assert record is not None
    assert record.selected_broker_id == "fubon"
    assert ledger.observe(
        result,
        market_session="regular_lot",
        feed_scores={"fubon": complete.score},
    ) is None


def test_sequence_tracker_requires_recovery_after_gap_and_never_mixes_sessions() -> None:
    tracker = BrokerSequenceTracker()
    first = _event(
        "fubon",
        price="1000",
        exchange_time="2026-07-26T01:00:00+00:00",
        sequence=10,
        received_time="2026-07-26T01:00:00.010000+00:00",
    )
    gap = first.model_copy(update={"sequence": 13})

    assert tracker.observe(first).classification == "initial"
    observed_gap = tracker.observe(gap)
    assert observed_gap.classification == "gap"
    assert (observed_gap.missing_from, observed_gap.missing_to) == (11, 12)
    assert observed_gap.requires_snapshot_recovery is True
    assert tracker.has_unresolved_gap(
        broker_id="fubon",
        instrument_id="TWSE:2330",
        market_session="regular_lot",
        channel="trade",
    )
    tracker.mark_snapshot_recovered(
        broker_id="fubon",
        instrument_id="TWSE:2330",
        market_session="regular_lot",
        channel="trade",
        snapshot_sequence=13,
    )
    assert tracker.observe(gap.model_copy(update={"sequence": 14})).trading_allowed is True
    other_session = first.model_copy(update={"market_session": "odd_lot", "sequence": 1})
    assert tracker.observe(other_session).classification == "initial"


def test_subscription_limits_are_per_broker_and_duplicate_add_is_idempotent() -> None:
    manager = BrokerSubscriptionManager()
    fubon = BrokerSubscription("fubon", "TWSE:2330", "trade", "regular_lot")
    taishin = BrokerSubscription("taishin", "TWSE:2330", "trade", "regular_lot")

    manager.add(fubon, maximum=1)
    manager.add(fubon, maximum=1)
    manager.add(taishin, maximum=1)

    assert manager.list("fubon") == [fubon]
    assert manager.list("taishin") == [taishin]
    with pytest.raises(RuntimeError, match="limit reached"):
        manager.add(
            BrokerSubscription("fubon", "TWSE:2317", "trade", "regular_lot"),
            maximum=1,
        )


def test_worker_five_class_protocol_requires_host_receipts_and_never_direct_submits() -> None:
    with pytest.raises(ValidationError, match="Host authorization receipt"):
        BrokerWorkerRequest(
            broker_id="fubon",
            operation=BrokerWorkerOperation.ACCOUNT,
            action="snapshot",
            arguments={"account_alias": "main"},
        )
    with pytest.raises(ValidationError, match="secure-store references"):
        BrokerWorkerRequest(
            broker_id="fubon",
            operation=BrokerWorkerOperation.LOGIN_READONLY,
            action="login",
            secret_reference="plaintext-api-key",
            human_authorization_receipt_id="HAR-owner-approved",
        )
    with pytest.raises(ValidationError, match="must not be placed"):
        BrokerWorkerRequest(
            broker_id="fubon",
            operation=BrokerWorkerOperation.LOGIN_READONLY,
            action="login",
            arguments={"nested": {"api_key": "must-never-cross-ipc"}},
            secret_reference="keychain://stock-ai/fubon/main",
            human_authorization_receipt_id="HAR-owner-approved",
        )

    interface = BrokerWorkerInterface()
    health = asyncio.run(
        interface.execute(
            BrokerWorkerRequest(
                broker_id="fubon",
                operation=BrokerWorkerOperation.HEALTH,
                action="read",
            )
        )
    )
    direct_submit = asyncio.run(
        interface.execute(
            BrokerWorkerRequest(
                broker_id="fubon",
                operation=BrokerWorkerOperation.ORDER,
                action="place",
                human_authorization_receipt_id="HAR-owner-approved",
            )
        )
    )

    assert health.completed is True
    assert health.result["broker_id"] == "fubon"
    assert direct_submit.completed is False
    assert direct_submit.error_message == (
        "order operation failed inside the Host-isolated Broker Worker"
    )


def test_requirement_matrix_tracks_all_tests_without_false_real_acceptance_claims() -> None:
    root = Path(__file__).parents[1]
    status = yaml.safe_load(
        (root / "config" / "broker_requirement_status.yaml").read_text(
            encoding="utf-8"
        )
    )
    sandbox = yaml.safe_load(
        (root / "config" / "broker_sandbox_matrix.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert set(status["statuses"]) == {
        f"TEST-B{number:02d}" for number in range(1, 21)
    }
    assert status["policy"]["live_trading_enabled"] is False
    assert status["statuses"]["TEST-B01"]["acceptance_status"] == (
        "blocked_external_evidence"
    )
    assert status["statuses"]["TEST-B20"]["acceptance_status"] == (
        "blocked_external_evidence"
    )
    assert set(sandbox["brokers"]) == {
        "taishin",
        "fubon",
        "sinopac",
        "yuanta",
        "masterlink",
    }
    for broker in sandbox["brokers"].values():
        assert broker["sandbox_available"] is None
        assert broker["test_account_ready"] is None
        assert broker["evidence_receipt_ids"] == []


def test_rate_limit_governor_is_per_broker_and_honors_429_backoff() -> None:
    now = datetime.now(timezone.utc)
    governor = BrokerRateLimitGovernor(
        [
            BrokerRateLimitPolicy(
                broker_id="fubon",
                endpoint_group="market",
                requests_per_window=2,
                window_seconds=10,
                maximum_concurrency=1,
                policy_verified=True,
            )
        ]
    )

    first = governor.before_request("fubon", "market", now=now)
    concurrent = governor.before_request("fubon", "market", now=now)
    assert first.allowed is True
    assert concurrent.reason == "maximum_concurrency_reached"

    governor.complete_request(
        "fubon",
        "market",
        status_code=429,
        retry_after_seconds=5,
        now=now,
    )
    blocked = governor.before_request(
        "fubon",
        "market",
        now=now + timedelta(seconds=1),
    )
    other_broker = governor.before_request(
        "taishin",
        "market",
        now=now + timedelta(seconds=1),
    )
    assert blocked.allowed is False
    assert blocked.reason == "broker_backoff_active"
    assert blocked.retry_after_seconds == 4
    assert other_broker.allowed is True


def test_reconnect_requires_snapshot_recovery_before_feed_is_connected() -> None:
    planner = BrokerReconnectPlanner(base_delay_seconds=1, maximum_attempts=2)
    now = datetime.now(timezone.utc)

    first = planner.disconnected(
        "fubon",
        "trade",
        error="websocket closed",
        now=now,
    )
    reconnecting = planner.transport_connected(
        "fubon",
        "trade",
        now=now + timedelta(seconds=1),
    )
    recovered = planner.snapshot_recovered(
        "fubon",
        "trade",
        now=now + timedelta(seconds=2),
    )

    assert first.state == "backoff"
    assert first.next_attempt_at == now + timedelta(seconds=1)
    assert reconnecting.state == "recovering"
    assert reconnecting.requires_snapshot_recovery is True
    assert recovered.state == "connected"
    assert recovered.requires_snapshot_recovery is False


def test_cancel_requires_explicit_host_receipt_and_broker_confirmation() -> None:
    class ConfirmingGateway:
        async def cancel_order(self, broker_id: str, request: dict) -> BrokerOrderReceipt:
            return BrokerOrderReceipt(
                intent_id=request["intent_id"],
                broker_id=broker_id,
                broker_order_id=request["broker_order_id"],
                submitted_at=datetime.now(timezone.utc),
                status=BrokerOrderState.CANCELLED,
                accepted_quantity=Decimal("0"),
                raw_receipt_hash="c" * 64,
            )

    oms = BrokerOrderManagementGateway(gateway=ConfirmingGateway())
    entry = oms.register(_intent())
    entry.state = BrokerOrderState.SUBMITTING
    raw = BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"order_id": "FUBON-ORDER-1", "status": "ack"},
    )
    oms.apply_order_report(
        _order_report(
            raw,
            report_id="BOR-before-cancel",
            status=BrokerOrderState.ACKNOWLEDGED,
            filled="0",
            remaining="1",
        ),
        raw_event=raw,
    )

    with pytest.raises(PermissionError, match="Host authorization receipt"):
        asyncio.run(
            oms.cancel(
                entry.intent.intent_id,
                user_requested=True,
                human_authorization_receipt_id="",
            )
        )
    cancelled = asyncio.run(
        oms.cancel(
            entry.intent.intent_id,
            user_requested=True,
            human_authorization_receipt_id="HAR-user-clicked-cancel",
        )
    )

    assert cancelled.state == BrokerOrderState.CANCELLED
    assert cancelled.remaining_quantity == 0
    assert cancelled.action_receipts[-1].status == BrokerOrderState.CANCELLED


def test_vendored_shioaji_downloader_never_embeds_or_performs_login() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "external"
        / "FinRL"
        / "finrl"
        / "meta"
        / "preprocessor"
        / "shioajidownloader.py"
    )
    source = source_path.read_text(encoding="utf-8")

    assert "api.login(" not in source
    assert "api_key=" not in source
    assert "secret_key=" not in source
    assert "Pass an authenticated Shioaji client" in source
