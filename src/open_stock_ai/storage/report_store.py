from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from open_stock_ai.storage.sqlite_store import SQLiteStore


@dataclass
class ReportStore:
    store: SQLiteStore

    def save_preview(self, report: str) -> dict[str, Any]:
        return self.store.save_report(report)
