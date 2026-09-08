from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OpenAICompatibleClient:
    base_url: str
    api_key: str
    model: str

    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    def preview_chat_request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.llm_request_preview.v1",
            "provider": "openai_compatible",
            "base_url": self.base_url,
            "model": self.model,
            "enabled": self.enabled(),
            "api_key_configured": bool(self.api_key),
            "message_count": len(messages),
            "messages": messages,
            "execution_boundary": "preview_only_no_network_call",
        }
