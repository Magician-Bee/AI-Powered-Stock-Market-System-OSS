from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ObjectiveVersion:
    objective_id: str
    session_id: str
    revision: int
    objective: str
    added_requirements: tuple[str, ...] = ()
    removed_requirements: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    supersedes: str | None = None
    created_from_message: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("ObjectiveVersion requires a session_id")
        if not self.objective.strip():
            raise ValueError("ObjectiveVersion requires a non-empty objective")
        if self.revision < 1:
            raise ValueError("Objective revision must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "open_stock_ai.objective_version.v1",
            "objective_id": self.objective_id,
            "session_id": self.session_id,
            "revision": self.revision,
            "objective": self.objective,
            "added_requirements": list(self.added_requirements),
            "removed_requirements": list(self.removed_requirements),
            "constraints": list(self.constraints),
            "supersedes": self.supersedes,
            "created_from_message": self.created_from_message,
            "created_at": self.created_at,
        }


class ObjectiveStore(Protocol):
    def save(self, version: ObjectiveVersion) -> None: ...

    def list_for_session(self, session_id: str) -> list[ObjectiveVersion]: ...

    def get(self, objective_id: str) -> ObjectiveVersion | None: ...


class InMemoryObjectiveStore:
    def __init__(self) -> None:
        self._versions: dict[str, ObjectiveVersion] = {}

    def save(self, version: ObjectiveVersion) -> None:
        if version.objective_id in self._versions:
            raise ValueError(f"Objective version already exists: {version.objective_id}")
        if any(
            item.session_id == version.session_id and item.revision == version.revision
            for item in self._versions.values()
        ):
            raise ValueError(
                f"Objective revision {version.revision} already exists for {version.session_id}"
            )
        self._versions[version.objective_id] = version

    def list_for_session(self, session_id: str) -> list[ObjectiveVersion]:
        return sorted(
            (item for item in self._versions.values() if item.session_id == session_id),
            key=lambda item: item.revision,
        )

    def get(self, objective_id: str) -> ObjectiveVersion | None:
        return self._versions.get(objective_id)


class SQLiteObjectiveStore:
    """Adapter for the P71 ``agent_objective_versions`` table.

    This adapter intentionally does not create or migrate tables. The P71 migration
    owns schema creation; this module only defines the domain persistence boundary.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path).expanduser().resolve()

    def save(self, version: ObjectiveVersion) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                insert into agent_objective_versions(
                    objective_id, session_id, revision, objective,
                    added_requirements_json, removed_requirements_json,
                    constraints_json, supersedes, created_from_message, created_at,
                    payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.objective_id,
                    version.session_id,
                    version.revision,
                    version.objective,
                    json.dumps(version.added_requirements, ensure_ascii=False),
                    json.dumps(version.removed_requirements, ensure_ascii=False),
                    json.dumps(version.constraints, ensure_ascii=False),
                    version.supersedes,
                    version.created_from_message,
                    version.created_at,
                    json.dumps(version.to_dict(), ensure_ascii=False),
                ),
            )

    def list_for_session(self, session_id: str) -> list[ObjectiveVersion]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from agent_objective_versions where session_id=? order by revision",
                (session_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, objective_id: str) -> ObjectiveVersion | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_objective_versions where objective_id=?",
                (objective_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ObjectiveVersion:
        return ObjectiveVersion(
            objective_id=row["objective_id"],
            session_id=row["session_id"],
            revision=int(row["revision"]),
            objective=row["objective"],
            added_requirements=tuple(json.loads(row["added_requirements_json"] or "[]")),
            removed_requirements=tuple(
                json.loads(row["removed_requirements_json"] or "[]")
            ),
            constraints=tuple(json.loads(row["constraints_json"] or "[]")),
            supersedes=row["supersedes"],
            created_from_message=row["created_from_message"],
            created_at=row["created_at"],
        )


class ObjectiveManager:
    def __init__(self, store: ObjectiveStore) -> None:
        self.store = store

    def create_initial(
        self,
        *,
        session_id: str,
        objective: str,
        requirements: tuple[str, ...] = (),
        constraints: tuple[str, ...] = (),
        message_id: str | None = None,
    ) -> ObjectiveVersion:
        if self.store.list_for_session(session_id):
            raise ValueError(f"Session already has an objective: {session_id}")
        version = ObjectiveVersion(
            objective_id=f"OBJ-{uuid4().hex}",
            session_id=session_id,
            revision=1,
            objective=objective.strip(),
            added_requirements=_clean(requirements),
            constraints=_clean(constraints),
            created_from_message=message_id,
            created_at=_now(),
        )
        self.store.save(version)
        return version

    def revise(
        self,
        *,
        session_id: str,
        objective: str | None = None,
        added_requirements: tuple[str, ...] = (),
        removed_requirements: tuple[str, ...] = (),
        constraints: tuple[str, ...] | None = None,
        message_id: str | None = None,
    ) -> ObjectiveVersion:
        current = self.current(session_id)
        if current is None:
            raise KeyError(f"Session has no objective: {session_id}")
        resolved_objective = (objective or current.objective).strip()
        resolved_constraints = current.constraints if constraints is None else _clean(constraints)
        version = ObjectiveVersion(
            objective_id=f"OBJ-{uuid4().hex}",
            session_id=session_id,
            revision=current.revision + 1,
            objective=resolved_objective,
            added_requirements=_clean(added_requirements),
            removed_requirements=_clean(removed_requirements),
            constraints=resolved_constraints,
            supersedes=current.objective_id,
            created_from_message=message_id,
            created_at=_now(),
        )
        self.store.save(version)
        return version

    def current(self, session_id: str) -> ObjectiveVersion | None:
        versions = self.store.list_for_session(session_id)
        return versions[-1] if versions else None

    def history(self, session_id: str) -> tuple[ObjectiveVersion, ...]:
        return tuple(self.store.list_for_session(session_id))


def _clean(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in values if item.strip()))
