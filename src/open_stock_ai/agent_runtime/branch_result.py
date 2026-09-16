from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class BranchResult:
    conclusion: str
    confidence: float
    evidence_ids: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    remaining_gaps: tuple[str, ...] = ()
    branch_id: str | None = None
    output_digest: str | None = None
    compressed_from_chars: int = 0

    def __post_init__(self) -> None:
        if not self.conclusion.strip():
            raise ValueError("BranchResult conclusion cannot be empty")
        if not 0 <= float(self.confidence) <= 1:
            raise ValueError("BranchResult confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "conclusion": self.conclusion,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "contradictions": list(self.contradictions),
            "remaining_gaps": list(self.remaining_gaps),
            "output_digest": self.output_digest,
            "compressed_from_chars": self.compressed_from_chars,
        }

    def to_context(self, *, max_characters: int = 4000) -> dict[str, Any]:
        payload = self.to_dict()
        payload["conclusion"] = _truncate(self.conclusion, max_characters)
        return payload


class BranchResultCompressor:
    """Replace raw tool transcripts with a bounded, evidence-linked result contract."""

    def compress(
        self,
        *,
        conclusion: str,
        confidence: float,
        raw_outputs: Iterable[Any],
        evidence_ids: Iterable[str] = (),
        contradictions: Iterable[str] = (),
        remaining_gaps: Iterable[str] = (),
        branch_id: str | None = None,
        max_conclusion_characters: int = 4000,
    ) -> BranchResult:
        serialized = json.dumps(list(raw_outputs), ensure_ascii=False, default=str, sort_keys=True)
        return BranchResult(
            branch_id=branch_id,
            conclusion=_truncate(conclusion.strip(), max_conclusion_characters),
            confidence=float(confidence),
            evidence_ids=_unique(evidence_ids),
            contradictions=_unique(contradictions),
            remaining_gaps=_unique(remaining_gaps),
            output_digest=hashlib.sha256(serialized.encode()).hexdigest(),
            compressed_from_chars=len(serialized),
        )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value).strip()))


def _truncate(value: str, limit: int) -> str:
    safe_limit = max(1, int(limit))
    if len(value) <= safe_limit:
        return value
    if safe_limit == 1:
        return "…"
    return value[: safe_limit - 1].rstrip() + "…"
