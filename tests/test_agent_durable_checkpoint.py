from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from open_stock_ai.agent_runtime.forest.checkpoint_store import DurableCheckpointStore
from open_stock_ai.agent_runtime.forest.checkpoints import (
    CheckpointLevel,
    ScopedCheckpointManager,
)
from open_stock_ai.agent_runtime.final_runtime import FinalAgentRuntime
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from stock_ai.agent_run_store import AgentRunStore


def test_six_checkpoint_levels_reopen_and_restore_smallest_boundary(tmp_path: Path) -> None:
    database = tmp_path / "runtime.db"
    manager = ScopedCheckpointManager(DurableCheckpointStore(database))

    session = manager.save(
        level=CheckpointLevel.SESSION,
        session_id="AS-1",
        payload={"cursor": "session"},
        sequence=1,
    )
    run = manager.save(
        level=CheckpointLevel.RUN,
        session_id="AS-1",
        run_id="AR-1",
        payload={"cursor": "run"},
        sequence=2,
    )
    branch = manager.save(
        level=CheckpointLevel.BRANCH,
        session_id="AS-1",
        run_id="AR-1",
        branch_id="BR-1",
        payload={"cursor": "branch"},
        sequence=3,
    )
    step = manager.save(
        level=CheckpointLevel.STEP,
        session_id="AS-1",
        run_id="AR-1",
        branch_id="BR-1",
        step_id="BST-1",
        payload={"cursor": "step"},
        sequence=4,
    )
    decision = manager.save(
        level=CheckpointLevel.DECISION,
        session_id="AS-1",
        decision_id="DC-1",
        payload={"cursor": "decision"},
        sequence=5,
    )
    automation = manager.save(
        level=CheckpointLevel.AUTOMATION,
        session_id="AS-1",
        automation_id="AU-1",
        payload={"cursor": "automation"},
        sequence=6,
    )

    reopened = ScopedCheckpointManager(DurableCheckpointStore(database))
    assert reopened.latest_by_scope(
        level=CheckpointLevel.SESSION,
        session_id="AS-1",
    ) == session
    assert reopened.latest_by_scope(
        level=CheckpointLevel.RUN,
        session_id="AS-1",
        run_id="AR-1",
    ) == run
    assert reopened.latest_by_scope(
        level=CheckpointLevel.BRANCH,
        session_id="AS-1",
        run_id="AR-1",
        branch_id="BR-1",
    ) == branch
    assert reopened.latest_by_scope(
        level=CheckpointLevel.STEP,
        session_id="AS-1",
        run_id="AR-1",
        branch_id="BR-1",
        step_id="BST-1",
    ) == step
    assert reopened.latest_by_scope(
        level=CheckpointLevel.DECISION,
        session_id="AS-1",
        decision_id="DC-1",
    ) == decision
    assert reopened.latest_by_scope(
        level=CheckpointLevel.AUTOMATION,
        session_id="AS-1",
        automation_id="AU-1",
    ) == automation

    assert reopened.restore_smallest(
        session_id="AS-1",
        run_id="AR-1",
        branch_id="BR-1",
        step_id="BST-1",
    ) == step
    assert reopened.restore_smallest(
        session_id="AS-1",
        decision_id="DC-1",
    ) == decision
    assert reopened.restore_smallest(
        session_id="AS-1",
        automation_id="AU-1",
    ) == automation

    records = reopened.store.list_for_session("AS-1")
    assert {record.level for record in records} == set(CheckpointLevel)
    with sqlite3.connect(database) as conn:
        rows = conn.execute(
            """
            select status, processed_at from agent_runtime_events
             where event_type='agent.checkpoint'
            """
        ).fetchall()
    assert len(rows) == 6
    assert all(status == "processed" and processed_at for status, processed_at in rows)


