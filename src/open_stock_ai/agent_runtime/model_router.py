from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable, Literal, Mapping


class ModelRole(StrEnum):
    PLANNER = "planner"
    RESEARCHER = "researcher"
    CRITIC = "critic"
    FORMATTER = "formatter"
    REPAIR = "repair"


class ProviderMode(StrEnum):
    NATIVE_TOOL_CALLING = "native_tool_calling"
    STRUCTURED_JSON = "structured_json"
    TEXT_TO_JSON_ADAPTER = "text_to_json_adapter"


@dataclass(frozen=True, slots=True)
class ModelProfile:
    model_id: str
    provider: str
    mode: ProviderMode
    roles: frozenset[ModelRole]
    context_window: int
    cost_tier: int
    reliability: float
    tool_calling: bool = False
    local: bool = False


@dataclass(frozen=True, slots=True)
class ProviderNeutralRequest:
    task: str
    context: dict[str, Any]
    tool_schemas: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    plan: dict[str, Any]
    memory: tuple[dict[str, Any], ...]
    output_contract: dict[str, Any]


class ModelBudgetExceeded(RuntimeError):
    """Raised before a model call, or before accepting an oversized response."""


@dataclass(slots=True)
class HostModelBudget:
    """Host-owned token ledger shared by routing and provider execution.

    The router only selects a model when the fitted request and reserved output
    both fit.  The executor then charges the actual request/response, so a model
    cannot reset or self-declare its allowance.
    """

    scope_id: str
    limit: int
    consumed: int = 0
    max_cost_tier: int | None = None

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("Model budget limit must be positive")
        if self.consumed < 0 or self.consumed > self.limit:
            raise ValueError("Model budget consumption is outside its limit")
        if self.max_cost_tier is not None and self.max_cost_tier < 0:
            raise ValueError("Model budget cost tier cannot be negative")

    @property
    def remaining(self) -> int:
        return self.limit - self.consumed

    def charge(self, tokens: int) -> None:
        amount = max(0, int(tokens))
        if amount > self.remaining:
            raise ModelBudgetExceeded(
                f"{self.scope_id} token budget exceeded: "
                f"requested={amount}, remaining={self.remaining}"
            )
        self.consumed += amount

    def refund(self, tokens: int) -> None:
        self.consumed = max(0, self.consumed - max(0, int(tokens)))


@dataclass(frozen=True, slots=True)
class ModelRouteDecision:
    profile: ModelProfile
    request: ProviderNeutralRequest
    estimated_input_tokens: int
    reserved_output_tokens: int
    compacted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelExecutionResult:
    output: Any
    route: ModelRouteDecision
    input_tokens: int
    output_tokens: int


ModelProvider = Callable[[ProviderNeutralRequest], Awaitable[Any]]


class ModelRouter:
    def __init__(self, profiles: list[ModelProfile]) -> None:
        if not profiles:
            raise ValueError("At least one model profile is required")
        self.profiles = tuple(profiles)

    def route(
        self,
        role: ModelRole,
        *,
        required_context: int = 0,
        require_tool_calling: bool = False,
        high_risk: bool = False,
    ) -> ModelProfile:
        candidates = [
            item
            for item in self.profiles
            if role in item.roles
            and item.context_window >= required_context
            and (not require_tool_calling or item.tool_calling)
        ]
        if not candidates:
            raise LookupError(f"No model can satisfy role={role.value}")
        if high_risk or role is ModelRole.CRITIC:
            return max(candidates, key=lambda item: (item.reliability, -item.cost_tier))
        return min(candidates, key=lambda item: (item.cost_tier, -item.reliability))

    def route_request(
        self,
        role: ModelRole,
        request: ProviderNeutralRequest,
        *,
        budget: HostModelBudget,
        reserve_output_tokens: int = 512,
        require_tool_calling: bool = False,
        high_risk: bool = False,
    ) -> ModelRouteDecision:
        """Select and fit a real request under model and Host budget limits."""

        reserve = max(1, int(reserve_output_tokens))
        candidates = [
            item
            for item in self.profiles
            if role in item.roles
            and (not require_tool_calling or item.tool_calling)
            and (
                budget.max_cost_tier is None
                or item.cost_tier <= budget.max_cost_tier
            )
        ]
        if high_risk or role is ModelRole.CRITIC:
            candidates.sort(key=lambda item: (-item.reliability, item.cost_tier))
        else:
            candidates.sort(key=lambda item: (item.cost_tier, -item.reliability))

        for profile in candidates:
            available_input = min(
                profile.context_window - reserve,
                budget.remaining - reserve,
            )
            if available_input <= 0:
                continue
            fitted, compacted = self._fit_request(
                request,
                max_input_tokens=available_input,
                prefer_compact=profile.local,
            )
            estimated = estimate_tokens(fitted)
            if estimated <= available_input:
                reasons = [f"role:{role.value}", "host_budget_checked"]
                if compacted:
                    reasons.append("context_compacted")
                if high_risk or role is ModelRole.CRITIC:
                    reasons.append("reliability_preferred")
                else:
                    reasons.append("cost_preferred")
                return ModelRouteDecision(
                    profile=profile,
                    request=fitted,
                    estimated_input_tokens=estimated,
                    reserved_output_tokens=reserve,
                    compacted=compacted,
                    reasons=tuple(reasons),
                )
        raise LookupError(
            f"No model can satisfy role={role.value} within "
            f"budget={budget.remaining} tokens"
        )

    @staticmethod
    def compact_for_small_model(
        request: ProviderNeutralRequest,
        *,
        max_tools: int = 12,
        max_objective_chars: int = 600,
    ) -> ProviderNeutralRequest:
        return ProviderNeutralRequest(
            task=request.task[:max_objective_chars],
            context=request.context,
            tool_schemas=request.tool_schemas[:max_tools],
            evidence=request.evidence,
            plan=request.plan,
            memory=request.memory,
            output_contract=request.output_contract,
        )

    @classmethod
    def _fit_request(
        cls,
        request: ProviderNeutralRequest,
        *,
        max_input_tokens: int,
        prefer_compact: bool,
    ) -> tuple[ProviderNeutralRequest, bool]:
        if not prefer_compact and estimate_tokens(request) <= max_input_tokens:
            return request, False

        candidates = (
            cls.compact_for_small_model(request),
            _compact_request(request, max_items=8, max_text_chars=800),
            _compact_request(request, max_items=4, max_text_chars=320),
            _compact_request(request, max_items=1, max_text_chars=120),
        )
        for candidate in candidates:
            if estimate_tokens(candidate) <= max_input_tokens:
                return candidate, candidate != request
        return candidates[-1], True


