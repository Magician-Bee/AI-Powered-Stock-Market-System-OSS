from __future__ import annotations

import asyncio
from time import monotonic

from open_stock_ai.agent_runtime.forest import (
    BranchBudget,
    BranchManager,
    BranchStatus,
    ChildBranchSpec,
    ForestExecutionStatus,
    ForestExecutor,
    ForestLimits,
    RuntimeForestAuthority,
    JoinStatus,
    StepExecutionResult,
    StepStatus,
    TaskForest,
)


def _forest(*, max_active: int = 4, max_depth: int = 5, max_nodes: int = 128):
    forest = TaskForest.create(
        session_id="AS-EXECUTOR",
        objective_version_id="OBJ-1",
        root_goal="Build a verified portfolio decision",
        limits=ForestLimits(
            max_active_branches=max_active,
            max_depth=max_depth,
            max_nodes=max_nodes,
        ),
    )
    return forest, BranchManager(forest)


def _branch(
    forest: TaskForest,
    manager: BranchManager,
    objective: str,
    *steps: str,
    dependencies: tuple[str, ...] = (),
    budget: BranchBudget | None = None,
    result_contract: dict | None = None,
):
    branch = manager.spawn_child(
        parent_branch_id=forest.root_branch.branch_id,
        objective=objective,
        objective_version_id=forest.root_objective_version_id,
        dependencies=dependencies,
        budget=budget,
        result_contract=result_contract,
        semantic_merge=False,
    ).branch
    prior = None
    for title in steps:
        step = manager.add_local_step(
            branch.branch_id,
            title,
            dependencies=(prior,) if prior else (),
        )
        prior = step.step_id
    return branch


def test_branches_run_truly_in_parallel_while_each_local_plan_remains_ordered() -> None:
    async def scenario():
        forest, manager = _forest(max_active=2)
        technical = _branch(forest, manager, "Technical", "fetch-kline", "analyse-kline")
        research = _branch(forest, manager, "Research", "search", "verify-source")
        events: list[tuple[str, str, str, float]] = []
        active = 0
        peak = 0
        both_started = asyncio.Event()

        async def run_step(context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            events.append((context.branch.branch_id, context.step.title, "start", monotonic()))
            if active >= 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.5)
            await asyncio.sleep(0.05)
            events.append((context.branch.branch_id, context.step.title, "end", monotonic()))
            active -= 1
            return StepExecutionResult(conclusion=f"done {context.step.title}")

        started = monotonic()
        report = await ForestExecutor(forest, run_step).execute()
        elapsed = monotonic() - started
        return forest, technical, research, events, peak, elapsed, report

    forest, technical, research, events, peak, elapsed, report = asyncio.run(scenario())

    assert peak == 2
    assert report.peak_active_branches == 2
    assert elapsed < 0.19  # Four 50ms steps would take >=200ms if Branches were serial.
    for branch in (technical, research):
        branch_events = [(title, phase) for branch_id, title, phase, _ in events if branch_id == branch.branch_id]
        assert branch_events == [
            (next(iter(branch.local_plan.steps.values())).title, "start"),
            (next(iter(branch.local_plan.steps.values())).title, "end"),
            (list(branch.local_plan.steps.values())[1].title, "start"),
            (list(branch.local_plan.steps.values())[1].title, "end"),
        ]
        assert branch.status == BranchStatus.COMPLETED
    assert report.status == ForestExecutionStatus.COMPLETED
    assert all(
        step.status == StepStatus.COMPLETED
        for branch in forest.branches.values()
        for step in branch.local_plan.steps.values()
    )


