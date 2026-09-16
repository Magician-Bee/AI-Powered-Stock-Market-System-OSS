from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from open_stock_ai.agent_runtime.memory import (
    DurableMemoryGovernanceEngine,
    MemoryCandidate,
    MemoryDecision,
    MemoryManager,
    MemoryStatus,
    MemoryStore,
)
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from stock_ai.agent_run_store import AgentRunStore


def _candidate(
    content: str,
    *,
    kind: str = "project_state",
    semantic_key: str | None = None,
    source: str = "host_verified",
    run_id: str | None = None,
    host_verified: bool = False,
    durability: str = "long_term",
    importance: float = 0.9,
    future_relevance: float = 0.85,
    confidence: float = 0.95,
    expires_at: str | None = None,
    fingerprint: str | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        content=content,
        kind=kind,
        importance=importance,
        future_relevance=future_relevance,
        confidence=confidence,
        source=source,
        durability=durability,
        semantic_key=semantic_key,
        run_id=run_id,
        host_verified=host_verified,
        expires_at=expires_at,
        fingerprint=fingerprint,
    )


def test_memory_manager_exposes_durable_six_layer_candidate_api(tmp_path):
    path = tmp_path / "memory.sqlite"
    manager = MemoryManager(MemoryStore(path), project_root=tmp_path)
    layers = (
        "working",
        "session",
        "user_preference",
        "project_state",
        "episodic",
        "procedural",
    )

    results = [
        manager.consider(
            _candidate(
                f"durable {layer}",
                kind=layer,
                semantic_key=f"layer:{layer}",
                source="user_explicit" if layer == "user_preference" else "host_verified",
                host_verified=layer == "procedural",
            )
        )
        for layer in layers
    ]

    reopened = MemoryManager(MemoryStore(path), project_root=tmp_path)
    assert all(result.decision is MemoryDecision.SAVE for result in results)
    assert {record.layer.value for record in reopened.governance.records()} == set(layers)
    assert {item["kind"] for item in reopened.governance.candidates(status="save")} == set(layers)
    preference = next(record for record in reopened.governance.records() if record.layer.value == "user_preference")
    assert preference.advisory is True
    assert preference.source["enforcement"] == "contextual_only"


def test_completed_market_run_keeps_prices_in_working_memory_only(tmp_path):
    path = tmp_path / "market-memory.sqlite"
    manager = MemoryManager(MemoryStore(path), project_root=tmp_path)
    AgentSessionStore(path).create(
        session_id="AS-market", namespace="test", title="Market memory"
    )
    AgentRunStore(path).create_run(
        "AR-market",
        {
            "objective": "2330.TW 今天股價怎麼了？",
            "symbols": ["2330.TW"],
            "driver_id": "test",
            "autonomy": "advisory",
            "max_steps": 2,
            "session_id": "AS-market",
        },
    )

    result = manager.remember_run(
        session_id="AS-market",
        run_id="AR-market",
        objective="2330.TW 今天股價怎麼了？",
        summary="收盤 1,125 元，上漲 1.2%。",
        evidence={"successful_observation_count": 1},
    )

    assert result["consolidation_decision"] == "working_only_transient_market_data"
    assert result["candidate_id"] is None
    assert not any(record.layer.value == "episodic" for record in manager.governance.records())
    working = manager.store.get(result["working_memory_id"])
    assert working and "1,125 元" in working["content"]


def test_candidate_lifecycle_and_audit_survive_reopen(tmp_path):
    path = tmp_path / "memory.sqlite"
    first_engine = DurableMemoryGovernanceEngine(
        MemoryStore(path),
        namespace="durable:test",
    )

    saved = first_engine.consider(
        _candidate("Artifact 可點擊修改", semantic_key="artifact-edit")
    )
    merged = first_engine.consider(
        _candidate(" Artifact   可點擊修改 ", semantic_key="artifact-edit")
    )
    superseded = first_engine.consider(
        _candidate(
            "Artifact 必須支援局部修改",
            semantic_key="artifact-edit",
            source="user_explicit",
        )
    )
    rejected = first_engine.consider(
        _candidate(
            "Artifact 不用支援修改",
            semantic_key="artifact-edit",
            source="model_inference",
            durability="session",
            importance=0.1,
            future_relevance=0.1,
            confidence=0.1,
        )
    )
    expired = first_engine.consider(
        _candidate(
            "過期候選記憶",
            semantic_key="expired-candidate",
            expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )
    )

    reopened = DurableMemoryGovernanceEngine(MemoryStore(path), namespace="durable:test")
    decisions = [item["status"] for item in reopened.candidates()]

    assert saved.decision is MemoryDecision.SAVE
    assert merged.decision is MemoryDecision.MERGE
    assert superseded.decision is MemoryDecision.SUPERSEDE
    assert rejected.decision is MemoryDecision.REJECT
    assert expired.decision is MemoryDecision.EXPIRE
    assert decisions == ["save", "merge", "supersede", "reject", "expire"]
    assert [entry.decision.value for entry in reopened.audit_log] == decisions
    assert all(item["decided_at"] for item in reopened.candidates())


