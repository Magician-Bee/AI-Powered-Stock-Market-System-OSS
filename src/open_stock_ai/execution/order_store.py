from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OrderStore:
    def append_preview(self, order: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.order_preview.v1",
            "stored": False,
            "order": order,
            "storage_boundary": "preview_only_use_trade_store_for_paper_orders",
        }
