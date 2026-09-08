from __future__ import annotations

from typing import Any

from .policies import MemoryLayer, canonical_memory_layer
from .store import MemoryStore


class MemoryRetriever:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def relevant(
        self,
        *,
        namespace: str,
        objective: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        items = self.store.search(
            namespace=namespace,
            query=objective,
            session_id=session_id,
            limit=limit,
        )
        preference_recall = _requests_preference_recall(objective)
        total = len(items)
        return [
            {
                "memory_id": item["memory_id"],
                "kind": item["kind"],
                "layer": canonical_memory_layer(item["kind"]).value,
                "fact_type": item["fact_type"],
                "content": item["content"],
                "source": item["source"],
                "updated_at": item["updated_at"],
                "expires_at": item["expires_at"],
                "advisory": canonical_memory_layer(item["kind"])
                is MemoryLayer.USER_PREFERENCE,
                # Keep the durable store's lexical ordering visible to the
                # context broker.  When the user explicitly asks about prior
                # preferences, governed user preferences take precedence over
                # working summaries and generic historical episodes.
                "relevance_score": (
                    (10_000 if preference_recall and item["kind"] == "user_preference" else 0)
                    + max(1, total - index)
                ),
            }
            for index, item in enumerate(items)
        ]


def _requests_preference_recall(objective: str) -> bool:
    value = str(objective or "").casefold()
    return (
        "偏好" in value and any(anchor in value for anchor in ("先前", "之前", "架構", "延續"))
    ) or any(
        phrase in value
        for phrase in ("previous preference", "prior preference", "remembered preference")
    )
