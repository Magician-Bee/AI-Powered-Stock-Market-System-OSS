from __future__ import annotations

import asyncio
import sqlite3

from open_stock_ai.agent_runtime.final_runtime import FinalAgentRuntime
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from open_stock_ai.agent_runtime.repair import ErrorReceipt, FailureFingerprint
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.durable_agent_runtime import DurableAgentRuntime


def _plan() -> dict:
    return {
        "plan_id": "AP-recursive",
        "objective": "Research and validate a stock decision",
        "revision_number": 1,
        "completion_criteria": ["Both branches have validated results"],
        "nodes": [
            {
                "node_id": "research",
                "node_type": "subtask",
                "title": "Research evidence",
                "status": "ready",
                "order_index": 0,
            },
            {
                "node_id": "research-tool",
                "node_type": "tool",
                "title": "Fetch evidence",
                "tool_name": "market_data",
                "parent_id": "research",
                "status": "pending",
                "order_index": 1,
            },
            {
                "node_id": "analysis",
                "node_type": "subagent",
                "title": "Validate the decision",
                "dependencies": ["research-tool"],
                "status": "pending",
                "order_index": 2,
            },
        ],
    }


def _event(event_id: str, sequence: int, event_type: str, node_id: str | None = None, **payload):
    body = dict(payload)
    if node_id:
        body["node_id"] = node_id
    return {
        "event_id": event_id,
        "sequence": sequence,
        "timestamp": f"2026-08-08T00:00:{sequence:02d}+00:00",
        "type": event_type,
        "run_id": "AR-recursive",
        "payload": body,
    }


def test_plan_and_host_lifecycle_project_to_durable_recursive_forest(tmp_path):
    path = tmp_path / "forest.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-recursive",
        title="Recursive test",
        namespace="test",
    )
    AgentRunStore(path).create_run(
        "AR-recursive",
        {
            "objective": "Research and validate a stock decision",
            "driver_id": "test",
            "session_id": "AS-recursive",
        },
    )
    runtime = FinalAgentRuntime(path)
    runtime.create_forest(
        session_id="AS-recursive",
        run_id="AR-recursive",
        objective="Research and validate a stock decision",
    )

    proposed = _event("E-plan", 1, "plan.proposed", plan=_plan())
    projected = runtime.project_runtime_event("AR-recursive", proposed)
    assert [item["type"] for item in projected.events] == [
        "branch.created",
        "branch.created",
    ]

    forest = runtime.forest("AS-recursive", run_id="AR-recursive")
    assert forest is not None
    branches = {
        item.get("source_plan_node_id", "root"): item
        for item in forest["branches"]
    }
    assert set(branches) == {"root", "research", "analysis"}
    assert [step["source_node_id"] for step in branches["research"]["steps"]] == [
        "research",
        "research-tool",
    ]
    assert branches["analysis"]["dependencies"] == ["research-tool"]
    assert forest["join_nodes"][0]["status"] == "waiting"

    started = runtime.project_runtime_event(
        "AR-recursive",
        _event("E-start", 2, "step.started", "research"),
    )
    assert started.branch_id == branches["research"]["branch_id"]
    assert [item["type"] for item in started.events] == ["branch.started"]

    runtime.project_runtime_event(
        "AR-recursive",
        _event("E-research", 3, "step.completed", "research", result_summary="scope ready"),
    )
    completed_research = runtime.project_runtime_event(
        "AR-recursive",
        _event("E-tool", 4, "step.completed", "research-tool", result_summary="evidence ready"),
    )
    assert [item["type"] for item in completed_research.events] == ["branch.completed"]

    completed_analysis = runtime.project_runtime_event(
        "AR-recursive",
        _event("E-analysis", 5, "step.completed", "analysis", result_summary="validated"),
    )
    assert [item["type"] for item in completed_analysis.events] == [
        "branch.completed",
        "join.completed",
    ]

    restored = FinalAgentRuntime(path).forest("AS-recursive", run_id="AR-recursive")
    assert restored is not None
    assert restored["join_nodes"][0]["status"] == "completed"
    assert {
        item["status"]
        for item in restored["branches"]
        if item.get("source_plan_node_id")
    } == {"completed"}

    duplicate = runtime.project_runtime_event(
        "AR-recursive",
        _event("E-analysis", 5, "step.completed", "analysis", result_summary="validated"),
    )
    assert duplicate.events == []


def test_completed_join_reopens_when_plan_revision_adds_a_required_branch(tmp_path):
    path = tmp_path / "join-revision.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-join-revision",
        title="Join revision test",
        namespace="test",
    )
    AgentRunStore(path).create_run(
        "AR-recursive",
        {
            "objective": "Expand validated research",
            "driver_id": "test",
            "session_id": "AS-join-revision",
        },
    )
    runtime = FinalAgentRuntime(path)
    runtime.create_forest(
        session_id="AS-join-revision",
        run_id="AR-recursive",
        objective="Expand validated research",
    )
    first_plan = {
        "plan_id": "AP-join-revision",
        "objective": "Expand validated research",
        "revision_number": 1,
        "nodes": [
            {
                "node_id": "research",
                "node_type": "subtask",
                "title": "Research evidence",
                "status": "ready",
                "order_index": 0,
            }
        ],
    }
    runtime.project_runtime_event(
        "AR-recursive",
        _event("E-plan-one", 1, "plan.proposed", plan=first_plan),
    )
    completed = runtime.project_runtime_event(
        "AR-recursive",
        _event("E-research-one", 2, "step.completed", "research", result_summary="first result"),
    )
    assert [item["type"] for item in completed.events] == [
        "branch.completed",
        "join.completed",
    ]

    expanded_plan = {
        **first_plan,
        "revision_number": 2,
        "nodes": [
            {**first_plan["nodes"][0], "status": "completed"},
            {
                "node_id": "analysis",
                "node_type": "subagent",
                "title": "Independent analysis",
                "status": "ready",
                "order_index": 1,
            },
        ],
    }
    runtime.project_runtime_event(
        "AR-recursive",
        _event("E-plan-two", 3, "plan.revised", plan=expanded_plan),
    )
    reopened = runtime.forest("AS-join-revision", run_id="AR-recursive")
    assert reopened is not None
    join = reopened["join_nodes"][0]
    assert join["status"] == "waiting"
    assert "result" not in join
    assert len(join["required_branch_ids"]) == 2

    final = runtime.project_runtime_event(
        "AR-recursive",
        _event("E-analysis-two", 4, "step.completed", "analysis", result_summary="second result"),
    )
    assert [item["type"] for item in final.events] == [
        "branch.completed",
        "join.completed",
    ]
    restored = runtime.forest("AS-join-revision", run_id="AR-recursive")
    assert restored is not None
    joined = restored["join_nodes"][0]
    assert joined["status"] == "completed"
    assert len(joined["result"]["branch_results"]) == 2


def test_no_tool_completion_does_not_project_validation_receipt_as_a_fake_step(tmp_path):
    path = tmp_path / "no-tool-forest.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-no-tool",
        title="No tool test",
        namespace="test",
    )
    AgentRunStore(path).create_run(
        "AR-no-tool",
        {"objective": "Reply directly", "driver_id": "test", "session_id": "AS-no-tool"},
    )
    runtime = FinalAgentRuntime(path)
    runtime.create_forest(
        session_id="AS-no-tool",
        run_id="AR-no-tool",
        objective="Reply directly",
    )
    runtime.project_runtime_event(
        "AR-no-tool",
        _event(
            "E-empty-plan",
            1,
            "plan.proposed",
            plan={
                "plan_id": "AP-no-tool",
                "objective": "Reply directly",
                "revision_number": 1,
                "nodes": [],
            },
        ),
    )
    projected = runtime.project_runtime_event(
        "AR-no-tool",
        _event("E-validation", 2, "validation.passed", "provider-local-1"),
    )

    forest = runtime.forest("AS-no-tool", run_id="AR-no-tool")
    assert forest is not None
    root = next(item for item in forest["branches"] if item.get("source_plan_node_id") is None)
    assert [(item["title"], item["status"]) for item in root["steps"]] == [
        ("理解目標並建立局部計畫", "completed"),
    ]
    assert projected.step_id is None


