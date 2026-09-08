from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .policies import MemoryLayer, canonical_memory_layer, preference_semantics
from .trust import assess_memory_candidate


class MemoryDecision(str, Enum):
    SAVE = "save"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    EXPIRE = "expire"
    REJECT = "reject"
    QUARANTINE = "quarantine"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    content: str
    kind: MemoryLayer | str
    importance: float
    future_relevance: float
    confidence: float
    source: str | dict[str, Any]
    durability: str
    fingerprint: str | None = None
    semantic_key: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    host_verified: bool = False
    evidence_trust: float | None = None
    provenance: dict[str, Any] | None = None
    expires_at: str | None = None
    created_at: str = field(default_factory=lambda: _now().isoformat())

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Memory candidate content cannot be empty")
        canonical_memory_layer(self.kind)
        for name in ("importance", "future_relevance", "confidence"):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.durability not in {"run_local", "session", "long_term"}:
            raise ValueError("durability must be run_local, session, or long_term")
        if self.evidence_trust is not None and not 0 <= float(self.evidence_trust) <= 1:
            raise ValueError("evidence_trust must be between 0 and 1")

    @property
    def layer(self) -> MemoryLayer:
        return canonical_memory_layer(self.kind)

    @property
    def score(self) -> float:
        durability_weight = {
            "run_local": 0.0,
            "session": 0.04,
            "long_term": 0.1,
        }[self.durability]
        return round(
            min(
                1.0,
                self.importance * 0.35
                + self.future_relevance * 0.30
                + self.confidence * 0.25
                + durability_weight,
            ),
            4,
        )

    @property
    def resolved_fingerprint(self) -> str:
        if self.fingerprint:
            return self.fingerprint
        normalized = " ".join(self.content.casefold().split())
        return hashlib.sha256(f"{self.layer.value}:{normalized}".encode()).hexdigest()[:24]

    @property
    def resolved_semantic_key(self) -> str:
        return self.semantic_key or self.resolved_fingerprint

    @property
    def source_type(self) -> str:
        if isinstance(self.source, str):
            return self.source
        return str(self.source.get("type") or "unknown")


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    content: str
    layer: MemoryLayer
    score: float
    confidence: float
    source: dict[str, Any]
    durability: str
    semantic_key: str
    fingerprint: str
    status: MemoryStatus = MemoryStatus.ACTIVE
    evidence_trust: float = 1.0
    provenance: dict[str, Any] = field(default_factory=dict)
    quarantine_reason: str | None = None
    advisory: bool = False
    supersedes_id: str | None = None
    merged_from_ids: tuple[str, ...] = ()
    created_at: str = field(default_factory=lambda: _now().isoformat())
    status_changed_at: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryAuditEntry:
    audit_id: str
    decision: MemoryDecision
    candidate_fingerprint: str
    reason: str
    previous_memory_id: str | None
    resulting_memory_id: str | None
    created_at: str = field(default_factory=lambda: _now().isoformat())


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    decision: MemoryDecision
    reason: str
    record: MemoryRecord | None
    previous_record: MemoryRecord | None
    audit: MemoryAuditEntry
    candidate_id: str | None = None
    conflict_id: str | None = None


class ProceduralMemoryGate:
    """Promote a lesson only after repetition or explicit Host verification."""

    def __init__(self, *, minimum_distinct_runs: int = 2) -> None:
        self.minimum_distinct_runs = max(2, int(minimum_distinct_runs))
        self._runs_by_fingerprint: dict[str, set[str]] = {}

    def observe(self, candidate: MemoryCandidate) -> int:
        runs = self._runs_by_fingerprint.setdefault(candidate.resolved_fingerprint, set())
        if candidate.run_id:
            runs.add(candidate.run_id)
        return len(runs)

    def eligible(self, candidate: MemoryCandidate) -> bool:
        if candidate.host_verified:
            return True
        return self.observe(candidate) >= self.minimum_distinct_runs

    def occurrence_count(self, fingerprint: str) -> int:
        return len(self._runs_by_fingerprint.get(fingerprint, set()))


