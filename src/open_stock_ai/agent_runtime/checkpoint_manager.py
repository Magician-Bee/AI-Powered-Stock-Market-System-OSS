from __future__ import annotations

from typing import Any

from .checkpoint_store import CheckpointStore
from .plan_graph import PlanGraph


class CheckpointManager:
    def __init__(self, store: CheckpointStore) -> None:
        self.store = store

    def save(
        self,
        *,
        session_id: str,
        run_id: str,
        sequence: int,
        plan: PlanGraph,
        transcript: list[dict[str, Any]],
        trace: list[dict[str, Any]],
        context_state: dict[str, Any],
        rollback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "plan": plan.to_dict(),
            "transcript": transcript,
            "trace": trace,
            "context_state": _serializable_state(context_state),
        }
        return self.store.create(
            session_id=session_id,
            run_id=run_id,
            plan_revision=plan.revision_number,
            sequence=sequence,
            payload=payload,
            rollback=rollback,
        )

    def restore(self, run_id: str) -> dict[str, Any] | None:
        return self.store.latest(run_id)


def _serializable_state(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"record_event", "cancel_event"} and _is_json_value(item)
    }


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False
