from __future__ import annotations

import pytest

from open_stock_ai.agent_runtime.approval_manager import ApprovalManager, ApprovalRequiredError
from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from stock_ai.agent_run_store import AgentRunStore


def _run(path):
    sessions = AgentSessionStore(path)
    sessions.create(session_id="AS-approval", namespace="stock-ai", title="Approval")
    store = AgentRunStore(path)
    store.create_run(
        "AR-approval",
        {
            "objective": "Preview a paper order",
            "symbols": ["2330.TW"],
            "driver_id": "test",
            "autonomy": "paper_execute",
            "max_steps": 3,
            "session_id": "AS-approval",
        },
    )


def test_approval_requires_one_time_ui_challenge_and_is_visible_in_run_snapshot(tmp_path):
    path = tmp_path / "approval.sqlite"
    _run(path)
    manager = ApprovalManager(path)

    with pytest.raises(ApprovalRequiredError) as required:
        manager.require(
            run_id="AR-approval",
            step_id="step-order",
            tool_name="paper.order.preview",
            arguments={"symbol": "2330.TW", "quantity": 1},
            resource_scope={"account": "paper"},
            risk_class="financial_paper",
        )
    approval_id = required.value.approval["approval_id"]

    with pytest.raises(PermissionError, match="valid one-time"):
        manager.resolve(
            approval_id,
            approved=True,
            decided_by="stock-ai-ui",
            challenge="invalid",
        )
    challenge = manager.issue_challenge(approval_id)
    resolved = manager.resolve(
        approval_id,
        approved=True,
        decided_by="stock-ai-ui",
        challenge=challenge["challenge"],
    )

    assert resolved["status"] == "approved"
    assert manager.list("AR-approval")[0]["approval_id"] == approval_id
    assert "ui_challenge_digest" not in resolved["payload"]


def test_model_cannot_self_approve(tmp_path):
    path = tmp_path / "self-approval.sqlite"
    _run(path)
    manager = ApprovalManager(path)
    with pytest.raises(ApprovalRequiredError) as required:
        manager.require(
            run_id="AR-approval",
            step_id="step-order",
            tool_name="paper.order.preview",
            arguments={"symbol": "2330.TW"},
            resource_scope={"account": "paper"},
            risk_class="financial_paper",
        )
    approval_id = required.value.approval["approval_id"]
    challenge = manager.issue_challenge(approval_id)
    with pytest.raises(PermissionError, match="cannot approve"):
        manager.resolve(
            approval_id,
            approved=True,
            decided_by="model",
            challenge=challenge["challenge"],
        )
