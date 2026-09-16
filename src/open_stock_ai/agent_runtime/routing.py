from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from open_stock_ai.types import SymbolContext


class IntentHypothesis(BaseModel):
    type: str
    confidence: float
    evidence: list[str] = Field(default_factory=list)


class RoutingDecision(BaseModel):
    intents: list[IntentHypothesis]
    primary_task_kind: str
    negative_constraints: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    symbol_contexts: list[SymbolContext] = Field(default_factory=list)
    requires_symbol: bool = False
    requires_market_context: bool = False
    requires_project_context: bool = False
    ambiguities: list[str] = Field(default_factory=list)


class UnifiedMultiIntentRouter:
    """Produce auditable intent hypotheses while preserving negative constraints."""

    def route(
        self,
        objective: str,
        *,
        task_kind_hint: str,
        supplied_symbols: tuple[str, ...] = (),
        supplied_symbol_source: Literal["workflow_parameter", "ui_selected"] = "workflow_parameter",
    ) -> RoutingDecision:
        text = str(objective or "").strip()
        lowered = text.casefold()
        hypotheses: list[IntentHypothesis] = []
        negatives: list[str] = []
        if any(
            phrase in lowered
            for phrase in (
                "不要分析股票",
                "不分析股票",
                "do not analyze stocks",
                "not about stocks",
            )
        ):
            negatives.append("do_not_analyze_stocks")
        if any(
            phrase in lowered
            for phrase in (
                "不要查市場",
                "不查市場",
                "不要市場資料",
                "不需要市場資料",
                "no market lookup",
                "do not query market",
                "do not fetch market data",
            )
        ):
            negatives.append("do_not_query_market")
        if any(
            phrase in lowered
            for phrase in (
                "不要下單",
                "不下單",
                "do not trade",
                "analysis only",
                "只做分析",
            )
        ):
            negatives.append("do_not_execute_trades")
        if any(
            phrase in lowered
            for phrase in (
                "不要操作介面",
                "不操作介面",
                "不要操作 ui",
                "do not operate the ui",
                "do not operate ui",
                "do not use the user interface",
            )
        ):
            negatives.append("do_not_operate_ui")
        # A Task Forest/Fishbone label may identify a failed branch, but it
        # never grants UI control.  When the user explicitly asks for recovery
        # of a source/evidence gap, preserve the Host-selected research surface
        # even if a small model later emits an over-eager UI routing patch.
        recovery_terms = ("修復", "恢復", "根因", "repair", "recover", "root cause", "diagnose")
        evidence_terms = ("資料來源", "替代來源", "來源", "證據", "research", "source", "evidence")
        if _contains_any(lowered, recovery_terms) and _contains_any(lowered, evidence_terms):
            negatives.append("evidence_recovery_not_ui")

        evidence = [f"host_hint:{task_kind_hint}"]
        hypotheses.append(
            IntentHypothesis(
                type=task_kind_hint,
                confidence=1.0 if task_kind_hint == "market_radar" else 0.45,
                evidence=evidence,
            )
        )
        if _contains_any(lowered, ("專案", "程式碼", "repo", "repository", "project")):
            hypotheses.append(
                IntentHypothesis(
                    type="project_task",
                    confidence=0.8,
                    evidence=["project terminology"],
                )
            )
        if _contains_any(lowered, ("股票", "股價", "市場", "market", "stock", "universe", "symbol")):
            hypotheses.append(
                IntentHypothesis(
                    type="market_information",
                    confidence=0.78,
                    evidence=["market terminology"],
                )
            )
        elif _contains_stock_research_language(lowered):
            # Natural follow-up messages commonly name companies rather than
            # repeating 「股票」 or a ticker (for example「比較台新新光金的
            # 風險差異」).  In a stock workspace that is still a market
            # research request: route it to the Host market surface so it can
            # resolve names and collect evidence instead of treating it as
            # unactionable general chat.
            hypotheses.append(
                IntentHypothesis(
                    type="market_information",
                    confidence=0.72,
                    evidence=["stock research terminology"],
                )
            )
        if (
            "do_not_operate_ui" not in negatives
            and _contains_any(
                lowered,
                (
                    "介面",
                    "ui.",
                    "navigate",
                    "open panel",
                    "開啟面板",
                    "開啟設定",
                    "開啟 agent",
                ),
            )
        ):
            hypotheses.append(
                IntentHypothesis(
                    type="ui_task",
                    confidence=0.82,
                    evidence=["UI terminology"],
                )
            )
        hypotheses = _dedupe_hypotheses(hypotheses)

        whole_market = not supplied_symbols and text.startswith("[MARKET_SCOPE]")
        explicit_symbols = () if whole_market else tuple(_explicit_symbols(text))
        refers_to_selection = _contains_any(
            lowered,
            (
                "這支股票",
                "這檔股票",
                "目前選取",
                "目前這檔",
                "current stock",
                "selected symbol",
            ),
        )
        contexts: list[SymbolContext] = [
            SymbolContext(
                symbol=symbol,
                source="user_explicit",
                confidence=1.0,
                evidence=("objective_text",),
                user_confirmed=True,
            )
            for symbol in explicit_symbols
        ]
        for symbol in supplied_symbols:
            if symbol in explicit_symbols:
                continue
            contexts.append(
                SymbolContext(
                    symbol=symbol,
                    # Symbols in the Runtime request contract are explicit workflow
                    # parameters.  A passive UI selection must stay out of this list
                    # unless the UI has resolved a referential phrase first.
                    source=supplied_symbol_source,
                    confidence=1.0 if supplied_symbol_source == "workflow_parameter" else 0.55,
                    evidence=("run_symbols",),
                    user_confirmed=(
                        supplied_symbol_source == "workflow_parameter" or refers_to_selection
                    ),
                )
            )
        if not contexts:
            contexts.append(SymbolContext())

        market_forbidden = any(
            constraint in negatives
            for constraint in ("do_not_analyze_stocks", "do_not_query_market")
        )
        inferred_primary = hypotheses[0].type if hypotheses else task_kind_hint
        if (
            inferred_primary == "ui_task"
            and any(
                constraint in negatives
                for constraint in ("do_not_operate_ui", "evidence_recovery_not_ui")
            )
        ):
            inferred_primary = next(
                (item.type for item in hypotheses if item.type != "ui_task"),
                task_kind_hint,
            )
        primary = (
            # A Host-recognized Artifact request has a deliberately narrow
            # capability surface.  Phrases such as "不要查市場" must remain a
            # negative constraint, not promote the run to market information
            # and discard its completed Artifact receipt at finalization.
            "artifact_task"
            if task_kind_hint == "artifact_task"
            else "general_answer"
            if market_forbidden
            # ``market_decision`` is a Host-owned routing result for an
            # explicit order request.  A generic market-information
            # hypothesis is expected for the same text (it mentions symbols
            # and analysis), but it must not demote the bounded paper-order
            # protocol into the broader research surface.  That demotion
            # exposed recursive subtasks and let a routine paper order enter
            # recovery loops instead of following preview -> submit.
            else "market_decision"
            if task_kind_hint == "market_decision"
            # Prefer the strongest auditable intent hypothesis so explicit
            # symbols/market terms can widen a generic Host hint.  Negative UI
            # constraints are applied above and cannot grant UI authority.
            else inferred_primary
        )
        market_intent = primary in {"market_information", "market_decision", "market_radar"}
        requires_symbol = market_intent and not market_forbidden and not whole_market
        ambiguities = []
        if requires_symbol and not any(context.user_confirmed for context in contexts):
            ambiguities.append("market_intent_without_confirmed_symbol")
        if len(hypotheses) > 1:
            ambiguities.append("multiple_intents_detected")
        return RoutingDecision(
            intents=hypotheses,
            primary_task_kind=primary,
            negative_constraints=negatives,
            entities=list(explicit_symbols),
            symbol_contexts=contexts,
            requires_symbol=requires_symbol,
            requires_market_context=market_intent and not market_forbidden,
            requires_project_context=any(item.type == "project_task" for item in hypotheses),
            ambiguities=ambiguities,
        )

    def apply_model_correction(
        self,
        current: RoutingDecision,
        correction: dict[str, object] | None,
    ) -> RoutingDecision:
        """Accept a model routing hypothesis without surrendering Host constraints."""

        if not isinstance(correction, dict):
            return current
        candidate = str(correction.get("primary_task_kind") or "").strip()
        allowed = {
            "general_answer",
            "artifact_task",
            "project_task",
            "market_information",
            "market_decision",
            "market_radar",
            "ui_task",
            "current_information",
        }
        if candidate not in allowed:
            return current
        # An explicit Host-recognized Artifact operation is deliberately
        # narrower than a project task and requires an actual Host receipt.
        # Do not let a provider downgrade it to a prose-only answer after the
        # scoped Artifact capability has been selected.
        if current.primary_task_kind == "artifact_task" and candidate != "artifact_task":
            candidate = "artifact_task"
        if (
            any(
                constraint in current.negative_constraints
                for constraint in ("do_not_analyze_stocks", "do_not_query_market")
            )
            and candidate in {"market_information", "market_decision", "market_radar"}
        ):
            candidate = "general_answer"
        if (
            "evidence_recovery_not_ui" in current.negative_constraints
            and candidate == "ui_task"
            and current.primary_task_kind in {
                "market_information", "market_decision", "market_radar", "current_information"
            }
        ):
            candidate = current.primary_task_kind
        supplied = correction.get("intents")
        hypotheses = list(current.intents)
        if isinstance(supplied, list):
            for item in supplied:
                if not isinstance(item, dict):
                    continue
                intent = str(item.get("type") or "").strip()
                if intent not in allowed:
                    continue
                try:
                    confidence = max(0.0, min(float(item.get("confidence") or 0.0), 1.0))
                except (TypeError, ValueError):
                    continue
                hypotheses.append(
                    IntentHypothesis(
                        type=intent,
                        confidence=confidence,
                        evidence=["model_routing_correction"],
                    )
                )
        hypotheses = _dedupe_hypotheses(hypotheses)
        market_intent = candidate in {"market_information", "market_decision", "market_radar"}
        return current.model_copy(
            update={
                "intents": hypotheses,
                "primary_task_kind": candidate,
                "requires_symbol": market_intent and candidate != "market_radar",
                "requires_market_context": market_intent,
                "requires_project_context": candidate == "project_task",
            }
        )


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _contains_stock_research_language(text: str) -> bool:
    """Recognize terse, natural stock-analysis follow-ups without a ticker.

    The Host must not make users supply an exchange code just to compare the
    risk, valuation, or technical profile of companies named in ordinary
    language. Negative constraints are still applied by ``route`` before
    this signal can grant market access.
    """

    return _contains_any(
        text,
        (
            "技術面", "技術分析", "基本面", "籌碼", "估值", "本益比", "殖利率",
            "營收", "財報", "獲利", "波動", "風險差異", "風險比較", "比較風險",
            "risk comparison", "technical analysis", "fundamental analysis",
            "valuation", "earnings risk",
        ),
    )


