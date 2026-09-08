from __future__ import annotations

import json
import sqlite3
from typing import Any

from open_stock_ai.storage.sqlite_store import SQLiteStore

from .policy_proposal import PolicyProposalManager


class MemoryRetriever:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def retrieve(self, *, account_id: str, run_state: dict[str, Any] | None = None, limit: int = 20) -> dict[str, Any]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            episodes = conn.execute(
                "select * from agent_learning_episodes where account_id=? order by updated_at desc limit ?",
                (account_id, max(1, min(limit, 100))),
            ).fetchall()
            reflections = conn.execute(
                "select * from agent_reflections where account_id=? order by id desc limit ?",
                (account_id, max(1, min(limit, 100))),
            ).fetchall()
        return {
            "schema_version": "open_stock_ai.three_layer_memory.v1",
            "run_memory": dict(run_state or {}),
            "episodic_memory": [dict(row) for row in episodes],
            "reflection_memory": [self._reflection(row) for row in reflections],
            "policy_proposal_memory": PolicyProposalManager(self.store).list(account_id=account_id, limit=limit),
            "boundary": "proposals_are_inactive_until_exact_evaluation_shadow_and_human_promotion",
        }

    def _reflection(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("lessons_json", "next_rules_json", "payload_json"):
            try:
                item[key.removesuffix("_json")] = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                item[key.removesuffix("_json")] = []
        return item
