from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol


class ModelProvider(Protocol):
    provider_id: str

    async def start_session(self, session_id: str, *, project_root: str) -> None: ...

    async def generate(
        self,
        session_id: str,
        prompt: str,
        *,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> str: ...

    async def generate_structured(
        self,
        session_id: str,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]: ...

    async def continue_with_tool_results(
        self,
        session_id: str,
        prompt: str,
        output_schema: dict[str, Any],
        *,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]: ...

    async def cancel(self, session_id: str) -> None: ...

    async def compact_context(self, session_id: str) -> None: ...

    async def health(self) -> dict[str, Any]: ...

    def capabilities(self) -> dict[str, Any]: ...

    async def close_session(self, session_id: str) -> None: ...
