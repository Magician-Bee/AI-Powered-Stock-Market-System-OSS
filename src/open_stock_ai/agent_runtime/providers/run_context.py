"""Run ownership for model context; durable dialogue and receipts stay untouched."""
from __future__ import annotations

from typing import Any

from .transcript import provider_conversation_history, safe_arguments


def _source(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("source") if isinstance(item.get("source"), dict) else {}


def _owned(item: dict[str, Any], *, run_id: str, session_id: str) -> bool:
    source = _source(item)
    owner = item.get("run_id") or source.get("run_id")
    session = item.get("session_id") or source.get("session_id")
    return owner == run_id and (not session or session == session_id)


def _is_control(item: dict[str, Any]) -> bool:
    source = _source(item)
    content = item.get("content")
    return (
        source.get("type") in {"interaction_response", "control_message"}
        or source.get("routing") == "session_message"
        or item.get("type") in {"interaction_response", "control_message"}
        or isinstance(content, dict) and bool(content.get("interaction_id"))
    )


def scoped_session_history(
    history: list[dict[str, Any]], *, run_id: str, session_id: str, task_kind: str,
) -> list[dict[str, Any]]:
    """Old dialogue can supply context; only this Run's controls supply intent."""
    selected = []
    for item in history:
        if not isinstance(item, dict):
            continue
        session = item.get("session_id") or _source(item).get("session_id")
        if session and session != session_id:
            continue
        if _is_control(item) and not _owned(item, run_id=run_id, session_id=session_id):
            continue
        entry = dict(item)
        if not _owned(item, run_id=run_id, session_id=session_id):
            entry["source"] = {
                **_source(item), "instruction_authority": "none",
                "evidence_scope": "historical_non_evidentiary",
            }
        selected.append(entry)
    return provider_conversation_history(selected, task_kind=task_kind)


def scoped_run_memories(
    memories: list[dict[str, Any]], *, run_id: str, session_id: str,
) -> list[dict[str, Any]]:
    """Scope execution summaries, retaining governed procedural/domain learning.

    MemoryManager.remember_run writes complete past answers as agent_run
    episodes; these are not the procedural lessons extracted separately.
    """
    return [
        item for item in memories if isinstance(item, dict) and (
            not (item.get("kind") == "working" or (
                item.get("kind") == "episodic" and _source(item).get("type") == "agent_run"
            ))
            or _owned(item, run_id=run_id, session_id=session_id)
        )
    ]


def restore_provider_run_context(
    transcript: list[dict[str, Any]], *, session_history: list[dict[str, Any]] | None,
    run_id: str, session_id: str, task_kind: str, owned_control_ids: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Rebind legacy unscoped interactions using authoritative session rows.

    A checkpoint may predate ownership fields or contain an interaction copied
    from another Run. An interaction ID alone is never an ownership proof.
    Host control IDs come from this Run's durable control table, not model data.
    """
    scope = {"run_id": run_id, "session_id": session_id}
    prior_history = next((item.get("content") for item in transcript
                          if item.get("type") == "conversation_history"), [])
    history = scoped_session_history(
        session_history if session_history is not None else list(prior_history or []),
        task_kind=task_kind, **scope,
    )
    interactions = {
        str(item["content"]["interaction_id"]): item
        for item in history
        if _owned(item, **scope) and _source(item).get("type") == "interaction_response"
        and isinstance(item.get("content"), dict) and item["content"].get("interaction_id")
    }
    projected, seen = [], set()
    for original in transcript:
        item, kind = dict(original), original.get("type")
        content = item.get("content")
        if kind == "conversation_history":
            continue
        if kind == "interaction_response":
            identifier = str(content.get("interaction_id") or "") if isinstance(content, dict) else ""
            if identifier in interactions:
                item = {"role": "user", "type": kind, **scope,
                        "content": safe_arguments(interactions[identifier]["content"])}
            elif not _owned(item, **scope):
                continue
            if identifier:
                seen.add(identifier)
        elif kind == "control_message":
            control_id = content.get("control_id") if isinstance(content, dict) else None
            if not _owned(item, **scope) and control_id not in owned_control_ids:
                continue
            item = {**item, **scope}
        elif kind == "retrieved_memory" and isinstance(content, list):
            item["content"] = scoped_run_memories(content, **scope)
        projected.append(item)
    if history:
        projected.insert(1 if projected else 0, {"role": "host", "type": "conversation_history", "content": history})
    for identifier, item in interactions.items():
        if identifier not in seen:
            projected.append({"role": "user", "type": "interaction_response", **scope,
                              "content": safe_arguments(item["content"])})
    return projected
