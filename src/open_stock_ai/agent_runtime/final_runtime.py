from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from open_stock_ai.storage.migrations import (
    ManagedSQLiteConnection,
    apply_migrations,
    configure_connection,
)

from .artifacts import ArtifactVersionManager, SQLiteArtifactVersionStore
from .artifact_store import ArtifactStore
from .automation import (
    AutomationController,
    AutomationIntent,
    AutomationStore,
    HeadlessN8nAdapter,
    NotificationManager,
)
from .forest import (
    CheckpointLevel,
    DurableForestStore,
    ForestProjection,
    ScopedCheckpointManager,
)
from .forest.checkpoint_store import DurableCheckpointStore
from .interaction import SteeringIntent, SteeringRouter
from .observability_store import DurableRuntimeObservability
from .session import ObjectiveManager, SQLiteObjectiveStore
from .session_store import AgentSessionStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _validate_artifact_revision(
    *,
    content: Any,
    affected_node_ids: tuple[str, ...],
) -> dict[str, Any]:
    """Return a replayable Host validation receipt before version persistence."""

    failures: list[str] = []
    checks: list[dict[str, Any]] = []
    if content is None or (isinstance(content, str) and not content.strip()):
        failures.append("content_must_not_be_empty")
    try:
        json.dumps(content, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        failures.append("content_must_be_json_serializable")
    checks.append({"name": "content", "passed": not any(item.startswith("content_") for item in failures)})

    node_ids = tuple(str(item).strip() for item in affected_node_ids)
    if any(not item for item in node_ids):
        failures.append("affected_node_ids_must_not_be_blank")
    if len(set(node_ids)) != len(node_ids):
        failures.append("affected_node_ids_must_be_unique")
    checks.append(
        {
            "name": "affected_nodes",
            "passed": not any(item.startswith("affected_node_ids_") for item in failures),
            "node_count": len(node_ids),
        }
    )
    return {
        "valid": not failures,
        "validated_by": "host",
        "checks": checks,
        "failures": failures,
        "dependency_validation": {
            "status": "recorded",
            "affected_node_ids": list(node_ids),
        },
    }


class FinalAgentRuntime:
    """Durable coordinator for the P0-P105 Session-first domain.

    The provider loop remains owned by ``DurableAgentRuntime``.  This class owns
    the product-level Objective, Task Forest, interaction, Artifact version and
    Automation projections so API/UI consumers never have to infer them from
    model prose.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        n8n_backend: HeadlessN8nAdapter | None = None,
        notification_senders: Mapping[
            str,
            Callable[[Mapping[str, Any]], Mapping[str, Any] | bool],
        ]
        | None = None,
        market_calendar: Any | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_migrations(conn)
        self.objectives = ObjectiveManager(SQLiteObjectiveStore(self.path))
        self.sessions = AgentSessionStore(self.path)
        self.artifact_store = artifact_store
        self.artifact_versions = (
            artifact_store.versions
            if artifact_store is not None
            else ArtifactVersionManager(SQLiteArtifactVersionStore(self.path))
        )
        self.automation_store = AutomationStore(self.path, market_calendar=market_calendar)
        # In-app delivery is a durable first-party notification, not an
        # external side effect.  It must be available by default so an active
        # advisory Automation can complete its meaningful-change lifecycle
        # even when no email/Telegram/etc. provider has been configured.
        senders = {
            "in_app": lambda payload: {
                "accepted": True,
                "kind": "in_app",
                "payload": dict(payload),
            },
            **dict(notification_senders or {}),
        }
        self.automations = AutomationController(
            self.automation_store,
            n8n_backend=n8n_backend,
            notifications=NotificationManager(self.automation_store, senders),
        )
        self.forest_store = DurableForestStore(self.path)
        # Keep the old attribute for downstream integrations while ensuring
        # the runtime binds its writer to the named authoritative store.
        self.forest_projector = self.forest_store
        self.observability = DurableRuntimeObservability(self.path)
        self.checkpoints = ScopedCheckpointManager(DurableCheckpointStore(self.path))

    def project_runtime_event(
        self,
        run_id: str,
        event: dict[str, Any],
    ) -> ForestProjection:
        """Persist the executable PlanGraph event in its Task Forest location."""
        projection = self.forest_store.project(run_id, event)
        if projection.forest_id:
            event["forest_id"] = projection.forest_id
        if projection.branch_id:
            event["branch_id"] = projection.branch_id
        self.observability.project(run_id, event)
        self._checkpoint_runtime_event(run_id, event, projection)
        return projection

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        return self.observability.evidence(run_id)

    def kpis(self, *, run_id: str | None = None) -> dict[str, Any]:
        return self.observability.kpis(run_id=run_id)

    def observability_dashboard(self) -> dict[str, Any]:
        return self.observability.dashboard()

    def automation_backend_status(self) -> dict[str, dict[str, Any]]:
        return self.automations.backend_status()

    def record_metric(
        self,
        *,
        session_id: str,
        metric: str,
        value: float = 1.0,
        run_id: str | None = None,
        event_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.observability.record_metric(
            session_id=session_id,
            metric=metric,
            value=value,
            run_id=run_id,
            event_id=event_id,
            payload=payload,
        )

    def create_forest(
        self,
        *,
        session_id: str,
        run_id: str,
        objective: str,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        current = self.objectives.current(session_id)
        if current is None:
            version = self.objectives.create_initial(
                session_id=session_id,
                objective=objective,
                message_id=message_id,
            )
            objective_event = "objective.created"
        elif current.objective != objective.strip():
            version = self.objectives.revise(
                session_id=session_id,
                objective=objective,
                message_id=message_id,
            )
            objective_event = "objective.revised"
        else:
            version = current
            objective_event = None
        timestamp = _now()
        forest_id = f"TF-{uuid4().hex}"
        branch_id = f"BR-{uuid4().hex}"
        plan_id = f"BLP-{uuid4().hex}"
        step_id = f"BST-{uuid4().hex}"
        forest_payload = {
            "limits": {"max_depth": 5, "max_active_branches": 4, "max_nodes": 128},
            "scheduler": "parallel_between_branches_ordered_within_branch",
        }
        branch_payload = {
            "objective": objective.strip(),
            "completion_criteria": ["host_completion_gate_passed"],
            "budget": {"max_retries": 2, "max_tokens": 16000, "max_seconds": 600},
            "child_branch_ids": [],
            "evidence_ids": [],
        }
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_task_forests(
                    forest_id, session_id, run_id, objective_id, root_branch_id,
                    status, revision, created_at, updated_at, payload_json
                ) values (?, ?, ?, ?, ?, 'running', 1, ?, ?, ?)
                """,
                (forest_id, session_id, run_id, version.objective_id, branch_id, timestamp, timestamp, _json(forest_payload)),
            )
            conn.execute(
                """
                insert into agent_branches(
                    branch_id, forest_id, parent_branch_id, objective_id, status,
                    execution_mode, depth, plan_revision, result_json,
                    created_at, updated_at, payload_json
                ) values (?, ?, null, ?, 'running', 'master', 0, 1, null, ?, ?, ?)
                """,
                (branch_id, forest_id, version.objective_id, timestamp, timestamp, _json(branch_payload)),
            )
            conn.execute(
                """
                insert into agent_branch_plans(
                    branch_plan_id, branch_id, revision, status, created_at, payload_json
                ) values (?, ?, 1, 'active', ?, ?)
                """,
                (plan_id, branch_id, timestamp, _json({"objective": objective.strip()})),
            )
            conn.execute(
                """
                insert into agent_branch_steps(
                    step_id, branch_id, branch_plan_id, position, status,
                    created_at, updated_at, payload_json
                ) values (?, ?, ?, 0, 'ready', ?, ?, ?)
                """,
                (step_id, branch_id, plan_id, timestamp, timestamp, _json({"title": "理解目標並建立局部計畫"})),
            )
            conn.commit()
        checkpoint_payload = {
            "objective": objective.strip(),
            "objective_id": version.objective_id,
            "forest_id": forest_id,
            "root_branch_id": branch_id,
        }
        self.checkpoints.save(
            level=CheckpointLevel.SESSION,
            session_id=session_id,
            payload=checkpoint_payload,
            # The same durable objective may be executed by multiple runs in a
            # Session.  Its forest/root IDs are run-specific, so the checkpoint
            # idempotency scope must include the forest rather than colliding
            # with the first run's different payload.
            idempotency_key=f"session-forest-created:{forest_id}",
        )
        self.checkpoints.save(
            level=CheckpointLevel.RUN,
            session_id=session_id,
            run_id=run_id,
            payload=checkpoint_payload,
            idempotency_key=f"run-created:{run_id}",
        )
        self.checkpoints.save(
            level=CheckpointLevel.BRANCH,
            session_id=session_id,
            run_id=run_id,
            branch_id=branch_id,
            payload={**checkpoint_payload, "status": "running"},
            idempotency_key=f"branch-created:{branch_id}",
        )
        self.checkpoints.save(
            level=CheckpointLevel.STEP,
            session_id=session_id,
            run_id=run_id,
            branch_id=branch_id,
            step_id=step_id,
            payload={"title": "理解目標並建立局部計畫", "status": "ready"},
            idempotency_key=f"step-created:{step_id}",
        )
        return {
            "objective": version.to_dict(),
            "objective_event": objective_event,
            "forest_id": forest_id,
            "branch_id": branch_id,
            "plan_id": plan_id,
            "step_id": step_id,
        }

    def forest(self, session_id: str, *, run_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            clauses = ["session_id=?"]
            params: list[Any] = [session_id]
            if run_id:
                clauses.append("run_id=?")
                params.append(run_id)
            row = conn.execute(
                f"select * from agent_task_forests where {' and '.join(clauses)} order by updated_at desc limit 1",
                tuple(params),
            ).fetchone()
            if row is None:
                return None
            branch_rows = conn.execute(
                "select * from agent_branches where forest_id=? order by depth, created_at",
                (row["forest_id"],),
            ).fetchall()
            join_rows = conn.execute(
                "select * from agent_join_nodes where forest_id=? order by created_at",
                (row["forest_id"],),
            ).fetchall()
            branches = [self._branch(conn, item) for item in branch_rows]
        payload = _decode(row["payload_json"], {})
        return {
            "schema_version": "open_stock_ai.task_forest.v1",
            **dict(row),
            **payload,
            "branches": branches,
            "join_nodes": [{**dict(item), **_decode(item["payload_json"], {})} for item in join_rows],
        }

    def branch(self, branch_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from agent_branches where branch_id=?", (branch_id,)).fetchone()
            return self._branch(conn, row) if row else None

    def steer(
        self,
        *,
        session_id: str,
        message_id: str,
        content: str,
        target_branch_id: str | None = None,
        intent: str | None = None,
        affected_branch_ids: tuple[str, ...] = (),
        replacement_objective: str | None = None,
    ) -> dict[str, Any]:
        scoped_branch_id = target_branch_id or next(iter(affected_branch_ids), None)
        scoped_run_id: str | None = None
        if scoped_branch_id:
            with self._connect() as conn:
                scoped = conn.execute(
                    """
                    select f.run_id
                      from agent_branches b
                      join agent_task_forests f on f.forest_id=b.forest_id
                     where b.branch_id=? and f.session_id=?
                    """,
                    (scoped_branch_id, session_id),
                ).fetchone()
                scoped_run_id = str(scoped[0]) if scoped else None
        snapshot = self.forest(session_id, run_id=scoped_run_id)
        if snapshot is None:
            raise KeyError(f"Session has no Task Forest: {session_id}")
        resolved = SteeringIntent(intent) if intent else SteeringRouter.classify(None, content)
        root_id = str(snapshot["root_branch_id"])
        target_id = target_branch_id or root_id
        current = self.objectives.current(session_id)
        if current is None:
            raise KeyError(f"Session has no objective: {session_id}")
        timestamp = _now()
        created: list[str] = []
        cancelled: list[str] = []
        affected: list[str] = []

        if resolved in {SteeringIntent.SOFT_STEER, SteeringIntent.APPEND_REQUIREMENT, SteeringIntent.FORK_BRANCH, SteeringIntent.NEW_GOAL}:
            version = self.objectives.revise(
                session_id=session_id,
                added_requirements=(content,),
                message_id=message_id,
            )
            parent_id = root_id if resolved in {SteeringIntent.FORK_BRANCH, SteeringIntent.NEW_GOAL} else target_id
            created.append(self._insert_branch(snapshot, parent_id, version.objective_id, content, timestamp))
            affected.append(parent_id)
        elif resolved in {SteeringIntent.HARD_STEER, SteeringIntent.CORRECT_FACT}:
            version = self.objectives.revise(
                session_id=session_id,
                objective=replacement_objective or current.objective,
                added_requirements=(content,),
                message_id=message_id,
            )
            impacted = affected_branch_ids or (target_id,)
            with self._connect() as conn:
                for branch_id in impacted:
                    row = conn.execute(
                        "select parent_branch_id, payload_json from agent_branches where branch_id=? and forest_id=?",
                        (branch_id, snapshot["forest_id"]),
                    ).fetchone()
                    if row is None:
                        continue
                    affected.append(branch_id)
                    if row[0] is None:
                        payload = _decode(row[1], {})
                        payload["hard_steer_message"] = content
                        conn.execute(
                            "update agent_branches set objective_id=?, plan_revision=plan_revision+1, updated_at=?, payload_json=? where branch_id=?",
                            (version.objective_id, timestamp, _json(payload), branch_id),
                        )
                    else:
                        conn.execute(
                            "update agent_branches set status='cancelled', updated_at=?, payload_json=json_set(payload_json, '$.cancel_reason', ?) where branch_id=?",
                            (timestamp, content, branch_id),
                        )
                        cancelled.append(branch_id)
                conn.commit()
            for branch_id in cancelled:
                old = self.branch(branch_id) or {}
                created.append(self._insert_branch(snapshot, str(old.get("parent_branch_id") or root_id), version.objective_id, str(old.get("objective") or content), timestamp, replaces=branch_id))
        elif resolved is SteeringIntent.MODIFY_ARTIFACT:
            # A node/artifact chat is a scoped change request, never an edit
            # to the root objective.  Create a real child Branch so its
            # evidence, PlanGraph and eventual Artifact version are isolated
            # and the selected node remains traceable.
            version = current
            selected = None
            with self._connect() as conn:
                row = conn.execute(
                    "select content_json from agent_messages where message_id=?",
                    (message_id,),
                ).fetchone()
                if row is not None:
                    selected = _decode(row[0], {}).get("artifact_context_selection")
            selected = dict(selected or {})
            artifact_id = str(selected.get("artifact_id") or "")
            path = str(selected.get("path") or selected.get("node_id") or "selected node")
            scoped_objective = (
                f"Modify artifact {artifact_id or 'selected artifact'} at {path}: {content.strip()}"
            )
            child_id = self._insert_branch(snapshot, target_id, version.objective_id, scoped_objective, timestamp)
            with self._connect() as conn:
                branch = conn.execute("select payload_json from agent_branches where branch_id=?", (child_id,)).fetchone()
                payload = _decode(branch[0], {}) if branch else {}
                payload.update({
                    "steering_kind": "artifact_node_edit",
                    "artifact_context_selection": selected,
                    "source_node_id": selected.get("node_id"),
                    "source_artifact_id": artifact_id,
                })
                conn.execute("update agent_branches set payload_json=?, updated_at=? where branch_id=?", (_json(payload), timestamp, child_id))
                conn.commit()
            created.append(child_id)
            affected.append(target_id)
        elif resolved in {SteeringIntent.PAUSE_BRANCH, SteeringIntent.RESUME_BRANCH, SteeringIntent.CANCEL_BRANCH}:
            status = {
                SteeringIntent.PAUSE_BRANCH: "paused",
                SteeringIntent.RESUME_BRANCH: "ready",
                SteeringIntent.CANCEL_BRANCH: "cancelled",
            }[resolved]
            self.set_branch_status(target_id, status=status, reason=content)
            version = current
            affected.append(target_id)
        else:
            version = current

        with self._connect() as conn:
            conn.execute(
                "update agent_task_forests set revision=revision+1, updated_at=? where forest_id=?",
                (timestamp, snapshot["forest_id"]),
            )
            conn.commit()
        preserved = [
            item["branch_id"] for item in (self.forest(session_id) or {}).get("branches", [])
            if item["branch_id"] not in set(created + cancelled + affected) and item["status"] != "cancelled"
        ]
        return {
            "schema_version": "open_stock_ai.steering_outcome.v1",
            "intent": resolved.value,
            "objective_version_id": version.objective_id,
            "affected_branch_ids": affected,
            "created_branch_ids": created,
            "cancelled_branch_ids": cancelled,
            "preserved_branch_ids": preserved,
            "forest": self.forest(session_id),
        }

    def set_branch_status(self, branch_id: str, *, status: str, reason: str = "") -> dict[str, Any]:
        self.forest_store.transition_branch(
            branch_id,
            status=status,
            source="branch_control",
            reason=reason,
        )
        return self.branch(branch_id) or {}

    def link_branch_run(self, branch_id: str, run_id: str) -> dict[str, Any]:
        """Bind a dynamically created Branch to the same-Session follow-up Run."""
        branch = self.branch(branch_id)
        if branch is None:
            raise KeyError(branch_id)
        current_status = str(branch.get("status") or "ready")
        status = current_status if current_status not in {"pending", "ready"} else "ready"
        self.forest_store.transition_branch(
            branch_id,
            status=status,
            source="linked_run_bound",
            metadata={
                "follow_run_id": run_id,
                "execution_owner": "durable_agent_runtime",
            },
        )
        return self.branch(branch_id) or {}

    def create_subtask_branches(
        self,
        *,
        session_id: str,
        run_id: str,
        source_node_id: str,
        objectives: tuple[str, ...],
        role: str,
    ) -> list[dict[str, Any]]:
        """Create real, idempotent child Branches for one subtask Plan node.

        ``agent.run_subtasks`` waits for child Runs, but that wait used to be
        invisible in the parent Task Forest.  These records are the durable
        ownership/control surface for each independently executing objective;
        they are deliberately created before their Run is queued.
        """

        forest = self.forest(session_id, run_id=run_id)
        if forest is None:
            raise KeyError(f"Run has no Task Forest: {run_id}")
        node_id = source_node_id.strip()
        if not node_id:
            raise ValueError("source_node_id is required for subtask branches")
        normalized = tuple(dict.fromkeys(item.strip() for item in objectives if item.strip()))
        if not normalized:
            return []
        timestamp = _now()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            parent = conn.execute(
                """
                select * from agent_branches
                 where forest_id=? and json_extract(payload_json, '$.source_plan_node_id')=?
                   and json_extract(payload_json, '$.source_subtask_node_id') is null
                 order by created_at desc limit 1
                """,
                (forest["forest_id"], node_id),
            ).fetchone()
            if parent is None:
                parent = conn.execute(
                    "select * from agent_branches where branch_id=?",
                    (forest["root_branch_id"],),
                ).fetchone()
            if parent is None:
                raise KeyError(f"Task Forest root branch is missing: {run_id}")
            parent_id = str(parent["branch_id"])
            parent_payload = _decode(parent["payload_json"], {})
            parent_depth = int(parent["depth"])
            limits = dict(forest.get("limits") or {})
            max_depth = int(limits.get("max_depth") or 5)
            if parent_depth + 1 > max_depth:
                raise ValueError("Task Forest maximum branch depth reached")
            existing_rows = conn.execute(
                "select * from agent_branches where forest_id=?",
                (forest["forest_id"],),
            ).fetchall()
            existing_by_key = {
                str(payload.get("subtask_branch_key") or ""): row
                for row in existing_rows
                if (payload := _decode(row["payload_json"], {})).get("subtask_branch_key")
            }
            missing = [objective for objective in normalized if f"{node_id}:{objective.casefold()}" not in existing_by_key]
            if len(existing_rows) + len(missing) > int(limits.get("max_nodes") or 128):
                raise ValueError("Task Forest node budget exceeded")
            branch_ids: list[str] = []
            for objective in normalized:
                key = f"{node_id}:{objective.casefold()}"
                existing = existing_by_key.get(key)
                if existing is not None:
                    branch_ids.append(str(existing["branch_id"]))
                    continue
                branch_id = f"BR-{uuid4().hex}"
                plan_id = f"BLP-{uuid4().hex}"
                step_id = f"BST-{uuid4().hex}"
                payload = {
                    "objective": objective,
                    "role": role,
                    "source_parent_run_id": run_id,
                    "source_subtask_node_id": node_id,
                    "subtask_branch_key": key,
                    "execution_owner": "durable_agent_runtime",
                    "child_branch_ids": [],
                    "evidence_ids": [],
                }
                conn.execute(
                    """
                    insert into agent_branches(
                        branch_id, forest_id, parent_branch_id, objective_id, status,
                        execution_mode, depth, plan_revision, result_json,
                        created_at, updated_at, payload_json
                    ) values (?, ?, ?, ?, 'ready', 'parallel', ?, 1, null, ?, ?, ?)
                    """,
                    (
                        branch_id, forest["forest_id"], parent_id, forest["objective_id"],
                        parent_depth + 1, timestamp, timestamp, _json(payload),
                    ),
                )
                conn.execute(
                    "insert into agent_branch_plans(branch_plan_id, branch_id, revision, status, created_at, payload_json) values (?, ?, 1, 'active', ?, ?)",
                    (plan_id, branch_id, timestamp, _json({"objective": objective, "role": role})),
                )
                conn.execute(
                    """
                    insert into agent_branch_steps(
                        step_id, branch_id, branch_plan_id, position, status,
                        created_at, updated_at, payload_json
                    ) values (?, ?, ?, 0, 'ready', ?, ?, ?)
                    """,
                    (
                        step_id, branch_id, plan_id, timestamp, timestamp,
                        _json({"title": objective, "source_subtask_node_id": node_id, "role": role}),
                    ),
                )
                branch_ids.append(branch_id)
            child_ids = list(dict.fromkeys([*(parent_payload.get("child_branch_ids") or []), *branch_ids]))
            parent_payload["child_branch_ids"] = child_ids
            conn.execute(
                "update agent_branches set updated_at=?, payload_json=? where branch_id=?",
                (timestamp, _json(parent_payload), parent_id),
            )
            join = conn.execute(
                """
                select join_id, payload_json from agent_join_nodes
                 where forest_id=? and parent_branch_id=?
                   and json_extract(payload_json, '$.source_subtask_node_id')=?
                 order by created_at desc limit 1
                """,
                (forest["forest_id"], parent_id, node_id),
            ).fetchone()
            join_payload = {
                "source_subtask_node_id": node_id,
                "required_branch_ids": branch_ids,
                "objective": f"Join subtask results: {node_id}",
            }
            if join is None:
                conn.execute(
                    "insert into agent_join_nodes(join_id, forest_id, parent_branch_id, status, created_at, updated_at, payload_json) values (?, ?, ?, 'waiting', ?, ?, ?)",
                    (f"JOIN-{uuid4().hex}", forest["forest_id"], parent_id, timestamp, timestamp, _json(join_payload)),
                )
            else:
                conn.execute(
                    "update agent_join_nodes set updated_at=?, payload_json=? where join_id=?",
                    (timestamp, _json(join_payload), join["join_id"]),
                )
            conn.commit()
        return [self.branch(branch_id) or {"branch_id": branch_id} for branch_id in branch_ids]

    def set_linked_branch_run_status(
        self,
        branch_id: str,
        *,
        run_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mapped = {
            "queued": "ready",
            "planning": "running",
            "replanning": "replanning",
            "repairing": "repairing",
            "waiting_dependency": "waiting_dependency",
            "running": "running",
            "waiting_user_input": "waiting_user_input",
            "waiting_decision": "waiting_decision",
            "waiting_approval": "waiting_approval",
            "suspended": "paused",
            "completed": "completed",
            "partially_completed": "partially_completed",
            "max_steps_reached": "partially_completed",
            "failed": "failed",
            "interrupted": "failed",
            "cancelled": "cancelled",
        }.get(status)
        if mapped is None:
            raise ValueError(f"Unsupported linked Run status: {status}")
        self.forest_store.transition_branch(
            branch_id,
            status=mapped,
            source="linked_run_status",
            reason=str((error or {}).get("message") or ""),
            result=result,
            metadata={
                "follow_run_id": run_id,
                "follow_run_status": status,
                "execution_owner": "durable_agent_runtime",
                **({"follow_run_error": dict(error)} if error else {}),
            },
        )
        return self.branch(branch_id) or {}

    def create_interaction(
        self,
        *,
        session_id: str,
        run_id: str | None,
        branch_id: str | None,
        waiting_state: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if waiting_state not in {"waiting_user_input", "waiting_decision", "waiting_approval"}:
            raise ValueError("Interaction waiting state must be clarification, decision or approval")
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
                (interaction_id, session_id, run_id, branch_id, waiting_state, waiting_state.removeprefix("waiting_"), timestamp, _json(payload)),
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
        return {"interaction_id": interaction_id, "session_id": session_id, "run_id": run_id, "branch_id": branch_id, "status": waiting_state, **payload}

    def respond_interaction(self, interaction_id: str, response: dict[str, Any]) -> dict[str, Any]:
        timestamp = _now()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from agent_decision_checkpoints where interaction_id=?", (interaction_id,)).fetchone()
            if row is None:
                raise KeyError(interaction_id)
            if row["status"] == "resolved":
                raise ValueError("Interaction is already resolved")
            conn.execute(
                "update agent_decision_checkpoints set status='resolved', responded_at=?, response_json=? where interaction_id=?",
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

    def interaction(self, interaction_id: str) -> dict[str, Any] | None:
        """Read one pending/recorded interaction without resolving it."""

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_decision_checkpoints where interaction_id=?",
                (interaction_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            **_decode(row["payload_json"], {}),
            "response": _decode(row["response_json"], None),
        }

    def interactions(self, session_id: str, *, open_only: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            sql = "select * from agent_decision_checkpoints where session_id=?"
            if open_only:
                sql += " and status!='resolved'"
            rows = conn.execute(sql + " order by created_at", (session_id,)).fetchall()
        return [{**dict(row), **_decode(row["payload_json"], {}), "response": _decode(row["response_json"], None)} for row in rows]

    def cancel_interactions_for_run(self, run_id: str, *, reason: str) -> int:
        """Close unresolved decision checkpoints when their owning Run ends.

        A waiting approval has no active model coroutine to consume a cancel
        signal.  Leaving its checkpoint open would make a later Session render
        a decision card for a Run that the user already cancelled.
        """

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

    def record_user_proposal(
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

    def update_user_proposal_evaluation(
        self,
        *,
        proposal_id: str,
        evaluation: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist a new model/evidence evaluation for a user proposal."""

        decision = str(evaluation.get("decision") or "ask").strip().casefold()
        if decision not in {"accept", "modify", "reject", "ask", "pending"}:
            raise ValueError(f"Unsupported proposal evaluation decision: {decision}")
        timestamp = _now()
        evaluation_id = f"PE-{uuid4().hex}"
        with self._connect() as conn:
            row = conn.execute(
                "select proposal_id, session_id, message_id, branch_id, proposal_type, payload_json from agent_user_proposals where proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                return None
            payload = _decode(row[5], {})
            payload["latest_evaluation"] = dict(evaluation)
            payload["evaluation_run_id"] = evaluation.get("evaluation_run_id")
            conn.execute(
                "update agent_user_proposals set status=?, payload_json=? where proposal_id=?",
                (decision, _json(payload), proposal_id),
            )
            conn.execute(
                "insert into agent_proposal_evaluations(evaluation_id, proposal_id, decision, created_at, payload_json) values (?, ?, ?, ?, ?)",
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

    def _checkpoint_runtime_event(
        self,
        run_id: str,
        event: dict[str, Any],
        projection: ForestProjection,
    ) -> None:
        event_type = str(event.get("type") or "")
        if event_type not in {
            "plan.proposed",
            "plan.revised",
            "step.completed",
            "step.failed",
            "checkpoint.created",
            "interaction.requested",
            "automation.activated",
            "automation.triggered",
            "run.completed",
            "run.failed",
        }:
            return
        with self._connect() as conn:
            row = conn.execute(
                "select session_id from agent_runs where run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return
        session_id = str(row[0])
        sequence = int(event.get("sequence") or 0)
        event_id = str(event.get("event_id") or f"{event_type}:{sequence}")
        payload = {
            "event_type": event_type,
            "event_id": event_id,
            "sequence": sequence,
            "payload": dict(event.get("payload") or {}),
        }
        self.checkpoints.save(
            level=CheckpointLevel.RUN,
            session_id=session_id,
            run_id=run_id,
            payload=payload,
            sequence=sequence,
            idempotency_key=f"runtime-event:{event_id}",
        )
        if projection.branch_id:
            self.checkpoints.save(
                level=CheckpointLevel.BRANCH,
                session_id=session_id,
                run_id=run_id,
                branch_id=projection.branch_id,
                payload=payload,
                sequence=sequence,
                idempotency_key=f"branch-event:{event_id}",
            )
        if projection.step_id:
            self.checkpoints.save(
                level=CheckpointLevel.STEP,
                session_id=session_id,
                run_id=run_id,
                branch_id=projection.branch_id,
                step_id=projection.step_id,
                payload=payload,
                sequence=sequence,
                idempotency_key=f"step-event:{event_id}",
            )
        if event_type == "interaction.requested":
            decision_id = str((event.get("payload") or {}).get("interaction_id") or "")
            if decision_id:
                self.checkpoints.save(
                    level=CheckpointLevel.DECISION,
                    session_id=session_id,
                    run_id=run_id,
                    branch_id=projection.branch_id,
                    decision_id=decision_id,
                    payload=payload,
                    sequence=sequence,
                    idempotency_key=f"decision-event:{event_id}",
                )

    def ensure_artifact_version(self, artifact_id: str, content: Any, *, changed_by: str = "host") -> dict[str, Any]:
        latest = self.artifact_versions.latest(artifact_id)
        if latest is None:
            latest = self.artifact_versions.create(artifact_id=artifact_id, content=content, changed_by=changed_by, reason="Initial artifact projection")
        return self._artifact_version(latest)

    def artifact_history(self, artifact_id: str) -> list[dict[str, Any]]:
        return [self._artifact_version(item) for item in self.artifact_versions.store.list_versions(artifact_id)]

    def artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("select * from agent_artifacts where artifact_id=?", (artifact_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "metadata": _decode(row["metadata_json"], {}), "versions": self.artifact_history(artifact_id)}

    def revise_artifact(
        self,
        *,
        artifact_id: str,
        expected_version: int,
        content: Any,
        changed_by: str,
        reason: str,
        message_id: str | None = None,
        affected_node_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        validation_result = _validate_artifact_revision(
            content=content,
            affected_node_ids=affected_node_ids,
        )
        if not validation_result["valid"]:
            raise ValueError(
                "Artifact revision validation failed: "
                + ", ".join(validation_result["failures"])
            )
        version = self.artifact_versions.revise(
            artifact_id=artifact_id,
            expected_version=expected_version,
            content=content,
            changed_by=changed_by,
            reason=reason,
            message_id=message_id,
            affected_node_ids=affected_node_ids,
            validation_result=validation_result,
        )
        return self._artifact_version(version)

    def restore_artifact(
        self,
        *,
        artifact_id: str,
        source_version: int,
        expected_version: int,
        changed_by: str,
    ) -> dict[str, Any]:
        return self._artifact_version(
            self.artifact_versions.restore(
                artifact_id=artifact_id,
                source_version=source_version,
                expected_version=expected_version,
                changed_by=changed_by,
                reason=f"Restore artifact version {source_version}",
            )
        )

    def select_artifact(self, *, session_id: str, artifact_id: str, artifact_version: int, target_type: str, path: str, branch_id: str | None = None, node_id: str | None = None, evidence_id: str | None = None) -> dict[str, Any]:
        if self.artifact_versions.store.get_version(artifact_id, artifact_version) is None:
            raise KeyError(f"Unknown artifact version: {artifact_id} v{artifact_version}")
        selection_id = f"ASEL-{uuid4().hex}"
        payload = {"evidence_id": evidence_id}
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_artifact_selections(
                    selection_id, artifact_id, artifact_version, session_id,
                    branch_id, target_type, node_id, path, created_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (selection_id, artifact_id, artifact_version, session_id, branch_id, target_type, node_id, path, _now(), _json(payload)),
            )
            conn.commit()
        return {"selection_id": selection_id, "session_id": session_id, "artifact_id": artifact_id, "artifact_version": artifact_version, "target_type": target_type, "path": path, "branch_id": branch_id, "node_id": node_id, **payload}

    def activate_automation(
        self,
        intent_payload: dict[str, Any],
        *,
        confirmed: bool,
        external_permission: bool = False,
        credential_refs: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        intent = AutomationIntent.from_dict(intent_payload)
        # Every callback can trigger an Agent reanalysis in the same durable
        # Session.  API callers are allowed to describe only the desired
        # monitoring outcome, so create that ownership boundary locally rather
        # than accepting an Automation that later fails on its first callback.
        if not intent.session_id:
            session = self.sessions.create(
                title=f"自動化：{intent.goal}"[:200],
                namespace="stock-ai",
                metadata={"created_by": "automation_activation", "automation_goal": intent.goal},
            )
            intent = replace(intent, session_id=str(session["session_id"]))
        outcome = self.automations.confirm_and_activate(
            intent,
            user_confirmed=confirmed,
            external_permission=external_permission,
            credential_refs=credential_refs,
        )
        response = {
            "schema_version": "open_stock_ai.automation_activation.v1",
            "automation": dict(outcome.automation),
            "intent_id": outcome.intent_id,
            "version": outcome.version,
            "dedup": outcome.dedup.decision.value,
            "policy": asdict(outcome.policy),
            "validation_errors": list(outcome.validation_errors),
            "natural_language": outcome.natural_language,
            "host_status": outcome.host_status,
            "artifact": dict(outcome.artifact),
        }
        automation_id = str(outcome.automation.get("automation_id") or "")
        if intent.session_id and automation_id:
            self.checkpoints.save(
                level=CheckpointLevel.AUTOMATION,
                session_id=intent.session_id,
                automation_id=automation_id,
                payload=response,
                sequence=int(outcome.version),
                idempotency_key=(
                    f"automation-activation:{automation_id}:v{outcome.version}:"
                    f"{outcome.dedup.decision.value}"
                ),
            )
            metric_event_id = f"automation:{automation_id}:v{outcome.version}:{outcome.dedup.decision.value}"
            self.record_metric(
                session_id=intent.session_id,
                metric="automation_activation",
                event_id=metric_event_id,
                payload={"automation_id": automation_id, "dedup": outcome.dedup.decision.value},
            )
            if outcome.dedup.decision.value == "reuse":
                self.record_metric(
                    session_id=intent.session_id,
                    metric="automation_duplicate",
                    event_id=metric_event_id,
                    payload={"automation_id": automation_id},
                )
        return response

    def automation(self, automation_id: str) -> dict[str, Any] | None:
        item = self.automation_store.get_automation(automation_id)
        if item is None:
            return None
        version = self.automation_store.latest_version(automation_id)
        return {**item, "version": version}

    def update_automation(self, automation_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        automation = self.automation_store.get_automation(automation_id)
        version = self.automation_store.latest_version(automation_id)
        if automation is None or version is None:
            raise KeyError(automation_id)
        merged = {**version["intent"], **dict(patch.get("intent") or patch)}
        intent = AutomationIntent.from_dict(merged)
        compiled = self.automations.compiler.compile(intent)
        validation = self.automations.compiler.validate(compiled)
        if not validation.valid:
            raise ValueError("Automation patch failed validation: " + "; ".join(validation.errors))
        compiled_payload = {
            "schema_version": compiled.schema_version,
            "backend": compiled.backend,
            "definition": dict(compiled.definition),
            "credential_refs": list(compiled.credential_refs),
            "model_routes": {key: dict(value) for key, value in compiled.model_routes.items()},
            "digest": compiled.digest,
        }
        next_version = self.automation_store.save_version(
            automation_id,
            intent=intent,
            compiled=compiled_payload,
            artifact=compiled.user_artifact,
            backend=compiled.backend,
        )
        return {"automation": self.automation_store.get_automation(automation_id), "version": next_version, "artifact": dict(compiled.user_artifact)}

    def list_automations(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            clauses: list[str] = []
            params: list[Any] = []
            if session_id:
                clauses.append("session_id=?")
                params.append(session_id)
            if user_id:
                clauses.append("user_id=?")
                params.append(user_id)
            if not include_archived:
                clauses.append("state<>'archived'")
            query = "select automation_id from agent_automations"
            if clauses:
                query += " where " + " and ".join(clauses)
            query += " order by updated_at desc limit ?"
            params.append(max(1, min(limit, 200)))
            rows = conn.execute(query, tuple(params)).fetchall()
        return [item for row in rows if (item := self.automation(str(row["automation_id"]))) is not None]

    def list_automation_callback_receipts(
        self,
        *,
        automation_id: str | None = None,
        submission_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return immutable n8n callback receipts without exposing secrets."""

        return self.automation_store.list_callback_receipts(
            automation_id=automation_id,
            submission_id=submission_id,
            limit=limit,
        )

    def set_run_status(self, run_id: str, status: str, *, result: dict[str, Any] | None = None) -> None:
        branch_status = {
            "queued": "ready",
            "running": "running",
            "waiting_user_input": "waiting_user_input",
            "waiting_decision": "waiting_decision",
            "waiting_approval": "waiting_approval",
            "suspended": "paused",
            "completed": "completed",
            "partially_completed": "partially_completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(status, status)
        timestamp = _now()
        with self._connect() as conn:
            row = conn.execute(
                "select forest_id, root_branch_id from agent_task_forests where run_id=? order by created_at desc limit 1",
                (run_id,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "update agent_task_forests set status=?, updated_at=? where forest_id=?",
                (status, timestamp, row[0]),
            )
            if branch_status in {
                "ready", "running", "waiting_user_input", "waiting_decision",
                "waiting_approval", "paused", "completed", "partially_completed",
                "failed", "cancelled",
            }:
                conn.execute(
                    "update agent_branches set status=?, result_json=coalesce(?, result_json), updated_at=? where branch_id=?",
                    (branch_status, _json(result) if result is not None else None, timestamp, row[1]),
                )
            conn.commit()

    def events_after(self, event_row_id: int = 0, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select rowid event_row_id, * from agent_events where rowid>? order by rowid limit ?",
                (max(0, int(event_row_id)), max(1, min(limit, 1000))),
            ).fetchall()
        return [
            {"event_row_id": int(row["event_row_id"]), **_decode(row["payload_json"], {})}
            for row in rows
        ]

    def restore_scoped_runtime_checkpoint(
        self,
        *,
        session_id: str,
        run_id: str,
        branch_id: str | None = None,
        step_id: str | None = None,
        decision_id: str | None = None,
        automation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Restore the narrowest durable checkpoint that contains Run state.

        The scoped checkpoint stream also records projection-only events.  Only
        ``checkpoint.created`` entries carry the orchestrator's complete
        replay state, so this method selects the narrowest compatible one and
        returns its embedded legacy-shaped payload for the provider loop.
        """

        specificity = {
            CheckpointLevel.STEP: 0,
            CheckpointLevel.DECISION: 1,
            CheckpointLevel.AUTOMATION: 2,
            CheckpointLevel.BRANCH: 3,
            CheckpointLevel.RUN: 4,
            CheckpointLevel.SESSION: 5,
        }
        candidates = []
        for record in self.checkpoints.store.list_for_session(session_id):
            if record.run_id and record.run_id != run_id:
                continue
            if record.branch_id and record.branch_id != branch_id:
                continue
            if record.step_id and record.step_id != step_id:
                continue
            if record.decision_id and record.decision_id != decision_id:
                continue
            if record.automation_id and record.automation_id != automation_id:
                continue
            envelope = dict(record.payload or {})
            if envelope.get("event_type") != "checkpoint.created":
                continue
            event_payload = envelope.get("payload")
            if not isinstance(event_payload, dict):
                continue
            raw_checkpoint = event_payload.get("checkpoint")
            if not isinstance(raw_checkpoint, dict):
                continue
            checkpoint = dict(raw_checkpoint)
            checkpoint_payload = checkpoint.get("payload")
            if not isinstance(checkpoint_payload, dict):
                continue
            # Runtime events redact keys containing token/secret. A recovery
            # checkpoint must be complete, not a redacted event projection.
            # The Durable runtime writes an unredacted internal record through
            # ``persist_runtime_checkpoint`` below.
            if not isinstance(checkpoint_payload.get("context_state"), dict):
                continue
            candidates.append((record, checkpoint))
        if not candidates:
            return None
        record, checkpoint = max(
            candidates,
            key=lambda item: (
                -specificity[item[0].level],
                item[0].sequence,
                item[0].created_at,
                item[0].checkpoint_id,
            ),
        )
        return {
            **checkpoint,
            "scoped_checkpoint": {
                "checkpoint_id": record.checkpoint_id,
                "level": record.level.value,
                "branch_id": record.branch_id,
                "step_id": record.step_id,
                "decision_id": record.decision_id,
                "automation_id": record.automation_id,
                "sequence": record.sequence,
            },
        }

    def persist_runtime_checkpoint(
        self,
        *,
        session_id: str,
        run_id: str,
        checkpoint: dict[str, Any],
        sequence: int,
        branch_id: str | None = None,
        step_id: str | None = None,
    ) -> None:
        """Store one canonical full replay copy without exposing it as an event.

        The run-level record recovers the provider loop. Branch and step IDs
        remain on normal durable events; duplicating a multi-megabyte replay
        body at each scope would otherwise create three equivalent payloads.
        """

        payload = {
            "event_type": "checkpoint.created",
            "payload": {"checkpoint": checkpoint},
            "recovery_payload": True,
        }
        base_key = str(checkpoint.get("checkpoint_id") or f"{run_id}:{sequence}")
        self.checkpoints.save(
            level=CheckpointLevel.RUN,
            session_id=session_id,
            run_id=run_id,
            payload=payload,
            sequence=sequence,
            idempotency_key=f"recovery-checkpoint:run:{base_key}",
        )

    def _insert_branch(self, forest: dict[str, Any], parent_id: str, objective_id: str, objective: str, timestamp: str, *, replaces: str | None = None) -> str:
        parent = next((item for item in forest["branches"] if item["branch_id"] == parent_id), None)
        if parent is None:
            raise KeyError(parent_id)
        if len(forest["branches"]) >= int(forest.get("limits", {}).get("max_nodes", 128)):
            raise ValueError("Task Forest node budget exceeded")
        normalized = " ".join(objective.casefold().split())
        duplicate = next(
            (
                item for item in forest["branches"]
                if item.get("parent_branch_id") == parent_id
                and item.get("status") != "cancelled"
                and item.get("branch_id") != replaces
                and " ".join(str(item.get("objective") or "").casefold().split()) == normalized
            ),
            None,
        )
        if duplicate is not None:
            return str(duplicate["branch_id"])
        branch_id = f"BR-{uuid4().hex}"
        plan_id = f"BLP-{uuid4().hex}"
        active = sum(
            item.get("status") in {
                "ready", "running", "waiting_user_input", "waiting_decision", "waiting_approval",
            }
            for item in forest["branches"]
        )
        branch_status = "ready" if active < int(forest.get("limits", {}).get("max_active_branches", 4)) else "pending"
        payload = {"objective": objective.strip(), "child_branch_ids": [], "evidence_ids": [], "replaces_branch_id": replaces}
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_branches(
                    branch_id, forest_id, parent_branch_id, objective_id, status,
                    execution_mode, depth, plan_revision, created_at, updated_at, payload_json
                ) values (?, ?, ?, ?, ?, 'parallel', ?, 1, ?, ?, ?)
                """,
                (branch_id, forest["forest_id"], parent_id, objective_id, branch_status, int(parent["depth"]) + 1, timestamp, timestamp, _json(payload)),
            )
            conn.execute(
                "insert into agent_branch_plans(branch_plan_id, branch_id, revision, status, created_at, payload_json) values (?, ?, 1, 'active', ?, ?)",
                (plan_id, branch_id, timestamp, _json({"objective": objective.strip()})),
            )
            conn.commit()
        return branch_id

    @staticmethod
    def _branch(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        steps = conn.execute(
            "select * from agent_branch_steps where branch_id=? order by position",
            (row["branch_id"],),
        ).fetchall()
        payload = _decode(row["payload_json"], {})
        return {**dict(row), **payload, "result": _decode(row["result_json"], None), "steps": [{**dict(item), **_decode(item["payload_json"], {})} for item in steps]}

    @staticmethod
    def _artifact_version(item: Any) -> dict[str, Any]:
        return {
            "artifact_version_id": item.artifact_version_id,
            "artifact_id": item.artifact_id,
            "version": item.version,
            "content": item.content,
            "changed_by": item.changed_by,
            "reason": item.reason,
            "message_id": item.message_id,
            "base_version": item.base_version,
            "affected_node_ids": list(item.affected_node_ids),
            "validation_result": item.validation_result,
            "restored_from_version": item.restored_from_version,
            "created_at": item.created_at,
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn
