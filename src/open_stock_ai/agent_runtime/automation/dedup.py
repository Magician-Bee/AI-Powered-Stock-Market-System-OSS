from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .intent import AutomationIntent


class DedupDecision(StrEnum):
    CREATE = "create"
    REUSE = "reuse"
    MERGE = "merge"
    UPDATE = "update"


@dataclass(frozen=True, slots=True)
class DedupResult:
    decision: DedupDecision
    automation_id: str | None
    fingerprint: str
    similarity: float
    reason: str


def semantic_fingerprint(intent: AutomationIntent) -> str:
    encoded = json.dumps(_semantic_projection(intent), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SemanticDeduplicator:
    def resolve(self, intent: AutomationIntent, candidates: Iterable[Mapping[str, Any]]) -> DedupResult:
        fingerprint = semantic_fingerprint(intent)
        target = _semantic_projection(intent)
        best: tuple[float, Mapping[str, Any]] | None = None
        for candidate in candidates:
            if str(candidate.get("semantic_fingerprint") or "") == fingerprint:
                return DedupResult(
                    DedupDecision.REUSE,
                    str(candidate.get("automation_id") or "") or None,
                    fingerprint,
                    1.0,
                    "identical semantic fingerprint",
                )
            source = candidate.get("semantic_projection") or candidate.get("intent") or {}
            score = _similarity(target, source)
            if best is None or score > best[0]:
                best = (score, candidate)
        if best and best[0] >= 0.9:
            return DedupResult(
                DedupDecision.UPDATE,
                str(best[1].get("automation_id") or "") or None,
                fingerprint,
                best[0],
                "same goal, symbol, trigger and user with changed condition or action",
            )
        if best and best[0] >= 0.72:
            return DedupResult(
                DedupDecision.MERGE,
                str(best[1].get("automation_id") or "") or None,
                fingerprint,
                best[0],
                "overlapping monitoring objective can share one automation",
            )
        return DedupResult(DedupDecision.CREATE, None, fingerprint, best[0] if best else 0.0, "no semantic duplicate")


def semantic_projection(intent: AutomationIntent) -> dict[str, Any]:
    return _semantic_projection(intent)


def _semantic_projection(intent: AutomationIntent) -> dict[str, Any]:
    return {
        "goal": _text(intent.goal),
        "symbol": _text(intent.symbol or ""),
        "trigger": _normalized(intent.trigger),
        "condition": _normalized(intent.decision_logic),
        "actions": _normalized(intent.actions),
        "user": _text(intent.user_id),
    }


def _normalized(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key).casefold(): _normalized(child) for key, child in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, float):
        return round(value, 8)
    return value


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    normalized_right = _normalized(right)
    exact_fields = ("symbol", "user")
    if any(left.get(key) != normalized_right.get(key) for key in exact_fields):
        return 0.0
    weights = {"goal": 0.25, "symbol": 0.15, "trigger": 0.2, "condition": 0.15, "actions": 0.15, "user": 0.1}
    score = 0.0
    for key, weight in weights.items():
        a = json.dumps(left.get(key), ensure_ascii=False, sort_keys=True)
        b = json.dumps(normalized_right.get(key), ensure_ascii=False, sort_keys=True)
        score += weight * (1.0 if a == b else SequenceMatcher(None, a, b).ratio())
    return score