def _explicit_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    lowered = text.casefold()
    numeric_is_non_market = _contains_any(
        lowered,
        ("錯誤碼", "錯誤代碼", "error code", "status code", "api", "http", "版本", "port"),
    )
    if not numeric_is_non_market:
        for match in re.finditer(r"(?<![A-Za-z0-9])(\d{4,6})(?:\.(TW|TWO))?(?![A-Za-z0-9])", text, re.IGNORECASE):
            code, suffix = match.groups()
            symbols.append(f"{code}.{(suffix or 'TW').upper()}")
    # A ticker is a complete identifier, never the prefix of AC-/AE-/AR-
    # receipts or the exchange suffix inside a canonical Taiwan symbol.
    for match in re.finditer(r"(?<![A-Za-z0-9_.-])([A-Z]{1,5}(?:-USD)?)(?![A-Za-z0-9_.-])", text):
        token = match.group(1)
        if token not in {"AI", "API", "HTTP", "JSON", "MODEL", "RULE", "UI"}:
            symbols.append(token)
    return list(dict.fromkeys(symbols))


def normalized_market_symbols(
    routing: RoutingDecision, *, objective: str, supplied_symbols: tuple[str, ...],
) -> tuple[str, ...]:
    """An explicit Host whole-market mandate has no instrument restriction.

    Its examples, receipts and research results are not authorization to narrow
    the campaign. Nonempty workflow symbols retain their existing authority.
    """
    if not routing.requires_market_context or (
        not supplied_symbols and objective.lstrip().startswith("[MARKET_SCOPE]")
    ):
        return ()
    return tuple(dict.fromkeys(item.symbol for item in routing.symbol_contexts
                               if item.symbol and item.user_confirmed))


def restore_host_market_scope(
    routing: RoutingDecision, *, objective: str, supplied_symbols: tuple[str, ...],
) -> RoutingDecision:
    """A stale routing checkpoint cannot replace the current Host market mandate."""
    if not supplied_symbols and objective.lstrip().startswith("[MARKET_SCOPE]"):
        return routing.model_copy(update={
            "entities": [], "symbol_contexts": [SymbolContext()], "requires_symbol": False,
            "ambiguities": [item for item in routing.ambiguities if item != "market_intent_without_confirmed_symbol"],
        })
    return routing


def _dedupe_hypotheses(items: list[IntentHypothesis]) -> list[IntentHypothesis]:
    by_type: dict[str, IntentHypothesis] = {}
    for item in items:
        previous = by_type.get(item.type)
        if previous is None or item.confidence > previous.confidence:
            by_type[item.type] = item
    return sorted(by_type.values(), key=lambda item: item.confidence, reverse=True)
