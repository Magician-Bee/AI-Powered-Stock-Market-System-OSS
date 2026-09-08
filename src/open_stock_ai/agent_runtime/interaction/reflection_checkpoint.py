from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ReflectionCheckpoint:
    reflection_id: str
    session_id: str
    branch_id: str
    problem: str
    tentative_judgment: str
    evidence_summary: tuple[str, ...]
    preferred_option: str
    alternatives: tuple[str, ...]
    unknowns: tuple[str, ...]
    important_risks: tuple[str, ...]
    user_decision_question: str | None
    should_ask_user: bool

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        branch_id: str,
        problem: str,
        tentative_judgment: str,
        evidence_summary: tuple[str, ...],
        preferred_option: str,
        alternatives: tuple[str, ...],
        unknowns: tuple[str, ...] = (),
        important_risks: tuple[str, ...] = (),
        user_decision_question: str | None = None,
    ) -> ReflectionCheckpoint:
        if not preferred_option.strip():
            raise ValueError("Reflection must form a preferred option before asking the user")
        if user_decision_question and len(alternatives) < 1:
            raise ValueError("A user decision requires at least one alternative")
        return cls(
            reflection_id=f"REF-{uuid4().hex}",
            session_id=session_id,
            branch_id=branch_id,
            problem=problem.strip(),
            tentative_judgment=tentative_judgment.strip(),
            evidence_summary=tuple(item for item in evidence_summary if item.strip()),
            preferred_option=preferred_option.strip(),
            alternatives=tuple(item for item in alternatives if item.strip()),
            unknowns=tuple(item for item in unknowns if item.strip()),
            important_risks=tuple(item for item in important_risks if item.strip()),
            user_decision_question=(
                user_decision_question.strip() if user_decision_question else None
            ),
            should_ask_user=bool(user_decision_question),
        )

    def public_summary(self) -> dict[str, object]:
        """Return only user-safe reflection fields, never hidden chain-of-thought."""
        return {
            "reflection_id": self.reflection_id,
            "tentative_judgment": self.tentative_judgment,
            "main_evidence": list(self.evidence_summary),
            "preferred_option": self.preferred_option,
            "alternatives": list(self.alternatives),
            "important_risks": list(self.important_risks),
            "unknowns": list(self.unknowns),
            "user_decision_question": self.user_decision_question,
            "should_ask_user": self.should_ask_user,
        }
