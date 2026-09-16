from __future__ import annotations

import asyncio
import hashlib

import pytest

from open_stock_ai.agent_runtime import AgentRunContext
from stock_ai.agent_general_tools import GeneralAgentToolProvider


def test_project_edit_requires_explicit_mode_and_is_confined_to_project_root(tmp_path):
    provider = GeneralAgentToolProvider(tmp_path)
    advisory = AgentRunContext(run_id="AR-edit", autonomy="advisory", symbols=())
    elevated = AgentRunContext(
        run_id="AR-edit",
        autonomy="project_execute",
        symbols=(),
        allow_project_actions=True,
    )

    with pytest.raises(PermissionError, match="project_execute"):
        asyncio.run(
            provider.execute(
                "project.write_file",
                {"path": "proof.txt", "content": "one", "expected_before_sha256": None},
                advisory,
            )
        )
    created = asyncio.run(
        provider.execute(
            "project.write_file",
            {"path": "proof.txt", "content": "one", "expected_before_sha256": None},
            elevated,
        )
    )
    replaced = asyncio.run(
        provider.execute(
            "project.replace_text",
            {
                "path": "proof.txt",
                "old_text": "one",
                "new_text": "two",
                "expected_before_sha256": hashlib.sha256(b"one").hexdigest(),
            },
            elevated,
        )
    )
    readback = asyncio.run(provider.execute("project.read_file", {"path": "proof.txt"}, advisory))

    assert created["operation"] == "write"
    assert replaced["operation"] == "replace"
    assert readback["content"] == "1: two"
    with pytest.raises(PermissionError, match="inside the project root"):
        asyncio.run(
            provider.execute(
                "project.write_file",
                {
                    "path": str(tmp_path.parent / "escape.txt"),
                    "content": "blocked",
                    "expected_before_sha256": None,
                },
                elevated,
            )
        )
