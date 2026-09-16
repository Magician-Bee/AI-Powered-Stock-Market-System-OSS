from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from stock_ai.brokers.contracts import BrokerAuthorizationRequired
from stock_ai.brokers.gateway import UnifiedBrokerGateway
from stock_ai.brokers.registry import BrokerCapabilityRegistry
from stock_ai.brokers.taishin.official_sdk import (
    MacOSKeychainTaishinCredentialResolver,
    TaishinOfficialQuoteAdapter,
    TaishinQuoteSdk,
    TaishinReadonlyCredentials,
)
from stock_ai.brokers.taishin.readonly_onboarding import (
    build_taishin_readonly_onboarding_plan,
)
from stock_ai.brokers.worker_protocol import (
    BrokerWorkerInterface,
    BrokerWorkerOperation,
    BrokerWorkerRequest,
)


class _FakeMarketDataMart:
    pass


class _FakeRCode:
    OK = object()


class _FakeSolD:
    calls: list[tuple[object, str, str, str]] = []
    subscriptions: list[tuple[str, str]] = []
    unsubscriptions: list[tuple[str, str]] = []
    disconnects = 0
    emit_event = True

    def __init__(self, events: object, source_file: str) -> None:
        self.events = events
        self.source_file = source_file

    def Login(self, username: str, password: str, exchange: str) -> object:
        self.calls.append((self.events, username, password, exchange))
        return _FakeRCode.OK

    def Subscribe(self, exchange: str, symbol: str) -> object:
        self.subscriptions.append((exchange, symbol))
        if self.emit_event:
            self.events.OnMatch(
                SimpleNamespace(
                    Symbol=symbol,
                    MatchTime="09:00:00.000",
                    TxSeq=99,
                    MatchPrice="100.5",
                )
            )
        return _FakeRCode.OK

    def Unsubscribe(self, exchange: str, symbol: str) -> object:
        self.unsubscriptions.append((exchange, symbol))
        return _FakeRCode.OK

    def DisConnect(self) -> object:
        type(self).disconnects += 1
        return _FakeRCode.OK


class _FakeCredentialResolver:
    def __init__(self) -> None:
        self.references: list[str] = []

    def resolve(self, reference: str) -> TaishinReadonlyCredentials:
        self.references.append(reference)
        return TaishinReadonlyCredentials(username="masked-user", password="private-password")


def _sdk() -> TaishinQuoteSdk:
    return TaishinQuoteSdk(
        package_version="9.9.9-test",
        market_data_mart=_FakeMarketDataMart,
        sol_d=_FakeSolD,
        rcode=_FakeRCode,
    )


def test_taishin_adapter_reports_installed_sdk_without_claiming_unverified_capabilities():
    adapter = TaishinOfficialQuoteAdapter(sdk_loader=_sdk)

    profile = asyncio.run(adapter.probe_capabilities())
    authorization = asyncio.run(adapter.authorization_status())

    assert profile.sdk_version == "9.9.9-test"
    assert profile.platform == "installed_unverified"
    assert profile.capabilities.model_dump() == {
        name: None for name in profile.capabilities.model_dump()
    }
    assert profile.verification_receipts == []
    assert authorization.state == "requires_user_action"
    assert authorization.safe_to_continue_automatically is False


def test_taishin_onboarding_names_the_only_supported_official_python_sdk_package():
    plan = build_taishin_readonly_onboarding_plan()
    provenance = next(step for step in plan.steps if step.step_id == "official_sdk_provenance")

    assert "PY_TradeD" in plan.prerequisites[1]
    assert "PY_Trade_package" in provenance.detail


def test_taishin_adapter_fails_closed_without_a_host_keychain_reference():
    resolver = _FakeCredentialResolver()
    adapter = TaishinOfficialQuoteAdapter(sdk_loader=_sdk, credential_resolver=resolver)

    with pytest.raises(BrokerAuthorizationRequired, match="Keychain reference"):
        asyncio.run(adapter.login_readonly())

    assert resolver.references == []
    assert _FakeSolD.calls == []


def test_taishin_worker_uses_official_sdk_login_but_never_returns_credentials():
    _FakeSolD.calls = []
    resolver = _FakeCredentialResolver()
    adapter = TaishinOfficialQuoteAdapter(sdk_loader=_sdk, credential_resolver=resolver)
    gateway = UnifiedBrokerGateway(BrokerCapabilityRegistry(adapters=(adapter,)))
    worker = BrokerWorkerInterface(gateway)
    request = BrokerWorkerRequest(
        broker_id="taishin",
        operation=BrokerWorkerOperation.LOGIN_READONLY,
        action="login",
        secret_reference="keychain://stock-ai/taishin-quote",
        human_authorization_receipt_id="HAR-taishin-readonly-001",
    )

    response = asyncio.run(worker.execute(request))

    assert response.completed is True
    assert response.result == {
        "broker_id": "taishin",
        "sdk_package": "PY_Trade_package",
        "sdk_version": "9.9.9-test",
        "exchange": "TWS",
        "login_accepted": True,
        "quote_capability_verified": False,
        "next_required_step": "owner_quote_validation_and_receipt",
    }
    assert resolver.references == ["keychain://stock-ai/taishin-quote"]
    assert _FakeSolD.calls and _FakeSolD.calls[0][3] == "TWS"
    visible = response.model_dump_json()
    assert "private-password" not in visible
    assert "masked-user" not in visible


