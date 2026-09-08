from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection

from .plan_graph import PlanGraph


class PlanManager:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        with self._connect() as conn:
            apply_migrations(conn)

    def create(
        self,
        *,
        session_id: str,
        run_id: str,
        objective: str,
        plan: PlanGraph | None = None,
    ) -> PlanGraph:
        """Persist either a new free-form plan or a cloned workflow plan.

        A workflow is deliberately stored as a PlanGraph rather than translated
        into a fixed host flow.  The provider can therefore revise it exactly as
        it would a newly-created plan.
        """
        graph = plan or PlanGraph.create(objective)
        if not graph.objective.strip():
            graph.objective = objective.strip()
        now = _now()
        encoded = _json(graph.to_dict())
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_plans(
                    plan_id, session_id, run_id, status, objective, current_revision,
                    created_at, updated_at, plan_json
                ) values (?, ?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (graph.plan_id, session_id, run_id, objective, graph.revision_number, now, now, encoded),
            )
            conn.execute(
                """
                insert into agent_plan_revisions(
                    plan_id, run_id, revision, created_at, reason_summary, patch_json, plan_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (graph.plan_id, run_id, graph.revision_number, now, "initial plan", "{}", encoded),
            )
            conn.execute("update agent_runs set plan_id=? where run_id=?", (graph.plan_id, run_id))
            conn.commit()
        return graph

    def revise(
        self,
        graph: PlanGraph,
        *,
        run_id: str,
        patch: dict[str, Any],
        reason_summary: str,
    ) -> PlanGraph:
        """Append a revision, rebasing a stale checkpoint on the durable plan.

        A resumed Run can hold a checkpoint from before a prior recovery pass
        advanced the PlanGraph.  Treating that normal recovery race as a
        database fatal error makes the Agent stop precisely when it should be
        analysing and repairing itself.  Serialize writers and apply the new
        patch to the latest durable graph, preserving both audit revisions.
        """

        with self._connect() as conn:
            # ``BEGIN IMMEDIATE`` grants one plan writer at a time.  SQLite
            # then makes the current revision read + the next append atomic.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "select current_revision, plan_json from agent_plans where plan_id=? and run_id=?",
                (graph.plan_id, run_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Plan {graph.plan_id} does not belong to run {run_id}")
            durable_graph = self._latest_durable_graph(
                conn,
                plan_id=graph.plan_id,
                run_id=run_id,
                fallback_json=row[1],
            )
            # Never mutate the caller's possibly stale in-memory graph before
            # the durable append succeeds.  Replaying the patch against the
            # latest graph produces a monotonic, auditable revision number.
            working_graph = PlanGraph.from_dict(durable_graph.to_dict())
            working_graph.apply_patch(patch)
            now = _now()
            encoded = _json(working_graph.to_dict())
            conn.execute(
                """
                update agent_plans
                   set current_revision=?, updated_at=?, plan_json=?
                 where plan_id=? and run_id=?
                """,
                (working_graph.revision_number, now, encoded, working_graph.plan_id, run_id),
            )
            conn.execute(
                """
                insert into agent_plan_revisions(
                    plan_id, run_id, revision, created_at, reason_summary, patch_json, plan_json
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    working_graph.plan_id,
                    run_id,
                    working_graph.revision_number,
                    now,
                    reason_summary,
                    _json(patch),
                    encoded,
                ),
            )
            conn.commit()
        return working_graph

    def save_state(self, graph: PlanGraph, *, run_id: str, status: str = "active") -> None:
        with self._connect() as conn:
            # Checkpoints intentionally preserve a historical plan snapshot.
            # It must never overwrite a newer durable revision after another
            # recovery pass has appended one.  In that case the revision log
            # is authoritative; keeping it avoids a split-brain main row
            # whose ``current_revision`` says one thing while ``plan_json``
            # serializes an older graph.
            row = conn.execute(
                "select current_revision, plan_json from agent_plans where plan_id=? and run_id=?",
                (graph.plan_id, run_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Plan {graph.plan_id} does not belong to run {run_id}")
            durable_graph = self._latest_durable_graph(
                conn,
                plan_id=graph.plan_id,
                run_id=run_id,
                fallback_json=row[1],
            )
            graph_to_save = graph
            if durable_graph.revision_number > graph.revision_number:
                graph_to_save = durable_graph
            conn.execute(
                """
                update agent_plans set status=?, current_revision=?, updated_at=?, plan_json=?
                 where plan_id=? and run_id=?
                """,
                (
                    status,
                    graph_to_save.revision_number,
                    _now(),
                    _json(graph_to_save.to_dict()),
                    graph.plan_id,
                    run_id,
                ),
            )
            conn.commit()

    def for_run(self, run_id: str) -> PlanGraph | None:
        with self._connect() as conn:
            row = conn.execute(
                "select plan_id, plan_json from agent_plans where run_id=? order by updated_at desc limit 1",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            return self._latest_durable_graph(
                conn,
                plan_id=str(row[0]),
                run_id=run_id,
                fallback_json=row[1],
            )

    @staticmethod
    def _latest_durable_graph(
        conn: sqlite3.Connection,
        *,
        plan_id: str,
        run_id: str,
        fallback_json: str | None,
    ) -> PlanGraph:
        """Load the latest structure while retaining its durable runtime state.

        Structural edits are append-only revisions, whereas execution state
        (``running``, ``failed``, validated results) is persisted to the plan
        row at the same revision.  A later replan must start from that row
        when it matches the latest revision; otherwise it can resurrect a
        failed node as ``pending`` and schedule the identical tool again.
        """

        revision = conn.execute(
            """
            select plan_json from agent_plan_revisions
             where plan_id=? and run_id=?
             order by revision desc limit 1
            """,
            (plan_id, run_id),
        ).fetchone()
        revision_graph = PlanGraph.from_dict(_decode(revision[0] if revision else fallback_json, {}))
        row_graph = PlanGraph.from_dict(_decode(fallback_json, {}))
        if row_graph.revision_number == revision_graph.revision_number:
            return row_graph
        return revision_graph

    def revisions(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select revision, created_at, reason_summary, patch_json, plan_json
                  from agent_plan_revisions where run_id=? order by revision
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "revision": row[0],
                "created_at": row[1],
                "reason_summary": row[2],
                "patch": _decode(row[3], {}),
                "plan": _decode(row[4], {}),
            }
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