class HostModelExecutor:
    """Execute the router's fitted request and settle the Host token ledger."""

    def __init__(
        self,
        router: ModelRouter,
        providers: Mapping[str, ModelProvider],
    ) -> None:
        self.router = router
        self.providers = dict(providers)

    async def execute(
        self,
        role: ModelRole,
        request: ProviderNeutralRequest,
        *,
        budget: HostModelBudget,
        reserve_output_tokens: int = 512,
        require_tool_calling: bool = False,
        high_risk: bool = False,
    ) -> ModelExecutionResult:
        route = self.router.route_request(
            role,
            request,
            budget=budget,
            reserve_output_tokens=reserve_output_tokens,
            require_tool_calling=require_tool_calling,
            high_risk=high_risk,
        )
        provider = self.providers.get(route.profile.model_id)
        if provider is None:
            raise LookupError(f"No provider executor for model={route.profile.model_id}")

        reserved = route.estimated_input_tokens + route.reserved_output_tokens
        budget.charge(reserved)
        try:
            output = await provider(route.request)
        except BaseException:
            # The attempted input was spent, but an unproduced output must not
            # consume the rest of this Run/Branch allowance.
            budget.refund(route.reserved_output_tokens)
            raise
        output_tokens = estimate_tokens(output)
        if output_tokens > route.reserved_output_tokens:
            raise ModelBudgetExceeded(
                f"model={route.profile.model_id} response exceeded reserved output: "
                f"actual={output_tokens}, reserved={route.reserved_output_tokens}"
            )
        budget.refund(route.reserved_output_tokens - output_tokens)
        return ModelExecutionResult(
            output=output,
            route=route,
            input_tokens=route.estimated_input_tokens,
            output_tokens=output_tokens,
        )


