"""Fail-closed read-only bridge for Taishin's official Python quote SDK.

The broker distributes its quote component as a ``PY_TradeD`` wheel rather
than a public PyPI package.  This adapter deliberately imports that component
only when an account-owner-installed worker has it available.  It accepts a
Keychain *reference* from the Host, never credentials from an Agent request,
and it does not expose account or order operations.

The implementation follows Taishin's public Python quote documentation:
``PY_Trade_package.MarketDataMart``, ``Sol_D`` and ``RCode.OK``.  A successful
``Login`` is kept as provisional until the owner completes the broker's online
quote validation and the Host records an immutable integration receipt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import importlib
import json
from pathlib import PurePosixPath
import subprocess
from threading import Event
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlparse

from ...brokers.contracts import (
    BrokerAuthorizationRequired,
    BrokerCapabilities,
    BrokerCapabilityProfile,
    BrokerId,
    HumanAuthorizationStatus,
)
from ...brokers.declared_adapter import DeclaredBrokerAdapter


TAISHIN_QUOTE_DOCUMENTATION_URL = (
    "https://mlapi.tssco.com.tw/web_api/service/document/python-quote"
)
TAISHIN_SDK_PACKAGE = "PY_Trade_package"
_TAISHIN_EXCHANGE = "TWS"
_PROBE_TIMEOUT_MIN_SECONDS = 1.0
_PROBE_TIMEOUT_MAX_SECONDS = 30.0
_PUBLIC_QUOTE_FIELDS = (
    "Symbol",
    "MatchTime",
    "OrderBookTime",
    "TxSeq",
    "ObSeq",
    "MatchPrice",
    "BuyPrice",
    "BuyQty",
    "SellPrice",
    "SellQty",
)


class TaishinReadonlyQuoteProbeFailed(RuntimeError):
    """The official SDK did not complete a safe, bounded quote probe."""


@dataclass(frozen=True, slots=True)
class TaishinReadonlyCredentials:
    """Credentials exist only in the isolated worker's memory."""

    username: str
    password: str

    def __post_init__(self) -> None:
        if not self.username or not self.password:
            raise BrokerAuthorizationRequired("Taishin Keychain credentials are incomplete")


class TaishinCredentialResolver(Protocol):
    def resolve(self, reference: str) -> TaishinReadonlyCredentials: ...


class MacOSKeychainTaishinCredentialResolver:
    """Resolve a JSON credential bundle from a narrowly-scoped Keychain URI.

    The URI has the form ``keychain://<service>/<account>``.  The command's
    stdout is parsed in memory and is never persisted, returned to the caller,
    or included in exceptions/logs.
    """

    def resolve(self, reference: str) -> TaishinReadonlyCredentials:
        parsed = urlparse(reference)
        parts = PurePosixPath(unquote(parsed.path)).parts
        if parsed.scheme != "keychain" or not parsed.netloc or len(parts) != 2:
            raise BrokerAuthorizationRequired(
                "Taishin read-only login requires keychain://<service>/<account>"
            )
        service = parsed.netloc
        account = parts[1]
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            payload = json.loads(result.stdout)
        except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise BrokerAuthorizationRequired(
                "Taishin Keychain reference is unavailable to the isolated worker"
            ) from exc
        if not isinstance(payload, dict):
            raise BrokerAuthorizationRequired("Taishin Keychain item has an invalid credential envelope")
        return TaishinReadonlyCredentials(
            username=str(payload.get("username") or ""),
            password=str(payload.get("password") or ""),
        )


@dataclass(frozen=True, slots=True)
class TaishinQuoteSdk:
    package_version: str
    market_data_mart: type[Any]
    sol_d: type[Any]
    rcode: Any


def load_taishin_quote_sdk() -> TaishinQuoteSdk:
    """Load the account-owner-installed official wheel without installing it."""

    try:
        package = importlib.import_module(TAISHIN_SDK_PACKAGE)
        market_data = importlib.import_module(f"{TAISHIN_SDK_PACKAGE}.MarketDataMart")
        sol_d = importlib.import_module(f"{TAISHIN_SDK_PACKAGE}.Sol_D")
        model = importlib.import_module(f"{TAISHIN_SDK_PACKAGE}.SolPYAPI_Model")
    except ImportError as exc:
        raise BrokerAuthorizationRequired(
            "Taishin official PY_TradeD quote SDK is not installed in the isolated worker"
        ) from exc
    version = str(getattr(package, "__version__", "installed-unversioned"))
    return TaishinQuoteSdk(
        package_version=version,
        market_data_mart=market_data.MarketDataMart,
        sol_d=sol_d.Sol_D,
        rcode=model.RCode,
    )


