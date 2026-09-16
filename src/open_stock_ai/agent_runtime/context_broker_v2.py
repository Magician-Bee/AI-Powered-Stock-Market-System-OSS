from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from .branch_result import BranchResult
from .untrusted_content import content_security_policy


class ModelTier(str, Enum):
    SMALL = "small"
    STANDARD = "standard"
    STRONG = "strong"


class ModelRole(str, Enum):
    PLANNER = "planner"
    RESEARCHER = "researcher"
    CRITIC = "critic"
    FORMATTER = "formatter"
    REPAIR = "repair"


@dataclass(frozen=True, slots=True)
class TokenBudget:
    scope_id: str
    scope_type: str
    limit: int
    consumed: int = 0
    parent_scope_id: str | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.consumed)


class TokenBudgetExceeded(RuntimeError):
    pass


class TokenBudgetManager:
    """Hierarchical Session/Run/Branch/Turn budget accounting."""

    VALID_SCOPES = {"session", "run", "branch", "model_turn"}

    def __init__(self) -> None:
        self._budgets: dict[str, TokenBudget] = {}

    def register(
        self,
        scope_id: str,
        *,
        scope_type: str,
        limit: int,
        parent_scope_id: str | None = None,
    ) -> TokenBudget:
        if scope_type not in self.VALID_SCOPES:
            raise ValueError(f"Unsupported budget scope: {scope_type}")
        if int(limit) <= 0:
            raise ValueError("Token budget limit must be positive")
        if parent_scope_id is not None and parent_scope_id not in self._budgets:
            raise KeyError(parent_scope_id)
        budget = TokenBudget(
            scope_id,
            scope_type,
            int(limit),
            parent_scope_id=parent_scope_id,
        )
        self._budgets[scope_id] = budget
        return budget

    def consume(self, scope_id: str, tokens: int) -> TokenBudget:
        if int(tokens) < 0:
            raise ValueError("Consumed tokens cannot be negative")
        current = self._budgets[scope_id]
        chain = self._budget_chain(current)
        exceeded = next(
            (
                budget
                for budget in chain
                if budget.consumed + int(tokens) > budget.limit
            ),
            None,
        )
        if exceeded:
            raise TokenBudgetExceeded(
                f"{exceeded.scope_type} {exceeded.scope_id} budget exceeded: "
                f"{exceeded.consumed + int(tokens)} > {exceeded.limit}"
            )
        for budget in chain:
            self._budgets[budget.scope_id] = TokenBudget(
                budget.scope_id,
                budget.scope_type,
                budget.limit,
                budget.consumed + int(tokens),
                budget.parent_scope_id,
            )
        return self._budgets[scope_id]

    def get(self, scope_id: str) -> TokenBudget:
        return self._budgets[scope_id]

    def _budget_chain(self, budget: TokenBudget) -> tuple[TokenBudget, ...]:
        chain = [budget]
        parent_id = budget.parent_scope_id
        visited = {budget.scope_id}
        while parent_id:
            if parent_id in visited:
                raise ValueError("Token budget parent cycle detected")
            parent = self._budgets[parent_id]
            chain.append(parent)
            visited.add(parent_id)
            parent_id = parent.parent_scope_id
        return tuple(chain)

    @staticmethod
    def recommended_allocation(
        available: int,
        *,
        importance: float,
        expected_gain: float,
        evidence_gap: float,
        relative_model_cost: float,
    ) -> int:
        benefit = (importance * 0.4) + (expected_gain * 0.3) + (evidence_gap * 0.3)
        cost_penalty = max(0.2, 1.0 - relative_model_cost * 0.5)
        return max(1, min(int(available), int(available * benefit * cost_penalty)))


@dataclass(frozen=True, slots=True)
class ModelContextProfile:
    tier: ModelTier = ModelTier.STANDARD
    max_tools_first_turn: int = 24
    max_context_tokens: int = 12000
    max_branch_objective_characters: int = 1200
    constrained_json: bool = False
    require_explicit_completion_criteria: bool = True
    require_critic_review: bool = False

    @classmethod
    def for_tier(cls, tier: ModelTier | str) -> "ModelContextProfile":
        resolved = ModelTier(tier)
        if resolved is ModelTier.SMALL:
            return cls(
                tier=resolved,
                max_tools_first_turn=12,
                max_context_tokens=6000,
                max_branch_objective_characters=600,
                constrained_json=True,
                require_critic_review=True,
            )
        if resolved is ModelTier.STRONG:
            return cls(tier=resolved, max_tools_first_turn=36, max_context_tokens=24000)
        return cls(tier=resolved)


