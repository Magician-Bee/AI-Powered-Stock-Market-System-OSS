from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.sqlite_store import SQLiteStore


class PolicyProposalManager:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create(
        self,
        *,
        account_id: str,
        reflection_id: int | None,
        title: str,
        rules: list[str],
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        proposal_id = f"PP-{uuid4().hex}"
        now = _now()
        with self.store._connect() as conn:
            conn.execute(
                """
                insert into policy_proposals (
                    proposal_id, account_id, reflection_id, created_at, updated_at, status,
                    title, rules_json, evidence_json, payload_json
                ) values (?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?)
                """,
                (
                    proposal_id, account_id, reflection_id, now, now, title,
                    json.dumps(rules, ensure_ascii=False),
                    json.dumps(evidence or {}, ensure_ascii=False, default=str),
                    json.dumps({"activation": False}, ensure_ascii=False),
                ),
            )
            conn.commit()
        return self.get(proposal_id) or {}

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from policy_proposals where proposal_id = ?", (proposal_id,)).fetchone()
        return _proposal(row) if row else None

    def list(self, *, account_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            if account_id:
                rows = conn.execute(
                    "select * from policy_proposals where account_id = ? order by updated_at desc limit ?",
                    (account_id, max(1, min(limit, 200))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "select * from policy_proposals order by updated_at desc limit ?",
                    (max(1, min(limit, 200)),),
                ).fetchall()
        return [_proposal(row) for row in rows]


def _proposal(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("rules_json", "evidence_json", "evaluation_json", "shadow_json", "payload_json"):
        try:
            item[key.removesuffix("_json")] = json.loads(item.get(key) or "{}")
        except json.JSONDecodeError:
            item[key.removesuffix("_json")] = None
        item.pop(key, None)
    item["schema_version"] = "open_stock_ai.policy_proposal.v1"
    item["active"] = item.get("status") == "promoted"
    return item


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
