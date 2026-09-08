from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OptimisticVersionConflict(RuntimeError):
    def __init__(self, artifact_id: str, expected_version: int, current_version: int) -> None:
        self.artifact_id = artifact_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"Artifact {artifact_id} changed from expected v{expected_version} "
            f"to v{current_version}"
        )


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    artifact_version_id: str
    artifact_id: str
    version: int
    content: Any
    changed_by: str
    reason: str
    message_id: str | None = None
    base_version: int | None = None
    affected_node_ids: tuple[str, ...] = ()
    validation_result: dict[str, Any] = field(default_factory=dict)
    restored_from_version: int | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Artifact version must be positive")
        if not self.changed_by.strip():
            raise ValueError("Artifact version requires changed_by")
        if not self.reason.strip():
            raise ValueError("Artifact version requires a reason")


class ArtifactVersionStore(Protocol):
    def save(self, version: ArtifactVersion) -> None: ...

    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]: ...

    def get_version(self, artifact_id: str, version: int) -> ArtifactVersion | None: ...


class InMemoryArtifactVersionStore:
    def __init__(self) -> None:
        self._versions: dict[str, list[ArtifactVersion]] = {}

    def save(self, version: ArtifactVersion) -> None:
        versions = self._versions.setdefault(version.artifact_id, [])
        if any(item.version == version.version for item in versions):
            raise ValueError(
                f"Artifact version already exists: {version.artifact_id} v{version.version}"
            )
        versions.append(version)
        versions.sort(key=lambda item: item.version)

    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        return list(self._versions.get(artifact_id, ()))

    def get_version(self, artifact_id: str, version: int) -> ArtifactVersion | None:
        return next(
            (
                item
                for item in self._versions.get(artifact_id, ())
                if item.version == version
            ),
            None,
        )


