from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .plan_compiler import validate_json_value
from .completion_contract import evaluate_objective_completion, objective_completion_contract
from .measurement_parser import without_full_dates, without_indicator_windows
from .mutation_receipts import (
    build_domain_mutation_receipt,
    build_mutation_receipt,
    verify_domain_mutation_receipt,
    verify_mutation_receipt,
)
from .validation_layers import (
    EvidenceValidator,
    ExecutionValidator,
    ProtocolValidator,
    SemanticClaimValidator,
)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    passed: bool
    validator: str
    checks: tuple[dict[str, Any], ...]
    evidence_hash: str
    mutation_receipt: dict[str, Any] | None = None
    domain_mutation_receipt: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.validation_result.v1",
            "passed": self.passed,
            "validator": self.validator,
            "checks": list(self.checks),
            "evidence_hash": self.evidence_hash,
            "mutation_receipt": self.mutation_receipt,
            "domain_mutation_receipt": self.domain_mutation_receipt,
        }


def _material_numeric_claims(value: str) -> set[str]:
    """Extract numbers whose invention would materially change a user answer."""
    claims: set[str] = set()
    # Security identifiers identify the requested entity; they are not market
    # measurements and may originate directly from the user's objective.
    value = re.sub(r"\b\d{4,6}\.(?:TW|TWO)\b", "", value, flags=re.IGNORECASE)
    value = without_indicator_windows(without_full_dates(value))
    pattern = re.compile(
        r"(?P<prefix>[$＄])?(?<![A-Za-z0-9_])"
        r"(?P<number>[+\-]?\d[\d,]*(?:\.\d+)?)"
        r"(?P<suffix>[%％])?"
    )
    for match in pattern.finditer(value):
        raw = match.group("number").replace(",", "")
        try:
            number = float(raw)
        except ValueError:
            continue
        material = bool(
            match.group("prefix")
            or match.group("suffix")
            or "." in raw
            or abs(number) >= 20
        )
        if material:
            claims.add(f"{number:.12g}")
    return claims


def _final_summary_numeric_claims_check(
    *,
    final_summary: str | None,
    evidence_required: bool,
    evidence_ids: set[str],
    evidence_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Reject market numbers that cannot be found in the bound Host evidence."""
    bound_evidence = [
        evidence_catalog[evidence_id]
        for evidence_id in evidence_ids
        if evidence_id in evidence_catalog
    ]
    summary, count_claims = _research_history_count_claims(
        str(final_summary or ""), bound_evidence,
    )
    claims = _material_numeric_claims(summary)
    unsupported_counts = {str(item["count"]) for item in count_claims if not item["supported"]}
    claims.update(unsupported_counts)
    if not evidence_required or not claims:
        return {
            "name": "final_answer_numeric_claims_are_evidence_grounded",
            "passed": True,
            "measured_claims": sorted(claims),
            "unsupported_claims": [],
            "derived_history_count_claims": count_claims,
        }
    evidence_text = json.dumps(bound_evidence, ensure_ascii=False, sort_keys=True, default=str)
    supported_values = _material_numeric_claims(evidence_text)
    def supported_with_display_rounding(claim: str) -> bool:
        claim_value = float(claim)
        for supported in supported_values:
            supported_value = float(supported)
            tolerance = max(0.01, abs(supported_value) * 0.0005)
            if abs(abs(claim_value) - abs(supported_value)) <= tolerance:
                return True
        return False

    unsupported = sorted(
        (claim for claim in claims if claim in unsupported_counts or not supported_with_display_rounding(claim)),
        key=lambda item: float(item),
    )
    return {
        "name": "final_answer_numeric_claims_are_evidence_grounded",
        "passed": not unsupported,
        "measured_claims": sorted(claims, key=lambda item: float(item)),
        "unsupported_claims": unsupported,
        "derived_history_count_claims": count_claims,
    }


def _research_history_count_claims(
    summary: str, bound_evidence: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Bind explicit research-history counts without granting a numeric allowance.

    Remove only a supported count phrase from financial-number extraction. A
    second occurrence such as "price 60" must still have measurement evidence.
    """
    counts = {
        len(item["result"]["recent_history"])
        for item in bound_evidence
        if item.get("tool") == "market.research_pack"
        and item.get("ok") is True
        and (item.get("validation") or {}).get("passed") is True
        and isinstance(item.get("result"), dict)
        and isinstance(item["result"].get("recent_history"), list)
    }
    pattern = re.compile(
        r"研究包\s*(?:回傳|返回|提供|包含)\s*(?P<count>\d+)\s*(?:筆|點|個歷史點)|"
        r"\bresearch\s+pack\s+(?:returned|contains|provided)\s+(?P<english_count>\d+)\s+(?:rows?|history\s+points?)\b",
        re.IGNORECASE,
    )
    claims: list[dict[str, Any]] = []

    def replace(match: re.Match[str]) -> str:
        count = int(match.group("count") or match.group("english_count"))
        supported = count in counts
        claims.append({"count": count, "supported": supported, "source": "validated_recent_history_length"})
        return " " if supported else match.group(0)

    return pattern.sub(replace, summary), claims


_PERIOD_TOKEN = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*(?:[-/.年月])\s*"
    r"0?(?P<month>1[0-2]|[1-9])(?!\d)(?:月)?"
)
_PERIOD_FACT_KEYS = {
    "period",
    "fiscal_period",
    "reporting_period",
    "month",
    "trade_date",
    "date",
}


def _period_tokens(value: str) -> set[str]:
    return {
        f"{match.group('year')}-{int(match.group('month')):02d}"
        for match in _PERIOD_TOKEN.finditer(without_full_dates(value))
    }


def _period_fact_records(value: Any) -> list[dict[str, Any]]:
    """Return only atomic evidence rows explicitly keyed to one reporting period.

    A parent tool result can legitimately contain several months. Treating its
    complete JSON body as one evidence record would let a model cross-wire a
    July date with June's revenue. Only a mapping whose own period/date field
    identifies exactly one month is eligible to support a period-bound claim.
    """

    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        periods: set[str] = set()
        for key, item in value.items():
            if str(key).casefold() in _PERIOD_FACT_KEYS and isinstance(item, (str, int, float)):
                periods.update(_period_tokens(str(item)))
        if len(periods) == 1:
            records.append(
                {
                    "period": next(iter(periods)),
                    "measurements": _material_numeric_claims(
                        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                    ),
                }
            )
        for item in value.values():
            records.extend(_period_fact_records(item))
    elif isinstance(value, list):
        for item in value:
            records.extend(_period_fact_records(item))
    return records