def test_provider_turn_number_in_a_tool_event_never_becomes_a_forest_step(tmp_path):
    path = tmp_path / "provider-turn-forest.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-provider-turn",
        title="Provider turn test",
        namespace="test",
    )
    AgentRunStore(path).create_run(
        "AR-recursive",
        {"objective": "Use one verified tool", "driver_id": "test", "session_id": "AS-provider-turn"},
    )
    runtime = FinalAgentRuntime(path)
    runtime.create_forest(
        session_id="AS-provider-turn",
        run_id="AR-recursive",
        objective="Use one verified tool",
    )
    plan = {
        "plan_id": "AP-provider-turn",
        "objective": "Use one verified tool",
        "revision_number": 1,
        "nodes": [{
            "node_id": "verified-tool",
            "node_type": "tool",
            "title": "Fetch verified evidence",
            "status": "pending",
        }],
    }
    runtime.project_runtime_event(
        "AR-recursive", _event("E-plan", 1, "plan.proposed", plan=plan)
    )

    projected = runtime.project_runtime_event(
        "AR-recursive", _event("E-provider-turn", 2, "tool.failed", "2")
    )

    forest = runtime.forest("AS-provider-turn", run_id="AR-recursive")
    assert forest is not None
    source_ids = [
        step.get("source_node_id")
        for branch in forest["branches"]
        for step in branch["steps"]
    ]
    assert projected.step_id is None
    assert "2" not in source_ids
    assert "verified-tool" in source_ids


def test_run_completion_closes_superseded_pending_reasoning_steps(tmp_path):
    path = tmp_path / "run-completion-forest.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-run-complete",
        title="Run completion test",
        namespace="test",
    )
    AgentRunStore(path).create_run(
        "AR-recursive",
        {"objective": "Complete reasoning", "driver_id": "test", "session_id": "AS-run-complete"},
    )
    runtime = FinalAgentRuntime(path)
    runtime.create_forest(
        session_id="AS-run-complete",
        run_id="AR-recursive",
        objective="Complete reasoning",
    )
    runtime.project_runtime_event(
        "AR-recursive",
        _event(
            "E-pending-plan",
            1,
            "plan.proposed",
            plan={
                "plan_id": "AP-run-complete",
                "objective": "Complete reasoning",
                "revision_number": 1,
                "nodes": [
                    {
                        "node_id": "reasoning-one",
                        "node_type": "reasoning",
                        "title": "Synthesize result",
                        "status": "pending",
                    }
                ],
            },
        ),
    )
    runtime.project_runtime_event(
        "AR-recursive",
        _event("E-run-completed", 2, "run.completed"),
    )

    forest = runtime.forest("AS-run-complete", run_id="AR-recursive")
    assert forest is not None
    steps = [step for branch in forest["branches"] for step in branch["steps"]]
    reasoning = next(step for step in steps if step.get("source_node_id") == "reasoning-one")
    assert reasoning["status"] == "completed"
    assert reasoning["finalized_by_run_completion"] is True


def test_run_completion_never_closes_pending_steps_when_a_branch_failed(tmp_path):
    path = tmp_path / "run-completion-failure-forest.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-run-failure",
        title="Run completion failure test",
        namespace="test",
    )
    AgentRunStore(path).create_run(
        "AR-run-failure",
        {"objective": "Preserve failure", "driver_id": "test", "session_id": "AS-run-failure"},
    )
    runtime = FinalAgentRuntime(path)
    runtime.create_forest(
        session_id="AS-run-failure",
        run_id="AR-run-failure",
        objective="Preserve failure",
    )
    runtime.project_runtime_event(
        "AR-run-failure",
        _event(
            "E-failure-plan",
            1,
            "plan.proposed",
            plan={
                "plan_id": "AP-run-failure",
                "objective": "Preserve failure",
                "revision_number": 1,
                "nodes": [
                    {"node_id": "failed-tool", "node_type": "tool", "title": "Failed source", "status": "pending"},
                    {"node_id": "pending-synthesis", "node_type": "synthesis", "title": "Synthesize", "status": "pending"},
                ],
            },
        ),
    )
    runtime.project_runtime_event(
        "AR-run-failure",
        _event("E-failed-tool", 2, "step.failed", "failed-tool", error_summary="source unavailable"),
    )
    runtime.project_runtime_event(
        "AR-run-failure",
        _event("E-misclassified-complete", 3, "run.completed"),
    )

    forest = runtime.forest("AS-run-failure", run_id="AR-run-failure")
    assert forest is not None
    steps = [step for branch in forest["branches"] for step in branch["steps"]]
    failed = next(step for step in steps if step.get("source_node_id") == "failed-tool")
    pending = next(step for step in steps if step.get("source_node_id") == "pending-synthesis")
    assert failed["status"] == "failed"
    assert pending["status"] == "pending"
    assert "finalized_by_run_completion" not in pending