def test_runtime_forest_authority_keeps_one_execution_tree_across_capability_passes() -> None:
    async def scenario():
        authority = RuntimeForestAuthority(
            session_id="AS-AUTHORITY",
            run_id="AR-AUTHORITY",
            objective="Research a market decision",
        )
        started: list[str] = []

        async def run_parallel(capability):
            started.append(str(capability["call_id"]))
            await asyncio.sleep(0.01)
            return StepExecutionResult(
                payload={"call_id": capability["call_id"]},
                conclusion=f"done {capability['tool_name']}",
            )

        first = await authority.execute_capabilities(
            [
                {"call_id": "call-quote", "node_id": "quote", "tool_name": "market.quote"},
                {"call_id": "call-news", "node_id": "news", "tool_name": "web.research"},
            ],
            runner=run_parallel,
            parallel=True,
        )

        async def run_ordered(capability):
            started.append(str(capability["call_id"]))
            return StepExecutionResult(
                payload={"call_id": capability["call_id"]},
                conclusion="done risk check",
            )

        second = await authority.execute_capabilities(
            [{"call_id": "call-risk", "node_id": "risk", "tool_name": "risk.check"}],
            runner=run_ordered,
            parallel=False,
        )
        return authority, first, second, started

    authority, first, second, started = asyncio.run(scenario())

    assert first.report.forest_id == second.report.forest_id == authority.forest.forest_id
    assert set(started) == {"call-quote", "call-news", "call-risk"}
    assert authority.branch_for_call("call-quote", first).result is not None
    assert authority.branch_for_call("call-risk", second).result is not None
    snapshot = authority.snapshot()
    assert snapshot["execution_count"] == 2
    assert len(snapshot["branches"]) == 4  # root plus all three real capability branches
    assert {item["source_plan_node_id"] for item in snapshot["branches"]} >= {"quote", "news", "risk"}

    async def restore_scenario():
        restored = RuntimeForestAuthority.from_snapshot(
            snapshot,
            session_id="AS-AUTHORITY",
            run_id="AR-AUTHORITY",
            objective="Research a market decision",
        )

        async def run_restored(capability):
            return StepExecutionResult(conclusion=f"done {capability['tool_name']}")

        pass_after_restore = await restored.execute_capabilities(
            [{"call_id": "call-final", "node_id": "final", "tool_name": "analysis.summarize"}],
            runner=run_restored,
            parallel=False,
        )
        return restored, pass_after_restore

    restored, pass_after_restore = asyncio.run(restore_scenario())
    assert restored.forest.forest_id == authority.forest.forest_id
    assert pass_after_restore.report.forest_id == authority.forest.forest_id
    assert restored.snapshot()["execution_count"] == 3
    assert len(restored.snapshot()["branches"]) == 5


def test_runtime_forest_authority_adopts_the_durable_forest_and_root_branch_identity() -> None:
    authority = RuntimeForestAuthority(
        session_id="AS-DURABLE-AUTHORITY",
        run_id="AR-DURABLE-AUTHORITY",
        objective="Use the one durable Task Forest",
        forest_id="TF-durable-authority",
        root_branch_id="BR-durable-root",
        objective_version_id="OBJ-durable-authority",
    )

    snapshot = authority.snapshot()

    assert snapshot["forest_id"] == "TF-durable-authority"
    assert snapshot["root_branch_id"] == "BR-durable-root"
    assert snapshot["objective_version_id"] == "OBJ-durable-authority"
    assert authority.forest.root_branch.branch_id == "BR-durable-root"
    assert authority.forest.root_branch.objective_version_id == "OBJ-durable-authority"

    stale = {**snapshot, "forest_id": "TF-stale-memory"}
    restored = RuntimeForestAuthority.from_snapshot(
        stale,
        session_id="AS-DURABLE-AUTHORITY",
        run_id="AR-DURABLE-AUTHORITY",
        objective="Use the one durable Task Forest",
        forest_id="TF-durable-authority",
        root_branch_id="BR-durable-root",
        objective_version_id="OBJ-durable-authority",
    )

    assert restored.snapshot()["forest_id"] == "TF-durable-authority"
    assert restored.snapshot()["root_branch_id"] == "BR-durable-root"


def test_branch_dependencies_wait_for_upstream_result() -> None:
    async def scenario():
        forest, manager = _forest(max_active=3)
        upstream = _branch(forest, manager, "Collect evidence", "collect")
        downstream = _branch(
            forest,
            manager,
            "Risk review",
            "review",
            dependencies=(upstream.branch_id,),
        )
        times: dict[str, float] = {}

        async def run_step(context):
            times[f"{context.branch.branch_id}:start"] = monotonic()
            if context.branch.branch_id == upstream.branch_id:
                await asyncio.sleep(0.04)
            times[f"{context.branch.branch_id}:end"] = monotonic()
            return StepExecutionResult(conclusion=context.step.title)

        report = await ForestExecutor(forest, run_step).execute()
        return upstream, downstream, times, report

    upstream, downstream, times, report = asyncio.run(scenario())
    assert times[f"{downstream.branch_id}:start"] >= times[f"{upstream.branch_id}:end"]
    assert report.status == ForestExecutionStatus.COMPLETED


