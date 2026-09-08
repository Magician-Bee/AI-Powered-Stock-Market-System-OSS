from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class LLMStrategy:
    """Build the deterministic strategy hand-off for the Codex Agent Runtime.

    This is a prompt contract only. It does not make a model request or start a
    local FinGPT/OpenAI-compatible process.
    """

    model_family: str = "codex"

    def build_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = payload.get("symbol") or payload.get("request", {}).get("symbol") or "unknown"
        horizon = payload.get("horizon") or payload.get("request", {}).get("horizon") or "swing"
        return {
            "method": "codex_agent_strategy_prompt_boundary",
            "model_family": self.model_family,
            "ready": True,
            "symbol": symbol,
            "horizon": horizon,
            "system": "You are a paper-trading analyst. Return structured evidence only.",
            "user": (
                f"Analyze {symbol} for {horizon}. Use the supplied Open Stock AI "
                "market snapshot, adapter evidence, research metrics, and risk policy. "
                "Do not bypass RiskEngine or paper-only execution."
            ),
        }
