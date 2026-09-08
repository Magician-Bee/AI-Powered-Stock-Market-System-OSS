from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4


EvidenceFreshness = Literal["current", "recent", "stale", "unknown"]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


@dataclass(frozen=True, slots=True)
class SourceRequirement:
    role: str
    preferred_types: tuple[str, ...]
    primary_required: bool = False


@dataclass(frozen=True, slots=True)
class ResearchQuestion:
    question_id: str
    question: str
    claim_scope: str
    source_roles: tuple[str, ...]
    freshness_hours: int
    minimum_independent_sources: int = 2


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    objective: str
    questions: tuple[ResearchQuestion, ...]
    source_requirements: tuple[SourceRequirement, ...]
    conflict_rules: tuple[str, ...]
    stop_criteria: tuple[str, ...]
    plan_id: str = field(default_factory=lambda: f"RP-{uuid4().hex}")


@dataclass(frozen=True, slots=True)
class Evidence:
    claim: str
    source_type: str
    source: str
    published_at: str | None
    observed_at: str
    confidence: float
    supports: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    evidence_id: str = field(default_factory=lambda: f"EV-{uuid4().hex}")
    source_role: str = "independent"
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.claim.strip() or not self.source.strip():
            raise ValueError("Evidence requires a claim and traceable source")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Evidence confidence must be between 0 and 1")
        if _parse_time(self.observed_at) is None:
            raise ValueError("observed_at must be an ISO-8601 timestamp")

    def freshness(self, *, now: datetime | None = None, current_hours: int = 24) -> EvidenceFreshness:
        published = _parse_time(self.published_at)
        if published is None:
            return "unknown"
        anchor = now or datetime.now(timezone.utc)
        age_hours = max(0.0, (anchor - published).total_seconds() / 3600)
        if age_hours <= current_hours:
            return "current"
        if age_hours <= current_hours * 7:
            return "recent"
        return "stale"

    def to_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.evidence.v1",
            **asdict(self),
            "freshness": self.freshness(now=now),
        }