def test_recovery_link_is_persisted_on_original_failed_step(tmp_path):
    path = tmp_path / "recovery-link-forest.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-recovery-link",
        title="Recovery link test",
        namespace="test",
    )
    AgentRunStore(path).create_run(
        "AR-recovery-link",
        {"objective": "Link alternative evidence", "driver_id": "test", "session_id": "AS-recovery-link"},
    )
    runtime = FinalAgentRuntime(path)
    runtime.create_forest(
        session_id="AS-recovery-link",
        run_id="AR-recovery-link",
        objective="Link alternative evidence",
    )
    runtime.project_runtime_event(
        "AR-recovery-link",
        _event(
            "E-recovery-plan",
            1,
            "plan.proposed",
            plan={
                "plan_id": "AP-recovery-link",
                "objective": "Link alternative evidence",
                "revision_number": 1,
                "nodes": [
                    {"node_id": "failed-source", "node_type": "tool", "title": "Primary source", "status": "pending"},
                    {"node_id": "alternate-source", "node_type": "tool", "title": "Alternate source", "status": "pending"},
                ],
            },
        ),
    )
    runtime.project_runtime_event(
        "AR-recovery-link",
        _event("E-primary-failed", 2, "step.failed", "failed-source", error_summary="primary timeout"),
    )
    link = {
        "failed_node_id": "failed-source",
        "recovery_tool": "web.research",
        "recovery_call_id": "call-alternate",
        "reason": "independent public source",
    }
    runtime.project_runtime_event(
        "AR-recovery-link",
        _event(
            "E-recovery-linked",
            3,
            "recovery.linked",
            "alternate-source",
            recovery_for=[link],
        ),
    )

    forest = runtime.forest("AS-recovery-link", run_id="AR-recovery-link")
    assert forest is not None
    steps = [step for branch in forest["branches"] for step in branch["steps"]]
    failed = next(step for step in steps if step.get("source_node_id") == "failed-source")
    assert failed["status"] == "failed"
    assert failed["recovery_status"] == "recovered_with_alternative_evidence"
    assert failed["metadata"]["recovered_by"] == [link]


