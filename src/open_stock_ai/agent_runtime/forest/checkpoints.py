from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4


class CheckpointLevel(StrEnum):
    SESSION = "session"
    RUN = "run"
    BRANCH = "branch"
    STEP = "step"
    DECISION = "decision"
    AUTOMATION = "automation"


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    checkpoint_id: str
    level: CheckpointLevel
    session_id: str
    payload: dict[str, Any]
    run_id: str | None = None
    branch_id: str | None = None
    step_id: str | None = None
    decision_id: str | None = None
    automation_id: str | None = None
    sequence: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class CheckpointStore(Protocol):
    def save(self, checkpoint: CheckpointRecord) -> CheckpointRecord: ...

    def list_for_session(self, session_id: str) -> list[CheckpointRecord]: ...

    def latest_by_scope(
        self,
        *,
        level: CheckpointLevel,
        session_id: str,
        run_id: str | None = None,
        branch_id: str | None = None,
        step_id: str | None = None,
        decision_id: str | None = None,
        automation_id: str | None = None,
    ) -> CheckpointRecord | None: ...


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self._items: list[CheckpointRecord] = []

    def save(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        for existing in self._items:
            if existing.checkpoint_id != checkpoint.checkpoint_id:
                continue
            if _checkpoint_identity(existing) != _checkpoint_identity(checkpoint):
                raise ValueError(
                    f"Checkpoint id {checkpoint.checkpoint_id!r} already has different content"
                )
            return existing
        self._items.append(checkpoint)
        return checkpoint

    def list_for_session(self, session_id: str) -> list[CheckpointRecord]:
        return [item for item in self._items if item.session_id == session_id]

    def latest_by_scope(
        self,
        *,
        level: CheckpointLevel,
        session_id: str,
        run_id: str | None = None,
        branch_id: str | None = None,
        step_id: str | None = None,
        decision_id: str | None = None,
        automation_id: str | None = None,
    ) -> CheckpointRecord | None:
        candidates = [
            item
            for item in self._items
            if _matches_exact_scope(
                item,
                level=level,
                session_id=session_id,
                run_id=run_id,
                branch_id=branch_id,
                step_id=step_id,
                decision_id=decision_id,
                automation_id=automation_id,
            )
        ]
        return _latest(candidates)


class ScopedCheckpointManager:
    _specificity = {
        CheckpointLevel.STEP: 0,
        CheckpointLevel.DECISION: 1,
        CheckpointLevel.AUTOMATION: 2,
        CheckpointLevel.BRANCH: 3,
        CheckpointLevel.RUN: 4,
        CheckpointLevel.SESSION: 5,
    }

    def __init__(self, store: CheckpointStore) -> None:
        self.store = store

    def save(
        self,
        *,
        level: CheckpointLevel,
        session_id: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        branch_id: str | None = None,
        step_id: str | None = None,
        decision_id: str | None = None,
        automation_id: str | None = None,
        sequence: int = 0,
        checkpoint_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CheckpointRecord:
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("Checkpoint session_id is required")
        required = {
            CheckpointLevel.RUN: run_id,
            CheckpointLevel.BRANCH: branch_id,
            CheckpointLevel.STEP: step_id,
            CheckpointLevel.DECISION: decision_id,
            CheckpointLevel.AUTOMATION: automation_id,
        }
        if level in required and not required[level]:
            raise ValueError(f"{level.value} checkpoint requires its scoped identifier")
        if int(sequence) < 0:
            raise ValueError("Checkpoint sequence must be zero or greater")
        resolved_id = str(checkpoint_id or "").strip()
        if checkpoint_id is not None and not resolved_id:
            raise ValueError("checkpoint_id cannot be blank")
        if idempotency_key is not None:
            normalized_key = str(idempotency_key).strip()
            if not normalized_key:
                raise ValueError("idempotency_key cannot be blank")
            deterministic_id = _idempotent_checkpoint_id(
                level=level,
                session_id=session_id,
                idempotency_key=normalized_key,
            )
            if resolved_id and resolved_id != deterministic_id:
                raise ValueError("checkpoint_id conflicts with idempotency_key")
            resolved_id = deterministic_id
        item = CheckpointRecord(
            checkpoint_id=resolved_id or f"CP-{uuid4().hex}",
            level=level,
            session_id=session_id,
            run_id=run_id,
            branch_id=branch_id,
            step_id=step_id,
            decision_id=decision_id,
            automation_id=automation_id,
            sequence=int(sequence),
            payload=dict(payload),
        )
        return self.store.save(item)

    def latest_by_scope(
        self,
        *,
        level: CheckpointLevel,
        session_id: str,
        run_id: str | None = None,
        branch_id: str | None = None,
        step_id: str | None = None,
        decision_id: str | None = None,
        automation_id: str | None = None,
    ) -> CheckpointRecord | None:
        return self.store.latest_by_scope(
            level=level,
            session_id=session_id,
            run_id=run_id,
            branch_id=branch_id,
            step_id=step_id,
            decision_id=decision_id,
            automation_id=automation_id,
        )

    def restore_smallest(
        self,
        *,
        session_id: str,
        run_id: str | None = None,
        branch_id: str | None = None,
        step_id: str | None = None,
        decision_id: str | None = None,
        automation_id: str | None = None,
    ) -> CheckpointRecord | None:
        candidates = []
        for item in self.store.list_for_session(session_id):
            if item.run_id and item.run_id != run_id:
                continue
            if item.branch_id and item.branch_id != branch_id:
                continue
            if item.step_id and item.step_id != step_id:
                continue
            if item.decision_id and item.decision_id != decision_id:
                continue
            if item.automation_id and item.automation_id != automation_id:
                continue
            candidates.append(item)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                -self._specificity[item.level],
                item.sequence,
                item.created_at,
                item.checkpoint_id,
            ),
        )


