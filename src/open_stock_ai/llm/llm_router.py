from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LLMRouter:
    provider: str = "local_openai_compatible"
    ready: bool = False

    def route(self, task: str) -> dict:
        return {
            "schema_version": "open_stock_ai.llm_route.v1",
            "task": task,
            "provider": self.provider,
            "ready": self.ready,
            "execution_boundary": "route_only_no_network_call",
        }
