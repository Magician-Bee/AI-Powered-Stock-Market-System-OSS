from __future__ import annotations

from typing import Any, Awaitable, Callable

from open_stock_ai.agent_runtime.provider_capabilities import ProviderCapabilityProfile


class CodexProvider:
    provider_id = "codex"

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._project_roots: dict[str, str] = {}

    async def start_session(self, session_id: str, *, project_root: str) -> None:
        resolved = str(project_root)
        self._project_roots[session_id] = resolved
        await self.runtime.start_agent_run(session_id, project_root=resolved)

    async def generate(
        self,
        session_id: str,
        prompt: str,
        *,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> str:
        result = await self.generate_structured(
            session_id,
            prompt,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["content"],
                "properties": {"content": {"type": "string"}},
            },
            event_sink=event_sink,
        )
        return str(result["content"])

    async def generate_structured(
        self,
        session_id: str,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        return await self.runtime.run_agent_turn(
            prompt,
            output_schema,
            run_id=session_id,
            event_sink=event_sink,
        )

    async def continue_with_tool_results(
        self,
        session_id: str,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        return await self.generate_structured(
            session_id,
            prompt,
            output_schema,
            event_sink=event_sink,
        )

    async def cancel(self, session_id: str) -> None:
        await self.runtime.close_agent_run(session_id)
        self._project_roots.pop(session_id, None)

    async def compact_context(self, session_id: str) -> None:
        # The App Server owns its internal context window. Reopening the
        # provider session drops accumulated provider state; the host then
        # supplies its bounded, durable transcript on the next turn.
        await self.runtime.close_agent_run(session_id)
        await self.runtime.start_agent_run(
            session_id,
            project_root=self._project_roots.get(session_id),
        )

    async def health(self) -> dict[str, Any]:
        account = await self.runtime.account_status(refresh=False)
        return {
            "provider_id": self.provider_id,
            "enabled": True,
            "primary": True,
            "authenticated": bool(account.get("authenticated")),
        }

    def capabilities(self) -> dict[str, Any]:
        profile = ProviderCapabilityProfile(
            provider=self.provider_id,
            model="codex-app-server/default",
            native_tool_calling=True,
            json_schema=True,
            parallel_tool_calls=True,
            streaming=True,
            reasoning_format="opaque",
            persistent_session=True,
            schema_fallback=True,
            recommended_protocol="advanced_v1",
            conformance_passed=False,
        )
        return {
            "structured_output": True,
            "persistent_session": True,
            "tool_result_continuation": True,
            "private_chain_of_thought_exposed": False,
            "profile": profile.model_dump(),
        }

    async def close_session(self, session_id: str) -> None:
        await self.runtime.close_agent_run(session_id)
        self._project_roots.pop(session_id, None)