def test_recursive_spawn_executes_child_local_plan_and_enforces_branch_budget() -> None:
    async def scenario():
        forest, manager = _forest(max_active=3, max_depth=2, max_nodes=12)
        research = _branch(forest, manager, "External research", "split-sources")
        stable = _branch(forest, manager, "Portfolio", "calculate")
        executed: list[str] = []

        async def run_step(context):
            executed.append(context.step.title)
            if context.step.title == "split-sources":
                return StepExecutionResult(
                    conclusion="sources planned",
                    child_branches=(
                        ChildBranchSpec(
                            objective="US source",
                            step_titles=("open-official", "verify-official"),
                            budget=BranchBudget(max_tokens=5),
                        ),
                    ),
                )
            if context.step.title == "open-official":
                return StepExecutionResult(tokens_used=6)
            return StepExecutionResult(conclusion=context.step.title)

        report = await ForestExecutor(forest, run_step).execute()
        child = next(
            branch
            for branch in forest.branches.values()
            if branch.parent_branch_id == research.branch_id
        )
        return forest, research, stable, child, executed, report

    forest, research, stable, child, executed, report = asyncio.run(scenario())
    assert child.depth == 2
    assert executed[:2] == ["split-sources", "calculate"] or set(executed[:2]) == {
        "split-sources",
        "calculate",
    }
    assert "open-official" in executed
    assert "verify-official" not in executed
    assert child.status == BranchStatus.FAILED
    assert stable.status == BranchStatus.COMPLETED
    assert research.status == BranchStatus.COMPLETED
    assert report.status == ForestExecutionStatus.PARTIALLY_COMPLETED
    assert report.failed_branch_ids == (child.branch_id,)
    assert report.next_recovery
    assert forest.node_count <= forest.limits.max_nodes


def test_recursive_spawn_rejects_node_overflow_before_mutating_the_forest() -> None:
    async def scenario():
        forest, manager = _forest(max_active=2, max_nodes=4)
        parent = _branch(forest, manager, "Research", "expand")

        async def run_step(_context):
            return StepExecutionResult(
                child_branches=(
                    ChildBranchSpec(
                        objective="Oversized child",
                        step_titles=("one", "two"),
                    ),
                )
            )

        report = await ForestExecutor(forest, run_step).execute()
        return forest, parent, report

    forest, parent, report = asyncio.run(scenario())
    assert len(forest.branches) == 2
    assert forest.node_count == 3
    assert parent.status == BranchStatus.FAILED
    assert "node budget exceeded" in parent.local_state["failure"]["error"]
    assert report.status == ForestExecutionStatus.FAILED


def test_local_failure_does_not_cancel_siblings_and_join_returns_partial_result() -> None:
    async def scenario():
        forest, manager = _forest(max_active=3)
        successful = _branch(forest, manager, "Taiwan sources", "tw-official")
        failing = _branch(forest, manager, "US sources", "us-source-1", "us-source-2")
        join = forest.add_join(
            required_branch_ids=(successful.branch_id, failing.branch_id),
            objective="Merge cross-market evidence",
        )

        async def run_step(context):
            if context.step.title == "us-source-1":
                raise ConnectionError("source unavailable")
            return StepExecutionResult(
                conclusion="Taiwan evidence complete",
                evidence_ids=("EV-TW",),
                confidence=0.8,
            )

        report = await ForestExecutor(forest, run_step).execute()
        return forest, successful, failing, join, report

    forest, successful, failing, join, report = asyncio.run(scenario())
    assert successful.status == BranchStatus.COMPLETED
    assert failing.status == BranchStatus.FAILED
    assert list(failing.local_plan.steps.values())[1].status == StepStatus.BLOCKED
    assert join.status == JoinStatus.COMPLETED
    assert join.result is not None and join.result.partial
    assert join.result.metadata["failed_branch_ids"] == [failing.branch_id]
    assert join.result.evidence_ids == ("EV-TW",)
    assert report.status == ForestExecutionStatus.PARTIALLY_COMPLETED
    assert report.completed_branch_ids == (successful.branch_id,)
    assert report.failed_branch_ids == (failing.branch_id,)
    assert report.confidence_impact > 0
    assert report.completion_gate.required_incomplete_branch_ids == (failing.branch_id,)
    assert forest.root_branch.status == BranchStatus.COMPLETED


