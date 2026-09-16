from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field


class ValidationIssue(BaseModel):
    code: str
    message: str
    path: str | None = None


class LayerValidationReport(BaseModel):
    layer: Literal["protocol", "execution", "evidence", "semantic"]
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProtocolValidator:
    def validate(self, payload: Any, schema: dict[str, Any]) -> LayerValidationReport:
        issues: list[ValidationIssue] = []
        if not isinstance(payload, dict):
            issues.append(
                ValidationIssue(
                    code="protocol_invalid_json",
                    message="Provider output must normalize to one JSON object",
                )
            )
        else:
            for field in schema.get("required") or []:
                if field not in payload:
                    issues.append(
                        ValidationIssue(
                            code="protocol_missing_field",
                            message=f"Missing required field: {field}",
                            path=str(field),
                        )
                    )
            properties = schema.get("properties") or {}
            for field, value in payload.items():
                expected = properties.get(field, {}).get("type") if isinstance(properties, dict) else None
                if expected and not _matches_json_type(value, expected):
                    issues.append(
                        ValidationIssue(
                            code="protocol_schema_mismatch",
                            message=f"{field} does not match JSON type {expected}",
                            path=str(field),
                        )
                    )
        return LayerValidationReport(layer="protocol", valid=not issues, issues=issues)