def test_subtask_children_are_real_idempotent_branches_with_a_durable_join(tmp_path):
    path = tmp_path / "subtask-branches.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-subtasks",
        title="Subtask test",
        namespace="test",
    )
    AgentRunStore(path).create_run(
        "AR-subtasks",
        {"objective": "Compare sources", "driver_id": "test", "session_id": "AS-subtasks"},
    )
    runtime = FinalAgentRuntime(path)
    runtime.create_forest(
        session_id="AS-subtasks",
        run_id="AR-subtasks",
        objective="Compare sources",
    )
    runtime.project_runtime_event(
        "AR-subtasks",
        _event(
            "E-subtask-plan", 1, "plan.proposed",
            plan={
                "plan_id": "AP-subtasks",
                "objective": "Compare sources",
                "revision_number": 1,
                "nodes": [{"node_id": "delegate", "node_type": "subtask", "title": "Compare sources"}],
            },
        ),
    )

    children = runtime.create_subtask_branches(
        session_id="AS-subtasks",
        run_id="AR-subtasks",
        source_node_id="delegate",
        objectives=("Official source", "Independent source"),
        role="researcher",
    )
    repeated = runtime.create_subtask_branches(
        session_id="AS-subtasks",
        run_id="AR-subtasks",
        source_node_id="delegate",
        objectives=("Official source", "Independent source"),
        role="researcher",
    )

    assert [item["branch_id"] for item in repeated] == [item["branch_id"] for item in children]
    forest = runtime.forest("AS-subtasks", run_id="AR-subtasks")
    assert forest is not None
    delegated = next(item for item in forest["branches"] if item.get("source_plan_node_id") == "delegate")
    child_ids = {item["branch_id"] for item in children}
    assert child_ids <= set(delegated["child_branch_ids"])
    assert all(item["parent_branch_id"] == delegated["branch_id"] for item in children)
    assert all(item["source_subtask_node_id"] == "delegate" for item in children)
    assert all("source_plan_node_id" not in item for item in children)
    joins = [item for item in forest["join_nodes"] if item.get("source_subtask_node_id") == "delegate"]
    assert len(joins) == 1
    assert set(joins[0]["required_branch_ids"]) == child_ids

    runtime.set_linked_branch_run_status(
        children[0]["branch_id"],
        run_id="AR-child-official",
        status="completed",
        result={"status": "completed", "summary": "official source verified"},
    )
    completed_child = runtime.branch(children[0]["branch_id"])
    assert completed_child is not None
    assert completed_child["status"] == "completed"
    assert completed_child["steps"][0]["status"] == "completed"
    assert completed_child["steps"][0]["result_summary"] == "official source verified"
    assert completed_child["last_lifecycle_transition"]["source"] == "linked_run_status"

    # A late provider callback must not reopen a completed child and make its
    # parent Join wait forever.  The durable Forest transition boundary owns
    # this guard rather than relying on prompt wording or UI timing.
    runtime.set_linked_branch_run_status(
        children[0]["branch_id"],
        run_id="AR-child-official",
        status="running",
    )
    stale_child = runtime.branch(children[0]["branch_id"])
    assert stale_child is not None
    assert stale_child["status"] == "completed"
    assert stale_child["last_lifecycle_transition"]["ignored"] == "terminal_branch_cannot_reopen"

    runtime.set_linked_branch_run_status(
        children[1]["branch_id"],
        run_id="AR-child-independent",
        status="completed",
        result={"status": "completed", "summary": "independent source verified"},
    )
    joined = runtime.forest("AS-subtasks", run_id="AR-subtasks")
    assert joined is not None
    joined_subtasks = next(
        item for item in joined["join_nodes"] if item.get("source_subtask_node_id") == "delegate"
    )
    assert joined_subtasks["status"] == "completed"
    assert joined_subtasks["result"]["partial"] is False

    runtime.project_runtime_event(
        "AR-subtasks",
        _event(
            "E-subtask-plan-revised", 2, "plan.revised",
            plan={
                "plan_id": "AP-subtasks",
                "objective": "Compare sources",
                "revision_number": 2,
                "nodes": [{"node_id": "delegate", "node_type": "subtask", "title": "Compare sources"}],
            },
        ),
    )
    revised = runtime.forest("AS-subtasks", run_id="AR-subtasks")
    assert revised is not None
    assert next(item for item in revised["branches"] if item.get("source_plan_node_id") == "delegate")["branch_id"] == delegated["branch_id"]


def test_durable_runtime_emits_real_branch_and_join_events(tmp_path):
    class RecursiveService:
        snapshot_builder = None

        async def run(self, **kwargs):
            sink = kwargs["event_sink"]
            run_id = kwargs["run_id"]
            await sink(
                {
                    "event_id": "E-plan",
                    "sequence": 1,
                    "type": "plan.proposed",
                    "run_id": run_id,
                    "payload": {"plan": _plan()},
                }
            )
            sequence = 2
            for node_id in ("research", "research-tool", "analysis"):
                await sink(
                    {
                        "event_id": f"E-{node_id}",
                        "sequence": sequence,
                        "type": "step.completed",
                        "run_id": run_id,
                        "payload": {
                            "node_id": node_id,
                            "result_summary": f"{node_id} complete",
                        },
                    }
                )
                sequence += 1
            return {
                "schema_version": "open_stock_ai.agent_run.v2",
                "run_id": run_id,
                "status": "completed",
                "summary": "all recursive work completed",
                "activity": [],
            }

    async def scenario():
        path = tmp_path / "durable.sqlite"
        runtime = DurableAgentRuntime(
            service_provider=RecursiveService,
            store=AgentRunStore(path),
        )
        created = await runtime.create_run(objective="recursive execution")
        result = await runtime.wait(created["run_id"])
        assert result["status"] == "completed"

        snapshot = runtime.snapshot(created["run_id"])
        assert snapshot is not None
        event_types = [item["type"] for item in snapshot["events"]]
        assert event_types.count("branch.created") == 2
        assert event_types.count("branch.completed") == 2
        assert event_types.count("join.completed") == 1
        projected = [
            item
            for item in snapshot["forest"]["branches"]
            if item.get("source_plan_node_id")
        ]
        assert {item["status"] for item in projected} == {"completed"}
        assert all(item.get("branch_id") for item in snapshot["events"] if item["type"] == "step.completed")
        await runtime.close()

        restored = DurableAgentRuntime(
            service_provider=RecursiveService,
            store=AgentRunStore(path),
        ).snapshot(created["run_id"])
        assert restored is not None
        assert restored["forest"]["join_nodes"][0]["status"] == "completed"

    asyncio.run(scenario())


