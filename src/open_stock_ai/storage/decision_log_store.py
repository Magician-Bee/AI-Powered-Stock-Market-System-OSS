from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from open_stock_ai.storage.sqlite_store import SQLiteStore


@dataclass
class DecisionLogStore:
    store: SQLiteStore

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.save_decision_log(payload)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.recent_decision_logs(limit=limit)

    def review(self, symbol: str | None = None, limit: int = 100) -> dict[str, Any]:
        return self.store.decision_review(symbol=symbol, limit=limit)
