from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .versioning import ArtifactVersionStore, OptimisticVersionConflict


@dataclass(frozen=True, slots=True)
class ArtifactSelection:
    selection_id: str
    session_id: str
    artifact_id: str
    artifact_version: int
    selector: str
    branch_id: str | None = None
    node_id: str | None = None
    evidence_id: str | None = None
    context_label: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SelectionContextManager:
    def __init__(self, version_store: ArtifactVersionStore) -> None:
        self.version_store = version_store
        self._active_by_session: dict[str, ArtifactSelection] = {}

    def select(
        self,
        *,
        session_id: str,
        artifact_id: str,
        artifact_version: int,
        selector: str,
        branch_id: str | None = None,
        node_id: str | None = None,
        evidence_id: str | None = None,
        context_label: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactSelection:
        if self.version_store.get_version(artifact_id, artifact_version) is None:
            raise KeyError(f"Unknown artifact version: {artifact_id} v{artifact_version}")
        if not selector.strip():
            raise ValueError("Artifact selection requires an exact selector")
        item = ArtifactSelection(
            selection_id=f"ASEL-{uuid4().hex}",
            session_id=session_id,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            selector=selector,
            branch_id=branch_id,
            node_id=node_id,
            evidence_id=evidence_id,
            context_label=context_label,
            metadata=dict(metadata or {}),
        )
        self._active_by_session[session_id] = item
        return item

    def active(self, session_id: str) -> ArtifactSelection | None:
        return self._active_by_session.get(session_id)

    def clear(self, session_id: str) -> None:
        self._active_by_session.pop(session_id, None)

    def assert_current(self, selection: ArtifactSelection) -> None:
        versions = self.version_store.list_versions(selection.artifact_id)
        if not versions:
            raise KeyError(f"Unknown artifact: {selection.artifact_id}")
        current = versions[-1].version
        if current != selection.artifact_version:
            raise OptimisticVersionConflict(
                selection.artifact_id,
                selection.artifact_version,
                current,
            )

    def change_request(self, session_id: str, instruction: str) -> dict[str, Any]:
        selection = self.active(session_id)
        if selection is None:
            raise ValueError("No active artifact selection")
        self.assert_current(selection)
        return {
            "operation": "propose_change",
            "selection_id": selection.selection_id,
            "artifact_id": selection.artifact_id,
            "expected_version": selection.artifact_version,
            "selector": selection.selector,
            "instruction": instruction.strip(),
            "branch_id": selection.branch_id,
            "node_id": selection.node_id,
            "evidence_id": selection.evidence_id,
        }
