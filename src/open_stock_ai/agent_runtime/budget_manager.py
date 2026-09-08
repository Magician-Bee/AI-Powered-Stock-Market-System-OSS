from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class TokenBudget:
    limit: int
    used: int = 0
    reserved: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.reserved)

    def can_spend(self, tokens: int) -> bool:
        return tokens >= 0 and tokens <= self.remaining

    def consume(self, tokens: int) -> None:
        if not self.can_spend(tokens):
            raise ValueError("Token budget exceeded")
        self.used += tokens


class TokenBudgetManager:
    """Independent Session/Run/Branch/Turn token accounting."""

    VALID_SCOPES = {"session", "run", "branch", "turn"}

    def __init__(self) -> None:
        self._budgets: dict[tuple[str, str], TokenBudget] = {}

    def allocate(self, scope: str, identifier: str, *, limit: int, reserved: int = 0) -> TokenBudget:
        if scope not in self.VALID_SCOPES:
            raise ValueError(f"Unsupported budget scope: {scope}")
        if limit <= 0 or reserved < 0 or reserved > limit:
            raise ValueError("Invalid token budget")
        budget = TokenBudget(limit=limit, reserved=reserved)
        self._budgets[(scope, identifier)] = budget
        return budget

    def consume(self, scope: str, identifier: str, tokens: int) -> int:
        budget = self._budgets[(scope, identifier)]
        budget.consume(tokens)
        return budget.remaining

    def get(self, scope: str, identifier: str) -> TokenBudget:
        return self._budgets[(scope, identifier)]


@dataclass(frozen=True, slots=True)
class MarginalValueDecision:
    create_branch: bool
    expected_value: float
    reason: str


class MarginalValueCheck:
    def __init__(self, *, threshold: float = 0.2) -> None:
        self.threshold = threshold
        self._question_keys: set[str] = set()

    def evaluate(
        self,
        *,
        question_key: str,
        probability_of_changing_decision: float,
        decision_impact: float,
        normalized_cost: float,
    ) -> MarginalValueDecision:
        if question_key in self._question_keys:
            return MarginalValueDecision(False, 0.0, "duplicate_research_question")
        value = max(0.0, min(1.0, probability_of_changing_decision)) * max(0.0, min(1.0, decision_impact)) - max(0.0, normalized_cost)
        create = value >= self.threshold
        if create:
            self._question_keys.add(question_key)
        return MarginalValueDecision(create, value, "may_change_final_decision" if create else "insufficient_marginal_value")


QuestionAction = Literal["ask_user", "research_first", "state_assumption"]


class QuestionBudget:
    def __init__(self, limit: int = 3) -> None:
        if limit < 1 or limit > 3:
            raise ValueError("A checkpoint question budget must be between 1 and 3")
        self.limit = limit
        self.used = 0

    def decide(self, *, impact: float, independently_verifiable: bool) -> QuestionAction:
        if independently_verifiable:
            return "research_first"
        if impact < 0.6 or self.used >= self.limit:
            return "state_assumption"
        self.used += 1
        return "ask_user"