class MemoryGovernanceEngine:
    """Deterministic candidate consolidation with append-only conflict audit."""

    def __init__(self, *, save_threshold: float = 0.55) -> None:
        self.save_threshold = float(save_threshold)
        self.procedural_gate = ProceduralMemoryGate()
        self._records: dict[str, MemoryRecord] = {}
        self._audit: list[MemoryAuditEntry] = []

    @property
    def audit_log(self) -> tuple[MemoryAuditEntry, ...]:
        return tuple(self._audit)

    def records(self, *, include_inactive: bool = True) -> tuple[MemoryRecord, ...]:
        records = tuple(self._records.values())
        if include_inactive:
            return records
        return tuple(item for item in records if item.status is MemoryStatus.ACTIVE)

    def consider(self, candidate: MemoryCandidate) -> ConsolidationResult:
        now = _now()
        if _is_expired(candidate.expires_at, now):
            return self._result(MemoryDecision.EXPIRE, candidate, "candidate_expired")
        trust = assess_memory_candidate(candidate)
        if trust.quarantine:
            record = self._make_record(candidate, decision=MemoryDecision.QUARANTINE)
            self._records[record.memory_id] = record
            return self._result(
                MemoryDecision.QUARANTINE,
                candidate,
                trust.reason or "memory_candidate_quarantined",
                record=record,
            )
        if candidate.layer is MemoryLayer.PROCEDURAL and not self.procedural_gate.eligible(candidate):
            return self._result(
                MemoryDecision.REJECT,
                candidate,
                "procedural_requires_repeated_runs_or_host_verification",
            )
        if candidate.durability == "long_term" and candidate.score < self.save_threshold:
            return self._result(MemoryDecision.REJECT, candidate, "candidate_score_below_threshold")

        active = self._active_for_key(candidate.layer, candidate.resolved_semantic_key)
        if active and _normalize(active.content) == _normalize(candidate.content):
            merged = self._make_record(
                candidate,
                confidence=max(active.confidence, candidate.confidence),
                merged_from_ids=(*active.merged_from_ids, active.memory_id),
            )
            inactive = self._deactivate(active, MemoryStatus.SUPERSEDED)
            self._records[merged.memory_id] = merged
            return self._result(
                MemoryDecision.MERGE,
                candidate,
                "same_memory_reconfirmed",
                record=merged,
                previous=inactive,
            )

        if active:
            explicit = candidate.source_type in {"user_explicit", "user_instruction", "user_correction"}
            if explicit or candidate.score > active.score:
                successor = self._make_record(candidate, supersedes_id=active.memory_id)
                inactive = self._deactivate(active, MemoryStatus.SUPERSEDED)
                self._records[successor.memory_id] = successor
                return self._result(
                    MemoryDecision.SUPERSEDE,
                    candidate,
                    "explicit_or_higher_confidence_conflict",
                    record=successor,
                    previous=inactive,
                )
            return self._result(
                MemoryDecision.REJECT,
                candidate,
                "lower_confidence_conflict_preserved_existing_memory",
                previous=active,
            )

        saved = self._make_record(candidate)
        self._records[saved.memory_id] = saved
        return self._result(MemoryDecision.SAVE, candidate, "candidate_accepted", record=saved)

    def expire_due(self, *, at: datetime | None = None) -> tuple[MemoryRecord, ...]:
        now = at or _now()
        expired: list[MemoryRecord] = []
        for record in tuple(self._records.values()):
            if record.status is MemoryStatus.ACTIVE and _is_expired(record.expires_at, now):
                expired_record = self._deactivate(record, MemoryStatus.EXPIRED, at=now)
                expired.append(expired_record)
                candidate = MemoryCandidate(
                    content=record.content,
                    kind=record.layer,
                    importance=record.score,
                    future_relevance=record.score,
                    confidence=record.confidence,
                    source=record.source,
                    durability=record.durability,
                    fingerprint=record.fingerprint,
                    semantic_key=record.semantic_key,
                )
                self._result(
                    MemoryDecision.EXPIRE,
                    candidate,
                    "stored_memory_expired",
                    previous=record,
                )
        return tuple(expired)

    def _active_for_key(self, layer: MemoryLayer, semantic_key: str) -> MemoryRecord | None:
        return next(
            (
                item
                for item in reversed(tuple(self._records.values()))
                if item.layer is layer
                and item.semantic_key == semantic_key
                and item.status is MemoryStatus.ACTIVE
            ),
            None,
        )

    def _make_record(
        self,
        candidate: MemoryCandidate,
        *,
        decision: MemoryDecision = MemoryDecision.SAVE,
        confidence: float | None = None,
        supersedes_id: str | None = None,
        merged_from_ids: tuple[str, ...] = (),
    ) -> MemoryRecord:
        raw_source = (
            {"type": candidate.source}
            if isinstance(candidate.source, str)
            else dict(candidate.source)
        )
        advisory = candidate.layer is MemoryLayer.USER_PREFERENCE
        source = preference_semantics(raw_source) if advisory else raw_source
        trust = assess_memory_candidate(candidate)
        return MemoryRecord(
            memory_id=f"AMEM-{uuid4().hex}",
            content=candidate.content.strip(),
            layer=candidate.layer,
            score=candidate.score,
            confidence=candidate.confidence if confidence is None else confidence,
            source=source,
            durability=candidate.durability,
            semantic_key=candidate.resolved_semantic_key,
            fingerprint=candidate.resolved_fingerprint,
            status=(
                MemoryStatus.QUARANTINED
                if decision is MemoryDecision.QUARANTINE
                else MemoryStatus.ACTIVE
            ),
            evidence_trust=trust.score,
            provenance=trust.provenance,
            quarantine_reason=trust.reason,
            advisory=advisory,
            supersedes_id=supersedes_id,
            merged_from_ids=merged_from_ids,
            expires_at=candidate.expires_at,
        )

    def _deactivate(
        self,
        record: MemoryRecord,
        status: MemoryStatus,
        *,
        at: datetime | None = None,
    ) -> MemoryRecord:
        changed = replace(record, status=status, status_changed_at=(at or _now()).isoformat())
        self._records[record.memory_id] = changed
        return changed

    def _result(
        self,
        decision: MemoryDecision,
        candidate: MemoryCandidate,
        reason: str,
        *,
        record: MemoryRecord | None = None,
        previous: MemoryRecord | None = None,
    ) -> ConsolidationResult:
        audit = MemoryAuditEntry(
            audit_id=f"MAUD-{uuid4().hex}",
            decision=decision,
            candidate_fingerprint=candidate.resolved_fingerprint,
            reason=reason,
            previous_memory_id=previous.memory_id if previous else None,
            resulting_memory_id=record.memory_id if record else None,
        )
        self._audit.append(audit)
        return ConsolidationResult(decision, reason, record, previous, audit)


class BackgroundMemoryPipeline:
    """Schedule post-answer extraction without delaying the delivered result."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def schedule(self, work: Callable[[], Any | Awaitable[Any]]) -> asyncio.Task[Any]:
        async def execute() -> Any:
            result = work()
            return await result if inspect.isawaitable(result) else result

        task = asyncio.create_task(execute())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> list[Any]:
        if not self._tasks:
            return []
        return list(await asyncio.gather(*tuple(self._tasks)))


def _normalize(content: str) -> str:
    return " ".join(content.casefold().split())


def _is_expired(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _now() -> datetime:
    return datetime.now(timezone.utc)
