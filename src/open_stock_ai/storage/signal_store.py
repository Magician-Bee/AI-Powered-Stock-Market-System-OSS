from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from open_stock_ai.storage.sqlite_store import SQLiteStore


@dataclass
class SignalStore:
    store: SQLiteStore

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.save_signal(payload)
