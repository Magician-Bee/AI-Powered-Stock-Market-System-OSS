"""Protect the actual app-server response contract across Codex upgrades."""

from __future__ import annotations

import pytest
from openai_codex.generated.v2_all import ThreadStartResponse


@pytest.mark.parametrize("effort", ["high", "xhigh", "max", "ultra"])
def test_thread_start_preserves_new_reasoning_levels(effort):
    # A newer app-server can return an effort unknown when the Python SDK was
    # generated. The old 0.1.0b2 SDK rejected this before a model turn could run.
    response = ThreadStartResponse.model_validate(
        {
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
            "cwd": "/tmp/stock-ai-sdk-contract",
            "model": "current-account-model",
            "modelProvider": "openai",
            "reasoningEffort": effort,
            "sandbox": {"type": "readOnly"},
            "thread": {
                "id": "contract-thread",
                "sessionId": "contract-session",
                "cliVersion": "0.153.4",
                "createdAt": 1789059600,
                "updatedAt": 1789059600,
                "cwd": "/tmp/stock-ai-sdk-contract",
                "ephemeral": True,
                "modelProvider": "openai",
                "preview": "",
                "source": "exec",
                "status": {"type": "idle"},
                "turns": [],
            },
        }
    )

    assert response.reasoning_effort.value == effort
    assert response.model_dump(mode="json", by_alias=True)["reasoningEffort"] == effort
    assert response.thread.ephemeral is True
