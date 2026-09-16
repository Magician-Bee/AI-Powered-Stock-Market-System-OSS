from __future__ import annotations

import sqlite3
from collections.abc import Mapping


class P71SchemaContractError(RuntimeError):
    """Raised when the durable Agent domain schema is incomplete."""


# P71 is the durable boundary shared by the Session, Forest, interaction,
# recovery, memory, artifact, automation, notification, title, evidence and
# budget stores.  Keep the contract explicit so a database cannot claim the
# latest migration version while silently exposing an older table layout.
P71_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "agent_objective_versions": frozenset(
        {
            "objective_id", "session_id", "revision", "objective",
            "added_requirements_json", "removed_requirements_json",
            "constraints_json", "supersedes", "created_from_message",
            "created_at", "payload_json",
        }
    ),
    "agent_task_forests": frozenset(
        {
            "forest_id", "session_id", "run_id", "objective_id",
            "root_branch_id", "status", "revision", "created_at",
            "updated_at", "payload_json",
        }
    ),
    "agent_branches": frozenset(
        {
            "branch_id", "forest_id", "parent_branch_id", "objective_id",
            "status", "execution_mode", "depth", "plan_revision",
            "result_json", "created_at", "updated_at", "payload_json",
        }
    ),
    "agent_branch_plans": frozenset(
        {
            "branch_plan_id", "branch_id", "revision", "status", "created_at",
            "payload_json",
        }
    ),
    "agent_branch_steps": frozenset(
        {
            "step_id", "branch_id", "branch_plan_id", "position", "status",
            "created_at", "updated_at", "payload_json",
        }
    ),
    "agent_branch_dependencies": frozenset(
        {
            "branch_id", "dependency_branch_id", "dependency_type", "created_at",
            "payload_json",
        }
    ),
    "agent_join_nodes": frozenset(
        {
            "join_id", "forest_id", "parent_branch_id", "status",
            "created_at", "updated_at", "payload_json",
        }
    ),
    "agent_decision_checkpoints": frozenset(
        {
            "interaction_id", "session_id", "run_id", "branch_id", "status",
            "interaction_type", "created_at", "responded_at", "payload_json",
            "response_json",
        }
    ),
    "agent_user_proposals": frozenset(
        {
            "proposal_id", "session_id", "message_id", "branch_id",
            "proposal_type", "status", "created_at", "payload_json",
        }
    ),
    "agent_proposal_evaluations": frozenset(
        {"evaluation_id", "proposal_id", "decision", "created_at", "payload_json"}
    ),
    "agent_failure_fingerprints": frozenset(
        {
            "fingerprint", "first_seen_at", "last_seen_at", "occurrence_count",
            "identical_retry_count", "payload_json",
        }
    ),
    "agent_failure_ledger": frozenset(
        {
            "error_id", "session_id", "run_id", "branch_id", "step_id",
            "fingerprint", "category", "status", "created_at", "resolved_at",
            "payload_json",
        }
    ),
    "agent_repair_attempts": frozenset(
        {
            "repair_id", "error_id", "level", "strategy", "status",
            "output_hash", "arguments_hash", "started_at", "completed_at",
            "payload_json",
        }
    ),
    "agent_memory_candidates": frozenset(
        {
            "candidate_id", "namespace", "session_id", "run_id", "kind",
            "status", "importance", "future_relevance", "confidence",
            "durability", "created_at", "decided_at", "payload_json",
        }
    ),
    "agent_memory_conflicts": frozenset(
        {
            "conflict_id", "candidate_id", "existing_memory_id", "status",
            "created_at", "resolved_at", "payload_json",
        }
    ),
    "agent_memory_supersession": frozenset(
        {
            "supersession_id", "old_memory_id", "new_memory_id", "candidate_id",
            "created_at", "reason",
        }
    ),
    "agent_procedural_lessons": frozenset(
        {
            "lesson_id", "namespace", "fingerprint", "status",
            "verified_by_host", "occurrence_count", "created_at", "updated_at",
            "payload_json",
        }
    ),
    "agent_artifact_versions": frozenset(
        {
            "artifact_version_id", "artifact_id", "version", "parent_version",
            "changed_by", "change_reason", "reason", "message_id",
            "base_version", "affected_node_ids_json", "validation_result_json",
            "restored_from_version", "content_json", "validation_status",
            "sha256", "created_at", "payload_json",
        }
    ),
    "agent_artifact_selections": frozenset(
        {
            "selection_id", "artifact_id", "artifact_version", "session_id",
            "branch_id", "target_type", "node_id", "path", "created_at",
            "payload_json",
        }
    ),
    "agent_automation_intents": frozenset(
        {
            "intent_id", "session_id", "run_id", "branch_id", "user_id",
            "goal", "symbol", "kind", "status", "semantic_fingerprint",
            "intent_json", "created_at", "updated_at", "payload_json",
        }
    ),
    "agent_automations": frozenset(
        {
            "automation_id", "intent_id", "session_id", "user_id", "goal",
            "symbol", "kind", "state", "status", "backend",
            "backend_reference", "semantic_fingerprint", "current_version",
            "current_decision_json", "notification_state_json", "created_at",
            "updated_at", "expires_at", "payload_json",
        }
    ),
    "agent_automation_versions": frozenset(
        {
            "automation_version_id", "automation_id", "version", "status",
            "intent_json", "compiled_json", "artifact_json", "created_at",
            "payload_json",
        }
    ),
    "agent_automation_executions": frozenset(
        {
            "execution_id", "automation_id", "automation_version", "run_id",
            "stage", "status", "input_json", "output_json",
            "meaningful_change", "decision_changed", "created_at", "started_at",
            "completed_at", "payload_json",
        }
    ),
    "agent_notification_deliveries": frozenset(
        {
            "delivery_id", "automation_id", "execution_id", "user_id",
            "channel", "dedup_key", "status", "provider_receipt_json", "error",
            "created_at", "updated_at", "delivered_at", "acknowledged_at",
            "snoozed_until", "expires_at", "payload_json",
        }
    ),
    "agent_session_titles": frozenset(
        {"session_id", "current_revision", "title", "updated_at", "payload_json"}
    ),
    "agent_session_title_history": frozenset(
        {
            "title_history_id", "session_id", "revision", "title", "reason",
            "created_at", "payload_json",
        }
    ),
    "agent_evidence": frozenset(
        {
            "evidence_id", "session_id", "run_id", "branch_id", "claim",
            "source_type", "observed_at", "payload_json",
        }
    ),
    "agent_evidence_edges": frozenset(
        {"edge_id", "from_evidence_id", "to_evidence_id", "relation", "claim_key", "payload_json"}
    ),
    "agent_kpi_events": frozenset(
        {"kpi_event_id", "session_id", "run_id", "metric", "value", "created_at", "payload_json"}
    ),
    "agent_token_budgets": frozenset(
        {
            "budget_id", "session_id", "run_id", "branch_id", "scope",
            "allocated", "consumed", "updated_at", "payload_json",
        }
    ),
}