def test_conflicting_join_spawns_verification_branch_then_completes_merge() -> None:
    async def scenario():
        forest, manager = _forest(max_active=3)
        technical = _branch(forest, manager, "Technical", "technical-view")
        institutional = _branch(forest, manager, "Institutional", "flow-view")
        join = forest.add_join(
            required_branch_ids=(technical.branch_id, institutional.branch_id),
            objective="Resolve investment direction",
        )

        async def run_step(context):
            if context.step.metadata.get("kind") == "conflict_verification":
                return StepExecutionResult(
                    conclusion="Different time horizons explain the conflict",
                    decision="neutral",
                    evidence_ids=("EV-VERIFY",),
                    criteria_met=context.branch.completion_criteria,
                )
            if context.branch.branch_id == technical.branch_id:
                return StepExecutionResult(
                    conclusion="Short-term bullish",
                    decision="bullish",
                    evidence_ids=("EV-T",),
                )
            return StepExecutionResult(
                conclusion="Medium-term bearish",
                decision="bearish",
                evidence_ids=("EV-I",),
            )

        report = await ForestExecutor(forest, run_step).execute()
        return forest, join, report

    forest, join, report = asyncio.run(scenario())
    assert join.conflict_branch_id is not None
    verifier = forest.branches[join.conflict_branch_id]
    assert verifier.status == BranchStatus.COMPLETED
    assert verifier.local_state["conflicting_branch_ids"] == list(join.required_branch_ids)
    assert join.status == JoinStatus.COMPLETED
    assert join.result is not None
    assert join.result.decision == "neutral"
    assert join.result.metadata["resolved_by"] == verifier.branch_id
    assert set(join.result.evidence_ids) == {"EV-T", "EV-I", "EV-VERIFY"}
    assert report.status == ForestExecutionStatus.COMPLETED


def test_conflict_join_reports_partial_when_verification_branch_exceeds_node_budget() -> None:
    async def scenario():
        forest, manager = _forest(max_active=3, max_nodes=6)
        positive = _branch(forest, manager, "Positive", "positive")
        negative = _branch(forest, manager, "Negative", "negative")
        join = forest.add_join(
            required_branch_ids=(positive.branch_id, negative.branch_id),
            objective="Budget-bound conflict",
        )

        async def run_step(context):
            positive_view = context.branch.branch_id == positive.branch_id
            return StepExecutionResult(
                conclusion="positive" if positive_view else "negative",
                decision="bullish" if positive_view else "bearish",
            )

        report = await ForestExecutor(forest, run_step).execute()
        return forest, join, report

    forest, join, report = asyncio.run(scenario())
    assert forest.node_count == forest.limits.max_nodes
    assert join.conflict_branch_id is None
    assert join.status == JoinStatus.COMPLETED
    assert join.result is not None and join.result.partial
    assert join.result.metadata["resolution_blocked"]
    assert report.status == ForestExecutionStatus.PARTIALLY_COMPLETED
    assert join.join_id in report.completion_gate.invalid_result_ids


def test_completion_gate_rejects_model_completion_without_required_evidence() -> None:
    async def scenario():
        forest, manager = _forest()
        branch = _branch(
            forest,
            manager,
            "Evidence-required analysis",
            "claim-complete",
            result_contract={"required": ["conclusion", "evidence_ids"]},
        )

        async def run_step(_context):
            return StepExecutionResult(conclusion="The model says it is complete")

        report = await ForestExecutor(forest, run_step).execute()
        return branch, report

    branch, report = asyncio.run(scenario())
    assert branch.status == BranchStatus.PARTIALLY_COMPLETED
    assert report.status == ForestExecutionStatus.PARTIALLY_COMPLETED
    assert report.completion_gate.completed is False
    assert report.completion_gate.insufficient_evidence_branch_ids == (branch.branch_id,)
    assert "Result contract requires evidence_ids" in report.missing_evidence