def test_taishin_worker_runs_one_bounded_readonly_quote_probe_and_returns_only_evidence():
    _FakeSolD.calls = []
    _FakeSolD.subscriptions = []
    _FakeSolD.unsubscriptions = []
    _FakeSolD.disconnects = 0
    _FakeSolD.emit_event = True
    resolver = _FakeCredentialResolver()
    adapter = TaishinOfficialQuoteAdapter(sdk_loader=_sdk, credential_resolver=resolver)
    worker = BrokerWorkerInterface(UnifiedBrokerGateway(BrokerCapabilityRegistry(adapters=(adapter,))))
    request = BrokerWorkerRequest(
        broker_id="taishin",
        operation=BrokerWorkerOperation.MARKET,
        action="readonly_probe",
        arguments={"instrument_id": "TWSE:2330", "timeout_seconds": 1},
        secret_reference="keychain://stock-ai/taishin-quote",
        human_authorization_receipt_id="HAR-taishin-readonly-002",
    )

    response = asyncio.run(worker.execute(request))

    assert response.completed is True
    assert response.result == {
        "broker_id": "taishin",
        "sdk_package": "PY_Trade_package",
        "sdk_version": "9.9.9-test",
        "instrument_id": "TWSE:2330",
        "exchange": "TWS",
        "subscription_accepted": True,
        "event_received": True,
        "event_kind": "match",
        "observed_fields": ["MatchPrice", "MatchTime", "Symbol", "TxSeq"],
        "payload_sha256": "41ce9e11de1e7f67a8e5193c3b191d55a2c48c14ae3c162b01476facb9258ca2",
        "teardown": "unsubscribed_and_disconnected",
        "quote_capability_verified": False,
        "next_required_step": "owner_review_and_immutable_integration_receipt",
    }
    assert _FakeSolD.subscriptions == [("TWS", "2330")]
    assert _FakeSolD.unsubscriptions == [("TWS", "2330")]
    assert _FakeSolD.disconnects == 1
    visible = response.model_dump_json()
    assert "private-password" not in visible
    assert "masked-user" not in visible
    assert "100.5" not in visible


def test_taishin_quote_probe_times_out_but_still_unsubscribes_and_disconnects():
    _FakeSolD.subscriptions = []
    _FakeSolD.unsubscriptions = []
    _FakeSolD.disconnects = 0
    _FakeSolD.emit_event = False
    adapter = TaishinOfficialQuoteAdapter(
        sdk_loader=_sdk,
        credential_resolver=_FakeCredentialResolver(),
    )

    response = asyncio.run(
        BrokerWorkerInterface(
            UnifiedBrokerGateway(BrokerCapabilityRegistry(adapters=(adapter,)))
        ).execute(
            BrokerWorkerRequest(
                broker_id="taishin",
                operation=BrokerWorkerOperation.MARKET,
                action="readonly_probe",
                arguments={"instrument_id": "TWSE:2330", "timeout_seconds": 1},
                secret_reference="keychain://stock-ai/taishin-quote",
                human_authorization_receipt_id="HAR-taishin-readonly-003",
            )
        )
    )

    assert response.completed is False
    assert response.error_type == "TaishinReadonlyQuoteProbeFailed"
    assert _FakeSolD.unsubscriptions == [("TWS", "2330")]
    assert _FakeSolD.disconnects == 1
    _FakeSolD.emit_event = True


def test_taishin_quote_probe_requires_host_receipt_and_secure_reference():
    with pytest.raises(ValueError, match="Host authorization receipt"):
        BrokerWorkerRequest(
            broker_id="taishin",
            operation=BrokerWorkerOperation.MARKET,
            action="readonly_probe",
            arguments={"instrument_id": "TWSE:2330"},
            secret_reference="keychain://stock-ai/taishin-quote",
        )
    with pytest.raises(ValueError, match="secure-store reference"):
        BrokerWorkerRequest(
            broker_id="taishin",
            operation=BrokerWorkerOperation.MARKET,
            action="readonly_probe",
            arguments={"instrument_id": "TWSE:2330"},
            human_authorization_receipt_id="HAR-taishin-readonly-004",
        )


def test_taishin_keychain_resolver_accepts_only_json_credential_envelopes(monkeypatch):
    resolver = MacOSKeychainTaishinCredentialResolver()
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(stdout='{"username":"owner","password":"secret"}')

    monkeypatch.setattr("stock_ai.brokers.taishin.official_sdk.subprocess.run", fake_run)

    credentials = resolver.resolve("keychain://stock-ai/taishin-quote")

    assert credentials.username == "owner"
    assert credentials.password == "secret"
    assert observed["command"] == [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "stock-ai",
        "-a",
        "taishin-quote",
        "-w",
    ]
    assert observed["kwargs"] == {
        "check": True,
        "capture_output": True,
        "text": True,
        "timeout": 10,
    }


def test_taishin_keychain_resolver_rejects_file_and_plaintext_references():
    resolver = MacOSKeychainTaishinCredentialResolver()

    for reference in ("file:///tmp/secret", "plain-secret", "keychain://stock-ai"):
        with pytest.raises(BrokerAuthorizationRequired, match="keychain"):
            resolver.resolve(reference)