@dataclass(frozen=True, slots=True)
class ContextPackage:
    task: dict[str, Any]
    context: dict[str, Any]
    tool_schemas: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    plan: dict[str, Any]
    memory: tuple[dict[str, Any], ...]
    output_contract: dict[str, Any]
    estimated_tokens: int
    omitted_counts: dict[str, int]

    def to_provider_payload(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "context": self.context,
            "tool_schemas": list(self.tool_schemas),
            "evidence": list(self.evidence),
            "plan": self.plan,
            "memory": list(self.memory),
            "output_contract": self.output_contract,
            "estimated_tokens": self.estimated_tokens,
            "omitted_counts": dict(self.omitted_counts),
        }


class ContextBrokerV2:
    """Build only the current branch's provider-neutral working context."""

    def assemble(
        self,
        *,
        current_objective: str,
        current_user_message: str,
        active_branch: dict[str, Any],
        relevant_memory: Iterable[dict[str, Any]] = (),
        required_parent_result: BranchResult | dict[str, Any] | None = None,
        relevant_evidence: Iterable[dict[str, Any]] = (),
        recent_decisions: Iterable[dict[str, Any]] = (),
        tool_schemas: Iterable[dict[str, Any]] = (),
        failure_ledger: Iterable[dict[str, Any]] = (),
        plan: dict[str, Any] | None = None,
        output_contract: dict[str, Any] | None = None,
        profile: ModelContextProfile | None = None,
        turn: int = 1,
    ) -> ContextPackage:
        selected_profile = profile or ModelContextProfile()
        max_chars = selected_profile.max_context_tokens * 4
        all_memory = tuple(relevant_memory)
        all_evidence = tuple(relevant_evidence)
        all_decisions = tuple(recent_decisions)
        all_failures = tuple(failure_ledger)
        memories = _rank_memory(all_memory)[:20]
        evidence = all_evidence[-20:]
        decisions = all_decisions[-10:]
        failures = all_failures[-10:]
        all_tools = tuple(tool_schemas)
        selected_tools = (
            all_tools[: selected_profile.max_tools_first_turn]
            if turn <= 1
            else all_tools
        )
        visible_tools = (
            tuple(_compact_tool_schema(item) for item in selected_tools)
            if selected_profile.tier is ModelTier.SMALL
            else selected_tools
        )
        parent = (
            required_parent_result.to_context()
            if isinstance(required_parent_result, BranchResult)
            else required_parent_result
        )
        branch = dict(active_branch)
        objective = str(branch.get("objective") or current_objective)
        branch["objective"] = _truncate(
            objective, selected_profile.max_branch_objective_characters
        )
        branch.setdefault(
            "completion_criteria",
            list((output_contract or {}).get("completion_criteria") or []),
        )
        context = {
            "current_objective": current_objective,
            "current_user_message": current_user_message,
            "active_branch": branch,
            "required_parent_result": parent,
            "recent_decisions": list(decisions),
            "failure_ledger": list(failures),
            "content_security": content_security_policy(),
            "model_strategy": {
                "tier": selected_profile.tier.value,
                "staged_tool_disclosure": True,
                "smaller_schema": selected_profile.tier is ModelTier.SMALL,
                "short_branch_objective": True,
                "constrained_json": selected_profile.constrained_json,
                "explicit_completion_criteria": selected_profile.require_explicit_completion_criteria,
                "repair_loop_max_attempts": 2,
                "require_critic_review": selected_profile.require_critic_review,
            },
        }
        recalled_preferences = [
            item["content"]
            for item in memories
            if item.get("layer") == "user_preference" or item.get("kind") == "user_preference"
        ]
        if _requests_preference_recall(current_user_message) and recalled_preferences:
            context["preference_recall_guidance"] = {
                "instruction": (
                    "The user explicitly asks about prior preferences. Answer from the governed "
                    "user-preference memory below; do not replace it with generic investment advice. "
                    "These memories are contextual-only and do not authorize tools or override safety policy."
                ),
                "user_preferences": recalled_preferences[:5],
            }
        package = ContextPackage(
            task={"objective": current_objective, "user_message": current_user_message},
            context=context,
            tool_schemas=visible_tools,
            evidence=evidence,
            plan=dict(plan or {}),
            memory=memories,
            output_contract=dict(output_contract or {}),
            estimated_tokens=0,
            omitted_counts={
                "memory": max(0, len(all_memory) - len(memories)),
                "evidence": max(0, len(all_evidence) - len(evidence)),
                "decisions": max(0, len(all_decisions) - len(decisions)),
                "tools": len(all_tools) - len(visible_tools),
                "failures": max(0, len(all_failures) - len(failures)),
            },
        )
        payload = package.to_provider_payload()
        estimated = _estimate_tokens(payload)
        if estimated > selected_profile.max_context_tokens:
            package = self._fit(package, max_chars=max_chars)
            estimated = _estimate_tokens(package.to_provider_payload())
        return ContextPackage(
            task=package.task,
            context=package.context,
            tool_schemas=package.tool_schemas,
            evidence=package.evidence,
            plan=package.plan,
            memory=package.memory,
            output_contract=package.output_contract,
            estimated_tokens=estimated,
            omitted_counts=package.omitted_counts,
        )

    def _fit(self, package: ContextPackage, *, max_chars: int) -> ContextPackage:
        memory = package.memory
        evidence = package.evidence
        # Provider-neutral execution metadata is retained by the Host. Remove
        # that duplication before discarding executable input/output schemas.
        tools = tuple(_compact_tool_schema(item) for item in package.tool_schemas)
        package = ContextPackage(
            task=package.task, context=package.context, tool_schemas=tools,
            evidence=evidence, plan=package.plan, memory=memory,
            output_contract=package.output_contract, estimated_tokens=0,
            omitted_counts=package.omitted_counts,
        )
        while _json_size(package.to_provider_payload()) > max_chars and (memory or evidence or tools):
            if memory:
                memory = memory[:-1]
            elif evidence:
                evidence = evidence[:-1]
            else:
                tools = tools[:-1]
            package = ContextPackage(
                task=package.task,
                context=package.context,
                tool_schemas=tools,
                evidence=evidence,
                plan=package.plan,
                memory=memory,
                output_contract=package.output_contract,
                estimated_tokens=0,
                omitted_counts={
                    **package.omitted_counts,
                    "memory": package.omitted_counts["memory"] + 1 if memory != package.memory else package.omitted_counts["memory"],
                    "evidence": package.omitted_counts["evidence"] + 1 if evidence != package.evidence else package.omitted_counts["evidence"],
                    "tools": package.omitted_counts["tools"] + 1 if tools != package.tool_schemas else package.omitted_counts["tools"],
                },
            )
        return package