def _matches_exact_scope(
    item: CheckpointRecord,
    *,
    level: CheckpointLevel,
    session_id: str,
    run_id: str | None,
    branch_id: str | None,
    step_id: str | None,
    decision_id: str | None,
    automation_id: str | None,
) -> bool:
    if item.level != level or item.session_id != session_id:
        return False
    scope_identifiers = {
        CheckpointLevel.SESSION: (),
        CheckpointLevel.RUN: (("run_id", run_id),),
        CheckpointLevel.BRANCH: (("branch_id", branch_id),),
        CheckpointLevel.STEP: (("step_id", step_id),),
        CheckpointLevel.DECISION: (("decision_id", decision_id),),
        CheckpointLevel.AUTOMATION: (("automation_id", automation_id),),
    }
    for attribute, expected in scope_identifiers[level]:
        if not expected or getattr(item, attribute) != expected:
            return False
    optional_parents = {
        "run_id": run_id,
        "branch_id": branch_id,
        "step_id": step_id,
        "decision_id": decision_id,
        "automation_id": automation_id,
    }
    return all(
        expected is None or getattr(item, attribute) == expected
        for attribute, expected in optional_parents.items()
    )


def _latest(items: list[CheckpointRecord]) -> CheckpointRecord | None:
    if not items:
        return None
    return max(
        items,
        key=lambda item: (item.sequence, item.created_at, item.checkpoint_id),
    )


def _checkpoint_identity(item: CheckpointRecord) -> tuple[Any, ...]:
    return (
        item.checkpoint_id,
        item.level,
        item.session_id,
        item.run_id,
        item.branch_id,
        item.step_id,
        item.decision_id,
        item.automation_id,
        item.sequence,
        json.dumps(
            item.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )


def _idempotent_checkpoint_id(
    *,
    level: CheckpointLevel,
    session_id: str,
    idempotency_key: str,
) -> str:
    encoded = json.dumps(
        [level.value, session_id, idempotency_key],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"CP-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"
