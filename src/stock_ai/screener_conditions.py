from __future__ import annotations

"""Typed, allowlisted conditions for the market screener.

The screener receives user supplied strings, so this module deliberately does
not use ``eval`` or a general expression language.  A condition is one simple
comparison over an allowlisted field and either a typed literal or another
allowlisted numeric field. Conditions in a request are ANDed; an unavailable
field is a failed condition, never an implicit zero or an assumed pass.
"""

from dataclasses import dataclass
import re
from typing import Any, Literal


ConditionKind = Literal["number", "string", "boolean"]
_OPERATORS = {">", ">=", "<", "<=", "==", "!="}
_EQUALITY_OPERATORS = frozenset({"==", "!="})
_COMPARISON = re.compile(
    r"^\s*(?P<field>[a-z][a-z0-9_]*)\s*(?P<operator>>=|<=|==|!=|>|<)\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)


class ScreenerConditionError(ValueError):
    """Raised when a user-provided screener condition is not safe or typed."""


@dataclass(frozen=True)
class FieldDefinition:
    name: str
    kind: ConditionKind
    aliases: tuple[str, ...] = ()
    allowed_operators: frozenset[str] = frozenset(_OPERATORS)


@dataclass(frozen=True)
class ScreenerCondition:
    field: str
    operator: str
    value: float | str | bool
    value_is_field: bool = False

    def display(self) -> str:
        value = str(self.value).lower() if isinstance(self.value, bool) else str(self.value)
        return f"{self.field} {self.operator} {value}"


@dataclass(frozen=True)
class ConditionEvaluation:
    condition: ScreenerCondition
    passed: bool
    observed: float | str | bool | None
    reason: str | None = None
    comparison_observed: float | str | bool | None = None

    def receipt(self) -> dict[str, Any]:
        return {
            "condition": self.condition.display(),
            "field": self.condition.field,
            "operator": self.condition.operator,
            "expected": self.condition.value,
            "value_kind": "field" if self.condition.value_is_field else "literal",
            "comparison_observed": self.comparison_observed,
            "observed": self.observed,
            "passed": self.passed,
            "reason": self.reason,
        }


FIELD_REGISTRY: dict[str, FieldDefinition] = {
    "change_percent": FieldDefinition("change_percent", "number", aliases=("change_pct",)),
    "close": FieldDefinition("close", "number", aliases=("price", "last_price")),
    "volume": FieldDefinition("volume", "number"),
    "exchange": FieldDefinition("exchange", "string", allowed_operators=_EQUALITY_OPERATORS),
    "trading_state": FieldDefinition("trading_state", "string", allowed_operators=_EQUALITY_OPERATORS),
    "is_etf": FieldDefinition("is_etf", "boolean", allowed_operators=_EQUALITY_OPERATORS),
    "realtime": FieldDefinition("realtime", "boolean", aliases=("has_realtime_quote",), allowed_operators=_EQUALITY_OPERATORS),
    "buy_liquidity_confirmed": FieldDefinition("buy_liquidity_confirmed", "boolean", allowed_operators=_EQUALITY_OPERATORS),
    "sell_liquidity_confirmed": FieldDefinition("sell_liquidity_confirmed", "boolean", allowed_operators=_EQUALITY_OPERATORS),
    # These fields are read from persisted official/contracted data stores by
    # the service layer. They are never fabricated from the realtime quote.
    "revenue_yoy": FieldDefinition("revenue_yoy", "number", aliases=("revenue_yoy_percent",)),
    "institutional_buy_5d": FieldDefinition("institutional_buy_5d", "number", aliases=("institutional_net_5d",)),
    "pe_percentile": FieldDefinition("pe_percentile", "number", aliases=("pe_percentile_5y",)),
    "avg_turnover_20d": FieldDefinition("avg_turnover_20d", "number", aliases=("average_turnover_20d",)),
    "sma_60": FieldDefinition("sma_60", "number"),
}

_FIELD_ALIASES = {
    alias: definition.name
    for definition in FIELD_REGISTRY.values()
    for alias in (definition.name, *definition.aliases)
}
_LEGACY_ALIASES = {
    # This is the only legacy UI condition currently emitted by the shipped
    # screener.  It is a real, explicit realtime-data requirement.
    "real_quote": "realtime == true",
}


def parse_conditions(expressions: list[str] | tuple[str, ...] | None) -> list[ScreenerCondition]:
    """Parse an AND-list of typed comparisons with an allowlisted field set."""

    output: list[ScreenerCondition] = []
    for index, raw in enumerate(expressions or ()):
        expression = str(raw or "").strip()
        if not expression:
            raise ScreenerConditionError(f"condition[{index}] must not be empty")
        expression = _LEGACY_ALIASES.get(expression.casefold(), expression)
        match = _COMPARISON.fullmatch(expression)
        if match is None:
            raise ScreenerConditionError(
                f"condition[{index}] must use '<field> <operator> <value>'; received {expression!r}"
            )
        requested_field = match.group("field").casefold()
        field_name = _FIELD_ALIASES.get(requested_field)
        if field_name is None:
            choices = ", ".join(sorted(FIELD_REGISTRY))
            raise ScreenerConditionError(
                f"condition[{index}] uses unsupported field {requested_field!r}; allowed fields: {choices}"
            )
        definition = FIELD_REGISTRY[field_name]
        operator = match.group("operator")
        if operator not in definition.allowed_operators:
            raise ScreenerConditionError(
                f"condition[{index}] operator {operator!r} is not valid for {field_name}"
            )
        value, value_is_field = _parse_value(match.group("value"), definition, index)
        output.append(
            ScreenerCondition(
                field=field_name,
                operator=operator,
                value=value,
                value_is_field=value_is_field,
            )
        )
    return output


def evaluate_conditions(
    values: dict[str, Any],
    conditions: list[ScreenerCondition] | tuple[ScreenerCondition, ...],
) -> list[ConditionEvaluation]:
    """Evaluate parsed conditions without coercing missing values into passes."""

    output: list[ConditionEvaluation] = []
    for condition in conditions:
        definition = FIELD_REGISTRY[condition.field]
        raw = values.get(condition.field)
        observed = _normalize_observed(raw, definition)
        if observed is None:
            output.append(
                ConditionEvaluation(
                    condition=condition,
                    passed=False,
                    observed=None,
                    reason="field_unavailable_in_realtime_quote",
                )
            )
            continue
        comparison_value = condition.value
        if condition.value_is_field:
            comparison_definition = FIELD_REGISTRY[str(condition.value)]
            comparison_value = _normalize_observed(values.get(comparison_definition.name), comparison_definition)
            if comparison_value is None:
                output.append(
                    ConditionEvaluation(
                        condition=condition,
                        passed=False,
                        observed=observed,
                        reason="comparison_field_unavailable_in_screening_snapshot",
                    )
                )
                continue
        output.append(
            ConditionEvaluation(
                condition=condition,
                passed=_compare(observed, condition.operator, comparison_value),
                observed=observed,
                comparison_observed=comparison_value,
            )
        )
    return output


def _parse_value(
    raw: str,
    definition: FieldDefinition,
    index: int,
) -> tuple[float | str | bool, bool]:
    value = raw.strip()
    if definition.kind == "number":
        referenced_field = _FIELD_ALIASES.get(value.casefold())
        if referenced_field is not None:
            referenced_definition = FIELD_REGISTRY[referenced_field]
            if referenced_definition.kind != "number":
                raise ScreenerConditionError(
                    f"condition[{index}] cannot compare numeric {definition.name} to {referenced_field}"
                )
            return referenced_field, True
        try:
            return float(value), False
        except ValueError as exc:
            raise ScreenerConditionError(
                f"condition[{index}] requires a numeric value for {definition.name}"
            ) from exc
    if definition.kind == "boolean":
        normalized = value.casefold()
        if normalized in {"true", "1"}:
            return True, False
        if normalized in {"false", "0"}:
            return False, False
        raise ScreenerConditionError(
            f"condition[{index}] requires true or false for {definition.name}"
        )
    if value[:1] in {"'", '"'}:
        if len(value) < 2 or value[-1:] != value[:1]:
            raise ScreenerConditionError(f"condition[{index}] has an unterminated string literal")
        value = value[1:-1]
    if not value:
        raise ScreenerConditionError(f"condition[{index}] requires a non-empty string value")
    return value, False


def _normalize_observed(value: Any, definition: FieldDefinition) -> float | str | bool | None:
    if value is None:
        return None
    if definition.kind == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if definition.kind == "boolean":
        return bool(value)
    text = str(value).strip()
    return text if text else None


def _compare(left: float | str | bool, operator: str, right: float | str | bool) -> bool:
    if operator not in _OPERATORS:
        raise ScreenerConditionError(f"unsupported comparison operator {operator!r}")
    if isinstance(left, str):
        left = left.casefold()
        right = str(right).casefold()
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    return left <= right
