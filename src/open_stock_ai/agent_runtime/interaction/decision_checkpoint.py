from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class WaitingState(StrEnum):
    USER_INPUT = "waiting_user_input"
    DECISION = "waiting_decision"
    APPROVAL = "waiting_approval"


@dataclass(frozen=True, slots=True)
class DecisionOption:
    option_id: str
    label: str
    reason: str
    recommended: bool = False
    consequences: tuple[str, ...] = ()


@dataclass(slots=True)
class InteractionRequest:
    interaction_id: str
    session_id: str
    branch_id: str | None
    waiting_state: WaitingState
    prompt: str
    resolved: bool = False
    response: dict[str, Any] | None = None

    @classmethod
    def clarification(
        cls,
        *,
        session_id: str,
        prompt: str,
        branch_id: str | None = None,
    ) -> InteractionRequest:
        return cls(
            interaction_id=f"INT-{uuid4().hex}",
            session_id=session_id,
            branch_id=branch_id,
            waiting_state=WaitingState.USER_INPUT,
            prompt=prompt,
        )

    @classmethod
    def approval(
        cls,
        *,
        session_id: str,
        prompt: str,
        branch_id: str | None = None,
    ) -> InteractionRequest:
        return cls(
            interaction_id=f"INT-{uuid4().hex}",
            session_id=session_id,
            branch_id=branch_id,
            waiting_state=WaitingState.APPROVAL,
            prompt=prompt,
        )


@dataclass(slots=True)
class DecisionCheckpoint:
    interaction_id: str
    session_id: str
    question: str
    agent_preferred_option: str
    agent_view: str
    options: tuple[DecisionOption, ...]
    allow_free_text: bool = True
    branch_id: str | None = None
    reflection_id: str | None = None
    waiting_state: WaitingState = WaitingState.DECISION
    resolved: bool = False
    selected_option_id: str | None = None
    free_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.options) < 2:
            raise ValueError("Decision checkpoint requires at least two options")
        option_ids = [item.option_id for item in self.options]
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("Decision option ids must be unique")
        if self.agent_preferred_option not in option_ids:
            raise ValueError("Agent preferred option must be present in options")
        preferred = next(
            item for item in self.options if item.option_id == self.agent_preferred_option
        )
        if not preferred.recommended:
            raise ValueError("Agent preferred option must be marked recommended")
        if self.waiting_state != WaitingState.DECISION:
            raise ValueError("DecisionCheckpoint must use waiting_decision")

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        question: str,
        agent_preferred_option: str,
        agent_view: str,
        options: tuple[DecisionOption, ...],
        allow_free_text: bool = True,
        branch_id: str | None = None,
        reflection_id: str | None = None,
    ) -> DecisionCheckpoint:
        return cls(
            interaction_id=f"DEC-{uuid4().hex}",
            session_id=session_id,
            question=question,
            agent_preferred_option=agent_preferred_option,
            agent_view=agent_view,
            options=options,
            allow_free_text=allow_free_text,
            branch_id=branch_id,
            reflection_id=reflection_id,
        )

    def respond(
        self,
        *,
        option_id: str | None = None,
        free_text: str | None = None,
    ) -> dict[str, Any]:
        if self.resolved:
            raise ValueError("Decision checkpoint is already resolved")
        if option_id is not None and option_id not in {
            item.option_id for item in self.options
        }:
            raise ValueError(f"Unknown decision option: {option_id}")
        cleaned_text = free_text.strip() if free_text else None
        if cleaned_text and not self.allow_free_text:
            raise ValueError("This decision checkpoint does not allow free text")
        if option_id is None and not cleaned_text:
            raise ValueError("Decision response requires an option or free text")
        self.selected_option_id = option_id
        self.free_text = cleaned_text
        self.resolved = True
        return {
            "interaction_id": self.interaction_id,
            "selected_option_id": option_id,
            "free_text": cleaned_text,
            "used_agent_preference": option_id == self.agent_preferred_option,
        }

    def public_payload(self) -> dict[str, Any]:
        return {
            "interaction_id": self.interaction_id,
            "interaction_type": "decision_checkpoint",
            "waiting_state": self.waiting_state.value,
            "question": self.question,
            "agent_preferred_option": self.agent_preferred_option,
            "agent_view": self.agent_view,
            "options": [
                {
                    "id": item.option_id,
                    "label": item.label,
                    "reason": item.reason,
                    "recommended": item.recommended,
                    "consequences": list(item.consequences),
                }
                for item in self.options
            ],
            "allow_free_text": self.allow_free_text,
            "resolved": self.resolved,
        }