class TaishinOfficialQuoteAdapter(DeclaredBrokerAdapter):
    """Actual Taishin quote-SDK loader with a permanently closed order surface."""

    adapter_version = "taishin-quote-sdk-0.1.0"

    def __init__(
        self,
        *,
        sdk_loader: Callable[[], TaishinQuoteSdk] = load_taishin_quote_sdk,
        credential_resolver: TaishinCredentialResolver | None = None,
    ) -> None:
        super().__init__(
            broker_id="taishin",
            official_url="https://mlapi.tssco.com.tw/web_api/service/home",
            required_actions=[
                "open_account",
                "sign_api_agreement",
                "complete_api_test",
            ],
        )
        self._sdk_loader = sdk_loader
        self._credential_resolver = credential_resolver or MacOSKeychainTaishinCredentialResolver()

    async def authorization_status(self) -> HumanAuthorizationStatus:
        # An installed wheel never substitutes for the owner's online API
        # validation, so this remains a deliberate pause point.
        return await super().authorization_status()

    async def probe_capabilities(self) -> BrokerCapabilityProfile:
        try:
            sdk = self._sdk_loader()
        except BrokerAuthorizationRequired:
            sdk_version = "not_installed"
            platform = "unverified"
        else:
            sdk_version = sdk.package_version
            platform = "installed_unverified"
        return BrokerCapabilityProfile(
            broker_id="taishin",
            adapter_version=self.adapter_version,
            sdk_version=sdk_version,
            platform=platform,
            capabilities=BrokerCapabilities(),
            verified_at=None,
            verification_receipts=[],
        )

    async def login_readonly(self, *, secret_reference: str | None = None) -> dict[str, Any]:
        if not secret_reference:
            raise BrokerAuthorizationRequired(
                "Taishin read-only login requires a Host Keychain reference and authorization receipt"
            )
        sdk = self._sdk_loader()
        credentials = self._credential_resolver.resolve(secret_reference)
        return await asyncio.to_thread(self._login_in_worker, sdk, credentials)

    async def probe_readonly_quote(
        self,
        *,
        secret_reference: str | None = None,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Verify one official TWS subscription without exposing quote values.

        The probe is Host-only: it consumes a Keychain reference, subscribes
        to exactly one validated Taiwanese-security symbol, waits a bounded
        time for an official SDK callback, then always unsubscribes and
        disconnects.  Returned evidence contains only the requested symbol,
        public field *names* and a digest of the SDK callback.  It cannot
        itself turn any broker capability on; the owner still reviews and
        persists a separate immutable integration receipt.
        """

        if not secret_reference:
            raise BrokerAuthorizationRequired(
                "Taishin read-only quote probe requires a Host Keychain reference"
            )
        instrument_id, symbol, timeout_seconds = self._validate_quote_probe_request(request)
        sdk = self._sdk_loader()
        credentials = self._credential_resolver.resolve(secret_reference)
        return await asyncio.to_thread(
            self._quote_probe_in_worker,
            sdk,
            credentials,
            instrument_id,
            symbol,
            timeout_seconds,
        )

    @staticmethod
    def _validate_quote_probe_request(request: dict[str, Any]) -> tuple[str, str, float]:
        instrument_id = str(request.get("instrument_id") or "").strip().upper()
        prefix, separator, symbol = instrument_id.partition(":")
        if (
            prefix != "TWSE"
            or not separator
            or not symbol.isdigit()
            or not 4 <= len(symbol) <= 6
        ):
            raise BrokerAuthorizationRequired(
                "Taishin read-only quote probe requires instrument_id in the TWSE:<symbol> form"
            )
        try:
            timeout_seconds = float(request.get("timeout_seconds", 8))
        except (TypeError, ValueError) as exc:
            raise BrokerAuthorizationRequired(
                "Taishin read-only quote probe timeout must be a number of seconds"
            ) from exc
        if not _PROBE_TIMEOUT_MIN_SECONDS <= timeout_seconds <= _PROBE_TIMEOUT_MAX_SECONDS:
            raise BrokerAuthorizationRequired(
                "Taishin read-only quote probe timeout must be between 1 and 30 seconds"
            )
        return instrument_id, symbol, timeout_seconds

    @staticmethod
    def _login_in_worker(
        sdk: TaishinQuoteSdk, credentials: TaishinReadonlyCredentials
    ) -> dict[str, Any]:
        events = sdk.market_data_mart()
        client = sdk.sol_d(events, __file__)
        result = client.Login(credentials.username, credentials.password, _TAISHIN_EXCHANGE)
        if result != sdk.rcode.OK:
            raise BrokerAuthorizationRequired("Taishin official SDK rejected the read-only quote login")
        # No data capability is claimed here: the official flow separately
        # requires quote validation, a reconnect-safe subscription probe and a
        # durable Host receipt before any capability becomes true.
        return {
            "broker_id": "taishin",
            "sdk_package": TAISHIN_SDK_PACKAGE,
            "sdk_version": sdk.package_version,
            "exchange": _TAISHIN_EXCHANGE,
            "login_accepted": True,
            "quote_capability_verified": False,
            "next_required_step": "owner_quote_validation_and_receipt",
        }

    @staticmethod
    def _quote_probe_in_worker(
        sdk: TaishinQuoteSdk,
        credentials: TaishinReadonlyCredentials,
        instrument_id: str,
        symbol: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        callback_received = Event()
        observed: dict[str, Any] = {}

        def capture(kind: str, *callback_args: Any) -> None:
            payload = TaishinOfficialQuoteAdapter._callback_payload(callback_args)
            observed_symbol = str(payload.get("Symbol") or "").strip()
            # A shared SDK worker may emit an old queued event.  Do not count
            # it as proof for the requested subscription.
            if observed_symbol != symbol:
                return
            observed.update(
                event_kind=kind,
                observed_fields=sorted(payload),
                payload_sha256=TaishinOfficialQuoteAdapter._payload_digest(payload),
            )
            callback_received.set()

        events = sdk.market_data_mart()
        events.OnUpdateLastSnapshot = lambda *args: capture("last_snapshot", *args)
        events.OnMatch = lambda *args: capture("match", *args)
        client = sdk.sol_d(events, __file__)
        subscribed = False
        primary_error: BaseException | None = None
        cleanup_errors: list[str] = []
        try:
            login_result = client.Login(credentials.username, credentials.password, _TAISHIN_EXCHANGE)
            if login_result != sdk.rcode.OK:
                raise BrokerAuthorizationRequired("Taishin official SDK rejected the read-only quote login")
            subscribe_result = client.Subscribe(_TAISHIN_EXCHANGE, symbol)
            if subscribe_result != sdk.rcode.OK:
                raise TaishinReadonlyQuoteProbeFailed(
                    "Taishin official SDK rejected the read-only quote subscription"
                )
            subscribed = True
            if not callback_received.wait(timeout_seconds):
                raise TaishinReadonlyQuoteProbeFailed(
                    "Taishin official SDK did not emit a quote callback before the probe timeout"
                )
        except BaseException as exc:
            primary_error = exc
        finally:
            if subscribed:
                try:
                    unsubscribe_result = client.Unsubscribe(_TAISHIN_EXCHANGE, symbol)
                    if unsubscribe_result != sdk.rcode.OK:
                        cleanup_errors.append("unsubscribe_rejected")
                except Exception:
                    cleanup_errors.append("unsubscribe_failed")
            try:
                disconnect_result = client.DisConnect()
                if disconnect_result != sdk.rcode.OK:
                    cleanup_errors.append("disconnect_rejected")
            except Exception:
                cleanup_errors.append("disconnect_failed")
        if primary_error is not None:
            raise primary_error
        if cleanup_errors:
            raise TaishinReadonlyQuoteProbeFailed(
                "Taishin read-only quote probe cleanup failed: " + ", ".join(cleanup_errors)
            )
        return {
            "broker_id": "taishin",
            "sdk_package": TAISHIN_SDK_PACKAGE,
            "sdk_version": sdk.package_version,
            "instrument_id": instrument_id,
            "exchange": _TAISHIN_EXCHANGE,
            "subscription_accepted": True,
            "event_received": True,
            "event_kind": observed["event_kind"],
            "observed_fields": observed["observed_fields"],
            "payload_sha256": observed["payload_sha256"],
            "teardown": "unsubscribed_and_disconnected",
            "quote_capability_verified": False,
            "next_required_step": "owner_review_and_immutable_integration_receipt",
        }

    @staticmethod
    def _callback_payload(callback_args: tuple[Any, ...]) -> dict[str, Any]:
        """Extract only documented public quote fields for an evidence hash."""

        for candidate in reversed(callback_args):
            if isinstance(candidate, (list, tuple)) and candidate:
                candidate = candidate[0]
            if isinstance(candidate, dict):
                payload = {
                    field: candidate[field]
                    for field in _PUBLIC_QUOTE_FIELDS
                    if field in candidate
                }
            else:
                payload = {
                    field: getattr(candidate, field)
                    for field in _PUBLIC_QUOTE_FIELDS
                    if hasattr(candidate, field)
                }
            if payload:
                return payload
        return {}

    @staticmethod
    def _payload_digest(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


__all__ = [
    "MacOSKeychainTaishinCredentialResolver",
    "TAISHIN_QUOTE_DOCUMENTATION_URL",
    "TAISHIN_SDK_PACKAGE",
    "TaishinOfficialQuoteAdapter",
    "TaishinReadonlyQuoteProbeFailed",
    "TaishinQuoteSdk",
    "TaishinReadonlyCredentials",
    "load_taishin_quote_sdk",
]