def estimate_tokens(value: Any) -> int:
    """Stable provider-neutral estimate used for pre-call Host enforcement."""

    if isinstance(value, ProviderNeutralRequest):
        value = {
            "task": value.task,
            "context": value.context,
            "tool_schemas": value.tool_schemas,
            "evidence": value.evidence,
            "plan": value.plan,
            "memory": value.memory,
            "output_contract": value.output_contract,
        }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return max(1, (len(encoded) + 3) // 4)


def _compact_request(
    request: ProviderNeutralRequest,
    *,
    max_items: int,
    max_text_chars: int,
) -> ProviderNeutralRequest:
    return ProviderNeutralRequest(
        task=request.task[:max_text_chars],
        context=_bounded_value(
            request.context,
            max_items=max_items,
            max_text_chars=max_text_chars,
        ),
        tool_schemas=tuple(
            _bounded_value(
                item,
                max_items=max_items,
                max_text_chars=max_text_chars,
            )
            for item in request.tool_schemas[:max_items]
        ),
        evidence=tuple(
            _bounded_value(item, max_items=max_items, max_text_chars=max_text_chars)
            for item in request.evidence[:max_items]
        ),
        plan=_bounded_value(
            request.plan,
            max_items=max_items,
            max_text_chars=max_text_chars,
        ),
        memory=tuple(
            _bounded_value(item, max_items=max_items, max_text_chars=max_text_chars)
            for item in request.memory[:max_items]
        ),
        # The output contract is never truncated. Host validation must retain
        # the complete schema even when historical context is compressed.
        output_contract=request.output_contract,
    )


def _bounded_value(value: Any, *, max_items: int, max_text_chars: int) -> Any:
    if isinstance(value, str):
        return value[:max_text_chars]
    if isinstance(value, dict):
        return {
            str(key): _bounded_value(
                item,
                max_items=max_items,
                max_text_chars=max_text_chars,
            )
            for key, item in list(value.items())[:max_items]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_value(
                item,
                max_items=max_items,
                max_text_chars=max_text_chars,
            )
            for item in value[:max_items]
        ]
    return value


class CapabilityComposer:
    """Compose multi-intent capabilities, then reveal only the current phase."""

    INTENT_PREFIXES: dict[str, tuple[str, ...]] = {
        "market_information": ("market.", "web.", "browser.", "artifact.", "interaction."),
        "market_decision": ("market.", "portfolio.", "broker.account.", "broker.paper_order", "web.", "browser.", "risk.", "analysis.", "artifact.", "interaction."),
        "market_radar": ("market.", "web.", "browser.", "portfolio.", "risk.", "analysis.", "artifact.", "interaction."),
        "portfolio": ("portfolio.", "broker.account.", "risk."),
        "automation": ("scheduler.", "automation.", "notification.", "interaction.", "artifact."),
        "external_research": ("web.", "browser.", "artifact.", "interaction."),
    }
    PHASE_RULES: dict[str, tuple[str, ...]] = {
        "research": ("market.", "portfolio.read", "web.", "browser.read", "artifact.create", "interaction.ask"),
        "decision": ("risk.", "portfolio.", "analysis.", "market.", "web.", "browser."),
        "automation": ("scheduler.preview", "automation.plan", "automation.inspect"),
        "confirmation": ("automation.create", "notification.send", "broker.paper_order"),
    }

    def compose_prefixes(self, detected_intents: list[str]) -> tuple[str, ...]:
        prefixes = {
            prefix
            for intent in detected_intents
            for prefix in self.INTENT_PREFIXES.get(intent, ())
        }
        return tuple(sorted(prefixes))

    def disclose(
        self,
        manifest: list[dict[str, Any]],
        *,
        detected_intents: list[str],
        phase: Literal["research", "decision", "automation", "confirmation"],
        confirmed: bool = False,
        limit: int = 24,
    ) -> list[dict[str, Any]]:
        if phase == "confirmation" and not confirmed:
            return []
        intent_rules = self.compose_prefixes(detected_intents)
        phase_rules = self.PHASE_RULES[phase]
        selected = [
            item
            for item in manifest
            if _matches(str(item.get("name") or ""), intent_rules)
            and _matches(str(item.get("name") or ""), phase_rules)
        ]
        return selected[: max(1, limit)]


def _matches(name: str, rules: tuple[str, ...]) -> bool:
    return any(name == rule or (rule.endswith(".") and name.startswith(rule)) for rule in rules)


class ProviderClass(StrEnum):
    """Provider capability classes used by the executable P103 matrix.

    The aliases preserve the names used by the first matrix draft.  Iterating
    the enum yields the five provider-neutral capability classes below rather
    than product-specific model names.
    """

    STRONG_CLOUD = "strong_cloud"
    SMALL_CLOUD = "small_cloud"
    LOCAL_TOOL_NATIVE = "local_tool_native"
    LOCAL_TEXT_ONLY = "local_text_only"
    CODEX = "codex"

    CLAUDE = STRONG_CLOUD
    TEXT_ADAPTER = SMALL_CLOUD
    OLLAMA_STRONG = LOCAL_TOOL_NATIVE
    OLLAMA_WEAK = LOCAL_TEXT_ONLY
    OPENAI_CODEX = CODEX


class MatrixBehavior(StrEnum):
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_CALL = "tool_call"
    PARALLEL_BRANCHES = "parallel_branches"
    LONG_SESSION = "long_session"
    ERROR_REPAIR = "error_repair"
    APPROVAL_INTERACTION = "approval_interaction"
    REFLECTION = "reflection"
    AUTOMATION = "automation"

    TOOL_USE = TOOL_CALL
    BRANCH_PLANNING = PARALLEL_BRANCHES
    REPAIR = ERROR_REPAIR
    USER_INTERACTION = APPROVAL_INTERACTION


@dataclass(frozen=True, slots=True)
class MatrixGap:
    provider_class: ProviderClass
    behavior: MatrixBehavior


class ModelMatrixAuditor:
    def __init__(self) -> None:
        self._results: dict[tuple[ProviderClass, MatrixBehavior], bool] = {}

    def record(self, provider_class: ProviderClass, behavior: MatrixBehavior, *, passed: bool) -> None:
        self._results[(provider_class, behavior)] = bool(passed)

    def gaps(self) -> tuple[MatrixGap, ...]:
        return tuple(
            MatrixGap(provider, behavior)
            for provider in ProviderClass
            for behavior in MatrixBehavior
            if self._results.get((provider, behavior)) is not True
        )

    @property
    def complete(self) -> bool:
        return not self.gaps()
