from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection


@dataclass(frozen=True, slots=True)
class ApprovalRequiredError(PermissionError):
    approval: dict[str, Any]

    def __str__(self) -> str:
        return f"Approval required: {self.approval['approval_id']}"


class ApprovalManager:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        with self._connect() as conn:
            apply_migrations(conn)

    def require(
        self,
        *,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        resource_scope: dict[str, Any],
        risk_class: str,
        expires_seconds: int = 300,
    ) -> dict[str, Any]:
        digest = argument_digest(tool_name, arguments, resource_scope)
        existing = self._matching(run_id, step_id, digest)
        if existing and existing["status"] == "approved" and _not_expired(existing["expires_at"]):
            return existing
        if existing and existing["status"] == "pending":
            raise ApprovalRequiredError(existing)
        if existing and existing["status"] == "denied":
            raise PermissionError(
                f"The user denied approval {existing['approval_id']} for these exact arguments; revise the plan."
            )
        approval_id = f"AAP-{uuid4().hex}"
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=max(30, min(expires_seconds, 3600)))).isoformat()
        payload = {
            "arguments": arguments,
            "resource_scope": resource_scope,
            "cannot_be_self_approved_by_model": True,
        }
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_approvals(
                    approval_id, run_id, capability, resource_scope_json, status,
                    requested_at, expires_at, payload_json, step_id, tool_name,
                    argument_digest, risk_class
                ) values (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    run_id,
                    tool_name,
                    _json(resource_scope),
                    now.isoformat(),
                    expires_at,
                    _json(payload),
                    step_id,
                    tool_name,
                    digest,
                    risk_class,
                ),
            )
            conn.commit()
        raise ApprovalRequiredError(self.get(approval_id) or {})

    def issue_challenge(self, approval_id: str) -> dict[str, str]:
        approval = self.get(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        if approval["status"] != "pending" or not _not_expired(approval["expires_at"]):
            raise ValueError("Approval is no longer pending")
        challenge = secrets.token_urlsafe(32)
        payload = dict(approval["payload"])
        payload["ui_challenge_digest"] = hashlib.sha256(challenge.encode("utf-8")).hexdigest()
        payload["ui_challenge_issued_at"] = _now()
        with self._connect() as conn:
            conn.execute(
                "update agent_approvals set payload_json=? where approval_id=? and status='pending'",
                (_json(payload), approval_id),
            )
            conn.commit()
        return {"approval_id": approval_id, "challenge": challenge}

    def resolve(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        challenge: str,
    ) -> dict[str, Any]:
        if decided_by.casefold() in {"agent", "codex", "model"}:
            raise PermissionError("A model cannot approve its own action")
        approval = self.get(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        if approval["status"] != "pending":
            raise ValueError("Approval is no longer pending")
        expected = str(approval["payload"].get("ui_challenge_digest") or "")
        actual = hashlib.sha256(str(challenge or "").encode("utf-8")).hexdigest()
        if not expected or not hmac.compare_digest(expected, actual):
            raise PermissionError("Approval requires a valid one-time Stock AI UI challenge")
        status = "approved" if approved and _not_expired(approval["expires_at"]) else "denied"
        payload = dict(approval["payload"])
        payload.pop("ui_challenge_digest", None)
        payload.pop("ui_challenge_issued_at", None)
        with self._connect() as conn:
            conn.execute(
                """
                update agent_approvals
                   set status=?, decided_at=?, decided_by=?, result_json=?, payload_json=?
                 where approval_id=? and status='pending'
                """,
                (
                    status,
                    _now(),
                    decided_by,
                    _json({"approved": status == "approved"}),
                    _json(payload),
                    approval_id,
                ),
            )
            conn.commit()
        return self.get(approval_id) or {}

    def consume(self, approval_id: str, *, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update agent_approvals set status='consumed', result_json=?
                 where approval_id=? and status='approved'
                """,
                (_json(result), approval_id),
            )
            conn.commit()

    def pending(self, run_id: str | None = None) -> list[dict[str, Any]]:
        query = "select approval_id from agent_approvals where status='pending'"
        params: tuple[Any, ...] = ()
        if run_id:
            query += " and run_id=?"
            params = (run_id,)
        query += " order by requested_at"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [item for row in rows if (item := self.get(row[0])) is not None]

    def list(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select approval_id from agent_approvals
                 where run_id=? order by requested_at
                """,
                (run_id,),
            ).fetchall()
        return [item for row in rows if (item := self.get(row[0])) is not None]

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_approvals where approval_id=?", (approval_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "schema_version": "open_stock_ai.agent_approval.v1",
            "approval_id": row["approval_id"],
            "run_id": row["run_id"],
            "step_id": row["step_id"],
            "tool_name": row["tool_name"] or row["capability"],
            "resource_scope": json.loads(row["resource_scope_json"]),
            "argument_digest": row["argument_digest"],
            "risk_class": row["risk_class"],
            "status": row["status"],
            "requested_at": row["requested_at"],
            "decided_at": row["decided_at"],
            "expires_at": row["expires_at"],
            "decided_by": row["decided_by"],
            "payload": json.loads(row["payload_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def latest_for_step(self, run_id: str, step_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select approval_id from agent_approvals
                 where run_id=? and step_id=?
                 order by requested_at desc limit 1
                """,
                (run_id, step_id),
            ).fetchone()
        return self.get(row[0]) if row else None

    def _matching(self, run_id: str, step_id: str, digest: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select approval_id from agent_approvals
                 where run_id=? and step_id=? and argument_digest=?
                 order by requested_at desc limit 1
                """,
                (run_id, step_id, digest),
            ).fetchone()
        return self.get(row[0]) if row else None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def argument_digest(tool_name: str, arguments: dict[str, Any], scope: dict[str, Any]) -> str:
    encoded = _json({"tool": tool_name, "arguments": arguments, "scope": scope})
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _not_expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
