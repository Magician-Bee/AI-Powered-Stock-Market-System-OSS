"""Evidence trust assessment for durable memory candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MemoryTrustAssessment:
    score: float
    provenance: dict[str, Any]
    quarantine: bool
    reason: str | None = None


def assess_memory_candidate(candidate: Any) -> MemoryTrustAssessment:
    """Fail closed for explicitly untrusted or low-trust memory evidence."""

    raw_source = (
        {"type": candidate.source}
        if isinstance(candidate.source, str)
        else dict(candidate.source or {})
    )
    provenance = dict(candidate.provenance or {})
    source_type = str(raw_source.get("type") or "unknown").casefold()
    declared = candidate.evidence_trust
    if declared is None:
        declared = provenance.get("evidence_trust", raw_source.get("evidence_trust"))
    score = float(declared) if declared is not None else 1.0
    trust_level = str(
        provenance.get("trust_level") or raw_source.get("trust_level") or ""
    ).casefold()
    external = source_type in {
        "external_content", "external_provider", "web", "browser", "model_output"
    } or trust_level in {"untrusted", "untrusted_data", "quarantined"}
    if external and declared is None:
        score = min(score, 0.25)
    score = round(max(0.0, min(1.0, score)), 6)
    quarantine = bool(not candidate.host_verified and (score < 0.5 or external))
    reason = (
        "memory_evidence_untrusted_or_below_host_verification_threshold"
        if quarantine
        else None
    )
    provenance = {
        **raw_source,
        **provenance,
        "source_type": source_type,
        "evidence_trust": score,
        "host_verified": bool(candidate.host_verified),
        "quarantine": quarantine,
        "quarantine_reason": reason,
    }
    return MemoryTrustAssessment(score, provenance, quarantine, reason)