class ExecutionValidator:
    RECEIPT_KEYS = {
        "order_id",
        "fill_id",
        "artifact_id",
        "ui_command_id",
        "workflow_id",
        "memory_id",
        "mutation_receipt",
        "campaign_receipt_id",
    }

    def validate(
        self,
        result: Any,
        *,
        mutation_expected: bool,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> LayerValidationReport:
        issues: list[ValidationIssue] = []
        has_receipt = _contains_receipt(result, self.RECEIPT_KEYS)
        state_changed = bool(
            before is not None
            and after is not None
            and _state_identity(before) != _state_identity(after)
        )
        if mutation_expected and not (has_receipt or state_changed):
            issues.append(
                ValidationIssue(
                    code="execution_missing_receipt",
                    message=(
                        "A mutation requires a Host-verifiable execution receipt "
                        "or before/after state transition"
                    ),
                )
            )
        return LayerValidationReport(
            layer="execution",
            valid=not issues,
            issues=issues,
            metadata={
                "mutation_expected": mutation_expected,
                "has_receipt": has_receipt,
                "state_changed": state_changed,
            },
        )


class EvidenceValidator:
    def validate(
        self,
        evidence: list[dict[str, Any]],
        *,
        symbol: str | None = None,
        max_age_seconds: int | None = None,
        now: datetime | None = None,
    ) -> LayerValidationReport:
        issues: list[ValidationIssue] = []
        expected_symbol = str(symbol or "").strip().upper() or None
        current = now or datetime.now(timezone.utc)
        for index, item in enumerate(evidence):
            evidence_id = str(item.get("evidence_id") or item.get("id") or "").strip()
            if not evidence_id:
                issues.append(
                    ValidationIssue(
                        code="evidence_missing_id",
                        message="Evidence item requires a stable ID",
                        path=f"evidence[{index}]",
                    )
                )
            item_symbol = str(item.get("symbol") or "").strip().upper() or None
            if expected_symbol and item_symbol and item_symbol != expected_symbol:
                issues.append(
                    ValidationIssue(
                        code="evidence_symbol_mismatch",
                        message=f"Evidence symbol {item_symbol} does not match {expected_symbol}",
                        path=f"evidence[{index}].symbol",
                    )
                )
            if max_age_seconds is not None and item.get("observed_at"):
                observed = _parse_time(str(item["observed_at"]))
                if observed is None:
                    issues.append(
                        ValidationIssue(
                            code="evidence_invalid_timestamp",
                            message="Evidence timestamp is not ISO-8601",
                            path=f"evidence[{index}].observed_at",
                        )
                    )
                elif (current - observed).total_seconds() > max_age_seconds:
                    issues.append(
                        ValidationIssue(
                            code="evidence_stale",
                            message="Evidence is older than the allowed freshness window",
                            path=f"evidence[{index}].observed_at",
                        )
                    )
        return LayerValidationReport(layer="evidence", valid=not issues, issues=issues)


class SemanticClaimValidator:
    def validate(
        self,
        claims: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
    ) -> LayerValidationReport:
        evidence_by_id = {
            str(item.get("evidence_id") or item.get("id")): item
            for item in evidence
            if item.get("evidence_id") or item.get("id")
        }
        evidence_ids = set(evidence_by_id)
        issues: list[ValidationIssue] = []
        statuses: list[dict[str, Any]] = []
        for index, claim in enumerate(claims):
            claim_evidence = [str(value) for value in claim.get("evidence_ids") or []]
            missing = [value for value in claim_evidence if value not in evidence_ids]
            if not claim_evidence:
                status = "unsupported"
                issues.append(
                    ValidationIssue(
                        code="semantic_claim_without_evidence",
                        message="Claim has no bound evidence",
                        path=f"claims[{index}]",
                    )
                )
            elif missing:
                status = "partially_supported"
                issues.append(
                    ValidationIssue(
                        code="semantic_claim_missing_evidence",
                        message=f"Unknown evidence IDs: {', '.join(missing)}",
                        path=f"claims[{index}].evidence_ids",
                    )
                )
            else:
                bound = [evidence_by_id[value] for value in claim_evidence]
                status, detail = _semantic_entailment(claim, bound)
                if status == "contradicted":
                    issues.append(
                        ValidationIssue(
                            code="semantic_claim_contradicted",
                            message=detail or "Bound evidence contradicts the claim",
                            path=f"claims[{index}]",
                        )
                    )
                elif status != "supported":
                    issues.append(
                        ValidationIssue(
                            code="semantic_claim_not_entailed",
                            message=detail or "Bound evidence does not prove the claim",
                            path=f"claims[{index}]",
                        )
                    )
            statuses.append(
                {
                    "text": str(claim.get("text") or ""),
                    "type": str(claim.get("type") or "unknown"),
                    "evidence_ids": claim_evidence,
                    "status": status,
                }
            )
        return LayerValidationReport(
            layer="semantic",
            valid=not issues,
            issues=issues,
            metadata={"claims": statuses},
        )


def _semantic_entailment(
    claim: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> tuple[str, str]:
    metric = str(claim.get("metric") or claim.get("predicate") or "").strip()
    operator = str(claim.get("operator") or "").strip().casefold()
    expected = claim.get("value")
    if metric and operator and expected is not None:
        actual_values = [
            value
            for item in evidence
            for key, value in _flatten_scalars(item)
            if _metric_matches(metric, key)
        ]
        if not actual_values:
            return "uncertain", f"No evidence value matched metric {metric}"
        verdicts = [_compare_claim(value, operator, expected) for value in actual_values]
        if any(verdict is True for verdict in verdicts):
            return "supported", f"Structured evidence supports {metric} {operator} {expected}"
        if any(verdict is False for verdict in verdicts):
            return "contradicted", f"Structured evidence contradicts {metric} {operator} {expected}"
        return "uncertain", f"Metric {metric} was present but not comparable"

    text = str(claim.get("text") or "").strip()
    evidence_text = " ".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        for item in evidence
    )
    claim_direction = _direction(text)
    evidence_direction = _direction(evidence_text)
    if claim_direction and evidence_direction and claim_direction != evidence_direction:
        return "contradicted", (
            f"Claim direction {claim_direction} conflicts with evidence direction {evidence_direction}"
        )
    if claim_direction and claim_direction == evidence_direction:
        return "supported", f"Evidence supports the claim direction {claim_direction}"
    normalized_claim = _semantic_tokens(text)
    normalized_evidence = _semantic_tokens(evidence_text)
    if normalized_claim and len(normalized_claim & normalized_evidence) >= min(
        3,
        max(1, len(normalized_claim)),
    ):
        return "supported", "Evidence text contains the material claim terms"
    if (
        str(claim.get("type") or "") == "completion_criterion"
        and _generic_completion_criterion(text)
        and any(
            item.get("ok") is True
            and (
                not isinstance(item.get("validation"), dict)
                or item["validation"].get("passed") is True
            )
            for item in evidence
        )
    ):
        return "supported", "Host-validated execution evidence satisfies the generic completion criterion"
    return "uncertain", "Evidence is bound but semantic support could not be proven"


def _flatten_scalars(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten_scalars(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            rows.extend(_flatten_scalars(item, f"{prefix}[{index}]"))
    elif isinstance(value, (str, int, float, bool)) or value is None:
        rows.append((prefix.casefold(), value))
    return rows


def _metric_matches(metric: str, path: str) -> bool:
    expected = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", metric.casefold())
    actual = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", path.casefold())
    return bool(expected) and (actual.endswith(expected) or expected in actual)


def _compare_claim(actual: Any, operator: str, expected: Any) -> bool | None:
    try:
        left = float(actual)
        right = float(expected)
    except (TypeError, ValueError):
        left = str(actual).strip().casefold()
        right = str(expected).strip().casefold()
    if operator in {"eq", "equals", "=="}:
        return left == right
    if operator in {"ne", "!=", "not_equals"}:
        return left != right
    if not isinstance(left, float) or not isinstance(right, float):
        return None
    if operator in {"gt", ">"}:
        return left > right
    if operator in {"gte", ">="}:
        return left >= right
    if operator in {"lt", "<"}:
        return left < right
    if operator in {"lte", "<="}:
        return left <= right
    return None


def _direction(text: str) -> str | None:
    lowered = text.casefold()
    positive = (
        "成長",
        "增加",
        "上升",
        "改善",
        "擴張",
        "growth",
        "increase",
        "improve",
        "positive",
    )
    negative = (
        "衰退",
        "下降",
        "減少",
        "惡化",
        "萎縮",
        "decline",
        "decrease",
        "negative",
        "deterior",
    )
    has_positive = any(value in lowered for value in positive)
    has_negative = any(value in lowered for value in negative)
    if has_positive == has_negative:
        numeric_direction = re.search(
            r"(?:yoy|年增|成長率|change|growth)[^0-9+\\-]{0,12}([+\\-]?\\d+(?:\\.\\d+)?)",
            lowered,
        )
        if numeric_direction:
            return "positive" if float(numeric_direction.group(1)) > 0 else "negative"
        return None
    return "positive" if has_positive else "negative"


def _semantic_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", text.casefold())
        if token not in {"結果", "資料", "目前", "evidence", "result", "true", "false"}
    }


def _generic_completion_criterion(text: str) -> bool:
    lowered = text.casefold()
    return any(
        phrase in lowered
        for phrase in (
            "objective is satisfied",
            "observation is recorded",
            "verified",
            "completed",
            "已完成",
            "已記錄",
            "已驗證",
            "測試通過",
            "tests pass",
            "test passed",
            "mutation completed",
            "state changed",
        )
    )


def _matches_json_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _contains_receipt(value: Any, keys: set[str]) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item is not None and item != "" and item is not False:
                return True
            if _contains_receipt(item, keys):
                return True
    elif isinstance(value, list):
        return any(_contains_receipt(item, keys) for item in value)
    return False


def _state_identity(value: dict[str, Any]) -> str:
    explicit = value.get("hash")
    if explicit not in {None, ""}:
        return str(explicit)
    # Layer validation does not need a cryptographic receipt; it only compares
    # two Host-captured snapshots. Canonical JSON avoids ordering artifacts.
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