def test_provenance_failure_ledger_evidence_and_kpis_survive_restart(tmp_path):
    class ObservableService:
        snapshot_builder = None

        async def run(self, **kwargs):
            sink = kwargs["event_sink"]
            run_id = kwargs["run_id"]
            await sink(
                {
                    "event_id": "E-plan-observable",
                    "sequence": 1,
                    "type": "plan.proposed",
                    "run_id": run_id,
                    "payload": {"plan": _plan()},
                }
            )
            await sink(
                {
                    "event_id": "E-tool-started",
                    "sequence": 2,
                    "timestamp": "2026-08-08T00:00:02+00:00",
                    "type": "tool.started",
                    "run_id": run_id,
                    "payload": {
                        "node_id": "research-tool",
                        "call_id": "TC-observable",
                        "tool": "market_data",
                    },
                }
            )
            await sink(
                {
                    "event_id": "E-tool-completed",
                    "sequence": 3,
                    "timestamp": "2026-08-08T00:00:03+00:00",
                    "type": "tool.completed",
                    "run_id": run_id,
                    "payload": {
                        "node_id": "research-tool",
                        "call_id": "TC-observable",
                        "tool": "market_data",
                        "tool_provider": "host_market_data",
                        "result_summary": "fresh market evidence",
                        "evidence_ids": ["EV-observable"],
                        "evidence": {
                            "evidence_id": "EV-observable",
                            "claim": "fresh market evidence",
                            "source_type": "market_data",
                            "source": "official_exchange",
                            "observed_at": "2026-08-08T00:00:03+00:00",
                        },
                    },
                }
            )
            receipt = ErrorReceipt(
                category="timeout",
                component="tool_executor",
                location="$.plan.nodes.research-tool",
                expected="result",
                actual="timeout",
                retryable=True,
                branch_id="research-tool",
                error_id="ERR-observable",
            )
            fingerprint = FailureFingerprint.from_receipt(
                receipt,
                provider="test",
                model="test",
                tool="market_data",
                schema_version="v1",
            )
            await sink(
                {
                    "event_id": "E-validation",
                    "sequence": 4,
                    "type": "validation.passed",
                    "run_id": run_id,
                    "payload": {
                        "node_id": "research-tool",
                        "evidence_ids": ["validation-hash-must-not-be-evidence"],
                    },
                }
            )
            await sink(
                {
                    "event_id": "E-error-receipt",
                    "sequence": 5,
                    "type": "error.receipt.created",
                    "run_id": run_id,
                    "payload": {
                        "node_id": "research-tool",
                        "error_receipt": receipt.to_dict(),
                        "failure_fingerprint": fingerprint.to_dict(),
                    },
                }
            )
            await sink(
                {
                    "event_id": "E-repair",
                    "sequence": 6,
                    "type": "repair.attempted",
                    "run_id": run_id,
                    "payload": {
                        "node_id": "research-tool",
                        "error_id": receipt.error_id,
                        "fingerprint": fingerprint.digest,
                        "level": 3,
                        "strategy": "retry_temporary_failure",
                        "identical_retry_blocked": False,
                        "arguments": {"symbol": "2330.TW"},
                        "output": {"error": "timeout"},
                    },
                }
            )
            for sequence, node_id in enumerate(("research", "research-tool", "analysis"), start=7):
                await sink(
                    {
                        "event_id": f"E-done-{node_id}",
                        "sequence": sequence,
                        "type": "step.completed",
                        "run_id": run_id,
                        "payload": {"node_id": node_id, "result_summary": "done"},
                    }
                )
            await sink(
                {
                    "event_id": "E-memory-retrieved",
                    "sequence": 10,
                    "type": "memory.retrieved",
                    "run_id": run_id,
                    "payload": {"returned_count": 3, "relevant_count": 2},
                }
            )
            return {
                "schema_version": "open_stock_ai.agent_run.v2",
                "run_id": run_id,
                "status": "completed",
                "summary": "observable run complete",
                "activity": [],
            }

    async def scenario():
        path = tmp_path / "observable.sqlite"
        runtime = DurableAgentRuntime(
            service_provider=ObservableService,
            store=AgentRunStore(path),
        )
        created = await runtime.create_run(objective="observable recursive run")
        await runtime.wait(created["run_id"])
        snapshot = runtime.snapshot(created["run_id"])
        assert snapshot is not None
        completed = next(item for item in snapshot["events"] if item["type"] == "tool.completed")
        assert completed["provenance"]["success"] is True
        assert completed["provenance"]["latency_ms"] == 1000.0
        assert completed["provenance"]["provider"] == "host_market_data"
        assert completed["provenance"]["source"] == "host_market_data:market_data"
        assert {item["evidence_id"] for item in snapshot["evidence"]} == {"EV-observable"}
        assert snapshot["kpis"]["tool_call"] == 1.0
        assert snapshot["kpis"]["repair_attempt"] == 1.0
        assert snapshot["kpis"]["session_context_retrieval_precision"] == 2 / 3
        await runtime.close()

        reopened = DurableAgentRuntime(
            service_provider=ObservableService,
            store=AgentRunStore(path),
        ).snapshot(created["run_id"])
        assert reopened is not None
        assert reopened["evidence"] == snapshot["evidence"]
        assert reopened["kpis"] == snapshot["kpis"]
        with sqlite3.connect(path) as conn:
            assert conn.execute("select count(*) from agent_failure_ledger").fetchone()[0] == 1
            assert conn.execute("select count(*) from agent_repair_attempts").fetchone()[0] == 1
            assert conn.execute("select count(*) from agent_failure_fingerprints").fetchone()[0] == 1

    asyncio.run(scenario())


