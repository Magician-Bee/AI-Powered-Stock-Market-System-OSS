from __future__ import annotations

"""Host-side extraction of durable memory candidates from a completed Run."""

from typing import Any

from .governance import MemoryCandidate


class MemoryCandidateExtractor:
    """Extract only explicit unresolved goals and verified procedural lessons.

    This is intentionally conservative: raw prices, fluent model prose and
    one-off guesses are not promoted to long-term memory.
    """

    def extract(
        self,
        *,
        objective: str,
        summary: str,
        evidence: dict[str, Any],
        session_id: str,
        run_id: str,
    ) -> tuple[MemoryCandidate, ...]:
        candidates: list[MemoryCandidate] = []
        gaps = [str(item).strip() for item in evidence.get("remaining_gaps") or [] if str(item).strip()]
        if not gaps:
            gaps = [str(item).strip() for item in evidence.get("goal_completion_gaps") or [] if str(item).strip()]
        if gaps:
            candidates.append(
                MemoryCandidate(
                    content=f"Unresolved goal for this session: {objective}\nMissing: {'; '.join(gaps[:8])}",
                    kind="working",
                    importance=0.75,
                    future_relevance=0.8,
                    confidence=0.95 if evidence.get("completion_validation") else 0.8,
                    source={"type": "host_completion_gap", "run_id": run_id},
                    durability="session",
                    semantic_key=f"unresolved-goal:{session_id}:{objective.casefold()}",
                    run_id=run_id,
                    session_id=session_id,
                    host_verified=True,
                )
            )
        lessons = [str(item).strip() for item in evidence.get("procedural_lessons") or [] if str(item).strip()]
        if lessons:
            candidates.append(
                MemoryCandidate(
                    content="Verified procedural lesson: " + "; ".join(lessons[:8]),
                    kind="reflection",
                    importance=0.7,
                    future_relevance=0.75,
                    confidence=0.9,
                    source={"type": "host_procedural_lesson", "run_id": run_id},
                    durability="long_term",
                    semantic_key=f"procedural-lesson:{run_id}",
                    run_id=run_id,
                    session_id=session_id,
                    host_verified=True,
                )
            )
        return tuple(candidates)
