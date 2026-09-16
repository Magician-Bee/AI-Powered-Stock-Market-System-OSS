from __future__ import annotations

import os

import pytest

from stock_ai.brokers.transport_guard import default_broker_transport_guard


@pytest.fixture(scope="session", autouse=True)
def isolate_agent_runtime_storage(tmp_path_factory):
    """Never let tests share the desktop application's live SQLite database."""
    previous = os.environ.get("STOCK_AI_AGENT_DATA_ROOT")
    os.environ["STOCK_AI_AGENT_DATA_ROOT"] = str(
        tmp_path_factory.mktemp("stock-ai-agent-runtime")
    )
    yield
    if previous is None:
        os.environ.pop("STOCK_AI_AGENT_DATA_ROOT", None)
    else:
        os.environ["STOCK_AI_AGENT_DATA_ROOT"] = previous


@pytest.fixture(autouse=True)
def isolate_process_environment(tmp_path):
    """Keep tests independent from the developer's .env and from one another."""
    before = os.environ.copy()
    os.environ["OPEN_STOCK_AI_ENV_FILE"] = str(tmp_path / "missing-test.env")
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture(autouse=True)
def isolate_default_broker_transport_guard():
    """Keep process-shared broker admission state isolated per test."""
    default_broker_transport_guard.cache_clear()
    yield
    default_broker_transport_guard.cache_clear()
