"""Authenticated deployment bridge for the project-local headless n8n runtime.

The Agent owns intent, policy and compilation.  This module verifies the
compiler seal, converts that sealed product into an n8n execution graph, and
creates/updates/publishes it through n8n's public API.  Nothing in the returned
Host receipt contains node definitions, URLs, API keys or credential details;
the UI continues to render only ``CompiledWorkflow.user_artifact``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import ssl
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from open_stock_ai.agent_runtime.automation.backends import HeadlessN8nAdapter
from open_stock_ai.agent_runtime.automation.workflow_compiler import canonical_compiler_digest
from open_stock_ai.agent_runtime import (
    ExternalTransportGuard,
    RateLimitPolicy,
    ScopedRateLimitGovernor,
    default_external_transport_guard,
)

from .local_security import (
    AUTOMATION_CALLBACK_HEADER,
    AUTOMATION_NONCE_HEADER,
    AUTOMATION_SIGNATURE_HEADER,
    AUTOMATION_SOURCE_HEADER,
    AUTOMATION_TIMESTAMP_HEADER,
    derive_automation_callback_token,
)


HttpRequest = Callable[[str, str, Mapping[str, str], bytes | None, float], Mapping[str, Any]]
_COMPILER_SEAL = "open-stock-ai-automation-compiler-v1"
_COMPILER_SCHEMA = "open_stock_ai.compiled_automation.v1"
_LOCAL_N8N_CONTROL_PLANE_SCOPE = "automation:n8n:local:control-plane"
_LOCAL_STOCK_AI_CALLBACK_PROBE_SCOPE = "automation:stock-ai:local:callback-probe"
_LOCAL_N8N_CONTROL_PLANE_POLICY = RateLimitPolicy(
    requests_per_window=30,
    window_seconds=60,
    maximum_concurrency=4,
    policy_verified=True,
)


@dataclass(frozen=True, slots=True)
class N8nGatewayConfig:
    """Connection details for n8n's public API.

    The historic ``gateway_*`` names are retained because the application
    already reads them from ``.env``. ``gateway_url`` is the n8n instance
    root, ``gateway_token`` is only the scoped n8n public API key, and
    ``callback_secret`` is a separately rotatable callback-only secret.
    """

    gateway_url: str | None = None
    gateway_token: str | None = None
    callback_secret: str | None = None
    timeout_seconds: float = 15.0
    callback_url: str = "http://127.0.0.1:8000/api/agents/automations/events"
    deployment_mode: str = "local"
    remote_host_allowlist: tuple[str, ...] = ()
    remote_tls_certificate_sha256: str | None = None
    remote_mtls_certificate_path: str | None = None
    remote_mtls_key_path: str | None = None
    remote_tls_ca_bundle_path: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def api_key_configured(self) -> bool:
        return bool(str(self.gateway_token or "").strip())

    @property
    def callback_secret_configured(self) -> bool:
        return bool(str(self.callback_secret or "").strip())

    @property
    def callback_token(self) -> str:
        value = str(self.callback_secret or "").strip()
        return derive_automation_callback_token(value) if value else ""

    @property
    def base_url(self) -> str:
        value = str(self.gateway_url or "").strip().rstrip("/")
        if value.endswith("/api/v1"):
            value = value[: -len("/api/v1")]
        return value


class N8nGatewayExecutor:
    """Deploy compiler products to a real, authenticated n8n instance."""

    def __init__(
        self,
        config: N8nGatewayConfig,
        *,
        request: HttpRequest | None = None,
        transport_guard: ExternalTransportGuard | None = None,
    ) -> None:
        if not config.configured:
            raise ValueError("n8n base URL is required")
        parsed_base_url = urlsplit(config.base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname:
            raise ValueError("n8n base URL must use http or https")
        host = str(parsed_base_url.hostname).casefold()
        mode = str(config.deployment_mode or "local").casefold()
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        if mode == "local" and host not in loopback_hosts:
            raise ValueError("local n8n mode requires a loopback gateway URL")
        if mode == "remote":
            allowlist = {str(item).casefold() for item in config.remote_host_allowlist if str(item).strip()}
            if parsed_base_url.scheme != "https" or host not in allowlist:
                raise ValueError("remote n8n mode requires HTTPS and an explicit host allowlist")
            pin = str(config.remote_tls_certificate_sha256 or "").casefold()
            if len(pin) != 64 or any(character not in "0123456789abcdef" for character in pin):
                raise ValueError("remote n8n mode requires a SHA-256 TLS certificate pin")
            has_client_certificate = bool(str(config.remote_mtls_certificate_path or "").strip())
            has_client_key = bool(str(config.remote_mtls_key_path or "").strip())
            if has_client_certificate != has_client_key:
                raise ValueError("remote n8n mTLS requires both a client certificate and key")
        if mode not in {"local", "remote"}:
            raise ValueError("n8n deployment mode must be local or remote")
        callback = urlsplit(config.callback_url)
        if callback.scheme not in {"http", "https"} or callback.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("n8n callback URL must be loopback-only")
        self.config = config
        self._transport_guard = transport_guard or _transport_guard_for(config)
        self._request = request or self._secure_request

    def _secure_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        parsed = urlsplit(url)
        scope = _transport_scope(self.config, method, parsed)
        return self._transport_guard.call_sync(
            scope,
            lambda: _request_json(
                method,
                url,
                headers,
                body,
                timeout_seconds,
                certificate_sha256=(
                    str(self.config.remote_tls_certificate_sha256)
                    if str(self.config.deployment_mode).casefold() == "remote"
                    else None
                ),
                mtls_certificate_path=self.config.remote_mtls_certificate_path,
                mtls_key_path=self.config.remote_mtls_key_path,
                ca_bundle_path=self.config.remote_tls_ca_bundle_path,
            ),
        )

    def status(self) -> Mapping[str, Any]:
        """Probe process, owner setup and API authorization independently."""

        try:
            self._health_check()
        except Exception as exc:
            return {
                "configured": True,
                "activation_ready": False,
                "healthy": False,
                "owner_setup_complete": False,
                "setup_required": False,
                "api_key_configured": self.config.api_key_configured,
                "callback_secret_configured": self.config.callback_secret_configured,
                "api_authenticated": False,
                "callback_reachable": False,
                "readiness_code": "process_unreachable",
                "user_message": "外部工作流執行層無法連線；自動化不會假裝已啟用。",
                "reason": f"n8n health check failed: {type(exc).__name__}",
            }
        try:
            settings = self._instance_settings()
        except Exception as exc:
            return {
                "configured": True,
                "activation_ready": False,
                "healthy": True,
                "owner_setup_complete": False,
                "setup_required": False,
                "api_key_configured": self.config.api_key_configured,
                "callback_secret_configured": self.config.callback_secret_configured,
                "api_authenticated": False,
                "callback_reachable": False,
                "readiness_code": "owner_setup_unknown",
                "user_message": "n8n 已啟動，但無法確認 owner 與 API 設定；尚不可部署。",
                "reason": f"n8n settings check failed: {type(exc).__name__}",
            }
        setup_required = bool(
            dict(dict(settings.get("userManagement") or {})).get("showSetupOnFirstLoad")
        )
        if setup_required:
            return {
                "configured": True,
                "activation_ready": False,
                "healthy": True,
                "owner_setup_complete": False,
                "setup_required": True,
                "api_key_configured": self.config.api_key_configured,
                "callback_secret_configured": self.config.callback_secret_configured,
                "api_authenticated": False,
                "callback_reachable": False,
                "readiness_code": "owner_setup_required",
                "user_message": "n8n 已啟動，但尚未建立 owner 與 API key；目前不能部署自動化。",
            }
        if not self.config.api_key_configured:
            return {
                "configured": True,
                "activation_ready": False,
                "healthy": True,
                "owner_setup_complete": True,
                "setup_required": False,
                "api_key_configured": False,
                "callback_secret_configured": self.config.callback_secret_configured,
                "api_authenticated": False,
                "callback_reachable": False,
                "readiness_code": "api_key_required",
                "user_message": "n8n owner 已設定，但尚未提供 scoped API key；目前不能部署自動化。",
            }
        try:
            self._api("GET", "/workflows?limit=1&excludePinnedData=true")
        except Exception as exc:
            return {
                "configured": True,
                "activation_ready": False,
                "healthy": True,
                "owner_setup_complete": True,
                "setup_required": False,
                "api_key_configured": True,
                "callback_secret_configured": self.config.callback_secret_configured,
                "api_authenticated": False,
                "callback_reachable": False,
                "readiness_code": "api_auth_failed",
                "user_message": "n8n 已啟動，但 API key 未通過授權；目前不能部署自動化。",
                "reason": f"n8n API authorization failed: {type(exc).__name__}",
            }
        if not self.config.callback_secret_configured:
            return {
                "configured": True,
                "activation_ready": False,
                "healthy": True,
                "owner_setup_complete": True,
                "setup_required": False,
                "api_key_configured": True,
                "callback_secret_configured": False,
                "api_authenticated": True,
                "callback_reachable": False,
                "readiness_code": "callback_secret_required",
                "user_message": "n8n API 已授權，但尚未提供獨立 callback secret；目前不能部署自動化。",
            }
        try:
            self._callback_health_check()
        except Exception as exc:
            return {
                "configured": True,
                "activation_ready": False,
                "healthy": True,
                "owner_setup_complete": True,
                "setup_required": False,
                "api_key_configured": True,
                "callback_secret_configured": True,
                "api_authenticated": True,
                "callback_reachable": False,
                "readiness_code": "stock_ai_callback_unreachable",
                "user_message": "n8n 已授權，但 Stock AI callback 無法連線；目前不能部署自動化。",
                "reason": f"Stock AI callback health check failed: {type(exc).__name__}",
            }
        return {
            "configured": True,
            "activation_ready": True,
            "healthy": True,
            "owner_setup_complete": True,
            "setup_required": False,
            "api_key_configured": True,
            "callback_secret_configured": True,
            "api_authenticated": True,
            "callback_reachable": True,
            "readiness_code": "ready",
            "user_message": "外部工作流執行層已通過 health check 與 API 授權。",
        }

    def dry_run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Validate reachability, authorization and graph compilation without writes."""

        self._validate_contract(request)
        readiness = dict(self.status())
        if not readiness.get("activation_ready"):
            return _not_ready_result(readiness)
        workflow = self._workflow_payload(request)
        return {
            "accepted": True,
            "validated": True,
            "health_checked": True,
            "api_authenticated": True,
            "compiler_product_verified": True,
            "node_count": len(workflow["nodes"]),
            "opaque_credential_ref_count": len(request.get("credential_refs") or ()),
        }

    def __call__(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Create or update one workflow, publish it, then verify active state."""

        self._validate_contract(request)
        readiness = dict(self.status())
        if not readiness.get("activation_ready"):
            return _not_ready_result(readiness)
        workflow = self._workflow_payload(request)
        runtime_context = dict(request.get("runtime_context") or {})
        reference = str(runtime_context.get("target_backend_reference") or "").strip()
        deployed: Mapping[str, Any]
        operation: str
        if reference:
            deployed = self._api("PUT", f"/workflows/{quote(reference, safe='')}", workflow)
            operation = "updated"
        else:
            existing = self._find_existing(str(workflow["name"]))
            if existing:
                reference = str(existing["id"])
                deployed = self._api("PUT", f"/workflows/{quote(reference, safe='')}", workflow)
                operation = "updated"
            else:
                deployed = self._api("POST", "/workflows", workflow)
                reference = str(deployed.get("id") or "").strip()
                operation = "created"
        if not reference:
            raise RuntimeError("n8n workflow deployment returned no workflow id")

        published = self._api("POST", f"/workflows/{quote(reference, safe='')}/publish", {})
        if not _workflow_is_active(published):
            published = self._api("GET", f"/workflows/{quote(reference, safe='')}?excludePinnedData=true")
        if not _workflow_is_active(published):
            raise RuntimeError("n8n workflow was deployed but not published")
        return {
            "accepted": True,
            "published": True,
            "workflow_id": reference,
            "deployment_operation": operation,
            "compiler_product_verified": True,
            "health_checked": True,
            "api_authenticated": True,
            "workflow_digest": str(dict(request["compiler_contract"])["digest"]),
        }

    def publish(self, workflow_id: str) -> Mapping[str, Any]:
        self._health_check()
        result = self._api("POST", f"/workflows/{quote(workflow_id, safe='')}/publish", {})
        if not _workflow_is_active(result):
            raise RuntimeError("n8n workflow publish did not become active")
        return {"accepted": True, "published": True, "workflow_id": workflow_id}

    def unpublish(self, workflow_id: str) -> Mapping[str, Any]:
        self._health_check()
        result = self._api("POST", f"/workflows/{quote(workflow_id, safe='')}/unpublish", {})
        if _workflow_is_active(result):
            raise RuntimeError("n8n workflow remained active after unpublish")
        return {"accepted": True, "published": False, "workflow_id": workflow_id}

    def archive(self, workflow_id: str) -> Mapping[str, Any]:
        self._health_check()
        result = self._api("POST", f"/workflows/{quote(workflow_id, safe='')}/archive", {})
        if not bool(result.get("isArchived")):
            raise RuntimeError("n8n workflow archive was not acknowledged")
        return {"accepted": True, "archived": True, "workflow_id": workflow_id}

    def _health_check(self) -> None:
        result = self._request(
            "GET",
            f"{self.config.base_url}/healthz",
            {"Accept": "application/json"},
            None,
            float(self.config.timeout_seconds),
        )
        if str(result.get("status") or "").casefold() != "ok":
            raise RuntimeError("n8n health endpoint did not return ok")

    def _instance_settings(self) -> Mapping[str, Any]:
        result = self._request(
            "GET",
            f"{self.config.base_url}/rest/settings",
            {"Accept": "application/json"},
            None,
            float(self.config.timeout_seconds),
        )
        data = result.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("n8n settings response has no data object")
        return data

    def _callback_health_check(self) -> None:
        callback = urlsplit(self.config.callback_url)
        health_url = f"{callback.scheme}://{callback.netloc}/health"
        result = self._request(
            "GET",
            health_url,
            {"Accept": "application/json"},
            None,
            float(self.config.timeout_seconds),
        )
        if str(result.get("status") or "").casefold() != "ok":
            raise RuntimeError("Stock AI health endpoint did not return ok")

    def _api(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if not self.config.api_key_configured:
            raise PermissionError("n8n public API key is not configured")
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        return self._request(
            method,
            f"{self.config.base_url}/api/v1{path}",
            {
                "X-N8N-API-KEY": str(self.config.gateway_token),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body,
            float(self.config.timeout_seconds),
        )

    def _find_existing(self, name: str) -> Mapping[str, Any] | None:
        result = self._api(
            "GET",
            f"/workflows?name={quote(name, safe='')}&limit=2&excludePinnedData=true",
        )
        matches = [item for item in result.get("data") or () if str(item.get("name")) == name]
        if len(matches) > 1:
            raise RuntimeError("multiple n8n workflows share one compiler idempotency name")
        return dict(matches[0]) if matches else None

    @staticmethod
    def _validate_contract(request: Mapping[str, Any]) -> None:
        contract = dict(request.get("compiler_contract") or {})
        definition = dict(request.get("compiled_definition") or {})
        refs = [str(item) for item in request.get("credential_refs") or ()]
        if contract.get("compiler_seal") != _COMPILER_SEAL:
            raise TypeError("n8n accepts only a sealed WorkflowCompiler product")
        if contract.get("schema_version") != _COMPILER_SCHEMA or contract.get("backend") != "n8n":
            raise ValueError("unsupported compiler contract")
        digest = str(contract.get("digest") or "")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("compiler contract digest is invalid")
        if definition.get("engine") != "n8n" or not definition.get("trigger") or not definition.get("stages"):
            raise ValueError("compiler product has no deployable n8n graph")
        if not refs or any(not item.startswith(("secret://", "credential-ref://", "vault://")) for item in refs):
            raise ValueError("n8n credential references must remain opaque")
        if not str(request.get("idempotency_key") or "").strip():
            raise ValueError("n8n deployment requires a durable idempotency key")
        expected_digest = canonical_compiler_digest(
            str(contract.get("backend") or ""),
            definition,
            refs,
        )
        if not hmac.compare_digest(digest, expected_digest):
            raise ValueError("compiler contract digest does not match the sealed definition")

    def _workflow_payload(self, request: Mapping[str, Any]) -> dict[str, Any]:
        definition = dict(request["compiled_definition"])
        contract = dict(request["compiler_contract"])
        runtime_context = dict(request.get("runtime_context") or {})
        digest = str(contract["digest"])
        identity = str(runtime_context.get("automation_id") or digest[:12])
        name = f"Stock AI Automation {identity}"
        nodes = [
            _trigger_node(dict(definition["trigger"]), digest),
            _event_envelope_node(definition, digest, runtime_context, self.config.callback_token),
            _callback_node(self.config.callback_url, self.config.callback_token, digest),
        ]
        connections: dict[str, Any] = {}
        for current, following in zip(nodes, nodes[1:]):
            connections[str(current["name"])] = {
                "main": [[{"node": str(following["name"]), "type": "main", "index": 0}]]
            }
        return {
            "name": name,
            "nodes": nodes,
            "connections": connections,
            "settings": {
                "executionOrder": "v1",
                "timezone": "Asia/Taipei",
            },
        }


def build_n8n_gateway_adapter(config: N8nGatewayConfig) -> HeadlessN8nAdapter:
    """Probe readiness whenever an endpoint exists, even before owner setup."""

    return HeadlessN8nAdapter(N8nGatewayExecutor(config) if config.configured else None)


def _transport_guard_for(config: N8nGatewayConfig) -> ExternalTransportGuard:
    """Keep the local n8n control plane serialized without self-throttling it.

    The public n8n API has no project-owned rate declaration to fetch.  For the
    loopback process we start and own, the bounded 30 requests/minute policy is
    an explicit local operating limit.  It is deliberately not applied to a
    remote n8n host or any other external provider.
    """

    if str(config.deployment_mode or "local").casefold() != "local":
        return default_external_transport_guard()
    return ExternalTransportGuard(
        rate_limits=ScopedRateLimitGovernor(
            {
                _LOCAL_N8N_CONTROL_PLANE_SCOPE: _LOCAL_N8N_CONTROL_PLANE_POLICY,
                _LOCAL_STOCK_AI_CALLBACK_PROBE_SCOPE: _LOCAL_N8N_CONTROL_PLANE_POLICY,
            }
        )
    )


def _transport_scope(config: N8nGatewayConfig, method: str, parsed: Any) -> str:
    """Return a scoped guard key without letting loopback ports alias each other."""

    base = urlsplit(config.base_url)
    if (
        str(config.deployment_mode or "local").casefold() == "local"
        and (parsed.hostname or "").casefold() == (base.hostname or "").casefold()
        and parsed.port == base.port
    ):
        return _LOCAL_N8N_CONTROL_PLANE_SCOPE
    callback = urlsplit(config.callback_url)
    if (
        str(config.deployment_mode or "local").casefold() == "local"
        and (parsed.hostname or "").casefold() == (callback.hostname or "").casefold()
        and parsed.port == callback.port
        and parsed.path == "/health"
    ):
        return _LOCAL_STOCK_AI_CALLBACK_PROBE_SCOPE
    return (
        f"automation:n8n:{str(method).upper()}:"
        f"{(parsed.hostname or 'unknown').casefold()}:{parsed.port or _default_port(parsed)}:"
        f"{parsed.path or '/'}"
    )


def _default_port(parsed: Any) -> int:
    return 443 if str(parsed.scheme).casefold() == "https" else 80


def _trigger_node(trigger: Mapping[str, Any], digest: str) -> dict[str, Any]:
    return {
        "id": _node_id(digest, "trigger"),
        "name": "Compiler Trigger",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.3,
        "position": [0, 0],
        "parameters": {"rule": {"interval": [_schedule_interval(trigger)]}},
    }


def _schedule_interval(trigger: Mapping[str, Any]) -> dict[str, Any]:
    interval_seconds = int(trigger.get("interval_seconds") or 0)
    if interval_seconds:
        if interval_seconds < 60:
            return {"field": "seconds", "secondsInterval": max(1, interval_seconds)}
        if interval_seconds < 3600 and interval_seconds % 60 == 0:
            return {"field": "minutes", "minutesInterval": max(1, interval_seconds // 60)}
        if interval_seconds < 86400 and interval_seconds % 3600 == 0:
            return {"field": "hours", "hoursInterval": max(1, interval_seconds // 3600)}
    frequency = str(trigger.get("frequency") or "daily").casefold()
    hour, minute = _clock(str(trigger.get("at") or "09:00"))
    if frequency == "weekly":
        weekdays = trigger.get("weekdays") or trigger.get("days") or [1]
        return {
            "field": "weeks",
            "weeksInterval": max(1, int(trigger.get("every") or 1)),
            "triggerAtDay": [int(item) for item in weekdays],
            "triggerAtHour": hour,
            "triggerAtMinute": minute,
        }
    cron = str(trigger.get("cron") or trigger.get("cron_expression") or "").strip()
    if frequency == "cron" and cron:
        return {"field": "cronExpression", "expression": cron}
    return {
        "field": "days",
        "daysInterval": max(1, int(trigger.get("every") or 1)),
        "triggerAtHour": hour,
        "triggerAtMinute": minute,
    }


def _event_envelope_node(
    definition: Mapping[str, Any],
    digest: str,
    context: Mapping[str, Any],
    callback_token: str,
) -> dict[str, Any]:
    envelope = {
        "event_type": "automation.n8n.trigger",
        "payload": {
            "automation_id": context.get("automation_id"),
            "automation_version": context.get("automation_version"),
            "submission_id": context.get("submission_id"),
            "source": "n8n",
            "compiler_digest": digest,
            "compiled_stage_count": len(definition.get("stages") or ()),
            "market_calendar": (definition.get("trigger") or {}).get("market_calendar"),
        },
    }
    envelope_json = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    script = (
        "const crypto = require('crypto');\n"
        f"const event = {envelope_json};\n"
        "event.payload.source_event_id = `n8n:${$execution.id}`;\n"
        "const timestamp = Math.floor(Date.now() / 1000).toString();\n"
        "const nonce = `${$execution.id}:${Date.now()}`;\n"
        "const body = JSON.stringify(event);\n"
        f"const signature = crypto.createHmac('sha256', {json.dumps(callback_token)}).update(`${{timestamp}}.${{nonce}}.` + body).digest('hex');\n"
        # n8n's JSON request mode can serialize an equivalent object with
        # different bytes. The Host authenticates the exact wire body, so
        # retain the signed JSON and make the HTTP node transmit it verbatim.
        "event.__stock_ai_callback = { timestamp, nonce, signature, body };\n"
        "return [{ json: event }];"
    )
    return {
        "id": _node_id(digest, "event-envelope"),
        "name": "Compiler Event Envelope",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [260, 0],
        "parameters": {"mode": "runOnceForAllItems", "jsCode": script},
    }


def _callback_node(callback_url: str, callback_token: str, digest: str) -> dict[str, Any]:
    return {
        "id": _node_id(digest, "stock-ai-callback"),
        "name": "Stock AI Runtime Callback",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.5,
        "position": [520, 0],
        "retryOnFail": True,
        "maxTries": 5,
        "waitBetweenTries": 2000,
        "onError": "stopWorkflow",
        "parameters": {
            "method": "POST",
            "url": callback_url,
            "sendHeaders": True,
            "specifyHeaders": "keypair",
            "headerParameters": {
                "parameters": [
                    {"name": AUTOMATION_SOURCE_HEADER, "value": "n8n"},
                    {"name": AUTOMATION_CALLBACK_HEADER, "value": callback_token},
                    {
                        "name": AUTOMATION_TIMESTAMP_HEADER,
                        "value": "={{ $json.__stock_ai_callback.timestamp }}",
                    },
                    {
                        "name": AUTOMATION_NONCE_HEADER,
                        "value": "={{ $json.__stock_ai_callback.nonce }}",
                    },
                    {
                        "name": AUTOMATION_SIGNATURE_HEADER,
                        "value": "={{ $json.__stock_ai_callback.signature }}",
                    },
                ]
            },
            "sendBody": True,
            "contentType": "raw",
            "rawContentType": "application/json",
            "body": "={{ $json.__stock_ai_callback.body }}",
            "options": {"timeout": 15000},
        },
    }


def _clock(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        return 9, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return 9, 0
    return hour, minute


def _node_id(digest: str, suffix: str) -> str:
    return hashlib.sha256(f"{digest}:{suffix}".encode("utf-8")).hexdigest()[:32]


def _workflow_is_active(workflow: Mapping[str, Any]) -> bool:
    return bool(workflow.get("active") or workflow.get("activeVersion"))


def _not_ready_result(readiness: Mapping[str, Any]) -> dict[str, Any]:
    """Return a durable, secret-free pending receipt instead of fake success."""

    return {
        "accepted": False,
        "submission_status": "pending",
        "durable_submission_required": True,
        "recoverable": True,
        "healthy": bool(readiness.get("healthy")),
        "owner_setup_complete": bool(readiness.get("owner_setup_complete")),
        "setup_required": bool(readiness.get("setup_required")),
        "api_key_configured": bool(readiness.get("api_key_configured")),
        "api_authenticated": bool(readiness.get("api_authenticated")),
        "callback_reachable": bool(readiness.get("callback_reachable")),
        "readiness_code": str(readiness.get("readiness_code") or "not_ready"),
        "reason": str(readiness.get("user_message") or "n8n is not deployment-ready"),
    }


def _request_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
    *,
    certificate_sha256: str | None = None,
    mtls_certificate_path: str | None = None,
    mtls_key_path: str | None = None,
    ca_bundle_path: str | None = None,
) -> Mapping[str, Any]:
    request = Request(url, data=body, headers=dict(headers), method=method)
    context = ssl.create_default_context(cafile=ca_bundle_path or None)
    if mtls_certificate_path and mtls_key_path:
        context.load_cert_chain(mtls_certificate_path, mtls_key_path)
    opener = build_opener(_RejectRedirect(), HTTPSHandler(context=context))
    with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310 - validated n8n endpoint
        if certificate_sha256:
            _verify_peer_certificate_pin(response, certificate_sha256)
        raw = response.read().decode("utf-8")
    value = json.loads(raw) if raw else {}
    if not isinstance(value, dict):
        raise ValueError("n8n response must be a JSON object")
    return value


class _RejectRedirect(HTTPRedirectHandler):
    """Do not allow a configured n8n endpoint to forward API credentials."""

    def redirect_request(
        self,
        request: Request,
        response: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Request | None:
        raise HTTPError(
            request.full_url,
            code,
            "n8n redirects are not allowed",
            headers,
            response,
        )


def _verify_peer_certificate_pin(response: Any, expected_sha256: str) -> None:
    try:
        certificate = response.fp.raw._sock.getpeercert(binary_form=True)
    except (AttributeError, OSError) as exc:
        raise RuntimeError("remote n8n TLS peer certificate is unavailable for pin verification") from exc
    observed_sha256 = hashlib.sha256(certificate).hexdigest()
    if not hmac.compare_digest(observed_sha256, expected_sha256.casefold()):
        raise ssl.SSLError("remote n8n TLS certificate pin does not match")
