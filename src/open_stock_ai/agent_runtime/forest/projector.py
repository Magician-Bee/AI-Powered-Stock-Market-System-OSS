from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from open_stock_ai.storage.migrations import (
    ManagedSQLiteConnection,
    apply_migrations,
    configure_connection,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


@dataclass(slots=True)
class ForestProjection:
    forest_id: str | None = None
    branch_id: str | None = None
    step_id: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class DurableForestStore:
    """Own the durable Task Forest state projected from Host events.

    PlanGraph remains the public node and policy contract, while production
    serial and read-only batches execute through ``ForestExecutor`` at the
    capability boundary. This projector gives
    every executable node a durable Branch/LocalPlan/Step location, then
    advances those records only from real Host events. The UI therefore
    renders the actual execution tree rather than a parallel demo model or
    labels inferred from assistant prose.
    """

    _BRANCH_NODE_TYPES = {"subtask", "subagent"}
    _TERMINAL_STEP_STATUSES = {"completed", "failed", "blocked", "skipped", "cancelled"}

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_migrations(conn)

    def project(self, run_id: str, event: dict[str, Any]) -> ForestProjection:
        event_type = str(event.get("type") or "")
        payload = dict(event.get("payload") or {})
        projection = ForestProjection()
        if event_type in {"plan.created", "plan.proposed", "plan.revised"} and isinstance(payload.get("plan"), dict):
            projection = self.sync_plan(
                run_id,
                dict(payload["plan"]),
                source_event_id=str(event.get("event_id") or ""),
            )
        if event_type == "run.completed":
            self._finalize_completed_run_steps(
                run_id,
                event_id=str(event.get("event_id") or ""),
                timestamp=str(event.get("timestamp") or _now()),
            )
        if event_type == "recovery.linked":
            recovery = self._project_recovery_link(run_id, event)
            projection.forest_id = recovery.forest_id or projection.forest_id
            projection.branch_id = recovery.branch_id or projection.branch_id
            projection.step_id = recovery.step_id or projection.step_id
            projection.events.extend(recovery.events)
        node_id = str(
            event.get("node_id")
            or event.get("step_id")
            or payload.get("node_id")
            or payload.get("step_id")
            or ""
        )
        if node_id and event_type.startswith(("step.", "tool.", "validation.", "checkpoint.")):
            lifecycle = self._project_lifecycle(run_id, node_id, event)
            projection.forest_id = lifecycle.forest_id or projection.forest_id
            projection.branch_id = lifecycle.branch_id or projection.branch_id
            projection.step_id = lifecycle.step_id or projection.step_id
            projection.events.extend(lifecycle.events)
        return projection

    def transition_branch(
        self,
        branch_id: str,
        *,
        status: str,
        source: str,
        reason: str = "",
        result: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ForestProjection:
        """Apply a Host-owned Branch transition to the durable Forest.

        Child Runs and UI branch controls used to update ``agent_branches``
        through separate helper methods.  That let an old lifecycle callback
        move a terminal child back to ``running`` and meant join evaluation
        depended on which caller performed the update.  This is the one
        durable transition boundary for Branch state outside PlanGraph event
        projection: it records provenance, protects terminal states, keeps
        the Local Plan coherent, and re-evaluates the owning Join atomically.
        """

        allowed = {
            "ready",
            "running",
            "waiting_user_input",
            "waiting_decision",
            "waiting_approval",
            "paused",
            "cancelled",
            "completed",
            "partially_completed",
            "failed",
            "blocked",
            "repairing",
            "replanning",
            "waiting_dependency",
        }
        if status not in allowed:
            raise ValueError(f"Unsupported branch status: {status}")
        timestamp = _now()
        terminal = {"completed", "partially_completed", "failed", "cancelled"}
        terminal_step_status = {
            "completed": "completed",
            "partially_completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(status)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_branches where branch_id=?", (branch_id,)
            ).fetchone()
            if row is None:
                raise KeyError(branch_id)
            current = str(row["status"])
            payload = _decode(row["payload_json"], {})
            transition = {
                "source": source,
                "from": current,
                "to": status,
                "at": timestamp,
            }
            if reason:
                transition["reason"] = reason
                payload["status_reason"] = reason
            if metadata:
                payload.update(dict(metadata))
            # A completed child represents immutable evidence for its parent
            # Join.  Delayed worker/provider callbacks may add metadata but
            # may never reopen that evidence under the same Branch ID.
            if current in terminal and status not in terminal:
                transition["ignored"] = "terminal_branch_cannot_reopen"
                payload["last_lifecycle_transition"] = transition
                conn.execute(
                    "update agent_branches set updated_at=?, payload_json=? where branch_id=?",
                    (timestamp, _json(payload), branch_id),
                )
                conn.commit()
                return ForestProjection(
                    forest_id=str(row["forest_id"]), branch_id=branch_id
                )
            payload["last_lifecycle_transition"] = transition
            conn.execute(
                """
                update agent_branches
                   set status=?, result_json=coalesce(?, result_json),
                       updated_at=?, payload_json=?
                 where branch_id=?
                """,
                (
                    status,
                    _json(result) if result is not None else None,
                    timestamp,
                    _json(payload),
                    branch_id,
                ),
            )
            if status == "running":
                conn.execute(
                    """
                    update agent_branch_steps
                       set status='running', updated_at=?
                     where branch_id=? and status in ('ready', 'pending')
                    """,
                    (timestamp, branch_id),
                )
            elif terminal_step_status is not None:
                step_rows = conn.execute(
                    "select step_id, payload_json from agent_branch_steps where branch_id=?",
                    (branch_id,),
                ).fetchall()
                for step_row in step_rows:
                    step_payload = _decode(step_row["payload_json"], {})
                    step_payload.update(
                        {
                            "result_summary": str(
                                (result or {}).get("summary")
                                or (result or {}).get("result_summary")
                                or reason
                                or status
                            ),
                            "last_lifecycle_transition": transition,
                        }
                    )
                    conn.execute(
                        """
                        update agent_branch_steps
                           set status=?, updated_at=?, payload_json=?
                         where step_id=?
                        """,
                        (
                            terminal_step_status,
                            timestamp,
                            _json(step_payload),
                            step_row["step_id"],
                        ),
                    )
            events = self._evaluate_joins(conn, str(row["forest_id"]), timestamp)
            conn.execute(
                "update agent_task_forests set updated_at=? where forest_id=?",
                (timestamp, row["forest_id"]),
            )
            conn.commit()
        return ForestProjection(
            forest_id=str(row["forest_id"]),
            branch_id=branch_id,
            events=events,
        )

    def _project_recovery_link(self, run_id: str, event: dict[str, Any]) -> ForestProjection:
        """Persist the Host's recovery relation on the original failed step.

        The failed step stays failed as provenance, while ``recovered_by`` makes
        the visible Fishbone and Task Forest state distinguish a recovered
        failure from an unresolved one after a snapshot reload.
        """

        payload = dict(event.get("payload") or {})
        relations = [
            dict(item)
            for item in payload.get("recovery_for") or []
            if isinstance(item, dict) and item.get("failed_node_id")
        ]
        if not relations:
            return ForestProjection()
        event_id = str(event.get("event_id") or "")
        timestamp = str(event.get("timestamp") or _now())
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            forest = conn.execute(
                "select forest_id from agent_task_forests where run_id=? order by updated_at desc limit 1",
                (run_id,),
            ).fetchone()
            if forest is None:
                return ForestProjection()
            projection = ForestProjection(forest_id=str(forest["forest_id"]))
            for relation in relations:
                row = conn.execute(
                    """
                    select s.step_id, s.branch_id, s.payload_json
                      from agent_branch_steps s
                      join agent_branches b on b.branch_id=s.branch_id
                     where b.forest_id=?
                       and json_extract(s.payload_json, '$.source_node_id')=?
                     order by s.created_at desc limit 1
                    """,
                    (forest["forest_id"], str(relation["failed_node_id"])),
                ).fetchone()
                if row is None:
                    continue
                step_payload = _decode(row["payload_json"], {})
                recovered_by = list((step_payload.get("metadata") or {}).get("recovered_by") or [])
                if relation not in recovered_by:
                    recovered_by.append(relation)
                metadata = {
                    **dict(step_payload.get("metadata") or {}),
                    "recovered_by": recovered_by,
                    "recovery_linked_event_id": event_id or None,
                }
                step_payload.update(
                    {
                        "metadata": metadata,
                        "recovery_status": "recovered_with_alternative_evidence",
                        "updated_from_host_event": True,
                    }
                )
                conn.execute(
                    "update agent_branch_steps set updated_at=?, payload_json=? where step_id=?",
                    (timestamp, _json(step_payload), row["step_id"]),
                )
                projection.branch_id = str(row["branch_id"])
                projection.step_id = str(row["step_id"])
            conn.commit()
            return projection

    def _finalize_completed_run_steps(
        self,
        run_id: str,
        *,
        event_id: str,
        timestamp: str,
    ) -> None:
        """Close superseded/presentation-only steps after Host completion.

        Model plan revisions can leave an old reasoning placeholder pending
        even though every executable Branch and the Run itself completed. A
        completed Run is the Host authority that no such step remains live.
        """

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            # A Host completion event is only allowed to close presentation
            # placeholders. If a real branch already contains a failed or
            # blocked step, keep every unresolved step visible.
            failure = conn.execute(
                """
                select 1
                  from agent_branch_steps s
                  join agent_branches b on b.branch_id=s.branch_id
                  join agent_task_forests f on f.forest_id=b.forest_id
                 where f.run_id=? and s.status in ('failed','blocked')
                 limit 1
                """,
                (run_id,),
            ).fetchone()
            if failure is not None:
                return
            rows = conn.execute(
                """
                select s.step_id, s.payload_json
                  from agent_branch_steps s
                  join agent_branches b on b.branch_id=s.branch_id
                  join agent_task_forests f on f.forest_id=b.forest_id
                 where f.run_id=?
                   and s.status not in ('completed','failed','blocked','skipped','cancelled')
                """,
                (run_id,),
            ).fetchall()
            for row in rows:
                step_payload = _decode(row["payload_json"], {})
                # Only superseded reasoning/presentation nodes are safe to
                # close here. Executable pending/running work stays visible.
                if not (
                    step_payload.get("superseded_by_plan_id")
                    or step_payload.get("presentation_only") is True
                    or step_payload.get("node_type") in {"reasoning", "synthesis", "presentation"}
                ):
                    continue
                step_payload.update(
                    {
                        "last_runtime_event_id": event_id or None,
                        "last_runtime_event_type": "run.completed",
                        "finalized_by_run_completion": True,
                        "result_summary": step_payload.get("result_summary")
                        or "Run completed; no remaining executable work.",
                    }
                )
                conn.execute(
                    "update agent_branch_steps set status='completed', updated_at=?, payload_json=? where step_id=?",
                    (timestamp, _json(step_payload), row["step_id"]),
                )
            conn.commit()

    def sync_plan(
        self,
        run_id: str,
        plan: dict[str, Any],
        *,
        source_event_id: str = "",
    ) -> ForestProjection:
        nodes = [dict(item) for item in plan.get("nodes") or [] if isinstance(item, dict)]
        revision = max(1, int(plan.get("revision_number") or 1))
        plan_id = str(plan.get("plan_id") or _stable_id("AP", run_id, "plan"))
        timestamp = _now()
        created_events: list[dict[str, Any]] = []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            forest = conn.execute(
                "select * from agent_task_forests where run_id=? order by created_at desc limit 1",
                (run_id,),
            ).fetchone()
            if forest is None:
                return ForestProjection()
            forest_payload = _decode(forest["payload_json"], {})
            root_id = str(forest["root_branch_id"])
            objective_id = str(forest["objective_id"])
            node_by_id = {
                str(item.get("node_id") or ""): item
                for item in nodes
                if str(item.get("node_id") or "")
            }
            anchors = {
                node_id
                for node_id, node in node_by_id.items()
                if not node.get("parent_id") or str(node.get("node_type") or "") in self._BRANCH_NODE_TYPES
            }
            existing_rows = conn.execute(
                "select * from agent_branches where forest_id=?",
                (forest["forest_id"],),
            ).fetchall()
            existing_by_source = {
                str(payload.get("source_plan_node_id")): row
                for row in existing_rows
                if (payload := _decode(row["payload_json"], {})).get("source_plan_node_id")
            }
            branch_for_anchor: dict[str, str] = {}

            def nearest_anchor(node_id: str, *, include_self: bool = True) -> str | None:
                current = node_id if include_self else str(node_by_id.get(node_id, {}).get("parent_id") or "")
                visited: set[str] = set()
                while current and current not in visited:
                    visited.add(current)
                    if current in anchors:
                        return current
                    current = str(node_by_id.get(current, {}).get("parent_id") or "")
                return None

            def anchor_depth(anchor_id: str) -> int:
                depth = 1
                current = nearest_anchor(anchor_id, include_self=False)
                visited: set[str] = set()
                while current and current not in visited:
                    visited.add(current)
                    depth += 1
                    current = nearest_anchor(current, include_self=False)
                return depth

            for anchor_id in sorted(
                anchors,
                key=lambda item: (
                    anchor_depth(item),
                    int(node_by_id[item].get("order_index") or 0),
                    item,
                ),
            ):
                node = node_by_id[anchor_id]
                parent_anchor = nearest_anchor(anchor_id, include_self=False)
                parent_branch_id = branch_for_anchor.get(parent_anchor or "", root_id)
                existing = existing_by_source.get(anchor_id)
                branch_id = str(existing["branch_id"]) if existing is not None else _stable_id("BR", run_id, anchor_id)
                branch_for_anchor[anchor_id] = branch_id
                dependencies = [str(item) for item in node.get("dependencies") or []]
                payload = {
                    **(_decode(existing["payload_json"], {}) if existing is not None else {}),
                    "objective": str(node.get("title") or anchor_id),
                    "description": node.get("description"),
                    "source_plan_id": plan_id,
                    "source_plan_node_id": anchor_id,
                    "source_node_type": node.get("node_type"),
                    "completion_criteria": list(plan.get("completion_criteria") or []),
                    "dependencies": dependencies,
                    "result_contract": dict((node.get("metadata") or {}).get("result_contract") or {}),
                    "budget": dict((node.get("metadata") or {}).get("budget") or {}),
                    "assigned_agent": node.get("assigned_agent"),
                    "capability": node.get("capability"),
                    "last_plan_event_id": source_event_id or None,
                    "child_branch_ids": [],
                    "evidence_ids": list((_decode(existing["payload_json"], {}) if existing is not None else {}).get("evidence_ids") or []),
                }
                if existing is None:
                    active = conn.execute(
                        "select count(*) from agent_branches where forest_id=? and status in ('ready','running','waiting_user_input','waiting_decision','waiting_approval')",
                        (forest["forest_id"],),
                    ).fetchone()[0]
                    max_active = int((forest_payload.get("limits") or {}).get("max_active_branches", 4))
                    status = "ready" if int(active) < max_active else "pending"
                    conn.execute(
                        """
                        insert into agent_branches(
                            branch_id, forest_id, parent_branch_id, objective_id, status,
                            execution_mode, depth, plan_revision, result_json,
                            created_at, updated_at, payload_json
                        ) values (?, ?, ?, ?, ?, 'parallel', ?, ?, null, ?, ?, ?)
                        """,
                        (
                            branch_id,
                            forest["forest_id"],
                            parent_branch_id,
                            objective_id,
                            status,
                            anchor_depth(anchor_id),
                            revision,
                            timestamp,
                            timestamp,
                            _json(payload),
                        ),
                    )
                    created_events.append(
                        {
                            "type": "branch.created",
                            "branch_id": branch_id,
                            "node_id": anchor_id,
                            "payload": {
                                "summary": f"Created execution branch: {payload['objective']}",
                                "branch": {"branch_id": branch_id, **payload, "status": status},
                            },
                        }
                    )
                else:
                    conn.execute(
                        """
                        update agent_branches
                           set parent_branch_id=?, plan_revision=?, updated_at=?, payload_json=?
                         where branch_id=?
                        """,
                        (parent_branch_id, revision, timestamp, _json(payload), branch_id),
                    )
                branch_plan_id = _stable_id("BLP", branch_id, str(revision))
                conn.execute(
                    """
                    insert into agent_branch_plans(
                        branch_plan_id, branch_id, revision, status, created_at, payload_json
                    ) values (?, ?, ?, 'active', ?, ?)
                    on conflict(branch_id, revision) do update set
                        status='active', payload_json=excluded.payload_json
                    """,
                    (
                        branch_plan_id,
                        branch_id,
                        revision,
                        timestamp,
                        _json({"source_plan_id": plan_id, "source_plan_node_id": anchor_id}),
                    ),
                )

            for anchor_id, branch_id in branch_for_anchor.items():
                child_ids = [
                    candidate_id
                    for candidate_anchor, candidate_id in branch_for_anchor.items()
                    if nearest_anchor(candidate_anchor, include_self=False) == anchor_id
                ]
                row = conn.execute(
                    "select payload_json from agent_branches where branch_id=?",
                    (branch_id,),
                ).fetchone()
                payload = _decode(row[0], {})
                payload["child_branch_ids"] = child_ids
                conn.execute(
                    "update agent_branches set payload_json=?, updated_at=? where branch_id=?",
                    (_json(payload), timestamp, branch_id),
                )

            projected_branch_ids = set(branch_for_anchor.values())
            for node_id, node in node_by_id.items():
                anchor_id = nearest_anchor(node_id)
                branch_id = branch_for_anchor.get(anchor_id or "", root_id)
                plan_row = conn.execute(
                    "select branch_plan_id from agent_branch_plans where branch_id=? and revision=?",
                    (branch_id, revision),
                ).fetchone()
                if plan_row is None:
                    branch_plan_id = _stable_id("BLP", branch_id, str(revision))
                    conn.execute(
                        """
                        insert or ignore into agent_branch_plans(
                            branch_plan_id, branch_id, revision, status, created_at, payload_json
                        ) values (?, ?, ?, 'active', ?, ?)
                        """,
                        (branch_plan_id, branch_id, revision, timestamp, _json({"source_plan_id": plan_id})),
                    )
                else:
                    branch_plan_id = str(plan_row[0])
                step_id = _stable_id("BST", run_id, node_id)
                step_payload = {
                    "source_plan_id": plan_id,
                    "source_node_id": node_id,
                    "title": str(node.get("title") or node_id),
                    "description": node.get("description"),
                    "node_type": node.get("node_type"),
                    "parent_node_id": node.get("parent_id"),
                    "dependencies": list(node.get("dependencies") or []),
                    "tool_name": node.get("tool_name"),
                    "capability": node.get("capability"),
                    "assigned_agent": node.get("assigned_agent"),
                    "mandatory": bool(node.get("mandatory", False)),
                    "metadata": dict(node.get("metadata") or {}),
                }
                conn.execute(
                    """
                    insert into agent_branch_steps(
                        step_id, branch_id, branch_plan_id, position, status,
                        created_at, updated_at, payload_json
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(step_id) do update set
                        branch_id=excluded.branch_id,
                        branch_plan_id=excluded.branch_plan_id,
                        position=excluded.position,
                        status=case
                            when agent_branch_steps.status in ('running','completed','failed','blocked','skipped','cancelled')
                            then agent_branch_steps.status else excluded.status end,
                        updated_at=excluded.updated_at,
                        payload_json=json_patch(agent_branch_steps.payload_json, excluded.payload_json)
                    """,
                    (
                        step_id,
                        branch_id,
                        branch_plan_id,
                        int(node.get("order_index") or 0),
                        str(node.get("status") or "pending"),
                        timestamp,
                        timestamp,
                        _json(step_payload),
                    ),
                )

            if projected_branch_ids:
                placeholders = ",".join("?" for _ in projected_branch_ids)
                conn.execute(
                    f"delete from agent_branch_dependencies where branch_id in ({placeholders})",
                    tuple(projected_branch_ids),
                )
            for anchor_id, branch_id in branch_for_anchor.items():
                dependencies = [str(item) for item in node_by_id[anchor_id].get("dependencies") or []]
                for dependency_node_id in dependencies:
                    dependency_anchor = nearest_anchor(dependency_node_id)
                    dependency_branch_id = branch_for_anchor.get(dependency_anchor or "")
                    if dependency_branch_id and dependency_branch_id != branch_id:
                        conn.execute(
                            """
                            insert or ignore into agent_branch_dependencies(
                                branch_id, dependency_branch_id, dependency_type, created_at, payload_json
                            ) values (?, ?, 'completion', ?, ?)
                            """,
                            (branch_id, dependency_branch_id, timestamp, _json({"source_plan_id": plan_id})),
                        )

            required = sorted(projected_branch_ids)
            join_id = _stable_id("JOIN", run_id, plan_id)
            if required:
                join_payload = {
                    "source_plan_id": plan_id,
                    "required_branch_ids": required,
                    "objective": "Join validated branch results before final synthesis",
                }
                existing_join = conn.execute(
                    "select status, payload_json from agent_join_nodes where join_id=?",
                    (join_id,),
                ).fetchone()
                join_status = "waiting"
                if existing_join is not None:
                    existing_payload = _decode(existing_join["payload_json"], {})
                    existing_required = sorted(
                        str(item)
                        for item in existing_payload.get("required_branch_ids") or []
                    )
                    if existing_required == required:
                        join_status = str(existing_join["status"])
                        if "result" in existing_payload:
                            join_payload["result"] = existing_payload["result"]
                conn.execute(
                    """
                    insert into agent_join_nodes(
                        join_id, forest_id, parent_branch_id, status, created_at, updated_at, payload_json
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    on conflict(join_id) do update set
                        status=excluded.status,
                        updated_at=excluded.updated_at,
                        payload_json=excluded.payload_json
                    """,
                    (
                        join_id,
                        forest["forest_id"],
                        root_id,
                        join_status,
                        timestamp,
                        timestamp,
                        _json(join_payload),
                    ),
                )

            root_step = conn.execute(
                """
                select step_id, payload_json from agent_branch_steps
                 where branch_id=? order by created_at limit 1
                """,
                (root_id,),
            ).fetchone()
            if root_step is not None:
                root_payload = _decode(root_step["payload_json"], {})
                if root_payload.get("title") == "理解目標並建立局部計畫":
                    root_payload["superseded_by_plan_id"] = plan_id
                    conn.execute(
                        "update agent_branch_steps set status='completed', updated_at=?, payload_json=? where step_id=?",
                        (timestamp, _json(root_payload), root_step["step_id"]),
                    )

            forest_payload.update(
                {
                    "source_plan_id": plan_id,
                    "source_plan_revision": revision,
                    "scheduler": "parallel_between_branches_ordered_within_branch",
                }
            )
            conn.execute(
                """
                update agent_task_forests
                   set revision=max(revision, ?), updated_at=?, payload_json=?
                 where forest_id=?
                """,
                (revision, timestamp, _json(forest_payload), forest["forest_id"]),
            )
            conn.commit()
            return ForestProjection(
                forest_id=str(forest["forest_id"]),
                branch_id=root_id,
                events=created_events,
            )

    def _project_lifecycle(
        self,
        run_id: str,
        source_node_id: str,
        event: dict[str, Any],
    ) -> ForestProjection:
        event_type = str(event.get("type") or "")
        event_id = str(event.get("event_id") or "")
        payload = dict(event.get("payload") or {})
        timestamp = str(event.get("timestamp") or _now())
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select s.*, b.forest_id, b.status branch_status
                  from agent_branch_steps s
                  join agent_branches b on b.branch_id=s.branch_id
                  join agent_task_forests f on f.forest_id=b.forest_id
                 where f.run_id=? and json_extract(s.payload_json, '$.source_node_id')=?
                 order by s.created_at desc limit 1
                """,
                (run_id, source_node_id),
            ).fetchone()
            if row is None:
                # Provider-local turn numbers can be attached to tool events.
                # They are not PlanGraph node IDs and must never turn into
                # fake Task Forest steps.  A real tool event always follows a
                # Host-projected PlanGraph node, so an unknown ID is audit-only.
                return ForestProjection()
            if row is None:
                return ForestProjection()
            step_payload = _decode(row["payload_json"], {})
            if event_id and step_payload.get("last_runtime_event_id") == event_id:
                return ForestProjection(
                    forest_id=str(row["forest_id"]),
                    branch_id=str(row["branch_id"]),
                    step_id=str(row["step_id"]),
                )
            status = self._step_status(event_type, str(row["status"]))
            step_payload.update(
                {
                    "last_runtime_event_id": event_id or None,
                    "last_runtime_event_type": event_type,
                    "result_summary": payload.get("result_summary") or step_payload.get("result_summary"),
                    "error_summary": payload.get("error_summary") or payload.get("error") or step_payload.get("error_summary"),
                    "tool_call_id": event.get("tool_call_id") or payload.get("call_id") or step_payload.get("tool_call_id"),
                    "updated_from_host_event": True,
                }
            )
            conn.execute(
                "update agent_branch_steps set status=?, updated_at=?, payload_json=? where step_id=?",
                (status, timestamp, _json(step_payload), row["step_id"]),
            )
            old_branch_status = str(row["branch_status"])
            next_branch_status, result = self._branch_state(conn, str(row["branch_id"]), event_type)
            events: list[dict[str, Any]] = []
            if next_branch_status != old_branch_status:
                conn.execute(
                    "update agent_branches set status=?, result_json=coalesce(?, result_json), updated_at=? where branch_id=?",
                    (
                        next_branch_status,
                        _json(result) if result is not None else None,
                        timestamp,
                        row["branch_id"],
                    ),
                )
                branch_event = {
                    "running": "branch.started",
                    "completed": "branch.completed",
                    "partially_completed": "branch.completed",
                    "failed": "branch.failed",
                }.get(next_branch_status)
                if branch_event:
                    events.append(
                        {
                            "type": branch_event,
                            "branch_id": str(row["branch_id"]),
                            "node_id": source_node_id,
                            "payload": {
                                "summary": f"Branch {next_branch_status}: {step_payload.get('title') or source_node_id}",
                                "status": next_branch_status,
                                "result": result,
                            },
                        }
                    )
            events.extend(self._evaluate_joins(conn, str(row["forest_id"]), timestamp))
            conn.commit()
            return ForestProjection(
                forest_id=str(row["forest_id"]),
                branch_id=str(row["branch_id"]),
                step_id=str(row["step_id"]),
                events=events,
            )

    def _ensure_root_step(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        source_node_id: str,
        payload: dict[str, Any],
        timestamp: str,
    ) -> sqlite3.Row | None:
        forest = conn.execute(
            "select forest_id, root_branch_id, revision from agent_task_forests where run_id=? order by created_at desc limit 1",
            (run_id,),
        ).fetchone()
        if forest is None:
            return None
        branch_id = str(forest["root_branch_id"])
        plan = conn.execute(
            "select branch_plan_id from agent_branch_plans where branch_id=? order by revision desc limit 1",
            (branch_id,),
        ).fetchone()
        plan_id = str(plan[0]) if plan else _stable_id("BLP", branch_id, str(forest["revision"]))
        if plan is None:
            conn.execute(
                "insert into agent_branch_plans(branch_plan_id, branch_id, revision, status, created_at, payload_json) values (?, ?, ?, 'active', ?, '{}')",
                (plan_id, branch_id, int(forest["revision"]), timestamp),
            )
        step_id = _stable_id("BST", run_id, source_node_id)
        conn.execute(
            """
            insert or ignore into agent_branch_steps(
                step_id, branch_id, branch_plan_id, position, status, created_at, updated_at, payload_json
            ) values (?, ?, ?, 9999, 'pending', ?, ?, ?)
            """,
            (
                step_id,
                branch_id,
                plan_id,
                timestamp,
                timestamp,
                _json({"source_node_id": source_node_id, "title": payload.get("title") or source_node_id}),
            ),
        )
        return conn.execute(
            """
            select s.*, b.forest_id, b.status branch_status
              from agent_branch_steps s join agent_branches b on b.branch_id=s.branch_id
             where s.step_id=?
            """,
            (step_id,),
        ).fetchone()

    @classmethod
    def _step_status(cls, event_type: str, current: str) -> str:
        if event_type in {"step.started", "tool.started", "validation.started"}:
            return "running"
        if event_type in {"step.completed", "tool.completed", "validation.passed", "checkpoint.created"}:
            return "completed" if event_type == "step.completed" else current
        if event_type in {"step.failed", "tool.failed", "validation.failed"}:
            return "failed"
        if event_type == "step.waiting_approval":
            return "blocked"
        return current

    def _branch_state(
        self,
        conn: sqlite3.Connection,
        branch_id: str,
        event_type: str,
    ) -> tuple[str, dict[str, Any] | None]:
        rows = conn.execute(
            "select step_id, status, payload_json from agent_branch_steps where branch_id=? order by position",
            (branch_id,),
        ).fetchall()
        statuses = [str(item["status"]) for item in rows]
        if event_type in {"step.started", "tool.started"} and not all(
            item in self._TERMINAL_STEP_STATUSES for item in statuses
        ):
            return "running", None
        if not statuses or not all(item in self._TERMINAL_STEP_STATUSES for item in statuses):
            current = conn.execute("select status from agent_branches where branch_id=?", (branch_id,)).fetchone()
            return str(current[0]), None
        completed = [str(item["step_id"]) for item in rows if item["status"] in {"completed", "skipped"}]
        failed = [str(item["step_id"]) for item in rows if item["status"] in {"failed", "blocked"}]
        summaries = [
            str(payload.get("result_summary") or "")
            for item in rows
            if (payload := _decode(item["payload_json"], {})).get("result_summary")
        ]
        result = {
            "schema_version": "open_stock_ai.branch_result.v1",
            "conclusion": "\n".join(summaries),
            "completed_step_ids": completed,
            "failed_step_ids": failed,
            "partial": bool(completed and failed),
        }
        if failed and completed:
            return "partially_completed", result
        if failed:
            return "failed", result
        return "completed", result

    def _evaluate_joins(
        self,
        conn: sqlite3.Connection,
        forest_id: str,
        timestamp: str,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        joins = conn.execute(
            "select * from agent_join_nodes where forest_id=? and status!='completed'",
            (forest_id,),
        ).fetchall()
        for join in joins:
            payload = _decode(join["payload_json"], {})
            required = [str(item) for item in payload.get("required_branch_ids") or []]
            if not required:
                continue
            placeholders = ",".join("?" for _ in required)
            rows = conn.execute(
                f"select branch_id, status, result_json from agent_branches where branch_id in ({placeholders})",
                tuple(required),
            ).fetchall()
            states = {str(item["branch_id"]): str(item["status"]) for item in rows}
            if any(states.get(item) not in {"completed", "partially_completed", "failed", "cancelled"} for item in required):
                continue
            results = [_decode(item["result_json"], {}) for item in rows]
            status = "completed"
            payload["result"] = {
                "branch_results": results,
                "partial": any(states.get(item) != "completed" for item in required),
            }
            conn.execute(
                "update agent_join_nodes set status=?, updated_at=?, payload_json=? where join_id=?",
                (status, timestamp, _json(payload), join["join_id"]),
            )
            events.append(
                {
                    "type": "join.completed",
                    "branch_id": str(join["parent_branch_id"] or "") or None,
                    "node_id": str(join["join_id"]),
                    "payload": {
                        "summary": "Joined durable branch results.",
                        "join_id": str(join["join_id"]),
                        "result": payload["result"],
                    },
                }
            )
        return events

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn


# Compatibility name for integrations written before the Forest store was
# made explicit in the authoritative-store matrix.
DurableForestProjector = DurableForestStore
