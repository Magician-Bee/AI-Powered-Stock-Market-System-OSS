from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from open_stock_ai.agent_runtime.provider_capabilities import ProviderCapabilityProfile
from open_stock_ai.agent_runtime.transport_guard import ExternalTransportGuard
from open_stock_ai.agent_runtime.providers.normalizer import (
    ProviderOutputNormalizer,
    ProviderProtocolError,
)


EventSink = Callable[[dict[str, Any]], Awaitable[None] | None] | None

# A slow generation may legitimately use the configured provider timeout, but
# an unreachable remote endpoint must not leave a durable Agent Run apparently
# running for minutes before the recovery ladder can see a transport failure.
_MAX_PROVIDER_CONNECT_TIMEOUT_SECONDS = 10.0
_ADMISSION_WAIT_SECONDS = 1.05
# A 20B local/remote model can legitimately keep the single permitted slot
# busy for tens of seconds. Keep the next request in this provider-level queue
# instead of repeatedly failing a durable Run and starting a new recovery Run.
_MAX_ADMISSION_WAITS_PER_REQUEST = 60


def endpoint_transport_scope(scope_prefix: str, endpoint: str) -> str:
    """Return a stable, credential-free transport scope for one upstream.

    The shared guard is process-wide, but different configured endpoints must
    not open one another's circuits.  Callers which need a common circuit
    (for example generation and health) use the same prefix; distinct roles
    can retain independent admission state with different prefixes.  The
    normalized origin is hashed so private hostnames and paths are not emitted
    in circuit decision receipts.
    """

    parsed = urlsplit(str(endpoint or "").strip())
    scheme = (parsed.scheme or "unknown").casefold()
    host = (parsed.hostname or "unconfigured").casefold()
    try:
        port = parsed.port
    except ValueError:
        # Endpoint validation owns the user-facing error.  Scope derivation
        # must remain total so malformed configuration cannot bypass circuit
        # admission by crashing before it.
        port = None
    origin = f"{scheme}://{host}:{port if port is not None else 'default'}"
    identity = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:16]
    prefix = str(scope_prefix).strip().rstrip(":") or "external"
    return f"{prefix}:{identity}"


def provider_transport_scope(provider_id: str, endpoint: str) -> str:
    """Return the shared provider scope for generation and health checks."""

    return endpoint_transport_scope(f"provider:{str(provider_id).strip()}", endpoint)


def _is_provider_admission_wait(error: RuntimeError) -> bool:
    """Return true while another request owns the provider's safe slot."""

    message = str(error)
    return (
        message.startswith("external transport rate limit blocked provider:")
        and message.rsplit(": ", 1)[-1]
        in {"unverified_policy_conservative_interval", "maximum_concurrency_reached"}
    )


def _openai_message_content(response: httpx.Response) -> Any:
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI-compatible provider returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    native_calls = message.get("tool_calls") or []
    actions: list[dict[str, Any]] = []
    for index, item in enumerate(native_calls):
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(function.get("name") or item.get("name") or "").strip()
        if not name:
            continue
        arguments = function.get("arguments", item.get("arguments", "{}"))
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        actions.append(
            {
                "id": str(item.get("id") or f"native-{index + 1}"),
                "tool": name,
                "arguments": str(arguments or "{}"),
            }
        )
    if actions:
        # Some Ollama/OpenAI-compatible models emit native ``tool_calls`` even
        # when the Host requested a JSON response and supplied capabilities in
        # the prompt. Translate that valid intent into the provider-neutral
        # Universal protocol instead of sending an empty content string to the
        # protocol-repair model, which would otherwise erase the tool request.
        return {
            "status": "need_tools",
            "message": "Requesting Host-validated tool evidence.",
            "actions": actions,
            "evidence_ids": [],
            "remaining_gaps": ["waiting_for_tool_results"],
            "decision": None,
            "routing": None,
            "result": None,
            "interaction": None,
        }
    return content


