from __future__ import annotations

import copy

import pytest

from open_stock_ai.agent_runtime.plan_graph import PlanGraph


def _plan() -> PlanGraph:
    graph = PlanGraph.create("Build a validated answer", plan_id="AP-test")
    graph.apply_patch(
        {
            "operations": [
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "observe",
                        "node_type": "tool",
                        "title": "Observe",
                        "tool_name": "market.observe",
                        "order_index": 1,
                    },
                },
                {
                    "op": "add_node",
                    "node": {
                        "node_id": "validate",
                        "node_type": "validation",
                        "title": "Validate",
                        "dependencies": ["observe"],
                        "order_index": 2,
                    },
                },
            ]
        }
    )
    return graph


def test_plan_patch_supports_reorder_and_dependency_revision_without_changing_prior_snapshot():
    graph = _plan()
    prior = copy.deepcopy(graph.to_dict())
    prior_revision = graph.revision_number

    graph.apply_patch(
        {
            "operations": [
                {"op": "reorder_node", "node_id": "validate", "order_index": 0},
                {"op": "remove_dependency", "node_id": "validate", "dependency_id": "observe"},
                {"op": "add_dependency", "node_id": "observe", "dependency_id": "validate"},
            ]
        }
    )

    assert graph.revision_number == prior_revision + 1
    assert graph.to_dict()["nodes"][0]["node_id"] == "validate"
    assert graph.nodes["observe"].dependencies == ("validate",)
    assert prior["nodes"][0]["node_id"] == "observe"
    assert prior["revision_number"] == prior_revision


def test_plan_rejects_cycles_and_completed_step_regression():
    graph = _plan()
    with pytest.raises(ValueError, match="cycle"):
        graph.apply_patch(
            {
                "operations": [
                    {"op": "add_dependency", "node_id": "observe", "dependency_id": "validate"}
                ]
            }
        )

    completed = _plan()
    completed.mark("observe", "completed")
    with pytest.raises(ValueError, match="cannot transition"):
        completed.mark("observe", "running")
