from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


RadarAction = Literal["buy_now", "sell_now", "wait_to_buy", "wait_to_sell", "hold"]


class MarketRadarObservation(BaseModel):
    observation_id: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=1, max_length=32)
    statement: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=50)
    source_type: Literal["tool", "rule", "model"] = "tool"

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class MarketRadarItem(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(default="", max_length=200)
    action: RadarAction
    confidence: float = Field(ge=0, le=1)
    confidence_type: Literal["model_self_reported"] = "model_self_reported"
    reason: str = Field(min_length=1, max_length=3000)
    next_action: str = Field(min_length=1, max_length=1000)
    timing: str = Field(min_length=1, max_length=500)
    trigger: str = Field(min_length=1, max_length=1000)
    observation_ids: list[str] = Field(min_length=1, max_length=50)
    evidence_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: Any) -> float:
        confidence = float(value)
        return confidence / 100 if 1 < confidence <= 100 else confidence


class MarketRadarGroups(BaseModel):
    buy_now: list[str] = Field(default_factory=list)
    sell_now: list[str] = Field(default_factory=list)
    wait_to_buy: list[str] = Field(default_factory=list)
    wait_to_sell: list[str] = Field(default_factory=list)
    hold: list[str] = Field(default_factory=list)

    @field_validator("*")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip()))


class MarketRadarActionPlan(BaseModel):
    headline: str = Field(min_length=1, max_length=1000)
    next_review: str = Field(min_length=1, max_length=500)
    steps: list[str] = Field(default_factory=list, max_length=50)


class MarketRadarAnalysisLayer(BaseModel):
    summary: str = Field(min_length=1, max_length=5000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)


class MarketRadarRiskEvaluation(BaseModel):
    summary: str = Field(min_length=1, max_length=5000)
    risk_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    host_limits_applied: bool = False


class MarketRadarProvenance(BaseModel):
    origin: Literal["model"]
    provider: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=500)
    model_call_id: str = Field(min_length=1, max_length=500)
    model_call_succeeded: Literal[True]
    universe_source: str = Field(min_length=1, max_length=100)
    symbols_considered: list[str] = Field(min_length=1, max_length=100)
    fallback_used: Literal[False] = False

    @field_validator("symbols_considered")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip()))


class MarketRadarResult(BaseModel):
    schema_version: Literal["stock_ai.market_radar_result.v1"] = "stock_ai.market_radar_result.v1"
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    summary: str = Field(min_length=1, max_length=5000)
    items: list[MarketRadarItem] = Field(min_length=1, max_length=100)
    groups: MarketRadarGroups
    action_plan: MarketRadarActionPlan
    observations: list[MarketRadarObservation] = Field(min_length=1, max_length=500)
    rule_analysis: MarketRadarAnalysisLayer
    model_analysis: MarketRadarAnalysisLayer
    risk_evaluation: MarketRadarRiskEvaluation
    provenance: MarketRadarProvenance

    @model_validator(mode="after")
    def validate_internal_references(self) -> "MarketRadarResult":
        observation_ids = {item.observation_id for item in self.observations}
        item_symbols = [item.symbol for item in self.items]
        if len(item_symbols) != len(set(item_symbols)):
            raise ValueError("Market Radar items must contain each symbol at most once")
        for item in self.items:
            missing = set(item.observation_ids) - observation_ids
            if missing:
                raise ValueError(
                    f"{item.symbol} references unknown observations: {', '.join(sorted(missing))}"
                )
        grouped = {
            symbol
            for action in (
                self.groups.buy_now,
                self.groups.sell_now,
                self.groups.wait_to_buy,
                self.groups.wait_to_sell,
                self.groups.hold,
            )
            for symbol in action
        }
        if grouped != set(item_symbols):
            raise ValueError("Market Radar groups must contain every item symbol exactly by action")
        return self


class MarketRadarValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def market_radar_output_schema() -> dict[str, Any]:
    """Return the provider-facing structured-result schema.

    Provenance is present in the contract, but the Host always overwrites it
    with the verified invocation receipt before accepting the result.
    """

    schema = MarketRadarResult.model_json_schema()
    definitions = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}

    def inline_refs(node: Any) -> Any:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                name = reference.rsplit("/", 1)[-1]
                definition = definitions.get(name)
                if isinstance(definition, dict):
                    overrides = {key: value for key, value in node.items() if key != "$ref"}
                    return inline_refs({**definition, **overrides})
            return {
                key: inline_refs(value)
                for key, value in node.items()
                if key != "$defs"
            }
        if isinstance(node, list):
            return [inline_refs(value) for value in node]
        return node

    schema = inline_refs(schema)

    def close_objects(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
                properties = node.get("properties")
                if isinstance(properties, dict):
                    node["required"] = list(properties)
            for value in node.values():
                close_objects(value)
        elif isinstance(node, list):
            for value in node:
                close_objects(value)

    # Codex/OpenAI strict structured outputs reject an object node unless it
    # explicitly closes unknown properties. Pydantic omits that keyword for
    # its default ``extra=ignore`` models, so normalize the provider-facing
    # schema without changing the Host-side validation contract.
    close_objects(schema)
    return schema


def validate_market_radar_result(
    raw_result: Any,
    *,
    symbols: list[str] | tuple[str, ...],
    universe_source: str,
    provider: str,
    model_id: str,
    receipt: dict[str, Any],
    tool_trace: list[dict[str, Any]],
) -> MarketRadarResult:
    if not isinstance(raw_result, dict):
        raise MarketRadarValidationError(
            "market_radar_result_missing",
            "The completed Agent run did not return a structured MarketRadarResult",
        )
    normalized_symbols = list(
        dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    )
    receipt_id = str(receipt.get("call_id") or "").strip()
    if not receipt_id or receipt.get("status") != "succeeded":
        raise MarketRadarValidationError(
            "market_radar_receipt_invalid",
            "Market Radar requires a real succeeded model invocation receipt",
        )
    candidate = dict(raw_result)
    candidate["provenance"] = {
        "origin": "model",
        "provider": provider,
        "model_id": model_id,
        "model_call_id": receipt_id,
        "model_call_succeeded": True,
        "universe_source": universe_source,
        "symbols_considered": normalized_symbols,
        "fallback_used": False,
    }
    try:
        result = MarketRadarResult.model_validate(candidate)
    except Exception as exc:
        raise MarketRadarValidationError(
            "market_radar_schema_invalid",
            f"Structured Market Radar output failed validation: {exc}",
        ) from exc

    expected = set(normalized_symbols)
    item_symbols = {item.symbol for item in result.items}
    observation_symbols = {item.symbol for item in result.observations}
    if item_symbols != expected or observation_symbols != expected:
        raise MarketRadarValidationError(
            "market_radar_universe_incomplete",
            "Every resolved Universe symbol must have one card and at least one observation",
        )

    known_evidence = {
        str(value)
        for item in tool_trace
        if item.get("ok") is True
        for value in (
            item.get("call_id"),
            item.get("node_id"),
            (item.get("validation") or {}).get("evidence_hash"),
        )
        if value
    }
    referenced_evidence = {
        evidence_id
        for item in result.items
        for evidence_id in item.evidence_ids
    }
    referenced_evidence.update(
        evidence_id
        for observation in result.observations
        for evidence_id in observation.evidence_ids
    )
    referenced_evidence.update(result.rule_analysis.evidence_ids)
    referenced_evidence.update(result.model_analysis.evidence_ids)
    referenced_evidence.update(result.risk_evaluation.evidence_ids)
    unknown = referenced_evidence - known_evidence
    if unknown:
        raise MarketRadarValidationError(
            "market_radar_unknown_evidence",
            f"Market Radar referenced non-Host evidence IDs: {', '.join(sorted(unknown))}",
        )
    return result


def market_radar_ui_payload(result: MarketRadarResult) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    items_by_symbol = {
        item["symbol"]: {
            **item,
            "origin": "model",
            "model_call_succeeded": True,
        }
        for item in payload["items"]
    }
    payload["items"] = list(items_by_symbol.values())
    payload["groups"] = {
        action: [
            items_by_symbol[symbol]
            for symbol in symbols
            if symbol in items_by_symbol
        ]
        for action, symbols in payload["groups"].items()
    }
    payload["badge"] = "MODEL"
    payload["model_status"] = "succeeded"
    return payload