def _final_summary_period_measurements_check(
    *,
    final_summary: str | None,
    evidence_required: bool,
    evidence_ids: set[str],
    evidence_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Ensure a dated market measurement stays bound to its Host fact row."""

    text = str(final_summary or "")
    if not evidence_required or not text:
        return {
            "name": "final_answer_period_measurements_are_evidence_bound",
            "passed": True,
            "checked_claims": [],
            "unsupported_claims": [],
        }
    records = [
        record
        for evidence_id in evidence_ids
        if evidence_id in evidence_catalog
        for record in _period_fact_records(evidence_catalog[evidence_id])
    ]

    def supported_measurement(value: str, candidates: set[str]) -> bool:
        claim_value = float(value)
        for candidate in candidates:
            candidate_value = float(candidate)
            tolerance = max(0.01, abs(candidate_value) * 0.0005)
            # The numeric parser cannot infer the polarity carried by prose
            # such as "下降 15%". Sign/polarity is checked by the semantic
            # layer; this check protects the period-to-value association.
            if abs(abs(claim_value) - abs(candidate_value)) <= tolerance:
                return True
        return False

    checked_claims: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    for sentence in (item.strip() for item in re.split(r"[\n。！？!?]+", text) if item.strip()):
        periods = _period_tokens(sentence)
        if len(periods) != 1:
            continue
        period = next(iter(periods))
        measurements = _material_numeric_claims(sentence) - {period.split("-", 1)[0]}
        if not measurements:
            continue
        candidates = [item for item in records if item["period"] == period]
        passed = any(
            all(supported_measurement(value, candidate["measurements"]) for value in measurements)
            for candidate in candidates
        )
        claim = {
            "period": period,
            "measurements": sorted(measurements, key=float),
            "sentence": sentence,
        }
        checked_claims.append(claim)
        if not passed:
            unsupported.append(claim)
    return {
        "name": "final_answer_period_measurements_are_evidence_bound",
        "passed": not unsupported,
        "checked_claims": checked_claims,
        "unsupported_claims": unsupported,
    }


def _final_summary_placeholder_measurements_check(
    *,
    final_summary: str | None,
    evidence_required: bool,
) -> dict[str, Any]:
    """Reject fluent-looking placeholders where a verified market value belongs.

    Local models occasionally render unavailable values as ``X%`` or ``EPS of
    Y``.  They are not numeric claims, so numeric grounding alone cannot catch
    them, yet presenting them as a final answer would still be misleading.
    This is intentionally limited to common financial/market measurement
    contexts and only applies when the answer requires Host evidence.
    """
    text = str(final_summary or "")
    placeholders: set[str] = set()
    compact_value = re.compile(r"(?<![A-Za-z0-9_])(?:x|y|tbd|tba|n/?a)\s*(?:%|％|元|倍|points?)", re.IGNORECASE)
    contextual_value = re.compile(
        r"\b(?:eps|revenue|earnings|price|close|rsi|macd|margin|growth|"
        r"yield|p/?e|營收|盈餘|每股盈餘|股價|收盤|漲跌|成長率|殖利率|本益比)\b"
        r"[^\n]{0,36}?\b(?:x|y|tbd|tba|n/?a)\b",
        re.IGNORECASE,
    )
    for match in compact_value.finditer(text):
        placeholders.add(match.group(0))
    for match in contextual_value.finditer(text):
        placeholders.add(match.group(0))
    return {
        "name": "final_answer_has_no_placeholder_measurements",
        "passed": not evidence_required or not placeholders,
        "placeholders": sorted(placeholders),
    }


def _final_summary_scope_check(
    *,
    objective: str | None,
    task_kind: str | None,
    final_summary: str | None,
    evidence_required: bool,
) -> dict[str, Any]:
    """Reject a heading or preface masquerading as a multi-part final answer.

    The threshold is derived from the user's own objective length and explicit
    list structure.  It does not prescribe stock-specific sections or a fixed
    answer template; short questions remain free to receive short answers.
    """

    objective_text = " ".join(str(objective or "").split())
    answer = str(final_summary or "").strip()
    explicit_parts = [
        part.strip()
        for part in re.split(r"[、,，;；]|(?:以及|並且|and)\s+", objective_text, flags=re.IGNORECASE)
        if part.strip()
    ]
    market_scope = str(task_kind or "") in {
        "market_information", "market_decision", "market_radar", "current_information",
    }
    # A paper-order objective is bounded by its Host-verified preview and
    # durable execution receipt.  It is not a long-form research request just
    # because it names a symbol, asks for analysis, and requests a simulation.
    # Applying the long-form threshold here made completed routine runs loop
    # whenever the provider returned a concise acknowledgement.
    routine_paper_execution = objective_completion_contract(
        objective_text, task_kind
    )["paper_order_requested"]
    comprehensive = evidence_required and market_scope and not routine_paper_execution and (
        len(objective_text) >= 48 or len(explicit_parts) >= 4
    )
    min_characters = (
        max(120, min(260, len(objective_text) * 2))
        if comprehensive
        else 1
    )
    content_units = [
        item.strip()
        for item in re.split(r"[\n。！？!?]+", answer)
        if item.strip()
    ]
    min_content_units = 3 if comprehensive else 1
    normalized_answer = " ".join(answer.casefold().split())
    # A planning acknowledgement (for example "Requesting technical analysis")
    # has no user-facing conclusion.  It must not pass merely because a short
    # market question otherwise has a one-character minimum.  Keep this rule
    # deliberately narrow so normal concise answers such as "資料不足，暫不判讀"
    # remain valid.
    progress_only = (
        len(answer) < 180
        and (
            normalized_answer.startswith("requesting ")
            or normalized_answer.startswith("please provide ")
            or normalized_answer.startswith("請提供")
            or normalized_answer.startswith("正在請求")
            or normalized_answer.startswith("將請求")
        )
    )
    passed = (
        not comprehensive
        or (len(answer) >= min_characters and len(content_units) >= min_content_units)
    ) and not progress_only
    return {
        "name": "final_answer_covers_requested_scope",
        "passed": passed,
        "task_kind": str(task_kind or ""),
        "comprehensive_request": comprehensive,
        "routine_paper_execution": routine_paper_execution,
        "explicit_requirement_parts": len(explicit_parts),
        "answer_characters": len(answer),
        "minimum_characters": min_characters,
        "content_units": len(content_units),
        "minimum_content_units": min_content_units,
        "progress_only": progress_only,
    }


_MARKET_EVIDENCE_DIMENSIONS: tuple[tuple[str, tuple[str, ...], str, tuple[str, ...]], ...] = (
    (
        "portfolio",
        ("持股", "部位", "投資組合", "portfolio", "position"),
        "portfolio.snapshot or market.analyze_symbol",
        (),
    ),
    (
        "technical",
        ("技術", "均線", "rsi", "macd", "technical"),
        "market.research_pack or market.analyze_symbol",
        (),
    ),
    (
        "fundamental",
        ("基本面", "財務", "營收", "fundamental", "revenue"),
        "market.monthly_revenue",
        ("market.monthly_revenue",),
    ),
    (
        "institutional",
        ("法人", "外資", "投信", "自營商", "institutional"),
        "market.institutional_flow",
        ("market.institutional_flow",),
    ),
    (
        "external_research",
        ("外部研究", "新聞", "外部來源", "external research", "news"),
        "web.research or validated external framework evidence",
        ("web.research",),
    ),
    (
        "risk",
        ("風險", "反方", "risk", "critic", "counter"),
        "market.analyze_symbol",
        (),
    ),
    (
        "independent_critic",
        (
            "獨立 critic",
            "critic branch",
            "critic 分支",
            "批判分支",
            "獨立驗證代理",
            "verification branch",
            "conflict branch",
            "衝突分支",
        ),
        "agent.run_subtasks with role=critic",
        ("agent.run_subtasks",),
    ),
)


def _has_completed_critic_receipt(result: dict[str, Any]) -> bool:
    """Return whether a Critic receipt contains a completed child Run.

    Calling ``agent.run_subtasks`` proves only that a child was requested. A
    parent may present the independent Critic as completed only after at least
    one joined child result reaches the terminal ``completed`` state.
    """

    if str(result.get("role") or "").casefold() != "critic":
        return False
    items = result.get("items")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict)
        and isinstance(item.get("result"), dict)
        and str(item["result"].get("status") or "").casefold() == "completed"
        for item in items
    )


def requested_market_evidence_requirements(
    *,
    objective: str | None,
    task_kind: str | None,
) -> dict[str, Any]:
    """Derive the one Host-owned evidence contract used throughout a Run.

    Capability filtering, deterministic Host dispatch and final validation must
    share this interpretation. Keeping three separate keyword lists caused a
    paper Run to hide a required read-only tool, then discover the same missing
    evidence only after a paper receipt was already created.
    """
    market_scope = str(task_kind or "") in {
        "market_information",
        "market_decision",
        "market_radar",
    }
    objective_text = str(objective or "").casefold()
    requested_dimensions = [
        name
        for name, terms, _required_tool, _capability_names in _MARKET_EVIDENCE_DIMENSIONS
        if any(term in objective_text for term in terms)
    ]
    required_tool_by_dimension = {
        name: required_tool
        for name, _terms, required_tool, _capability_names in _MARKET_EVIDENCE_DIMENSIONS
    }
    required_capability_names = [
        capability
        for name, _terms, _required_tool, capabilities in _MARKET_EVIDENCE_DIMENSIONS
        if name in requested_dimensions
        for capability in capabilities
    ]
    return {
        "market_scope": market_scope,
        "requested_dimensions": requested_dimensions,
        "required_tools": [required_tool_by_dimension[name] for name in requested_dimensions],
        "required_capability_names": list(dict.fromkeys(required_capability_names)),
    }


def _requested_market_evidence_coverage_check(
    *,
    objective: str | None,
    task_kind: str | None,
    evidence_required: bool,
    evidence_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Require distinct Host evidence for market dimensions named by the user."""

    requirements = requested_market_evidence_requirements(
        objective=objective,
        task_kind=task_kind,
    )
    observations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence_catalog.values():
        tool = str(item.get("tool") or item.get("name") or "")
        call_id = str(item.get("call_id") or item.get("node_id") or id(item))
        identity = (tool, call_id)
        if identity in seen:
            continue
        seen.add(identity)
        observations.append(item)

    tools = {str(item.get("tool") or item.get("name") or "") for item in observations}
    results = [item.get("result") for item in observations if isinstance(item.get("result"), dict)]
    result_text = json.dumps(results, ensure_ascii=False, sort_keys=True, default=str).casefold()

    covered = {
        "portfolio": (
            "portfolio.snapshot" in tools
            or ("autonomy.status" in tools and any(isinstance(result.get("account"), dict) for result in results))
            or any("portfolio_status" in result or "paper_position" in result for result in results)
        ),
        "technical": (
            (bool(tools & {"market.research_pack", "market.analyze_symbol"})
             and any("technical_features" in result or "signal_summary" in result or "pipeline_workspace" in result
                     for result in results))
            or ("autonomy.evidence" in tools and any(
                result.get("kind") == "price_history"
                or result.get("schema_version") == "open_stock_ai.autonomy_evidence_batch.v1"
                for result in results))
            or ("autonomy.research" in tools and any(isinstance(result.get("results"), list) and result["results"]
                                                       for result in results))
        ),
        "fundamental": "market.monthly_revenue" in tools,
        "institutional": "market.institutional_flow" in tools,
        "external_research": (
            bool(tools & {"web.fetch", "web.research"})
            or any(tool.startswith("external.") for tool in tools)
            or any(marker in result_text for marker in ("fingpt", "finrobot", "tradingagents"))
        ),
        "risk": (
            "market.analyze_symbol" in tools
            and any("risk_summary" in result or "pipeline_workspace" in result for result in results)
        ),
        "independent_critic": (
            "agent.run_subtasks" in tools
            and any(
                _has_completed_critic_receipt(result)
                for result in results
            )
        ),
    }
    requested_dimensions = list(requirements["requested_dimensions"])
    missing = [name for name in requested_dimensions if not covered[name]]
    required_tools = {
        name: required_tool
        for name, _terms, required_tool, _capability_names in _MARKET_EVIDENCE_DIMENSIONS
    }
    # A single topic such as "latest risk" must remain answerable by one
    # authoritative capability.  This cross-source gate is reserved for
    # explicitly multi-dimensional requests where silently collapsing several
    # requested perspectives into one generic analysis would be misleading.
    applies = evidence_required and requirements["market_scope"] and len(requested_dimensions) >= 2
    return {
        "name": "requested_market_evidence_coverage",
        "passed": not applies or not missing,
        "applies": applies,
        "requested_dimensions": requested_dimensions,
        "covered_dimensions": [name for name, present in covered.items() if present],
        "missing_dimensions": missing,
        "required_tools": [required_tools[name] for name in missing],
    }


class ValidatorEngine:
    """Host-owned validation. Provider text can never mark a capability successful."""

    def __init__(self) -> None:
        self.protocol_validator = ProtocolValidator()
        self.execution_validator = ExecutionValidator()
        self.evidence_validator = EvidenceValidator()
        self.semantic_validator = SemanticClaimValidator()

    def validate_tool_result(
        self,
        *,
        tool: dict[str, Any],
        arguments: dict[str, Any],
        result: Any,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        postconditions: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    ) -> ValidationResult:
        checks: list[dict[str, Any]] = []
        output_schema = tool.get("output_schema") or {"type": "object"}
        protocol_report = self.protocol_validator.validate(result, output_schema)
        execution_report = self.execution_validator.validate(
            result,
            mutation_expected=bool(tool.get("mutating")),
            before=before,
            after=after,
        )
        checks.extend(
            (
                _layer_check(protocol_report),
                _layer_check(execution_report),
            )
        )
        checks.append(
            {
                "name": "result_is_object",
                "passed": isinstance(result, dict),
            }
        )
        schema_errors = validate_json_value(result, output_schema) if isinstance(result, dict) else []
        checks.append(
            {
                "name": "output_schema",
                "passed": not schema_errors,
                "errors": schema_errors,
            }
        )
        if isinstance(result, dict):
            semantic_failure = (
                result.get("ok") is False
                or result.get("success") is False
                or str(result.get("status") or "").casefold() in {"failed", "error", "rejected"}
            )
            checks.append({"name": "semantic_status", "passed": not semantic_failure})
        if tool.get("mutating"):
            before_hash = str((before or {}).get("hash") or _hash(before or {}))
            after_hash = str((after or {}).get("hash") or _hash(after or {}))
            state_changed = bool(before is not None and after is not None and before_hash != after_hash)
            host_receipt = _mutation_receipt(
                name=str(tool.get("name") or ""),
                arguments=arguments,
                result=result,
            )
            if str(tool.get("name") or "") in {"autonomy.activate", "autonomy.manage", "autonomy.propose_plan", "autonomy.close_plan"}:
                checks.append({"name": "campaign_mutation_scope_receipt", "passed": host_receipt})
            checks.append(
                {
                    "name": "mutation_before_after_evidence",
                    "passed": state_changed or host_receipt,
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                    "state_changed": state_changed,
                    "host_receipt": host_receipt,
                }
            )
            mutation_receipt = build_mutation_receipt(
                name=str(tool.get("name") or ""),
                arguments=arguments,
                result=result,
                before=before,
                after=after,
                accepted=state_changed or host_receipt,
            )
            checks.append(
                {
                    "name": "content_addressed_mutation_receipt",
                    "passed": True,
                    "receipt_sha256": mutation_receipt["receipt_sha256"],
                    "authority": "host_validator",
                }
            )
            domain_mutation_receipt = build_domain_mutation_receipt(
                provider=str(tool.get("provider") or "host_runtime"),
                tool=tool,
                mutation_receipt=mutation_receipt,
            )
            checks.append(
                {
                    "name": "content_addressed_domain_mutation_receipt",
                    "passed": verify_domain_mutation_receipt(
                        domain_mutation_receipt,
                        mutation_receipt=mutation_receipt,
                    ),
                    "receipt_sha256": domain_mutation_receipt["receipt_sha256"],
                    "provider": domain_mutation_receipt["provider"],
                }
            )
        else:
            mutation_receipt = None
            domain_mutation_receipt = None
        if tool.get("rollback_support"):
            rollback_token = result.get("rollback_token") if isinstance(result, dict) else None
            checks.append(
                {
                    "name": "declared_rollback_support_has_host_token",
                    "passed": isinstance(rollback_token, str) and len(rollback_token) >= 10,
                }
            )
        if str(tool.get("name") or "").startswith("project.") and tool.get("mutating"):
            before_digest = result.get("before_sha256") if isinstance(result, dict) else None
            after_digest = result.get("after_sha256") if isinstance(result, dict) else None
            project_name = str(tool.get("name") or "")
            checks.append(
                {
                    "name": "project_mutation_hash_transition",
                    "passed": _valid_project_transition(
                        name=project_name,
                        result=result if isinstance(result, dict) else {},
                    ),
                    "before_sha256": before_digest,
                    "after_sha256": after_digest,
                }
            )
        checks.extend(
            _tool_specific_checks(
                name=str(tool.get("name") or ""),
                arguments=arguments,
                result=result,
                before=before or {},
                after=after or {},
            )
        )
        for condition in [*(tool.get("postconditions") or []), *postconditions]:
            if isinstance(condition, dict):
                checks.append(
                    _check_postcondition(
                        result,
                        condition,
                        mutation_receipt=mutation_receipt,
                    )
                )
        passed = all(bool(item.get("passed")) for item in checks)
        return ValidationResult(
            passed,
            str(tool.get("validator") or "default"),
            tuple(checks),
            _hash(result),
            mutation_receipt,
            domain_mutation_receipt,
        )

    def validate_completion(
        self,
        *,
        state: str,
        objective: str | None = None,
        task_kind: str | None = None,
        final_summary: str | None = None,
        has_pending_tool_calls: bool,
        plan: dict[str, Any],
        successful_observations: int,
        evidence_required: bool,
        completion_evaluation: dict[str, Any] | None = None,
        known_evidence_ids: set[str] | None = None,
        evidence_catalog: dict[str, dict[str, Any]] | None = None,
        failure_recovery_coverage: dict[str, list[str]] | None = None,
        decision: dict[str, Any] | None = None,
    ) -> ValidationResult:
        nodes = plan.get("nodes") or []
        terminal_statuses = {"completed", "skipped", "failed", "blocked", "cancelled"}
        unfinished = [
            str(item.get("node_id") or "")
            for item in nodes
            if item.get("status") not in terminal_statuses
            and item.get("node_type") != "finalize"
        ]
        failed_nodes = [
            str(item.get("node_id") or "")
            for item in nodes
            if item.get("status") in {"failed", "blocked", "cancelled"}
            and item.get("node_type") != "finalize"
        ]
        finalize_nodes = [item for item in nodes if item.get("node_type") == "finalize"]
        completed_ids = {
            str(item.get("node_id") or "")
            for item in nodes
            if item.get("status") in {"completed", "skipped"}
        }
        finalize_blocked = [
            str(item.get("node_id") or "")
            for item in finalize_nodes
            if any(str(dependency) not in completed_ids for dependency in item.get("dependencies") or [])
        ]
        evaluation = completion_evaluation or {}
        evidence_ids = {
            str(item) for item in evaluation.get("evidence_ids") or [] if str(item).strip()
        }
        unknown_evidence = sorted(evidence_ids - set(known_evidence_ids or set()))
        remaining_gaps = [
            str(item) for item in evaluation.get("remaining_gaps") or [] if str(item).strip()
        ]
        criteria = [str(item) for item in plan.get("completion_criteria") or [] if str(item).strip()]
        criterion_results = [
            item for item in evaluation.get("criterion_results") or [] if isinstance(item, dict)
        ]
        criterion_by_text = {
            str(item.get("criterion") or ""): item
            for item in criterion_results
            if str(item.get("criterion") or "")
        }
        custom_criteria = criteria != ["The user objective is satisfied by validated evidence."]
        grounded_criteria_required = custom_criteria or evidence_required
        layer_evidence = [
            {
                **item,
                "evidence_id": evidence_id,
            }
            for evidence_id, item in (evidence_catalog or {}).items()
        ]
        evidence_report = self.evidence_validator.validate(layer_evidence)
        semantic_report = self.semantic_validator.validate(
            [
                {
                    "text": str(item.get("criterion") or ""),
                    "type": "completion_criterion",
                    "evidence_ids": list(item.get("evidence_ids") or []),
                }
                for item in criterion_results
            ],
            layer_evidence,
        )
        criterion_checks = [
            _criterion_check(
                criterion,
                criterion_by_text.get(criterion),
                known_evidence_ids=known_evidence_ids or set(),
                evidence_catalog=evidence_catalog or {},
                required=grounded_criteria_required,
            )
            for criterion in criteria
        ]
        criteria_grounded = all(item["passed"] for item in criterion_checks)
        # A failed branch must never be waived merely because some unrelated
        # tool happened to succeed.  The Host has to record an explicit link
        # from every failed Plan node to validated evidence produced by a
        # distinct recovery action (P1/P40/P77/P78).  This is deliberately a
        # capability-independent receipt: the relationship is established by
        # the orchestrator, not by model prose or a tool-name heuristic here.
        recovery_coverage = {
            str(node_id): [str(evidence_id) for evidence_id in evidence_ids if str(evidence_id)]
            for node_id, evidence_ids in (failure_recovery_coverage or {}).items()
        }
        recovered_failed_nodes = {
            node_id
            for node_id in failed_nodes
            if recovery_coverage.get(node_id)
            and all(
                evidence_id in (known_evidence_ids or set())
                for evidence_id in recovery_coverage[node_id]
            )
        }
        unresolved_failed_nodes = [
            node_id for node_id in failed_nodes if node_id not in recovered_failed_nodes
        ]
        failed_nodes_are_nonblocking = not unresolved_failed_nodes
        semantic_hard_failure_codes = {
            "semantic_claim_contradicted",
            "semantic_claim_missing_evidence",
            "semantic_claim_without_evidence",
        }
        semantic_hard_failures = [
            issue.code
            for issue in semantic_report.issues
            if issue.code in semantic_hard_failure_codes
        ]
        advisory_uncertain_entailment_accepted = (
            grounded_criteria_required
            and not semantic_report.valid
            and not semantic_hard_failures
            and criteria_grounded
            and evidence_report.valid
        )
        summary_text = " ".join(str(final_summary or "").split()).casefold()
        placeholder_summaries = {
            "no further action required.",
            "no further action required",
            "model turn completed.",
            "model turn completed",
            "agent produced no decision.",
            "agent produced no decision",
        }
        meaningful_final_summary = (
            final_summary is None
            or (bool(summary_text) and summary_text not in placeholder_summaries)
        )
        numeric_summary_check = _final_summary_numeric_claims_check(
            final_summary=final_summary,
            evidence_required=evidence_required,
            evidence_ids=evidence_ids,
            evidence_catalog=evidence_catalog or {},
        )
        period_measurement_check = _final_summary_period_measurements_check(
            final_summary=final_summary,
            evidence_required=evidence_required,
            evidence_ids=evidence_ids,
            evidence_catalog=evidence_catalog or {},
        )
        placeholder_measurement_check = _final_summary_placeholder_measurements_check(
            final_summary=final_summary,
            evidence_required=evidence_required,
        )
        scope_check = _final_summary_scope_check(
            objective=objective,
            task_kind=task_kind,
            final_summary=final_summary,
            evidence_required=evidence_required,
        )
        market_coverage_check = _requested_market_evidence_coverage_check(
            objective=objective,
            task_kind=task_kind,
            evidence_required=evidence_required,
            evidence_catalog=evidence_catalog or {},
        )
        objective_contract_check = evaluate_objective_completion(
            objective=str(objective or ""),
            task_kind=task_kind,
            observations=(evidence_catalog or {}).values(),
            decision=decision,
        )
        from .autonomy_claims import campaign_summary_claims_check
        campaign_claims = campaign_summary_claims_check(objective=str(objective or ""), task_kind=task_kind,
            final_summary=final_summary, observations=[row for key, row in (evidence_catalog or {}).items() if key in evidence_ids])
        checks = (
            _layer_check(evidence_report),
            {
                **_layer_check(semantic_report),
                # Stable/general reasoning is permitted without external evidence.
                # For evidence-backed criteria, a lexical "not entailed" result is
                # advisory when every criterion is explicitly bound to valid,
                # Host-known evidence. Contradictions and missing evidence remain
                # hard failures.
                "passed": (
                    semantic_report.valid
                    or not grounded_criteria_required
                    or advisory_uncertain_entailment_accepted
                ),
                "advisory_uncertain_entailment_accepted": (
                    advisory_uncertain_entailment_accepted
                ),
                "hard_failure_codes": semantic_hard_failures,
            },
            {"name": "provider_requested_completion", "passed": state == "complete"},
            {
                "name": "final_answer_is_meaningful",
                "passed": meaningful_final_summary,
                "measured": final_summary is not None,
            },
            numeric_summary_check,
            campaign_claims,
            period_measurement_check,
            placeholder_measurement_check,
            scope_check,
            market_coverage_check,
            objective_contract_check,
            {"name": "no_pending_tool_calls", "passed": not has_pending_tool_calls},
            {"name": "plan_nodes_finished", "passed": not unfinished, "unfinished": unfinished},
            {
                "name": "failed_plan_nodes_are_nonblocking",
                "passed": failed_nodes_are_nonblocking,
                "failed_nodes": failed_nodes,
                "recovered_failed_nodes": sorted(recovered_failed_nodes),
                "unresolved_failed_nodes": unresolved_failed_nodes,
                "recovery_coverage": recovery_coverage,
                "covered_by_alternative_host_evidence": bool(failed_nodes)
                and failed_nodes_are_nonblocking,
            },
            {
                "name": "finalize_dependencies_satisfied",
                "passed": not finalize_blocked,
                "blocked": finalize_blocked,
            },
            {
                "name": "completion_criteria_declared",
                "passed": bool(plan.get("completion_criteria")),
            },
            {
                "name": "provider_completion_criteria_met",
                "passed": evaluation.get("criteria_met") is True,
            },
            {
                "name": "provider_reported_no_remaining_gaps",
                "passed": not remaining_gaps,
                "remaining_gaps": remaining_gaps,
            },
            {
                "name": "completion_evidence_ids_are_host_known",
                "passed": not unknown_evidence,
                "unknown_evidence_ids": unknown_evidence,
            },
            {
                "name": "validated_observation_present",
                "passed": successful_observations > 0 or not evidence_required,
            },
            {
                "name": "completion_criteria_have_host_grounded_results",
                "passed": criteria_grounded,
                "criteria": criterion_checks,
            },
        )
        return ValidationResult(all(item["passed"] for item in checks), "completion_evaluator", checks, _hash(plan))

    def validate_plan_node(
        self,
        *,
        node: dict[str, Any],
        dependency_evidence: list[dict[str, Any]],
    ) -> ValidationResult:
        missing = [
            dependency
            for dependency in node.get("dependencies") or []
            if not any(item.get("node_id") == dependency and item.get("ok") is True for item in dependency_evidence)
        ]
        checks: list[dict[str, Any]] = [
            {
                "name": "validation_node_dependencies_have_success_evidence",
                "passed": not missing,
                "missing": missing,
            }
        ]
        for condition in node.get("postconditions") or []:
            target = next(
                (
                    item.get("result")
                    for item in reversed(dependency_evidence)
                    if item.get("ok") is True and isinstance(item.get("result"), dict)
                ),
                {},
            )
            if isinstance(condition, dict):
                checks.append(_check_postcondition(target, condition))
        return ValidationResult(
            all(bool(item.get("passed")) for item in checks),
            "plan_node_validator",
            tuple(checks),
            _hash(dependency_evidence),
        )


def _layer_check(report: Any) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    return {
        "name": f"{payload['layer']}_validation_layer",
        "passed": bool(payload["valid"]),
        "layer": payload["layer"],
        "issues": payload["issues"],
        "metadata": payload["metadata"],
    }


def _check_postcondition(
    result: Any,
    condition: dict[str, Any],
    *,
    mutation_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if condition.get("predicate") == "host_validated_mutation":
        return {
            "name": "postcondition:host_validated_mutation",
            "passed": (
                isinstance(mutation_receipt, dict)
                and mutation_receipt.get("accepted") is True
                and verify_mutation_receipt(mutation_receipt)
            ),
            "authority": "host_validator",
            "receipt_sha256": (
                mutation_receipt.get("receipt_sha256")
                if isinstance(mutation_receipt, dict)
                else None
            ),
        }
    path = str(condition.get("path") or "")
    expected = condition.get("equals", True)
    current = result
    for segment in path.split(".") if path else []:
        if not isinstance(current, dict) or segment not in current:
            return {"name": f"postcondition:{path}", "passed": False, "reason": "path_missing"}
        current = current[segment]
    return {
        "name": f"postcondition:{path or '$'}",
        "passed": current == expected,
        "expected": expected,
        "actual": current,
    }


def _mutation_receipt(*, name: str, arguments: dict[str, Any], result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if name in {"autonomy.activate", "autonomy.manage", "autonomy.propose_plan", "autonomy.close_plan"}:
        from .autonomy_contract import valid_campaign_mutation
        return valid_campaign_mutation(name, arguments, result)
    if name == "terminal.run":
        return result.get("exit_code") == 0 and result.get("timed_out") is not True
    if name.startswith("project."):
        if name == "project.rollback_change":
            return result.get("confirmed") is True and bool(result.get("path") or result.get("paths"))
        return _valid_project_transition(name=name, result=result)
    if name.startswith("ui."):
        acknowledgement = result.get("acknowledgement")
        return isinstance(acknowledgement, dict) and acknowledgement.get("ok") is True
    if name.startswith("browser."):
        return bool(result.get("url") or result.get("page") or result.get("title"))
    if name == "paper.submit_order":
        order = _paper_order(result)
        return bool(order.get("order_id")) and str(order.get("status") or "") not in {
            "",
            "failed",
            "error",
            "rejected",
        }
    if name == "paper.mark_to_market":
        return (
            result.get("error_count") == 0
            and isinstance(result.get("account"), dict)
            and isinstance(result.get("order_updates"), list)
        )
    if name in {"memory.write", "memory.update"}:
        return bool(result.get("memory_id")) and result.get("content") == arguments.get("content")
    if name == "memory.archive":
        return result.get("confirmed") is True and result.get("memory_id") == arguments.get("memory_id")
    if name == "workflow.save_current":
        return bool(result.get("workflow_id")) and result.get("name") == arguments.get("name")
    if name in {"artifact.create_text", "artifact.create_structured"}:
        return bool(result.get("artifact_id")) and _is_sha256(result.get("sha256"))
    if name == "git.create_branch":
        return (
            result.get("exit_code") == 0
            and result.get("branch") == arguments.get("name")
            and _is_git_oid(result.get("after_sha256"))
        )
    if name == "git.commit":
        return (
            result.get("exit_code") == 0
            and _is_git_oid(result.get("before_sha256"))
            and _is_git_oid(result.get("after_sha256"))
            and result.get("before_sha256") != result.get("after_sha256")
        )
    if name == "git.restore":
        return result.get("exit_code") == 0 and result.get("confirmed") is True
    if name == "notifications.send":
        requested = [str(item) for item in arguments.get("channels") or []]
        items = result.get("items")
        if not requested or not isinstance(items, list):
            return False
        delivered = {str(item.get("channel")): item for item in items if isinstance(item, dict)}
        if any(channel not in delivered for channel in requested):
            return False
        if arguments.get("dry_run", True):
            return (
                result.get("dry_run") is True
                and all(delivered[channel].get("mode") == "dry_run" for channel in requested)
            )
        return result.get("sent_count") == len(requested) and all(
            delivered[channel].get("sent") is True for channel in requested
        )
    if name.startswith("schedule."):
        schedule_id = result.get("schedule_id")
        if not isinstance(schedule_id, str) or not schedule_id:
            return False
        if name != "schedule.create" and schedule_id != arguments.get("schedule_id"):
            return False
        if name == "schedule.cancel":
            return result.get("enabled") is False
        if name == "schedule.resume":
            return result.get("enabled") is True
        return True
    if name.startswith("external."):
        return (
            result.get("external_module_loaded") is True
            and result.get("module_under_locked_path") is True
            and _is_sha256(result.get("module_sha256"))
            and isinstance(result.get("result"), dict)
        )
    return (
        result.get("mutation_performed") is True
        and isinstance(result.get("receipt"), dict)
        and bool(result["receipt"])
    )


def _tool_specific_checks(
    *,
    name: str,
    arguments: dict[str, Any],
    result: Any,
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    checks: list[dict[str, Any]] = []
    if name == "terminal.run":
        checks.append(
            {
                "name": "terminal_process_succeeded",
                "passed": result.get("exit_code") == 0 and result.get("timed_out") is not True,
                "exit_code": result.get("exit_code"),
                "timed_out": result.get("timed_out"),
            }
        )
    if name == "project.read_file":
        digest = result.get("sha256")
        checks.append(
            {
                "name": "project_read_has_content_digest",
                "passed": isinstance(digest, str) and len(digest) == 64,
            }
        )
    if name in {"project.write_file", "project.replace_text"}:
        syntax_validation = result.get("syntax_validation")
        checks.append(
            {
                "name": "project_candidate_syntax_validated",
                "passed": (
                    isinstance(syntax_validation, dict)
                    and syntax_validation.get("passed") is True
                ),
                "syntax_validation": syntax_validation,
            }
        )
    if name.startswith("ui.") and name not in {"ui.get_state", "ui.wait_for_state"}:
        acknowledgement = result.get("acknowledgement") or {}
        checks.append(
            {
                "name": "ui_command_acknowledged",
                "passed": isinstance(acknowledgement, dict) and acknowledgement.get("ok") is True,
            }
        )
        expected_key = {
            "ui.navigate": ("current_view", arguments.get("view")),
            "ui.select_symbol": ("current_symbol", arguments.get("symbol")),
        }.get(name)
        if expected_key is not None and isinstance(after.get("ui"), dict):
            key, expected = expected_key
            current = ((after.get("ui") or {}).get("state") or {}).get(key)
            checks.append(
                {
                    "name": f"ui_post_state:{key}",
                    "passed": current == expected,
                    "expected": expected,
                    "actual": current,
                }
            )
    if name == "paper.submit_order":
        order = _paper_order(result)
        order_id = order.get("order_id")
        expected_symbol = str(arguments.get("symbol") or "").upper()
        actual_symbol = str(order.get("symbol") or "").upper()
        expected_side = str(arguments.get("side") or "").casefold()
        if expected_side in {"add", "reduce"}:
            expected_side = {"add": "buy", "reduce": "sell"}[expected_side]
        actual_side = str(order.get("side") or order.get("action") or "").casefold()
        account_orders = [
            item
            for key in ("open_orders", "recent_orders", "orders")
            for item in ((result.get("account") or {}).get(key) or [])
            if isinstance(item, dict)
        ]
        checks.append(
            {
                "name": "paper_order_persisted",
                "passed": bool(order_id)
                and str(order.get("status") or "") not in {"", "failed", "error", "rejected"}
                and (not expected_symbol or actual_symbol == expected_symbol)
                and (not expected_side or actual_side == expected_side)
                and any(item.get("order_id") == order_id for item in account_orders),
                "order_id": order_id,
                "expected_symbol": expected_symbol,
                "actual_symbol": actual_symbol,
                "expected_side": expected_side,
                "actual_side": actual_side,
            }
        )
    if name == "paper.mark_to_market":
        checks.append(
            {
                "name": "paper_marks_persisted_without_errors",
                "passed": result.get("error_count") == 0 and isinstance(result.get("account"), dict),
                "error_count": result.get("error_count"),
            }
        )
    if name.startswith("browser.") and name in {"browser.click", "browser.fill"}:
        checks.append(
            {
                "name": "browser_action_has_page_state",
                "passed": bool(result.get("url") or result.get("page") or result.get("title")),
            }
        )
    if name == "web.fetch":
        status_code = result.get("status_code")
        content = str(
            result.get("content") or result.get("text") or result.get("title") or ""
        ).strip()
        page_text = " ".join(
            str(result.get(key) or "")
            for key in ("title", "content", "final_url", "requested_url")
        ).casefold()
        checks.append(
            {
                "name": "web_fetch_returned_a_readable_page",
                "passed": (
                    (status_code is None or (isinstance(status_code, int) and 200 <= status_code < 400))
                    and bool(content)
                    and not any(
                        marker in page_text
                        for marker in ("page-not-found", "404 not found", "page not found")
                    )
                ),
                "status_code": status_code,
                "final_url": result.get("final_url"),
            }
        )
    if name == "web.research":
        sources = [item for item in result.get("sources") or [] if isinstance(item, dict)]
        native_research = result.get("schema_version") == "open_stock_ai.web_research.v1"
        checks.append(
            {
                "name": "web_research_opened_at_least_one_source",
                # Generic provider receipts are retained for framework
                # compatibility; the canonical research receipt must expose
                # an actual opened source.
                "passed": (not native_research) or int(result.get("source_count") or len(sources)) > 0,
                "source_count": int(result.get("source_count") or len(sources)),
                "canonical_receipt": native_research,
            }
        )
    if name.startswith("schedule."):
        checks.append(
            {
                "name": "schedule_persisted",
                "passed": (
                    isinstance(result.get("schedule_id"), str)
                    and bool(result.get("schedule_id"))
                    and (
                        name == "schedule.create"
                        or result.get("schedule_id") == arguments.get("schedule_id")
                    )
                ),
            }
        )
    if name == "notifications.send":
        checks.append(
            {
                "name": "notification_delivery_receipts_match_request",
                "passed": _mutation_receipt(name=name, arguments=arguments, result=result),
            }
        )
    if name.startswith("external."):
        checks.append(
            {
                "name": "external_execution_has_locked_module_evidence",
                "passed": _mutation_receipt(name=name, arguments=arguments, result=result),
            }
        )
    return checks


def _paper_order(result: dict[str, Any]) -> dict[str, Any]:
    candidates = (
        result.get("order"),
        (result.get("broker") or {}).get("order"),
        result.get("fill"),
        (result.get("broker") or {}).get("fill"),
        (result.get("oms") or {}).get("order"),
    )
    return next((item for item in candidates if isinstance(item, dict) and item.get("order_id")), {})


def _valid_project_transition(*, name: str, result: dict[str, Any]) -> bool:
    before_digest = result.get("before_sha256")
    after_digest = result.get("after_sha256")
    if name in {"project.write_file", "project.replace_text"}:
        return (
            (before_digest is None or _is_sha256(before_digest))
            and _is_sha256(after_digest)
            and before_digest != after_digest
            and bool(result.get("path"))
        )
    if name == "project.move_file":
        return (
            _is_sha256(before_digest)
            and after_digest == before_digest
            and bool(result.get("source"))
            and bool(result.get("destination"))
            and result.get("confirmed") is True
        )
    if name == "project.delete_file":
        return (
            _is_sha256(before_digest)
            and after_digest is None
            and bool(result.get("path"))
            and result.get("confirmed") is True
        )
    if name == "project.rollback_change":
        return result.get("confirmed") is True and bool(result.get("path") or result.get("paths"))
    return False


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _is_git_oid(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _criterion_financial_claim_text(criterion: str) -> str:
    """Remove only negative conduct/answer constraints, not account-state claims.

    Negation applies to its clause: "no trading; order filled" still requires
    a receipt. "Do not invent positions" is an answer constraint, whereas
    "there are no positions" is a portfolio-state assertion.
    """
    clauses = re.split(
        r"[，；;。\n]|(?:並且|而且|但是|且|但)|\bbut\b|\band\b(?=\s+(?:execute|place|submit|verify|check|confirm|complete|the|we|i)\b)",
        criterion.casefold(),
    )
    claims = []
    for clause in clauses:
        clause = clause.strip(" ,：:")
        if re.match(
            r"^(?:確保|保證|必須)?\s*(?:未|不得|不要|不應|不可|不|沒有|避免)"
            r"(?:虛構|編造|杜撰|捏造|謊稱|聲稱|宣稱)",
            clause,
        ) or re.match(
            r"^(?:確保|保證|必須)?\s*(?:未|不得|不要|不應|不可|不)(?:將|把)"
            r".{0,32}(?:表述為|視為|當作|當成|當|稱為).{0,16}(?:交易許可|下單許可|可交易|可下單)",
            clause,
        ) or re.match(
            r"^(?:(?:no|without)\s+(?:fabricated|invented|fictional)|"
            r"(?:do not|must not|never|did not|does not)\s+(?:fabricate|invent|make up|claim))\b",
            clause,
        ):
            continue
        clause = re.sub(
            r"(?:不要|不得|不|未|禁止)(?:執行)?(?:交易|下單|送出委託)"
            r"(?:(?:、|或|和|與)(?:交易|下單|自動化|建立自動化))*", "", clause,
        )
        clause = re.sub(
            r"\b(?:do not|don't|must not|never|did not)\s+(?:trade|place(?:\s+(?:any|an?|paper))?\s+orders?)\b|"
            r"\b(?:no trades?|without trading)\b", "", clause,
        )
        claims.append(clause)
    return " ".join(claims)


def _criterion_requires_financial_evidence(criterion: str) -> bool:
    claims = _criterion_financial_claim_text(criterion)
    claims = re.sub(r"交易所|交易日|交易時段|交易時間|交易量", "", claims)
    return bool(
        re.search(r"委託|持倉|交易|下單|訂單|\b(?:orders?|positions?|holdings|trades?|trading|fills?)\b", claims)
        or re.search(r"(?:已|完成|確認|本次).{0,10}(?:買進|買入|賣出|成交(?!量|價))", claims)
    )


def _criterion_check(
    criterion: str,
    result: dict[str, Any] | None,
    *,
    known_evidence_ids: set[str],
    evidence_catalog: dict[str, dict[str, Any]],
    required: bool,
) -> dict[str, Any]:
    default_criterion = criterion == "The user objective is satisfied by validated evidence."
    if not required:
        return {
            "criterion": criterion,
            "passed": True,
            "reason": "stable_general_answer_does_not_require_external_evidence",
        }
    if (
        default_criterion
        and evidence_catalog
        and (
            result is None
            or (
                result.get("met") is True
                and not [item for item in result.get("evidence_ids") or [] if str(item).strip()]
            )
        )
    ):
        host_evidence_ids = sorted(evidence_catalog)
        return {
            "criterion": criterion,
            "passed": True,
            "reason": "default_criterion_bound_to_host_validated_observation",
            "evidence_ids": host_evidence_ids,
        }
    if result is None:
        return {
            "criterion": criterion,
            "passed": False,
            "reason": "criterion_result_missing",
        }
    evidence_ids = {str(item) for item in result.get("evidence_ids") or [] if str(item).strip()}
    unknown = sorted(evidence_ids - known_evidence_ids)
    matching = [evidence_catalog[item] for item in evidence_ids if item in evidence_catalog]
    lowered = criterion.casefold()
    required_prefixes: tuple[str, ...] = ()
    if any(token in lowered for token in ("test", "pytest", "測試", "compile", "編譯", "syntax")):
        required_prefixes = ("terminal.run",)
    elif any(token in lowered for token in ("ui", "介面", "畫面", "dom")):
        required_prefixes = ("ui.", "browser.")
    elif any(token in lowered for token in ("file", "檔案", "寫入", "修改", "hash")):
        required_prefixes = ("project.",)
    elif _criterion_requires_financial_evidence(criterion):
        required_prefixes = ("paper.", "portfolio.")
    type_matched = (
        any(
            (not required_prefixes or str(item.get("tool") or "").startswith(required_prefixes))
            and item.get("ok") is True
            and (item.get("validation") or {}).get("passed") is True
            for item in matching
        )
    )
    return {
        "criterion": criterion,
        "passed": (
            result.get("met") is True
            and bool(evidence_ids)
            and not unknown
            and type_matched
        ),
        "evidence_ids": sorted(evidence_ids),
        "unknown_evidence_ids": unknown,
        "required_tool_prefixes": list(required_prefixes),
        "evidence_type_matched": type_matched,
    }


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