def test_success_resolves_only_the_failure_for_its_branch_and_kpis_handle_empty_evidence(tmp_path):
    path = tmp_path / "scoped-repair.sqlite"
    AgentSessionStore(path).create(
        session_id="AS-recursive",
        title="Scoped repair",
        namespace="test",
    )
    AgentRunStore(path).create_run(
        "AR-recursive",
        {
            "objective": "Repair failures independently",
            "driver_id": "test",
            "session_id": "AS-recursive",
        },
    )
    runtime = FinalAgentRuntime(path)
    runtime.create_forest(
        session_id="AS-recursive",
        run_id="AR-recursive",
        objective="Repair failures independently",
    )
    runtime.project_runtime_event(
        "AR-recursive",
        _event("E-plan", 1, "plan.proposed", plan=_plan()),
    )

    for sequence, (node_id, error_id) in enumerate(
        (("research-tool", "ERR-research"), ("analysis", "ERR-analysis")),
        start=2,
    ):
        receipt = ErrorReceipt(
            category="timeout",
            component="tool_executor",
            location=f"$.plan.nodes.{node_id}",
            expected="result",
            actual="timeout",
            retryable=True,
            branch_id=node_id,
            error_id=error_id,
        )
        fingerprint = FailureFingerprint.from_receipt(
            receipt,
            provider="test",
            model="test",
            tool=node_id,
            schema_version="v1",
        )
        runtime.project_runtime_event(
            "AR-recursive",
            _event(
                f"E-error-{node_id}",
                sequence,
                "error.receipt.created",
                node_id,
                error_receipt=receipt.to_dict(),
                failure_fingerprint=fingerprint.to_dict(),
            ),
        )

    runtime.project_runtime_event(
        "AR-recursive",
        _event("E-recovered", 4, "step.completed", "research-tool", result_summary="recovered"),
    )

    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "select error_id, status from agent_failure_ledger order by error_id"
        ).fetchall()
    assert rows == [("ERR-analysis", "open"), ("ERR-research", "resolved")]
    assert runtime.kpis(run_id="AR-recursive")["evidence_freshness"] == 0.0
