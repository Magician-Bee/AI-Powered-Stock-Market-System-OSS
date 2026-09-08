import pytest

from open_stock_ai.agent_runtime.guardrails import (
    CostBudgetExceeded,
    CostBudgetManager,
    RunawayExecutionExceeded,
    RunawayExecutionGuard,
    RunawayExecutionLimits,
)


def test_cost_budget_charges_global_session_provider_and_tool_atomically() -> None:
    manager = CostBudgetManager()
    manager.register("global", "global", limit=10)
    manager.register("session", "session-1", limit=8)
    manager.register("provider", "ollama", limit=7)
    manager.register("tool", "market.quote", limit=2)

    manager.charge(
        2,
        session_id="session-1",
        provider_id="ollama",
        tool_name="market.quote",
    )
    snapshot = manager.snapshot()
    assert snapshot["global:global"]["consumed"] == 2
    assert snapshot["session:session-1"]["consumed"] == 2
    assert snapshot["provider:ollama"]["consumed"] == 2
    assert snapshot["tool:market.quote"]["consumed"] == 2

    with pytest.raises(CostBudgetExceeded, match="tool market.quote"):
        manager.charge(
            1,
            session_id="session-1",
            provider_id="ollama",
            tool_name="market.quote",
        )
    assert manager.snapshot()["global:global"]["consumed"] == 2


def test_cost_budget_cannot_be_bypassed_by_a_new_provider_or_session() -> None:
    manager = CostBudgetManager()
    manager.register("global", "global", limit=3)
    manager.register("session", "session-1", limit=10)
    manager.register("provider", "provider-a", limit=10)
    manager.charge(3, session_id="session-1", provider_id="provider-a")

    with pytest.raises(CostBudgetExceeded, match="global global"):
        manager.charge(1, session_id="session-2", provider_id="provider-b")


def test_runaway_guard_blocks_recursive_depth_and_repeated_tool_calls() -> None:
    guard = RunawayExecutionGuard(
        RunawayExecutionLimits(
            max_depth=2,
            max_nodes=3,
            max_tool_calls=4,
            max_repeated_signature=2,
            max_no_progress_turns=2,
        )
    )
    guard.observe_branch(depth=2, nodes=3)
    guard.observe_tool(tool_name="market.quote", arguments={"symbol": "2330.TW"})
    guard.observe_tool(tool_name="market.quote", arguments={"symbol": "2330.TW"})
    with pytest.raises(RunawayExecutionExceeded, match="repeated tool signature"):
        guard.observe_tool(tool_name="market.quote", arguments={"symbol": "2330.TW"})
    with pytest.raises(RunawayExecutionExceeded, match="recursive branch depth"):
        guard.observe_branch(depth=3)


def test_runaway_guard_blocks_no_progress_chaos() -> None:
    guard = RunawayExecutionGuard(RunawayExecutionLimits(max_no_progress_turns=2))
    guard.observe_turn(progress=False)
    guard.observe_turn(progress=False)
    with pytest.raises(RunawayExecutionExceeded, match="no observable progress"):
        guard.observe_turn(progress=False)
    guard.observe_turn(progress=True)
    assert guard.snapshot()["no_progress_turns"] == 0
