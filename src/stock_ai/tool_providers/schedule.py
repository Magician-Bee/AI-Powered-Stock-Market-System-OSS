from __future__ import annotations

from typing import Any

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec


class ScheduleToolProvider:
    provider_id = "scheduler"

    def __init__(self) -> None:
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="schedule.list",
                    description="List durable advisory Agent schedules and their latest run IDs.",
                    category="schedule",
                    schedules=("durable-agent-scheduler",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                    },
                ),
                AgentToolSpec(
                    name="schedule.create",
                    description="Create a durable advisory-only time, interval, cron, event or condition schedule.",
                    category="schedule",
                    mutating=True,
                    requires_project_execution=True,
                    schedules=("durable-agent-scheduler",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "objective", "trigger_type"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 200},
                            "objective": {"type": "string", "minLength": 1, "maxLength": 8000},
                            "trigger_type": {
                                "type": "string",
                                "enum": ["one_shot", "interval", "cron", "event", "condition"],
                            },
                            "next_run_at": {"type": "string"},
                            "interval_seconds": {"type": "integer", "minimum": 60, "maximum": 31536000},
                            "cron_expression": {"type": "string"},
                            "event_type": {"type": "string"},
                            "condition": {"type": "object"},
                            "misfire_policy": {
                                "type": "string",
                                "enum": ["run_once", "catch_up", "skip"],
                            },
                            "market_calendar": {"type": "string", "enum": ["taiwan"]},
                            "expires_at": {"type": "string"},
                            "dedup_key": {"type": "string"},
                            "symbols": {"type": "array", "maxItems": 20, "items": {"type": "string"}},
                            "max_steps": {"type": "integer", "minimum": 1, "maximum": 12},
                        },
                    },
                ),
                AgentToolSpec(
                    name="schedule.update",
                    description="Update a durable advisory schedule.",
                    category="schedule",
                    mutating=True,
                    requires_project_execution=True,
                    schedules=("durable-agent-scheduler",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["schedule_id", "changes"],
                        "properties": {
                            "schedule_id": {"type": "string"},
                            "changes": {"type": "object"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="schedule.resume",
                    description="Resume a paused durable advisory schedule.",
                    category="schedule",
                    mutating=True,
                    requires_project_execution=True,
                    schedules=("durable-agent-scheduler",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["schedule_id"],
                        "properties": {"schedule_id": {"type": "string"}},
                    },
                ),
                AgentToolSpec(
                    name="schedule.cancel",
                    description="Disable a durable Agent schedule; requires project_execute approval.",
                    category="schedule",
                    mutating=True,
                    requires_project_execution=True,
                    schedules=("durable-agent-scheduler",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["schedule_id"],
                        "properties": {"schedule_id": {"type": "string"}},
                    },
                ),
            )
        }

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def describe(self) -> dict[str, Any]:
        return {"configured": True, "runtime_ready": True, "health": "ready", "advisory_only": True}

    async def execute(self, name: str, arguments: dict[str, Any], context: AgentRunContext) -> dict[str, Any]:
        from stock_ai.agent_service import get_agent_run_runtime

        if name not in self._specs:
            raise ValueError(f"Unknown schedule tool: {name}")
        if self._specs[name].requires_project_execution and not context.allow_project_actions:
            raise PermissionError(f"{name} requires autonomy=project_execute")
        runtime = get_agent_run_runtime()
        if name == "schedule.list":
            items = runtime.list_schedules(limit=int(arguments.get("limit") or 100))
            return {"schema_version": "open_stock_ai.agent_schedule_list.v1", "count": len(items), "items": items}
        if name == "schedule.create":
            return runtime.create_schedule({**arguments, "autonomy": "advisory"})
        if name == "schedule.update":
            schedule = runtime.update_schedule(
                str(arguments["schedule_id"]),
                dict(arguments["changes"]),
            )
            if schedule is None:
                raise ValueError("Agent schedule not found")
            return schedule
        if name == "schedule.resume":
            schedule = runtime.resume_schedule(str(arguments["schedule_id"]))
            if schedule is None:
                raise ValueError("Agent schedule not found")
            return schedule
        schedule = runtime.disable_schedule(str(arguments["schedule_id"]))
        if schedule is None:
            raise ValueError("Agent schedule not found")
        return schedule
