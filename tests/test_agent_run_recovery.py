from __future__ import annotations

import asyncio

from open_stock_ai.agent_runtime.session_store import AgentSessionStore
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.durable_agent_runtime import DurableAgentRuntime


class _BlockingService:
    default_driver = "test"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, **kwargs):
        run_id = kwargs["run_id"]
        sequence = int(kwargs.get("initial_sequence") or 0) + 1
        await kwargs["event_sink"](
            {
                "event_id": f"ARE-{sequence}",
                "sequence": sequence,
                "run_id": run_id,
                "session_id": kwargs["session_id"],
                "type": "context.snapshot.created",
                "timestamp": "2026-07-28T00:00:00+00:00",
                "snapshot_id": "AES-recovery",
                "snapshot_hash": "hash",
                "snapshot": {"ui": {"view": "agent"}, "market": {"symbols": ["2330.TW"]}},
                "payload": {
                    "snapshot_id": "AES-recovery",
                    "snapshot_hash": "hash",
                    "snapshot": {"ui": {"view": "agent"}, "market": {"symbols": ["2330.TW"]}},
                },
            }
        )
        self.started.set()
        await self.release.wait()
        return {
            "schema_version": "open_stock_ai.agent_run.v2",
            "run_id": run_id,
            "status": "completed",
            "summary": "Recovered and completed.",
        }


def test_pause_snapshot_resume_keeps_durable_sequence_and_environment(tmp_path):
    async def scenario():
        path = tmp_path / "recovery.sqlite"
        service = _BlockingService()
        runtime = DurableAgentRuntime(
            service_provider=lambda: service,
            store=AgentRunStore(path),
            session_store=AgentSessionStore(path),
        )
        created = await runtime.create_run(objective="Recover me", symbols=["2330.TW"])
        run_id = created["run_id"]
        await service.started.wait()

        paused = await runtime.pause(run_id)
        snapshot = runtime.snapshot(run_id)

        assert paused["status"] == "suspended"
        assert snapshot["schema_version"] == "open_stock_ai.agent_run_snapshot.v2"
        assert snapshot["last_sequence"] >= 2
        assert snapshot["environment_snapshot"]["market"]["symbols"] == ["2330.TW"]
        assert [event["sequence"] for event in snapshot["events"]] == sorted(
            event["sequence"] for event in snapshot["events"]
        )

        service.release.set()
        await runtime.resume(run_id)
        result = await runtime.wait(run_id)
        resumed = runtime.snapshot(run_id)
        await runtime.close()
        return result, resumed

    result, resumed = asyncio.run(scenario())
    assert result["status"] == "completed"
    sequences = [event["sequence"] for event in resumed["events"]]
    assert len(sequences) == len(set(sequences))
    assert sequences == sorted(sequences)


def test_persisted_event_payload_redacts_secrets_before_replay(tmp_path):
    path = tmp_path / "redaction.sqlite"
    store = AgentRunStore(path)
    store.create_run(
        "AR-secret",
        {
            "objective": "redact",
            "symbols": [],
            "driver_id": "test",
            "autonomy": "advisory",
            "max_steps": 1,
        },
    )
    store.append_event(
        "AR-secret",
        {
            "sequence": 1,
            "type": "tool.started",
            "run_id": "AR-secret",
            "api_key": "do-not-store",
            "arguments": {"authorization": "Bearer private", "symbol": "2330.TW"},
        },
    )

    replay = store.events_after("AR-secret")
    assert replay[0]["api_key"] == "[redacted]"
    assert replay[0]["arguments"]["authorization"] == "[redacted]"
    assert "do-not-store" not in str(replay)
    assert "Bearer private" not in str(replay)