class SQLiteArtifactVersionStore:
    """Adapter for P71 artifact version rows; migrations are owned elsewhere."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()

    def save(self, version: ArtifactVersion) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                insert into agent_artifact_versions(
                    artifact_version_id, artifact_id, version, content_json,
                    changed_by, change_reason, reason, message_id, parent_version, base_version,
                    affected_node_ids_json, validation_result_json,
                    restored_from_version, validation_status, sha256, created_at,
                    payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.artifact_version_id,
                    version.artifact_id,
                    version.version,
                    json.dumps(version.content, ensure_ascii=False, default=str),
                    version.changed_by,
                    version.reason,
                    version.reason,
                    version.message_id,
                    version.base_version,
                    version.base_version,
                    json.dumps(version.affected_node_ids, ensure_ascii=False),
                    json.dumps(version.validation_result, ensure_ascii=False),
                    version.restored_from_version,
                    "valid" if version.validation_result.get("valid", True) else "invalid",
                    hashlib.sha256(
                        json.dumps(version.content, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                    ).hexdigest(),
                    version.created_at,
                    json.dumps({"content": version.content}, ensure_ascii=False, default=str),
                ),
            )

    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_artifact_versions
                 where artifact_id=? order by version
                """,
                (artifact_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get_version(self, artifact_id: str, version: int) -> ArtifactVersion | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select * from agent_artifact_versions
                 where artifact_id=? and version=?
                """,
                (artifact_id, version),
            ).fetchone()
        return self._from_row(row) if row else None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ArtifactVersion:
        return ArtifactVersion(
            artifact_version_id=row["artifact_version_id"],
            artifact_id=row["artifact_id"],
            version=int(row["version"]),
            content=json.loads(row["content_json"]),
            changed_by=row["changed_by"],
            reason=row["reason"],
            message_id=row["message_id"],
            base_version=row["base_version"],
            affected_node_ids=tuple(json.loads(row["affected_node_ids_json"] or "[]")),
            validation_result=json.loads(row["validation_result_json"] or "{}"),
            restored_from_version=row["restored_from_version"],
            created_at=row["created_at"],
        )


class ArtifactVersionManager:
    def __init__(self, store: ArtifactVersionStore) -> None:
        self.store = store

    def create(
        self,
        *,
        artifact_id: str,
        content: Any,
        changed_by: str,
        reason: str,
        message_id: str | None = None,
        affected_node_ids: tuple[str, ...] = (),
        validation_result: dict[str, Any] | None = None,
    ) -> ArtifactVersion:
        if self.store.list_versions(artifact_id):
            raise ValueError(f"Artifact already has versions: {artifact_id}")
        return self._save(
            artifact_id=artifact_id,
            version=1,
            content=content,
            changed_by=changed_by,
            reason=reason,
            message_id=message_id,
            base_version=None,
            affected_node_ids=affected_node_ids,
            validation_result=validation_result,
        )

    def revise(
        self,
        *,
        artifact_id: str,
        expected_version: int,
        content: Any,
        changed_by: str,
        reason: str,
        message_id: str | None = None,
        affected_node_ids: tuple[str, ...] = (),
        validation_result: dict[str, Any] | None = None,
    ) -> ArtifactVersion:
        latest = self.latest(artifact_id)
        if latest is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        if latest.version != expected_version:
            raise OptimisticVersionConflict(
                artifact_id,
                expected_version,
                latest.version,
            )
        return self._save(
            artifact_id=artifact_id,
            version=latest.version + 1,
            content=content,
            changed_by=changed_by,
            reason=reason,
            message_id=message_id,
            base_version=latest.version,
            affected_node_ids=affected_node_ids,
            validation_result=validation_result,
        )

    def restore(
        self,
        *,
        artifact_id: str,
        source_version: int,
        expected_version: int,
        changed_by: str,
        reason: str,
        message_id: str | None = None,
    ) -> ArtifactVersion:
        source = self.store.get_version(artifact_id, source_version)
        if source is None:
            raise KeyError(f"Unknown artifact version: {artifact_id} v{source_version}")
        latest = self.latest(artifact_id)
        if latest is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        if latest.version != expected_version:
            raise OptimisticVersionConflict(artifact_id, expected_version, latest.version)
        return self._save(
            artifact_id=artifact_id,
            version=latest.version + 1,
            content=source.content,
            changed_by=changed_by,
            reason=reason,
            message_id=message_id,
            base_version=latest.version,
            affected_node_ids=source.affected_node_ids,
            validation_result=source.validation_result,
            restored_from_version=source_version,
        )

    def undo(
        self,
        *,
        artifact_id: str,
        expected_version: int,
        changed_by: str,
        message_id: str | None = None,
    ) -> ArtifactVersion:
        if expected_version <= 1:
            raise ValueError("Initial artifact version cannot be undone")
        return self.restore(
            artifact_id=artifact_id,
            source_version=expected_version - 1,
            expected_version=expected_version,
            changed_by=changed_by,
            reason=f"Undo artifact version {expected_version}",
            message_id=message_id,
        )

    def latest(self, artifact_id: str) -> ArtifactVersion | None:
        versions = self.store.list_versions(artifact_id)
        return versions[-1] if versions else None

    def compare(self, artifact_id: str, left_version: int, right_version: int) -> str:
        left = self.store.get_version(artifact_id, left_version)
        right = self.store.get_version(artifact_id, right_version)
        if left is None or right is None:
            raise KeyError("Cannot compare missing artifact versions")
        left_text = _content_text(left.content).splitlines(keepends=True)
        right_text = _content_text(right.content).splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                left_text,
                right_text,
                fromfile=f"{artifact_id}-v{left_version}",
                tofile=f"{artifact_id}-v{right_version}",
            )
        )

    def _save(
        self,
        *,
        artifact_id: str,
        version: int,
        content: Any,
        changed_by: str,
        reason: str,
        message_id: str | None,
        base_version: int | None,
        affected_node_ids: tuple[str, ...],
        validation_result: dict[str, Any] | None,
        restored_from_version: int | None = None,
    ) -> ArtifactVersion:
        item = ArtifactVersion(
            artifact_version_id=f"AV-{uuid4().hex}",
            artifact_id=artifact_id,
            version=version,
            content=content,
            changed_by=changed_by,
            reason=reason,
            message_id=message_id,
            base_version=base_version,
            affected_node_ids=affected_node_ids,
            validation_result=dict(validation_result or {}),
            restored_from_version=restored_from_version,
            created_at=_now(),
        )
        self.store.save(item)
        return item


def _content_text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(
        content,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
