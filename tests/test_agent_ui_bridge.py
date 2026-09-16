from __future__ import annotations

import asyncio
import sqlite3

import pytest

from open_stock_ai.agent_runtime import AgentRunContext
from stock_ai.agent_ui_bridge import AgentUIBridge
from stock_ai.agent_event_bus import AgentEventBus
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.tool_providers.ui import UIToolProvider


def test_ui_bridge_dispatches_to_connected_view_and_waits_for_acknowledgement():
    async def scenario():
        bridge = AgentUIBridge()
        bridge.update_state({"current_view": "home", "current_symbol": "2330.TW"})
        pending = asyncio.create_task(bridge.dispatch("navigate", {"view": "settings"}))
        await asyncio.sleep(0)
        commands = bridge.commands_after(0)
        assert commands[0]["action"] == "navigate"
        bridge.update_state({"current_view": "settings", "current_symbol": "2330.TW"})
        bridge.complete(commands[0]["command_id"], {"ok": True, "result": {"current_view": "settings"}})
        result = await pending
        assert result["acknowledgement"]["result"]["current_view"] == "settings"
        assert result["ui_state"]["state"]["current_view"] == "settings"
        assert bridge.commands_after(0) == []

    asyncio.run(scenario())


def test_ui_bridge_state_commands_and_results_survive_process_reconstruction(tmp_path):
    async def scenario():
        path = tmp_path / "runtime.sqlite"
        first = AgentUIBridge(path)
        first.update_state({"current_view": "home"})
        pending = asyncio.create_task(
            first.dispatch("navigate", {"view": "settings"}, timeout_seconds=2)
        )
        await asyncio.sleep(0.05)

        reconstructed = AgentUIBridge(path)
        commands = reconstructed.commands_after(0)
        assert commands and commands[0]["action"] == "navigate"
        reconstructed.update_state({"current_view": "settings"})
        reconstructed.complete(
            commands[0]["command_id"],
            {"ok": True, "result": {"current_view": "settings"}},
        )

        result = await pending
        assert result["acknowledgement"]["result"]["current_view"] == "settings"
        assert reconstructed.snapshot()["last_command_id"] == commands[0]["command_id"]
        assert reconstructed.commands_after(0) == []

    asyncio.run(scenario())


def test_ui_bridge_publishes_state_to_durable_scheduler_inbox(tmp_path):
    path = tmp_path / "runtime.sqlite"
    store = AgentRunStore(path)
    bus = AgentEventBus()
    bus.configure(store)
    bridge = AgentUIBridge(path, event_publisher=bus.publish)

    bridge.update_state({"current_view": "settings"})
    events = store.claim_runtime_events(
        owner="runtime",
        at="2026-07-18T10:00:00+00:00",
        lease_expires_at="2026-07-18T10:00:30+00:00",
    )

    assert len(events) == 1
    assert events[0]["event_type"] == "ui.state.updated"
    assert events[0]["payload"]["state"]["current_view"] == "settings"


def test_ui_bridge_coalesces_identical_heartbeat_events_but_refreshes_presence(tmp_path):
    path = tmp_path / "runtime.sqlite"
    store = AgentRunStore(path)
    bus = AgentEventBus()
    bus.configure(store)
    bridge = AgentUIBridge(path, event_publisher=bus.publish)

    first = bridge.update_state({"current_view": "settings", "current_symbol": "2887.TW"})
    second = bridge.update_state({"current_view": "settings", "current_symbol": "2887.TW"})
    events = store.claim_runtime_events(
        owner="runtime",
        at="2026-08-31T12:00:00+00:00",
        lease_expires_at="2026-08-31T12:00:30+00:00",
    )

    assert first["last_seen"] != second["last_seen"]
    assert len(events) == 1
    assert events[0]["event_type"] == "ui.state.updated"


def test_ui_bridge_acknowledges_state_when_another_runtime_write_has_database_locked(tmp_path, monkeypatch):
    bridge = AgentUIBridge(tmp_path / "runtime.sqlite")
    original = bridge._connect

    class LockedConnection:
        def __enter__(self):
            raise sqlite3.OperationalError("database is locked")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(bridge, "_connect", lambda: LockedConnection())

    snapshot = bridge.update_state({"current_view": "market"})

    assert snapshot["state"]["current_view"] == "market"
    assert snapshot["persistence"] == "deferred_database_lock"
    monkeypatch.setattr(bridge, "_connect", original)


def test_event_bus_drops_only_locked_telemetry_write(tmp_path, monkeypatch):
    store = AgentRunStore(tmp_path / "runtime.sqlite")
    bus = AgentEventBus()
    bus.configure(store)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "publish_runtime_event", locked)

    assert bus.publish("news.updated", {"symbol": "6603.TWO"}) is None


def test_processed_runtime_event_compaction_preserves_recent_and_dedup_tombstones(tmp_path):
    path = tmp_path / "runtime.sqlite"
    store = AgentRunStore(path)
    events = [
        store.publish_runtime_event(
            "ui.state.updated",
            {"index": index, "large": "x" * 10_000},
            dedup_key=f"dedup-{index}" if index % 2 else None,
        )
        for index in range(8)
    ]
    for event in events:
        claimed = store.claim_runtime_events(
            owner="runtime",
            at="2026-08-10T00:00:00+00:00",
            lease_expires_at="2026-08-10T00:01:00+00:00",
            limit=1,
        )
        assert claimed
        assert store.complete_runtime_event(claimed[0]["event_id"], owner="runtime")

    result = store.compact_processed_runtime_events(keep_recent=2)

    assert result == {"compacted": 3, "deleted": 3, "kept_recent": 2}
    with sqlite3.connect(path) as conn:
        remaining = conn.execute(
            "select event_id, dedup_key, payload_json from agent_runtime_events order by created_at"
        ).fetchall()
    assert len(remaining) == 5
    assert sum(payload == "{}" for _, _, payload in remaining) == 3
    replay = store.publish_runtime_event(
        "ui.state.updated",
        {"index": 1, "large": "new"},
        dedup_key="dedup-1",
    )
    assert replay["event_id"] == events[1]["event_id"]


def test_ui_tool_fill_order_never_submits_financial_action():
    bridge = AgentUIBridge()
    provider = UIToolProvider(bridge)
    names = {item["name"] for item in provider.manifest()}

    assert {
        "ui.get_state",
        "ui.navigate",
        "ui.select_symbol",
        "ui.open_panel",
        "ui.fill_order",
        "ui.submit_action",
        "ui.wait_for_state",
    }.issubset(names)
    submit_schema = next(item for item in provider.manifest() if item["name"] == "ui.submit_action")
    assert "submit_order" not in submit_schema["input_schema"]["properties"]["action"]["enum"]


def test_ui_tool_requires_a_live_ui_for_commands():
    provider = UIToolProvider(AgentUIBridge())
    context = AgentRunContext(run_id="AR-ui", autonomy="advisory", symbols=())

    with pytest.raises(RuntimeError, match="No Stock AI browser"):
        asyncio.run(provider.execute("ui.navigate", {"view": "settings"}, context))