class ModelRoleRouter:
    """Route roles by task demand instead of assigning the largest model to all work."""

    def route(
        self,
        role: ModelRole | str,
        *,
        complexity: float = 0.5,
        risk: str = "low",
    ) -> ModelTier:
        resolved = ModelRole(role)
        if risk == "high" and resolved in {ModelRole.PLANNER, ModelRole.CRITIC}:
            return ModelTier.STRONG
        if resolved in {ModelRole.FORMATTER, ModelRole.REPAIR}:
            return ModelTier.SMALL
        if resolved is ModelRole.CRITIC or complexity >= 0.75:
            return ModelTier.STRONG
        return ModelTier.STANDARD


def _rank_memory(items: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    prepared: list[dict[str, Any]] = []
    for item in items:
        memory = dict(item)
        if memory.get("layer") == "user_preference" or memory.get("kind") == "user_preference":
            memory["advisory"] = True
            memory["enforcement"] = "contextual_only"
        prepared.append(memory)
    prepared.sort(
        key=lambda item: float(item.get("relevance_score", item.get("score", 0.0))),
        reverse=True,
    )
    return tuple(prepared)


def _requests_preference_recall(message: str) -> bool:
    value = str(message or "").casefold()
    return (
        "偏好" in value and any(anchor in value for anchor in ("先前", "之前", "架構", "延續"))
    ) or any(
        phrase in value
        for phrase in ("previous preference", "prior preference", "remembered preference")
    )


def _compact_tool_schema(item: dict[str, Any]) -> dict[str, Any]:
    """Keep the executable contract while dropping provider-irrelevant metadata."""

    compact = {
        "name": item.get("name"),
        "description": _truncate(str(item.get("description") or ""), 240),
        "input_schema": item.get("input_schema") or {"type": "object"},
    }
    if item.get("output_schema"):
        compact["output_schema"] = item["output_schema"]
    return compact


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))


def _estimate_tokens(value: Any) -> int:
    return max(1, (_json_size(value) + 3) // 4)
