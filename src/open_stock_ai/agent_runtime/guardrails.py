from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class CostBudgetExceeded(RuntimeError):
    """Raised when Host-owned cost units would exceed any active limit."""

    def __init__(
        self,
        *,
        scope: str,
        identifier: str,
        requested: int,
        used: int,
        limit: int,
    ) -> None:
        self.scope = scope
        self.identifier = identifier
        self.requested = requested
        self.used = used
        self.limit = limit
        super().__init__(
            f"{scope} {identifier} cost budget exceeded: "
            f"{used + requested} > {limit}"
        )


@dataclass(frozen=True, slots=True)
class CostBudget:
    scope: str
    identifier: str
    limit: int
    consumed: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.consumed)


class CostBudgetManager:
    """Atomic global/session/provider/tool cost accounting.

    Cost units are intentionally provider-neutral. Model calls charge estimated
    input/output tokens; tools charge one unit per requested execution. A
    provider or tool cannot reset the global or session allowance, and all
    applicable limits are checked before any counter is changed.
    """

    VALID_SCOPES = frozenset({"global", "session", "provider", "tool"})

    def __init__(self) -> None:
        self._budgets: dict[tuple[str, str], CostBudget] = {}

    def register(self, scope: str, identifier: str, *, limit: int) -> CostBudget:
        if scope not in self.VALID_SCOPES:
            raise ValueError(f"Unsupported cost budget scope: {scope}")
        if not str(identifier).strip():
            raise ValueError("Cost budget identifier is required")
        if int(limit) <= 0:
            raise ValueError("Cost budget limit must be positive")
        budget = CostBudget(scope, str(identifier), int(limit))
        self._budgets[(scope, str(identifier))] = budget
        return budget

    def ensure(self, scope: str, identifier: str, *, limit: int) -> CostBudget:
        return self._budgets.get((scope, str(identifier))) or self.register(
            scope, identifier, limit=limit
        )

    def charge(
        self,
        units: int,
        *,
        session_id: str,
        provider_id: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, CostBudget]:
        amount = int(units)
        if amount < 0:
            raise ValueError("Cost units cannot be negative")
        keys = [("global", "global"), ("session", str(session_id))]
        if provider_id:
            keys.append(("provider", str(provider_id)))
        if tool_name:
            keys.append(("tool", str(tool_name)))
        budgets = [self._budgets[key] for key in keys if key in self._budgets]
        exceeded = next(
            (
                budget
                for budget in budgets
                if budget.consumed + amount > budget.limit
            ),
            None,
        )
        if exceeded is not None:
            raise CostBudgetExceeded(
                scope=exceeded.scope,
                identifier=exceeded.identifier,
                requested=amount,
                used=exceeded.consumed,
                limit=exceeded.limit,
            )
        updated: dict[str, CostBudget] = {}
        for budget in budgets:
            replacement = CostBudget(
                budget.scope,
                budget.identifier,
                budget.limit,
                budget.consumed + amount,
            )
            self._budgets[(budget.scope, budget.identifier)] = replacement
            updated[f"{budget.scope}:{budget.identifier}"] = replacement
        return updated

    def snapshot(self) -> dict[str, Any]:
        return {
            f"{scope}:{identifier}": {
                "scope": scope,
                "identifier": identifier,
                "limit": budget.limit,
                "consumed": budget.consumed,
                "remaining": budget.remaining,
            }
            for (scope, identifier), budget in sorted(self._budgets.items())
        }


class RunawayExecutionExceeded(RuntimeError):
    """Raised when repeated or unbounded execution is detected by the Host."""


@dataclass(frozen=True, slots=True)
class RunawayExecutionLimits:
    max_depth: int = 5
    max_nodes: int = 128
    max_tool_calls: int = 256
    max_repeated_signature: int = 2
    max_no_progress_turns: int = 3

    def __post_init__(self) -> None:
        if any(
            int(value) <= 0
            for value in (
                self.max_depth,
                self.max_nodes,
                self.max_tool_calls,
                self.max_repeated_signature,
                self.max_no_progress_turns,
            )
        ):
            raise ValueError("Runaway execution limits must be positive")


class RunawayExecutionGuard:
    """Fail-closed guard for recursive, repeated, and no-progress runs."""

    def __init__(self, limits: RunawayExecutionLimits | None = None) -> None:
        self.limits = limits or RunawayExecutionLimits()
        self.tool_calls = 0
        self.nodes = 0
        self.no_progress_turns = 0
        self._signatures: dict[str, int] = {}

    def observe_branch(self, *, depth: int, nodes: int | None = None) -> None:
        if int(depth) > self.limits.max_depth:
            raise RunawayExecutionExceeded(
                f"recursive branch depth exceeded: {depth} > {self.limits.max_depth}"
            )
        if nodes is not None:
            self.nodes = max(self.nodes, int(nodes))
            if self.nodes > self.limits.max_nodes:
                raise RunawayExecutionExceeded(
                    f"task forest node limit exceeded: {self.nodes} > {self.limits.max_nodes}"
                )

    def observe_tool(self, *, tool_name: str, arguments: dict[str, Any]) -> str:
        self.tool_calls += 1
        if self.tool_calls > self.limits.max_tool_calls:
            raise RunawayExecutionExceeded(
                f"tool call limit exceeded: {self.tool_calls} > {self.limits.max_tool_calls}"
            )
        encoded = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        signature = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        count = self._signatures.get(signature, 0) + 1
        self._signatures[signature] = count
        if count > self.limits.max_repeated_signature:
            raise RunawayExecutionExceeded(
                f"repeated tool signature exceeded: {tool_name} ({count})"
            )
        return signature

    def observe_turn(self, *, progress: bool) -> None:
        self.no_progress_turns = 0 if progress else self.no_progress_turns + 1
        if self.no_progress_turns > self.limits.max_no_progress_turns:
            raise RunawayExecutionExceeded(
                "run made no observable progress within the configured turn limit"
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "limits": {
                "max_depth": self.limits.max_depth,
                "max_nodes": self.limits.max_nodes,
                "max_tool_calls": self.limits.max_tool_calls,
                "max_repeated_signature": self.limits.max_repeated_signature,
                "max_no_progress_turns": self.limits.max_no_progress_turns,
            },
            "tool_calls": self.tool_calls,
            "nodes": self.nodes,
            "no_progress_turns": self.no_progress_turns,
            "unique_tool_signatures": len(self._signatures),
        }
