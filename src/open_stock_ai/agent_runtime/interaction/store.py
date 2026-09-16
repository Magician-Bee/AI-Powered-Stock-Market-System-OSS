from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, configure_connection

from ..forest import CheckpointLevel, ScopedCheckpointManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


class DurableInteractionStore:
    """Own P71 decision checkpoints and user-proposal persistence."""

    def __init__(
        self,
        db_path: str | Path,
        checkpoints: ScopedCheckpointManager,
    ) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.checkpoints = checkpoints

    def create(
        self,
        *,
        session_id: str,
        run_id: str | None,
        branch_id: str | None,
        waiting_state: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if waiting_state not in {
            "waiting_user_input",
            "waiting_decision",
            "waiting_approval",
        }:
            raise ValueError(
                "Interaction waiting state must be clarification, decision or approval"
            )
        interaction_id = f"INT-{uuid4().hex}"
        timestamp = _now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_decision_checkpoints(
                    interaction_id, session_id, run_id, branch_id, status,
                    interaction_type, created_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction_id,
                    session_id,
                    run_id,
                    branch_id,
                    waiting_state,
                    waiting_state.removeprefix("waiting_"),
                    timestamp,
                    _json(payload),
                ),
            )
            conn.commit()
        self.checkpoints.save(
            level=CheckpointLevel.DECISION,
            session_id=session_id,
            run_id=run_id,
            branch_id=branch_id,
            decision_id=interaction_id,
            payload={"waiting_state": waiting_state, **payload},
            idempotency_key=f"interaction-created:{interaction_id}",
        )
        return {
            "interaction_id": interaction_id,
            "session_id": session_id,
            "run_id": run_id,
            "branch_id": branch_id,
            "status": waiting_state,
            **payload,
        }

    def respond(self, interaction_id: str, response: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_decision_checkpoints where interaction_id=?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(interaction_id)
            if row["status"] == "resolved":
                raise ValueError("Interaction is already resolved")
            conn.execute(
                """
                update agent_decision_checkpoints
                   set status='resolved', responded_at=?, response_json=?
                 where interaction_id=?
                """,
                (timestamp, _json(response), interaction_id),
            )
            conn.commit()
        return {
            "interaction_id": interaction_id,
            "session_id": str(row["session_id"]),
            "run_id": str(row["run_id"] or "") or None,
            "branch_id": str(row["branch_id"] or "") or None,
            "previous_status": str(row["status"]),
            "status": "resolved",
            "responded_at": timestamp,
            "response": response,
            "interaction_payload": _decode(row["payload_json"], {}),
        }

    def get(self, interaction_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_decision_checkpoints where interaction_id=?",
                (interaction_id,),
            ).fetchone()
        if row is None:
            return None
        return self._interaction_row(row)

    def list_for_session(
        self,
        session_id: str,
        *,
        open_only: bool = False,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            sql = "select * from agent_decision_checkpoints where session_id=?"
            if open_only:
                sql += " and status!='resolved'"
            rows = conn.execute(sql + " order by created_at", (session_id,)).fetchall()
        return [self._interaction_row(row) for row in rows]

    def cancel_for_run(self, run_id: str, *, reason: str) -> int:
        """Close unresolved checkpoints when their owning Run ends."""

        timestamp = _now()
        response = _json({"cancelled": True, "reason": reason})
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update agent_decision_checkpoints
                   set status='cancelled', responded_at=?, response_json=?
                 where run_id=? and status not in ('resolved', 'cancelled', 'expired')
                """,
                (timestamp, response, run_id),
            )
            conn.commit()
            return int(cursor.rowcount)

    def record_proposal(
        self,
        *,
        session_id: str,
        message_id: str | None,
        branch_id: str | None,
        proposal_type: str,
        proposal: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> dict[str, Any]:
        proposal_id = f"UP-{uuid4().hex}"
        evaluation_id = f"PE-{uuid4().hex}"
        timestamp = _now()
        status = str(evaluation.get("decision") or "pending")
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_user_proposals(
                    proposal_id, session_id, message_id, branch_id,
                    proposal_type, status, created_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    session_id,
                    message_id,
                    branch_id,
                    proposal_type,
                    status,
                    timestamp,
                    _json(proposal),
                ),
            )
            conn.execute(
                """
                insert into agent_proposal_evaluations(
                    evaluation_id, proposal_id, decision, created_at, payload_json
                ) values (?, ?, ?, ?, ?)
                """,
                (evaluation_id, proposal_id, status, timestamp, _json(evaluation)),
            )
            conn.commit()
        return {
            "proposal_id": proposal_id,
            "evaluation_id": evaluation_id,
            "status": status,
            "proposal": proposal,
            "evaluation": evaluation,
        }

    def update_proposal_evaluation(
        self,
        *,
        proposal_id: str,
        evaluation: dict[str, Any],
    ) -> dict[str, Any] | None:
        decision = str(evaluation.get("decision") or "ask").strip().casefold()
        if decision not in {"accept", "modify", "reject", "ask", "pending"}:
            raise ValueError(f"Unsupported proposal evaluation decision: {decision}")
        timestamp = _now()
        evaluation_id = f"PE-{uuid4().hex}"
        with self._connect() as conn:
            row = conn.execute(
                """
                select proposal_id, session_id, message_id, branch_id,
                       proposal_type, payload_json
                  from agent_user_proposals
                 where proposal_id=?
                """,
                (proposal_id,),
            ).fetchone()
            if row is None:
                return None
            payload = _decode(row[5], {})
            payload["latest_evaluation"] = dict(evaluation)
            payload["evaluation_run_id"] = evaluation.get("evaluation_run_id")
            conn.execute(
                """
                update agent_user_proposals set status=?, payload_json=?
                 where proposal_id=?
                """,
                (decision, _json(payload), proposal_id),
            )
            conn.execute(
                """
                insert into agent_proposal_evaluations(
                    evaluation_id, proposal_id, decision, created_at, payload_json
                ) values (?, ?, ?, ?, ?)
                """,
                (evaluation_id, proposal_id, decision, timestamp, _json(evaluation)),
            )
            conn.commit()
        return {
            "proposal_id": proposal_id,
            "evaluation_id": evaluation_id,
            "session_id": str(row[1]),
            "message_id": row[2],
            "branch_id": row[3],
            "proposal_type": row[4],
            "decision": decision,
            "evaluation": dict(evaluation),
        }

    @staticmethod
    def _interaction_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            **_decode(row["payload_json"], {}),
            "response": _decode(row["response_json"], None),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=5,
            factory=ManagedSQLiteConnection,
        )
        configure_connection(conn)
        return conn
