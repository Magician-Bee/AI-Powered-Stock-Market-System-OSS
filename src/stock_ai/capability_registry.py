from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec
from open_stock_ai.agent_runtime.plan_compiler import validate_json_value


_SAFE_AGENT_BROKER_ORDER_TOOLS = frozenset(
    {
        "broker.order.preview",
        "broker.order.propose",
        "broker.order.status",
        "broker.order.cancel_proposal",
    }
)


class CapabilityProvider(Protocol):
    provider_id: str

    def manifest(self) -> list[dict[str, Any]]: ...

    def has_tool(self, name: str) -> bool: ...

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]: ...


class CapabilityRegistry:
    """Single routing and truth layer for every host-executed Agent capability."""

    def __init__(self, providers: Iterable[CapabilityProvider] = ()) -> None:
        self._providers: list[CapabilityProvider] = []
        for provider in providers:
            self.register(provider)

    def register(self, provider: CapabilityProvider) -> None:
        provider_id = str(getattr(provider, "provider_id", "") or "").strip()
        if not provider_id:
            raise ValueError("Capability provider requires provider_id")
        if any(existing.provider_id == provider_id for existing in self._providers):
            raise ValueError(f"Duplicate capability provider: {provider_id}")
        self._providers.append(provider)

    async def prepare(self, context: AgentRunContext) -> None:
        for provider in self._providers:
            method = getattr(provider, "prepare", None)
            if not callable(method):
                continue
            result = method(context)
            if inspect.isawaitable(result):
                await result

    def manifest(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        owners: dict[str, str] = {}
        for provider in self._providers:
            for raw in provider.manifest():
                item = dict(raw)
                name = str(item.get("name") or "").strip()
                if not name:
                    raise ValueError(f"Capability provider {provider.provider_id} returned an unnamed tool")
                _assert_agent_execution_boundary(item)
                if name in owners:
                    raise ValueError(
                        f"Duplicate Agent tool {name}: {owners[name]} and {provider.provider_id}"
                    )
                owners[name] = provider.provider_id
                item["provider"] = provider.provider_id
                items.append(item)
        return items

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        _assert_agent_execution_boundary({"name": name})
        for provider in self._providers:
            if provider.has_tool(name):
                spec = next(
                    (item for item in provider.manifest() if item.get("name") == name),
                    None,
                )
                if spec is None:
                    raise RuntimeError(f"Capability provider did not publish its executable tool: {name}")
                _assert_agent_execution_boundary(spec)
                errors = validate_json_value(arguments, spec.get("input_schema") or {"type": "object"})
                if errors:
                    raise ValueError(f"Invalid arguments for {name}: {errors}")
                result = await provider.execute(name, arguments, context)
                if not isinstance(result, dict):
                    raise RuntimeError(f"Capability {name} returned a non-object result")
                return result
        raise ValueError(f"Unknown Stock AI capability: {name}")

    def describe(self) -> dict[str, Any]:
        providers = []
        for provider in self._providers:
            describe = getattr(provider, "describe", None)
            status = describe() if callable(describe) else {}
            manifest = provider.manifest()
            providers.append(
                {
                    "id": provider.provider_id,
                    "tool_count": len(manifest),
                    "tools": [item.get("name") for item in manifest],
                    **(status if isinstance(status, dict) else {}),
                }
            )
        return {
            "schema_version": "open_stock_ai.capability_registry.v1",
            "provider_count": len(providers),
            "tool_count": sum(item["tool_count"] for item in providers),
            "providers": providers,
            "execution_owner": "host",
            "inventory_is_not_execution": True,
        }


def _assert_agent_execution_boundary(tool: dict[str, Any]) -> None:
    """Keep real brokerage submission outside every Agent capability surface.

    This is enforced by the host registry rather than a model prompt or a
    provider convention.  A future provider cannot publish a real action, and
    a forged call is denied before any provider is dispatched.
    """

    name = str(tool.get("name") or "").strip()
    risk_class = str(tool.get("risk_class") or "").strip()
    if risk_class == "financial_real_action" or name.startswith("live."):
        raise PermissionError("Agent real brokerage actions are permanently denied by host policy")
    if name.startswith("broker.order.") and name not in _SAFE_AGENT_BROKER_ORDER_TOOLS:
        raise PermissionError("Agent broker order submission is permanently denied by host policy")


class BoundCapabilityProvider:
    """Adapter for an existing spec set and its host executor."""

    def __init__(
        self,
        *,
        provider_id: str,
        specs: dict[str, AgentToolSpec],
        executor: Callable[[str, dict[str, Any], AgentRunContext], Awaitable[dict[str, Any]]],
        status: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._specs = specs
        self._executor = executor
        self._status = status

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        return await self._executor(name, arguments, context)

    def describe(self) -> dict[str, Any]:
        if self._status is not None:
            return self._status()
        return {"configured": True, "runtime_ready": True, "health": "ready"}