def test_explicit_conflict_supersession_keeps_old_memory_as_audit(tmp_path):
    path = tmp_path / "memory.sqlite"
    worker = """
import json
import sys
from open_stock_ai.agent_runtime.memory import DurableMemoryGovernanceEngine, MemoryCandidate, MemoryStore

db_path, content, source = sys.argv[1:]
engine = DurableMemoryGovernanceEngine(MemoryStore(db_path), namespace="durable:test")
result = engine.consider(MemoryCandidate(
    content=content,
    kind="project_state",
    importance=0.9,
    future_relevance=0.85,
    confidence=0.95,
    source=source,
    durability="long_term",
    semantic_key="external-sources",
))
print(json.dumps({
    "decision": result.decision.value,
    "memory_id": result.record.memory_id if result.record else None,
}))
"""

    def run_worker(content: str, source: str) -> dict:
        completed = subprocess.run(
            [sys.executable, "-c", worker, str(path), content, source],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    old = run_worker("使用者不需要外部資料", "host_verified")
    new = run_worker("股票分析一定要查外部來源", "user_explicit")

    reopened = DurableMemoryGovernanceEngine(MemoryStore(path), namespace="durable:test")
    records = reopened.records(include_inactive=True)
    conflicts = reopened.conflicts()
    supersessions = reopened.supersessions()

    assert new["decision"] == "supersede"
    assert len(records) == 2
    assert next(item for item in records if item.memory_id == old["memory_id"]).status is MemoryStatus.SUPERSEDED
    assert next(item for item in records if item.memory_id == new["memory_id"]).status is MemoryStatus.ACTIVE
    assert conflicts[0]["status"] == "resolved_superseded"
    assert conflicts[0]["payload"]["resulting_memory_id"] == new["memory_id"]
    assert supersessions[0]["old_memory_id"] == old["memory_id"]
    assert supersessions[0]["new_memory_id"] == new["memory_id"]


def test_procedural_distinct_run_and_host_verified_gates_survive_process_reopen(tmp_path):
    path = tmp_path / "memory.sqlite"
    worker = """
import json
import sys
from open_stock_ai.agent_runtime.memory import DurableMemoryGovernanceEngine, MemoryCandidate, MemoryStore

db_path, namespace, fingerprint, run_id, host_verified = sys.argv[1:]
engine = DurableMemoryGovernanceEngine(MemoryStore(db_path), namespace=namespace)
result = engine.consider(MemoryCandidate(
    content=f"lesson:{fingerprint}",
    kind="procedural",
    importance=0.9,
    future_relevance=0.9,
    confidence=0.95,
    source="host_verified" if host_verified == "1" else "repair_observation",
    durability="long_term",
    fingerprint=fingerprint,
    semantic_key=fingerprint,
    run_id=run_id,
    host_verified=host_verified == "1",
))
print(json.dumps({"decision": result.decision.value, "candidate_id": result.candidate_id}))
"""

    def run_worker(fingerprint: str, run_id: str, *, host_verified: bool = False) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                worker,
                str(path),
                "durable:process",
                fingerprint,
                run_id,
                "1" if host_verified else "0",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    first = run_worker("schema-repair", "run-1")
    duplicate_run = run_worker("schema-repair", "run-1")
    second_run = run_worker("schema-repair", "run-2")
    verified = run_worker("host-approved-repair", "run-3", host_verified=True)

    reopened = DurableMemoryGovernanceEngine(
        MemoryStore(path),
        namespace="durable:process",
    )
    lessons = {item["fingerprint"]: item for item in reopened.procedural_lessons()}

    assert first["decision"] == "reject"
    assert duplicate_run["decision"] == "reject"
    assert second_run["decision"] == "save"
    assert verified["decision"] == "save"
    assert lessons["schema-repair"]["occurrence_count"] == 2
    assert lessons["schema-repair"]["status"] == "promoted"
    assert lessons["schema-repair"]["payload"]["run_ids"] == ["run-1", "run-2"]
    assert lessons["host-approved-repair"]["verified_by_host"] == 1
    assert lessons["host-approved-repair"]["status"] == "promoted"
    assert len(reopened.candidates()) == 4
    assert len(reopened.records(include_inactive=False)) == 2