def _protocol_correction_prompt(
    *,
    output_schema: dict[str, Any],
    invalid_output: Any,
) -> str:
    schema = json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
    if isinstance(invalid_output, str):
        raw_output = invalid_output
    else:
        raw_output = json.dumps(invalid_output, ensure_ascii=False, default=str)
    return (
        "Required JSON schema:\n"
        f"{schema[:24_000]}\n\n"
        "Invalid model output to convert:\n"
        f"{raw_output[:32_000]}"
    )


class OpenAICompatibleProvider:
    provider_id = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        transport_guard: ExternalTransportGuard | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.transport_guard = transport_guard

    @property
    def transport_scope(self) -> str:
        return provider_transport_scope(self.provider_id, self.base_url)

    def _http_timeout(self) -> httpx.Timeout:
        """Keep connection establishment bounded independently of generation."""
        total = max(0.1, float(self.timeout_seconds))
        return httpx.Timeout(
            timeout=total,
            connect=min(total, _MAX_PROVIDER_CONNECT_TIMEOUT_SECONDS),
        )

    async def start_session(self, session_id: str, *, project_root: str) -> None:
        del session_id, project_root

    async def generate(
        self,
        session_id: str,
        prompt: str,
        *,
        event_sink: EventSink = None,
    ) -> str:
        result = await self.generate_structured(
            session_id,
            prompt,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["content"],
                "properties": {"content": {"type": "string"}},
            },
            event_sink=event_sink,
        )
        return str(result["content"])

    async def generate_structured(
        self,
        session_id: str,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        event_sink: EventSink = None,
    ) -> dict[str, Any]:
        if not self.base_url or not self.model:
            raise RuntimeError("OpenAI-compatible provider requires a base URL and model")
        endpoint = (
            self.base_url
            if self.base_url.endswith("/chat/completions")
            else f"{self.base_url}/chat/completions"
        )
        headers = self._headers()
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "open_stock_ai_agent_turn",
                "strict": True,
                "schema": output_schema,
            },
        }
        schema_instruction = json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
        request = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return exactly one JSON object matching the supplied protocol. "
                        "Do not use markdown fences. The application runtime instructions and schema are "
                        "not the end user's request: answer the supplied OBJECTIVE in the response message "
                        "and never claim that the user requested JSON, a schema, or a protocol. "
                        "Required JSON schema:\n"
                        f"{schema_instruction}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            # A bounded completion is essential for single-queue local and
            # remote Ollama servers.  Without it a disconnected structured
            # request may keep generating until the model context is full and
            # block every later Agent Run.
            "max_tokens": 4_096,
            "response_format": response_format,
        }
        if "gpt-oss" in self.model.lower():
            # GPT-OSS cannot fully disable thinking.  Low effort preserves its
            # protocol reasoning while keeping interactive Agent runs usable.
            request["reasoning_effort"] = "low"
        started_at = time.perf_counter()
        await _emit(
            event_sink,
            {
                "type": "model.provider.requested",
                "provider": self.provider_id,
                "model": self.model,
                "session_id": session_id,
            },
        )
        try:
            async with httpx.AsyncClient(
                timeout=self._http_timeout(),
                **({"transport": self.transport} if self.transport is not None else {}),
            ) as client:
                response = await self._post_with_transient_retry(
                    client,
                    endpoint,
                    headers=headers,
                    request=request,
                    event_sink=event_sink,
                    phase="generation",
                )
                if response.status_code in {400, 404, 415, 422}:
                    request.pop("response_format", None)
                    request["messages"][0]["content"] += (
                        " The server does not support response_format; raw JSON is still required."
                    )
                    response = await self._post_with_transient_retry(
                        client,
                        endpoint,
                        headers=headers,
                        request=request,
                        event_sink=event_sink,
                        phase="schema_fallback",
                    )
                response.raise_for_status()
            content = _openai_message_content(response)
            try:
                result = _json_object(content)
            except ProviderProtocolError as protocol_error:
                await _emit(
                    event_sink,
                    {
                        "type": "model.provider.protocol_retry",
                        "provider": self.provider_id,
                        "model": self.model,
                        "reason": protocol_error.code,
                    },
                )
                correction_request = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You repair model output protocols. Return exactly one JSON object "
                                "that matches the supplied schema. Preserve the useful answer and "
                                "evidence references from the invalid output. Do not explain the repair, "
                                "do not use markdown fences, and do not emit any text outside the object."
                            ),
                        },
                        {
                            "role": "user",
                            "content": _protocol_correction_prompt(
                                output_schema=output_schema,
                                invalid_output=content,
                            ),
                        },
                    ],
                    "temperature": 0,
                    "max_tokens": 4_096,
                }
                if "gpt-oss" in self.model.lower():
                    correction_request["reasoning_effort"] = "low"
                if "response_format" in request:
                    correction_request["response_format"] = response_format
                async with httpx.AsyncClient(
                    timeout=self._http_timeout(),
                    **({"transport": self.transport} if self.transport is not None else {}),
                ) as client:
                    response = await self._post_with_transient_retry(
                        client,
                        endpoint,
                        headers=headers,
                        request=correction_request,
                        event_sink=event_sink,
                        phase="protocol_repair",
                    )
                    if response.status_code in {400, 404, 415, 422}:
                        correction_request.pop("response_format", None)
                        correction_request["messages"][0]["content"] += (
                            " The server does not support response_format; raw JSON is still required."
                        )
                        response = await self._post_with_transient_retry(
                            client,
                            endpoint,
                            headers=headers,
                            request=correction_request,
                            event_sink=event_sink,
                            phase="protocol_repair_schema_fallback",
                        )
                    response.raise_for_status()
                result = _json_object(_openai_message_content(response))
        except Exception as exc:
            await _emit(
                event_sink,
                {
                    "type": "model.provider.failed",
                    "provider": self.provider_id,
                    "model": self.model,
                    "error": str(exc),
                },
            )
            raise
        await _emit(
            event_sink,
            {
                "type": "model.provider.completed",
                "provider": self.provider_id,
                "model": self.model,
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
            },
        )
        return result

    async def _post_with_transient_retry(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        headers: dict[str, str],
        request: dict[str, Any],
        event_sink: EventSink,
        phase: str,
    ) -> httpx.Response:
        """Retry one transient transport/5xx failure, never semantic failures."""

        attempt = 1
        admission_waits = 0
        while attempt <= 2:
            try:
                operation = lambda: client.post(endpoint, headers=headers, json=request)
                response = (
                    await self.transport_guard.call(
                        self.transport_scope, operation
                    )
                    if self.transport_guard is not None
                    else await operation()
                )
            except RuntimeError as exc:
                # A process-wide guard may have just admitted a health check
                # for this endpoint. That short interval is queueing, not a
                # provider failure that should restart the durable Agent Run.
                if (
                    _is_provider_admission_wait(exc)
                    and admission_waits < _MAX_ADMISSION_WAITS_PER_REQUEST
                ):
                    admission_waits += 1
                    await _emit(
                        event_sink,
                        {
                            "type": "model.provider.admission_wait",
                            "provider": self.provider_id,
                            "model": self.model,
                            "phase": phase,
                            "wait_seconds": _ADMISSION_WAIT_SECONDS,
                            "reason": str(exc).rsplit(": ", 1)[-1],
                        },
                    )
                    await asyncio.sleep(_ADMISSION_WAIT_SECONDS)
                    continue
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == 2:
                    raise
                await _emit(
                    event_sink,
                    {
                        "type": "model.provider.transport_retry",
                        "provider": self.provider_id,
                        "model": self.model,
                        "phase": phase,
                        "attempt": attempt + 1,
                        "reason": type(exc).__name__,
                    },
                )
                await asyncio.sleep(0.1)
                attempt += 1
                continue
            if response.status_code not in {408, 429} and response.status_code < 500:
                return response
            if attempt == 2:
                return response
            await _emit(
                event_sink,
                {
                    "type": "model.provider.transport_retry",
                    "provider": self.provider_id,
                    "model": self.model,
                    "phase": phase,
                    "attempt": attempt + 1,
                    "reason": f"HTTP {response.status_code}",
                },
            )
            await asyncio.sleep(0.1)
            attempt += 1
        raise RuntimeError("Transient provider retry loop ended unexpectedly")

    async def continue_with_tool_results(
        self,
        session_id: str,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        event_sink: EventSink = None,
    ) -> dict[str, Any]:
        return await self.generate_structured(
            session_id,
            prompt,
            output_schema,
            event_sink=event_sink,
        )

    async def cancel(self, session_id: str) -> None:
        del session_id

    async def compact_context(self, session_id: str) -> None:
        del session_id

    async def health(self) -> dict[str, Any]:
        if not self.base_url or not self.model:
            return {
                "provider_id": self.provider_id,
                "configured": False,
                "reachable": False,
            }
        models_endpoint = (
            self.base_url.rsplit("/chat/completions", 1)[0]
            if self.base_url.endswith("/chat/completions")
            else self.base_url
        )
        models_endpoint = f"{models_endpoint.rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    timeout=min(self.timeout_seconds, 15.0),
                    connect=min(
                        self.timeout_seconds,
                        _MAX_PROVIDER_CONNECT_TIMEOUT_SECONDS,
                    ),
                ),
                transport=self.transport,
            ) as client:
                operation = lambda: client.get(models_endpoint, headers=self._headers())
                response = (
                    await self.transport_guard.call(
                        self.transport_scope, operation
                    )
                    if self.transport_guard is not None
                    else await operation()
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            return {
                "provider_id": self.provider_id,
                "configured": True,
                "reachable": False,
                "error": str(exc),
            }
        model_ids = [
            str(item.get("id"))
            for item in payload.get("data") or []
            if isinstance(item, dict) and item.get("id")
        ]
        return {
            "provider_id": self.provider_id,
            "configured": True,
            "reachable": True,
            "model": self.model,
            "model_available": not model_ids or self.model in model_ids,
            "model_count": len(model_ids),
        }

    def capabilities(self) -> dict[str, Any]:
        profile = ProviderCapabilityProfile(
            provider=self.provider_id,
            model=self.model or None,
            native_tool_calling=False,
            json_schema=True,
            parallel_tool_calls=False,
            streaming=False,
            reasoning_format="unknown",
            persistent_session=False,
            schema_fallback=True,
            recommended_protocol="universal_v1",
            conformance_passed=False,
        )
        return {
            "configured": bool(self.base_url and self.model),
            "structured_output": True,
            "persistent_session": False,
            "tool_result_continuation": True,
            "private_chain_of_thought_exposed": False,
            "protocol": "openai_chat_completions",
            "model": self.model or None,
            "credential_configured": bool(self.api_key),
            "profile": profile.model_dump(),
        }

    async def close_session(self, session_id: str) -> None:
        del session_id

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "no-key":
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


class ExternalAgentProvider:
    provider_id = "external-agent"

    def __init__(
        self,
        *,
        endpoint: str,
        framework: str,
        model: str = "",
        token: str = "",
        timeout_seconds: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
        transport_guard: ExternalTransportGuard | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.framework = framework
        self.model = model
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.transport_guard = transport_guard

    @property
    def transport_scope(self) -> str:
        return provider_transport_scope(self.provider_id, self.endpoint)

    async def start_session(self, session_id: str, *, project_root: str) -> None:
        del session_id, project_root

    async def generate(
        self,
        session_id: str,
        prompt: str,
        *,
        event_sink: EventSink = None,
    ) -> str:
        result = await self.generate_structured(
            session_id,
            prompt,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["content"],
                "properties": {"content": {"type": "string"}},
            },
            event_sink=event_sink,
        )
        return str(result["content"])

    async def generate_structured(
        self,
        session_id: str,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        event_sink: EventSink = None,
    ) -> dict[str, Any]:
        if not self.endpoint:
            raise RuntimeError("External Agent provider requires an endpoint")
        request = {
            "protocol": (
                "open_stock_ai.universal_provider_turn.v1"
                if "status" in set(output_schema.get("required") or [])
                else "open_stock_ai.advanced_provider_turn.v1"
            ),
            "session_id": session_id,
            "prompt": prompt,
            "output_schema": output_schema,
            "framework": self.framework,
            "model": self.model or None,
        }
        started_at = time.perf_counter()
        await _emit(
            event_sink,
            {
                "type": "model.provider.requested",
                "provider": self.provider_id,
                "framework": self.framework,
                "model": self.model or None,
                "session_id": session_id,
            },
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                **({"transport": self.transport} if self.transport is not None else {}),
            ) as client:
                operation = lambda: client.post(
                    self.endpoint,
                    headers=self._headers(),
                    json=request,
                )
                response = (
                    await self.transport_guard.call(
                        self.transport_scope, operation
                    )
                    if self.transport_guard is not None
                    else await operation()
                )
                response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and isinstance(result.get("decision"), dict):
                result = result["decision"]
            elif isinstance(result, dict) and "output" in result:
                result = result["output"]
            result = ProviderOutputNormalizer().normalize(result).payload
        except Exception as exc:
            await _emit(
                event_sink,
                {
                    "type": "model.provider.failed",
                    "provider": self.provider_id,
                    "framework": self.framework,
                    "error": str(exc),
                },
            )
            raise
        await _emit(
            event_sink,
            {
                "type": "model.provider.completed",
                "provider": self.provider_id,
                "framework": self.framework,
                "model": self.model or None,
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
            },
        )
        return result

    async def continue_with_tool_results(
        self,
        session_id: str,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        event_sink: EventSink = None,
    ) -> dict[str, Any]:
        return await self.generate_structured(
            session_id,
            prompt,
            output_schema,
            event_sink=event_sink,
        )

    async def cancel(self, session_id: str) -> None:
        del session_id

    async def compact_context(self, session_id: str) -> None:
        del session_id

    async def health(self) -> dict[str, Any]:
        if not self.endpoint:
            return {
                "provider_id": self.provider_id,
                "configured": False,
                "reachable": False,
            }
        try:
            async with httpx.AsyncClient(
                timeout=min(self.timeout_seconds, 15.0),
                transport=self.transport,
            ) as client:
                operation = lambda: client.get(self.endpoint, headers=self._headers())
                response = (
                    await self.transport_guard.call(
                        self.transport_scope, operation
                    )
                    if self.transport_guard is not None
                    else await operation()
                )
        except Exception as exc:
            return {
                "provider_id": self.provider_id,
                "configured": True,
                "reachable": False,
                "error": str(exc),
            }
        return {
            "provider_id": self.provider_id,
            "configured": True,
            "reachable": response.status_code < 500,
            "status_code": response.status_code,
        }

    def capabilities(self) -> dict[str, Any]:
        profile = ProviderCapabilityProfile(
            provider=self.provider_id,
            model=self.model or None,
            native_tool_calling=False,
            json_schema=False,
            parallel_tool_calls=False,
            streaming=False,
            reasoning_format="unknown",
            persistent_session=False,
            schema_fallback=True,
            recommended_protocol="universal_v1",
            conformance_passed=False,
        )
        return {
            "configured": bool(self.endpoint),
            "structured_output": True,
            "persistent_session": False,
            "tool_result_continuation": True,
            "private_chain_of_thought_exposed": False,
            "protocol": "open_stock_ai.provider_turn.v1",
            "framework": self.framework or None,
            "model": self.model or None,
            "credential_configured": bool(self.token),
            "profile": profile.model_dump(),
        }

    async def close_session(self, session_id: str) -> None:
        del session_id

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


async def _emit(event_sink: EventSink, event: dict[str, Any]) -> None:
    if event_sink is None:
        return
    result = event_sink(event)
    if inspect.isawaitable(result):
        await result


def _json_object(value: Any) -> dict[str, Any]:
    return ProviderOutputNormalizer().normalize(value).payload


__all__ = ["ExternalAgentProvider", "OpenAICompatibleProvider"]
