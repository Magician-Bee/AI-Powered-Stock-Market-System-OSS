from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LiveExecutorDisabled:
    reason: str = "live trading is disabled"

    def submit(self, order: dict) -> dict:
        return {
            "schema_version": "open_stock_ai.live_execution_disabled.v1",
            "submitted": False,
            "order": order,
            "reason": self.reason,
            "execution_boundary": "live_order_blocked",
        }
