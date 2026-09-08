from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .intent import AutomationKind


@dataclass(frozen=True, slots=True)
class AutomationOpportunity:
    kind: AutomationKind
    worthwhile: bool
    confidence: float
    reason: str


class OpportunityDetector:
    """Classify whether a goal deserves automation before an intent is created."""

    def __init__(self, classifier: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
        self.classifier = classifier

    def detect(self, goal: str, context: Mapping[str, Any] | None = None) -> AutomationOpportunity:
        normalized = goal.strip().casefold()
        details = dict(context or {})
        # An explicit user constraint takes precedence over both the optional
        # classifier and keyword inference.  Discussing an architecture must
        # not become an automation proposal just because it mentions 監控.
        if _explicitly_forbids_automation(normalized):
            return AutomationOpportunity(
                AutomationKind.NO_AUTOMATION,
                False,
                1.0,
                "the user explicitly requested no automation creation or activation",
            )
        if self.classifier is not None:
            result = dict(self.classifier(goal, details))
            kind = AutomationKind(str(result.get("kind") or AutomationKind.NO_AUTOMATION.value))
            return AutomationOpportunity(
                kind=kind,
                worthwhile=kind != AutomationKind.NO_AUTOMATION,
                confidence=max(0.0, min(float(result.get("confidence", 0.5)), 1.0)),
                reason=str(result.get("reason") or "classified by automation opportunity model"),
            )
        if not normalized:
            return AutomationOpportunity(AutomationKind.NO_AUTOMATION, False, 1.0, "empty goal")
        contextual = _contextual_opportunity(details)
        if contextual is not None:
            return contextual
        if any(token in normalized for token in ("每天", "每日", "每週", "weekly", "daily", "定期")):
            kind = AutomationKind.RECURRING
        elif any(token in normalized for token in ("當", "一旦", "突破", "跌破", "監控", "watch", "cross")):
            kind = AutomationKind.CONDITION_WATCH
        elif any(token in normalized for token in ("事件", "公告", "event")):
            kind = AutomationKind.EVENT_WATCH
        elif any(token in normalized for token in ("寄信", "email", "line", "telegram", "google", "webhook")):
            kind = AutomationKind.CROSS_SYSTEM
        elif any(token in normalized for token in ("稍後", "明天", "一次", "one-shot")):
            kind = AutomationKind.ONE_SHOT
        else:
            return AutomationOpportunity(
                AutomationKind.NO_AUTOMATION,
                False,
                0.72,
                "the request has no future trigger or repeatable monitoring value",
            )
        return AutomationOpportunity(kind, True, 0.84, "future trigger and repeatable decision value detected")


def _explicitly_forbids_automation(normalized_goal: str) -> bool:
    """Recognize direct no-automation constraints without over-blocking discussion."""

    chinese = (
        r"(?:不要|不必|無須|毋須|禁止|別|不需|不需要)"
        r"(?:再|去|直接)?(?:建立|新增|啟用|執行|設置|設定|做)?"
        r"(?:任何)?\s*(?:自動化|automation|monitoring|schedule|reminder|workflow|監控|排程|提醒|工作流|流程)"
    )
    coordinated_chinese = (
        r"(?:不要|不必|無須|毋須|禁止|別|不需|不需要)"
        r"(?:下單|交易|買進|賣出|操作)"
        r"\s*(?:、|，|,|或|以及|也不要)\s*"
        r"(?:再|去|直接)?(?:建立|新增|啟用|執行|設置|設定|做)?"
        r"\s*(?:自動化|automation|monitoring|schedule|reminder|workflow|監控|排程|提醒|工作流|流程)"
    )
    english = (
        r"\b(?:do\s+not|don't|dont|no|without|never)\s+"
        r"(?:create|enable|run|set\s+up|add)?\s*"
        r"(?:any\s+)?(?:automation|monitoring|schedule|reminder|workflow)\b"
    )
    return bool(
        re.search(chinese, normalized_goal)
        or re.search(coordinated_chinese, normalized_goal)
        or re.search(english, normalized_goal)
    )


def _contextual_opportunity(context: Mapping[str, Any]) -> AutomationOpportunity | None:
    """Score product facts without depending on words in the user's sentence.

    The intent/model layer may already know that work has a future trigger,
    repeats, crosses systems, or has enough expected value to justify a watch.
    Those structured facts are stronger evidence than spotting words such as
    ``監控`` in an architecture discussion.  Text inference remains a fallback
    for callers that do not yet provide an intent assessment.
    """

    assessment = context.get("automation_assessment")
    facts = dict(assessment) if isinstance(assessment, Mapping) else dict(context)
    explicit_value = facts.get("worthwhile")
    if explicit_value is False:
        return AutomationOpportunity(
            AutomationKind.NO_AUTOMATION,
            False,
            _confidence(facts.get("confidence"), 0.96),
            str(facts.get("reason") or "structured product assessment rejected automation"),
        )

    trigger = facts.get("trigger")
    trigger_facts = dict(trigger) if isinstance(trigger, Mapping) else {}
    cadence = facts.get("cadence") or facts.get("recurrence") or trigger_facts.get("frequency")
    interval = facts.get("interval_seconds") or trigger_facts.get("interval_seconds")
    run_at = facts.get("run_at") or trigger_facts.get("run_at") or trigger_facts.get("at")
    event_source = facts.get("event_source") or trigger_facts.get("event_type")
    condition = facts.get("condition") or trigger_facts.get("condition")
    channels = tuple(str(item).strip() for item in facts.get("notification_channels") or () if str(item).strip())
    cross_system = bool(facts.get("cross_system")) or any(channel != "in_app" for channel in channels)
    repeatable = bool(facts.get("repeatable"))
    future = bool(facts.get("future_trigger") or facts.get("requires_follow_up"))
    impact = _unit_score(facts.get("decision_impact"), default=0.0)
    expected_value = _unit_score(facts.get("expected_value"), default=0.0)
    manual_cost = _unit_score(facts.get("manual_cost"), default=0.0)

    kind: AutomationKind | None = None
    if cross_system:
        kind = AutomationKind.CROSS_SYSTEM
    elif cadence or interval or repeatable:
        kind = AutomationKind.RECURRING
    elif event_source:
        kind = AutomationKind.EVENT_WATCH
    elif condition or trigger_facts.get("field") or trigger_facts.get("operator"):
        kind = AutomationKind.CONDITION_WATCH
    elif run_at or future:
        kind = AutomationKind.ONE_SHOT

    if kind is None:
        return None
    score = (
        0.42
        + (0.18 if future or run_at or event_source or condition else 0.0)
        + (0.14 if repeatable or cadence or interval else 0.0)
        + 0.12 * impact
        + 0.08 * expected_value
        + 0.06 * manual_cost
    )
    if explicit_value is True:
        score = max(score, 0.8)
    worthwhile = score >= 0.6
    return AutomationOpportunity(
        kind if worthwhile else AutomationKind.NO_AUTOMATION,
        worthwhile,
        _confidence(facts.get("confidence"), score),
        str(facts.get("reason") or "structured future value and trigger assessment"),
    )


def _unit_score(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    labels = {"low": 0.25, "medium": 0.6, "high": 1.0}
    if isinstance(value, str) and value.strip().casefold() in labels:
        return labels[value.strip().casefold()]
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default


def _confidence(value: Any, default: float) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return max(0.0, min(default, 1.0))