def test_checkpoint_save_is_idempotent_and_latest_is_sequence_ordered(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.db"
    manager = ScopedCheckpointManager(DurableCheckpointStore(database))

    first = manager.save(
        level=CheckpointLevel.BRANCH,
        session_id="AS-1",
        run_id="AR-1",
        branch_id="BR-1",
        payload={"phase": "research", "sources": ["twse"]},
        sequence=7,
        idempotency_key="branch-safe-after-twse",
    )
    duplicate = manager.save(
        level=CheckpointLevel.BRANCH,
        session_id="AS-1",
        run_id="AR-1",
        branch_id="BR-1",
        payload={"sources": ["twse"], "phase": "research"},
        sequence=7,
        idempotency_key="branch-safe-after-twse",
    )
    assert duplicate == first

    with pytest.raises(ValueError, match="different content"):
        manager.save(
            level=CheckpointLevel.BRANCH,
            session_id="AS-1",
            run_id="AR-1",
            branch_id="BR-1",
            payload={"phase": "changed"},
            sequence=7,
            idempotency_key="branch-safe-after-twse",
        )

    latest = manager.save(
        level=CheckpointLevel.BRANCH,
        session_id="AS-1",
        run_id="AR-1",
        branch_id="BR-1",
        payload={"phase": "joined"},
        sequence=8,
        idempotency_key="branch-safe-after-join",
    )
    assert manager.latest_by_scope(
        level=CheckpointLevel.BRANCH,
        session_id="AS-1",
        run_id="AR-1",
        branch_id="BR-1",
    ) == latest

    with sqlite3.connect(database) as conn:
        count = conn.execute(
            """
            select count(*) from agent_runtime_events
             where event_type='agent.checkpoint'
            """
        ).fetchone()[0]
    assert count == 2


def test_repeated_objective_runs_create_distinct_session_forest_checkpoints(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.db"
    AgentSessionStore(database).create(
        session_id="AS-repeat",
        title="Repeated objective",
        namespace="test",
    )
    runs = AgentRunStore(database)
    for run_id in ("AR-repeat-1", "AR-repeat-2"):
        runs.create_run(
            run_id,
            {
                "objective": "請分析 2330.TW",
                "driver_id": "test",
                "session_id": "AS-repeat",
            },
        )

    runtime = FinalAgentRuntime(database)
    first = runtime.create_forest(
        session_id="AS-repeat",
        run_id="AR-repeat-1",
        objective="請分析 2330.TW",
    )
    second = runtime.create_forest(
        session_id="AS-repeat",
        run_id="AR-repeat-2",
        objective="請分析 2330.TW",
    )

    assert first["forest_id"] != second["forest_id"]
    session_checkpoints = [
        item
        for item in runtime.checkpoints.store.list_for_session("AS-repeat")
        if item.level == CheckpointLevel.SESSION
    ]
    assert len(session_checkpoints) == 2
    assert {item.payload["forest_id"] for item in session_checkpoints} == {
        first["forest_id"],
        second["forest_id"],
    }


def test_final_runtime_restores_narrowest_scoped_checkpoint_with_full_replay_state(
    tmp_path: Path,
) -> None:
    runtime = FinalAgentRuntime(tmp_path / "runtime.db")

    def envelope(marker: str) -> dict:
        return {
            "event_type": "checkpoint.created",
            "payload": {
                "checkpoint": {
                    "checkpoint_id": f"legacy-{marker}",
                    "sequence": 9,
                    "payload": {
                        "plan": {"plan_id": "AP-checkpoint"},
                        "transcript": [{"role": "host", "content": marker}],
                        "trace": [{"call_id": marker, "ok": True}],
                        "context_state": {"current_step": 2},
                    },
                }
            },
        }

    runtime.checkpoints.save(
        level=CheckpointLevel.RUN,
        session_id="AS-scoped",
        run_id="AR-scoped",
        payload=envelope("run"),
        sequence=8,
    )
    runtime.checkpoints.save(
        level=CheckpointLevel.STEP,
        session_id="AS-scoped",
        run_id="AR-scoped",
        branch_id="BR-research",
        step_id="BST-source",
        payload=envelope("step"),
        sequence=9,
    )

    restored = runtime.restore_scoped_runtime_checkpoint(
        session_id="AS-scoped",
        run_id="AR-scoped",
        branch_id="BR-research",
        step_id="BST-source",
    )

    assert restored["checkpoint_id"] == "legacy-step"
    assert restored["payload"]["transcript"][0]["content"] == "step"
    assert restored["scoped_checkpoint"]["checkpoint_id"].startswith("CP-")
    assert restored["scoped_checkpoint"]["level"] == "step"
    assert restored["scoped_checkpoint"]["branch_id"] == "BR-research"
    assert restored["scoped_checkpoint"]["step_id"] == "BST-source"
    assert restored["scoped_checkpoint"]["sequence"] == 9


def test_final_runtime_persists_one_run_level_recovery_copy_per_checkpoint(tmp_path: Path) -> None:
    runtime = FinalAgentRuntime(tmp_path / "runtime.db")
    checkpoint = {
        "checkpoint_id": "AC-single-copy",
        "payload": {
            "plan": {"plan_id": "AP-single-copy"},
            "transcript": [{"role": "host", "content": "resume"}],
            "trace": [],
            "context_state": {"current_step": 1},
        },
    }

    runtime.persist_runtime_checkpoint(
        session_id="AS-single-copy",
        run_id="AR-single-copy",
        checkpoint=checkpoint,
        sequence=11,
        branch_id="BR-research",
        step_id="BST-source",
    )

    records = runtime.checkpoints.store.list_for_session("AS-single-copy")
    assert len(records) == 1
    assert records[0].level == CheckpointLevel.RUN
    restored = runtime.restore_scoped_runtime_checkpoint(
        session_id="AS-single-copy",
        run_id="AR-single-copy",
        branch_id="BR-research",
        step_id="BST-source",
    )
    assert restored is not None
    assert restored["checkpoint_id"] == "AC-single-copy"
    assert restored["payload"]["transcript"][0]["content"] == "resume"


def test_checkpoint_survives_independent_python_process_reopen(tmp_path: Path) -> None:
    database = tmp_path / "runtime.db"
    project_root = Path(__file__).resolve().parents[1]
    writer = """
import json, sys
from open_stock_ai.agent_runtime.forest.checkpoint_store import DurableCheckpointStore
from open_stock_ai.agent_runtime.forest.checkpoints import CheckpointLevel, ScopedCheckpointManager
manager = ScopedCheckpointManager(DurableCheckpointStore(sys.argv[1]))
record = manager.save(
    level=CheckpointLevel.AUTOMATION,
    session_id='AS-PROCESS',
    automation_id='AU-PROCESS',
    payload={'next_run_at': '2026-08-11T01:00:00+00:00'},
    sequence=11,
    idempotency_key='scheduled-boundary',
)
print(json.dumps({'checkpoint_id': record.checkpoint_id, 'created_at': record.created_at}))
"""
    reader = """
import json, sys
from open_stock_ai.agent_runtime.forest.checkpoint_store import DurableCheckpointStore
from open_stock_ai.agent_runtime.forest.checkpoints import CheckpointLevel, ScopedCheckpointManager
manager = ScopedCheckpointManager(DurableCheckpointStore(sys.argv[1]))
record = manager.latest_by_scope(
    level=CheckpointLevel.AUTOMATION,
    session_id='AS-PROCESS',
    automation_id='AU-PROCESS',
)
print(json.dumps({
    'checkpoint_id': record.checkpoint_id,
    'sequence': record.sequence,
    'payload': record.payload,
}))
"""
    written = subprocess.run(
        [sys.executable, "-c", writer, str(database)],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )
    loaded = subprocess.run(
        [sys.executable, "-c", reader, str(database)],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )

    created = json.loads(written.stdout)
    restored = json.loads(loaded.stdout)
    assert restored == {
        "checkpoint_id": created["checkpoint_id"],
        "sequence": 11,
        "payload": {"next_run_at": "2026-08-11T01:00:00+00:00"},
    }
