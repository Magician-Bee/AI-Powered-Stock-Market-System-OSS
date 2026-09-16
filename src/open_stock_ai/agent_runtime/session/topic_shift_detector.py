from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TopicShift:
    shifted: bool
    similarity: float
    previous_terms: tuple[str, ...]
    current_terms: tuple[str, ...]


class TopicShiftDetector:
    """Deterministic first-pass detector; a model may review borderline cases."""

    def __init__(self, *, similarity_threshold: float = 0.22) -> None:
        self.similarity_threshold = max(0.0, min(float(similarity_threshold), 1.0))

    def detect(self, previous_intent: str, current_intent: str) -> TopicShift:
        previous = _terms(previous_intent)
        current = _terms(current_intent)
        if not previous or not current:
            similarity = 0.0
        else:
            overlap = len(previous & current)
            # A follow-up often keeps only the company/entity and one decision
            # phrase, so pure Jaccard similarity over-penalizes added detail.
            similarity = max(
                overlap / len(previous | current),
                overlap / min(len(previous), len(current)),
            )
        return TopicShift(
            shifted=bool(previous_intent.strip()) and similarity < self.similarity_threshold,
            similarity=round(similarity, 4),
            previous_terms=tuple(sorted(previous)),
            current_terms=tuple(sorted(current)),
        )


def _terms(text: str) -> set[str]:
    normalized = text.casefold()
    terms = set(re.findall(r"[a-z0-9_.-]{2,}", normalized))
    for sequence in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(sequence) == 1:
            terms.add(sequence)
        else:
            terms.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return terms
