from __future__ import annotations

import asyncio

from open_stock_ai.agent_runtime import AgentOrchestrator, AgentToolSpec
from open_stock_ai.agent_runtime.contracts import build_runtime_event


class _Driver:
    driver_id = "contract-test"

    def __init__(self) -> None:
        self.turn = 0

    def describe(self):
        return {"id": self.driver_id, "configured": True}

    async def decide(self, turn):
        self.turn += 1
        if self.turn == 1:
            return {
                "state": "continue",
                "summary": "Read the durable source.",
                "tool_calls": [
                    {"id": "call-1", "name": "test.observe", "arguments": {"symbol": "2330.TW"}}
                ],
                "decision": None,
            }
        return {
            "state": "complete",
            "summary": "The observation was validated.",
            "tool_calls": [],
            "decision": {
                "action": "watch",
                "symbol": "2330.TW",
                "confidence": 75,
                "rationale": "validated observation",
                "next_check": "tomorrow",
            },
        }


class _Tools:
    def manifest(self):
        return [
            AgentToolSpec(
                name="test.observe",
                description="Observe a test value.",
                category="test",
                input_schema={
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {"symbol": {"type": "string"}},
                    "additionalProperties": False,
                },
                skills=("evidence-reading",),
                packages=("TestEvidence",),
            ).to_dict()
        ]

    async def execute(self, name, arguments, context):
        return {"status": "ok", "symbol": arguments["symbol"], "evidence_ids": ["EV-1"]}


def test_event_envelope_is_canonical_redacted_and_payload_cannot_override_identity():
    event = build_runtime_event(
        "tool.started",
        sequence=7,
        run_id="AR-real",
        session_id="AS-real",
        payload={
            "run_id": "AR-forged",
            "session_id": "AS-forged",
            "call_id": "call-7",
            "api_token": "secret-value",
            "arguments": {"password": "never-store", "symbol": "2330.TW"},
        },
        previous_event_id="ARE-prior",
    )

    assert event["run_id"] == "AR-real"
    assert event["session_id"] == "AS-real"
    assert event["tool_call_id"] == "call-7"
    assert event["event_id"].startswith("ARE-")
    assert event["causation_id"] == "ARE-prior"
    assert len(event["trace_id"]) == 64
    assert len(event["span_id"]) == 16
    assert event["parent_span_id"]
    assert event["redacted"] is True
    assert event["payload"]["api_token"] == "[redacted]"
    assert event["payload"]["arguments"]["password"] == "[redacted]"
    assert "secret-value" not in str(event)
    assert "never-store" not in str(event)


def test_orchestrator_emits_correlated_step_tool_skill_and_validation_lifecycle():
    events: list[dict] = []
    runtime = AgentOrchestrator(
        drivers={"contract-test": _Driver()},
        tools=_Tools(),
        default_driver="contract-test",
    )

    result = asyncio.run(
        runtime.run(
            objective="Observe 2330 and report",
            symbols=["2330.TW"],
            run_id="AR-contract",
            session_id="AS-contract",
            event_sink=events.append,
        )
    )

    event_types = {event["type"] for event in events}
    assert result["status"] == "completed"
    assert {
        "run.started",
        "plan.proposed",
        "step.started",
        "tool.queued",
        "tool.started",
        "skill.selected",
        "skill.started",
        "tool.completed",
        "validation.started",
        "validation.passed",
        "step.completed",
        "assistant.message.completed",
        "run.completed",
    } <= event_types
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["run_id"] == "AR-contract" for event in events)
    assert all(event["session_id"] == "AS-contract" for event in events)
    tool_events = [event for event in events if event["type"].startswith("tool.")]
    assert {event["tool_call_id"] for event in tool_events} == {"call-1"}
    assert all(event["step_id"] for event in tool_events)