P71_REQUIRED_INDEXES = frozenset(
    {
        "idx_agent_objectives_session_revision",
        "idx_agent_forests_session_updated",
        "idx_agent_branches_forest_status",
        "idx_agent_branch_steps_branch_position",
        "idx_agent_interactions_session_status",
        "idx_agent_failures_fingerprint_status",
        "idx_agent_memory_candidates_status",
        "idx_agent_artifact_versions_current",
        "idx_agent_artifact_selections_session",
        "idx_agent_automations_active_fingerprint",
        "idx_agent_automation_executions_status",
        "idx_agent_notifications_dedup",
        "idx_agent_kpi_metric_created",
    }
)


def verify_p71_schema(conn: sqlite3.Connection) -> dict[str, object]:
    """Verify the complete P71 durable schema without modifying the database."""

    objects = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            "select name, type from sqlite_master where type in ('table', 'index')"
        )
    }
    missing_tables = sorted(
        name for name in P71_REQUIRED_COLUMNS if objects.get(name) != "table"
    )
    missing_columns: dict[str, list[str]] = {}
    for table, required in P71_REQUIRED_COLUMNS.items():
        if table in missing_tables:
            continue
        actual = {str(row[1]) for row in conn.execute(f'pragma table_info("{table}")')}
        absent = sorted(required - actual)
        if absent:
            missing_columns[table] = absent
    missing_indexes = sorted(
        name for name in P71_REQUIRED_INDEXES if objects.get(name) != "index"
    )

    if missing_tables or missing_columns or missing_indexes:
        details = []
        if missing_tables:
            details.append(f"missing_tables={','.join(missing_tables)}")
        if missing_columns:
            encoded = ";".join(
                f"{table}:{','.join(columns)}" for table, columns in sorted(missing_columns.items())
            )
            details.append(f"missing_columns={encoded}")
        if missing_indexes:
            details.append(f"missing_indexes={','.join(missing_indexes)}")
        raise P71SchemaContractError("P71 schema contract failed: " + " | ".join(details))

    return {
        "valid": True,
        "table_count": len(P71_REQUIRED_COLUMNS),
        "index_count": len(P71_REQUIRED_INDEXES),
    }
