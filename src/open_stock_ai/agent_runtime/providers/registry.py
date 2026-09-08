from __future__ import annotations

from typing import Any

from open_stock_ai.agent_runtime.provider_capabilities import (
    ProviderCapabilityProfile,
    ProviderConformanceReport,
    ProviderConformanceSuite,
    negotiated_profile,
)

from .base import ModelProvider


DISABLED_PROVIDER_INTERFACES = (
    "local-openai",
    "anthropic",
    "gemini",
)


class ProviderRegistry:
    """Own the configured model providers used to build Agent drivers."""

    def __init__(self, providers: tuple[ModelProvider, ...], *, primary: str = "codex") -> None:
        self._providers = {provider.provider_id: provider for provider in providers}
        if primary not in self._providers:
            raise ValueError(f"Primary provider is not enabled: {primary}")
        self.primary = primary
        self._conformance: dict[str, ProviderConformanceReport] = {}

    def get(self, provider_id: str | None = None) -> ModelProvider:
        selected = provider_id or self.primary
        provider = self._providers.get(selected)
        if provider is None:
            raise ValueError(f"Provider is disabled: {selected}")
        return provider

    def describe(self) -> dict[str, Any]:
        enabled = []
        for provider_id, provider in self._providers.items():
            capabilities = provider.capabilities()
            enabled.append(
                {
                    "provider_id": provider_id,
                    "enabled": True,
                    "configured": capabilities.get("configured", True),
                    "primary": provider_id == self.primary,
                    "capabilities": capabilities,
                }
            )
        disabled = [
            {
                "provider_id": provider_id,
                "enabled": False,
                "primary": False,
                "process_started": False,
                "model_loaded": False,
                "requests_sent": 0,
                "interface_reserved": True,
            }
            for provider_id in DISABLED_PROVIDER_INTERFACES
        ]
        return {
            "schema_version": "open_stock_ai.provider_registry.v1",
            "primary_provider": self.primary,
            "items": [*enabled, *disabled],
            "model_capability_tiers": True,
            "conformance": {
                provider_id: report.model_dump(mode="json")
                for provider_id, report in self._conformance.items()
            },
        }

    async def negotiate(
        self,
        provider_id: str,
        *,
        session_id: str,
        force: bool = False,
        include_expensive: bool = False,
    ) -> dict[str, Any]:
        provider = self.get(provider_id)
        report = self._conformance.get(provider_id)
        if report is None or force or include_expensive:
            report = await ProviderConformanceSuite().probe(
                provider,
                session_id=session_id,
                include_expensive=include_expensive,
            )
            self._conformance[provider_id] = report
        capabilities = provider.capabilities()
        profile = ProviderCapabilityProfile.model_validate(capabilities.get("profile") or {
            "provider": provider_id,
            "model": capabilities.get("model"),
        })
        selected = negotiated_profile(profile, report)
        return {
            "profile": selected.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
            "protocol": selected.recommended_protocol,
        }
