from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from .models import Evidence


EdgeKind = Literal["supports", "contradicts"]


class EvidenceGraph:
    def __init__(self) -> None:
        self._evidence: dict[str, Evidence] = {}
        self._conclusions: set[str] = set()
        self._edges: set[tuple[str, str, EdgeKind]] = set()

    def add_evidence(self, evidence: Evidence) -> None:
        if evidence.evidence_id in self._evidence:
            raise ValueError(f"Duplicate evidence_id: {evidence.evidence_id}")
        self._evidence[evidence.evidence_id] = evidence
        for target in evidence.supports:
            self._edges.add((evidence.evidence_id, target, "supports"))
        for target in evidence.contradicts:
            self._edges.add((evidence.evidence_id, target, "contradicts"))

    def add_conclusion(self, conclusion_id: str) -> None:
        if not conclusion_id:
            raise ValueError("conclusion_id is required")
        self._conclusions.add(conclusion_id)

    def connect(self, evidence_id: str, target_id: str, relation: EdgeKind) -> None:
        if evidence_id not in self._evidence:
            raise KeyError(evidence_id)
        if target_id not in self._evidence and target_id not in self._conclusions:
            raise KeyError(target_id)
        self._edges.add((evidence_id, target_id, relation))

    def evidence_for(self, target_id: str, relation: EdgeKind | None = None) -> tuple[Evidence, ...]:
        identifiers = {
            source
            for source, target, edge_relation in self._edges
            if target == target_id and (relation is None or relation == edge_relation)
        }
        return tuple(self._evidence[item] for item in sorted(identifiers))

    @property
    def source_diversity(self) -> int:
        return len({item.source_type for item in self._evidence.values()})

    def freshness_score(self, *, now: datetime | None = None) -> float:
        if not self._evidence:
            return 0.0
        weights = {"current": 1.0, "recent": 0.7, "stale": 0.2, "unknown": 0.0}
        anchor = now or datetime.now(timezone.utc)
        return sum(weights[item.freshness(now=anchor)] for item in self._evidence.values()) / len(self._evidence)


@dataclass(frozen=True, slots=True)
class ConflictResolution:
    status: Literal["no_conflict", "resolved", "needs_more_research", "unresolved"]
    preferred_evidence_id: str | None
    reason: str
    follow_up_roles: tuple[str, ...] = ()


class ConflictResolver:
    PRIMARY_TYPES = {"company_ir", "exchange", "regulator"}

    def resolve(self, supporting: list[Evidence], contradicting: list[Evidence]) -> ConflictResolution:
        if not supporting or not contradicting:
            return ConflictResolution("no_conflict", None, "Only one side has evidence")
        primary = [item for item in supporting + contradicting if item.source_type in self.PRIMARY_TYPES]
        if len(primary) == 1:
            return ConflictResolution("resolved", primary[0].evidence_id, "Single traceable primary source outranks secondary claims")
        ranked = sorted(supporting + contradicting, key=self._rank, reverse=True)
        if len(ranked) >= 2 and self._rank(ranked[0]) - self._rank(ranked[1]) >= 0.25:
            return ConflictResolution("resolved", ranked[0].evidence_id, "Freshness and confidence materially outrank the alternative")
        roles = tuple(sorted({"primary", "independent_news"} - {item.source_role for item in supporting + contradicting}))
        if roles:
            return ConflictResolution("needs_more_research", None, "Material conflict lacks independent role coverage", roles)
        return ConflictResolution("unresolved", None, "Conflicting credible sources must remain explicit")

    @staticmethod
    def _rank(item: Evidence) -> float:
        freshness = {"current": 0.25, "recent": 0.12, "stale": 0.0, "unknown": -0.1}[item.freshness()]
        primary = 0.25 if item.source_type in ConflictResolver.PRIMARY_TYPES else 0.0
        return item.confidence * 0.5 + freshness + primary
