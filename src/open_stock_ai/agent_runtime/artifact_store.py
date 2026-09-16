from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection

from .artifacts import ArtifactVersionManager, SQLiteArtifactVersionStore


class ArtifactStore:
    STRUCTURED_MEDIA_TYPE = "application/vnd.open-stock-ai.structured+json"
    STRUCTURED_RENDERERS = {
        "table", "chart", "graph", "timeline", "canvas", "map", "fishbone",
        "dag", "swimlane", "workflow", "gantt", "evidence_graph", "task_forest",
        "decision_matrix", "risk_table", "risk_bar", "risk_radar",
    }

    def __init__(self, db_path: str | Path, root: Path) -> None:
        self.path = Path(db_path).expanduser().resolve()
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        with self._connect() as conn:
            apply_migrations(conn)
        # Artifact metadata and its immutable content lineage are one runtime
        # authority.  Keeping the manager on this store prevents production
        # writers from creating an artifact first and relying on a later
        # snapshot/event projection to invent version 1.
        self.versions = ArtifactVersionManager(SQLiteArtifactVersionStore(self.path))

    def create_text(
        self,
        *,
        session_id: str,
        run_id: str,
        name: str,
        content: str,
        media_type: str = "text/plain",
        kind: str = "report",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_id = f"AA-{uuid4().hex}"
        run_root = (self.root / run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        run_root.chmod(0o700)
        safe_name = "".join(character if character.isalnum() or character in "._-" else "_" for character in name)
        target = run_root / (safe_name or f"{artifact_id}.txt")
        target.write_text(content, encoding="utf-8")
        target.chmod(0o600)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_artifacts(
                    artifact_id, session_id, run_id, kind, name, path, media_type,
                    sha256, created_at, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    session_id,
                    run_id,
                    kind,
                    name,
                    str(target),
                    media_type,
                    digest,
                    created_at,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        self.versions.create(
            artifact_id=artifact_id,
            content=self._version_content(
                name=name,
                kind=kind,
                media_type=media_type,
                digest=digest,
                content=content,
                metadata=metadata or {},
            ),
            changed_by="artifact_store",
            reason="Initial artifact creation",
        )
        return {
            "artifact_id": artifact_id,
            "session_id": session_id,
            "run_id": run_id,
            "kind": kind,
            "name": name,
            "path": str(target),
            "media_type": media_type,
            "sha256": digest,
            "created_at": created_at,
            "metadata": metadata or {},
        }

    def create_structured(
        self,
        *,
        session_id: str,
        run_id: str,
        name: str,
        renderer: str,
        document: dict[str, Any] | list[Any],
        schema_version: str,
        branch_id: str | None = None,
        node_id: str | None = None,
        evidence_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a renderer-ready, provenance-bound structured artifact.

        The renderer is data, not executable code.  Keeping the validated
        document in the same immutable artifact store as text reports gives
        the UI a durable source for graph/chart/table/canvas rendering and
        gives node-chat a stable artifact + node coordinate to target.
        """
        renderer = str(renderer or "").strip().casefold()
        if renderer not in self.STRUCTURED_RENDERERS:
            raise ValueError(f"Unsupported structured artifact renderer: {renderer}")
        if not str(schema_version or "").strip():
            raise ValueError("schema_version is required for structured artifacts")
        if not isinstance(document, (dict, list)):
            raise TypeError("Structured artifact document must be an object or array")
        payload = {
            "schema_version": str(schema_version),
            "renderer": renderer,
            "document": document,
        }
        metadata_payload = {
            **dict(metadata or {}),
            "renderer": renderer,
            "schema_version": str(schema_version),
            "branch_id": branch_id,
            "node_id": node_id,
            "evidence_ids": [str(item) for item in evidence_ids or [] if str(item).strip()],
            "interactive": True,
            "provenance": {
                "session_id": session_id,
                "run_id": run_id,
                "branch_id": branch_id,
                "node_id": node_id,
            },
        }
        return self.create_text(
            session_id=session_id,
            run_id=run_id,
            name=name,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type=self.STRUCTURED_MEDIA_TYPE,
            kind="structured_visualization",
            metadata=metadata_payload,
        ) | {
            "renderer": renderer,
            "schema_version": str(schema_version),
            "document": document,
            "branch_id": branch_id,
            "node_id": node_id,
            "evidence_ids": metadata_payload["evidence_ids"],
            "interactive": True,
        }

    def list(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from agent_artifacts where run_id=? order by created_at",
                (run_id,),
            ).fetchall()
        return [self._inflate(dict(row)) for row in rows]

    def get(self, run_id: str, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select * from agent_artifacts
                 where run_id=? and artifact_id=?
                """,
                (run_id, artifact_id),
            ).fetchone()
        if row is None:
            return None
        return self._inflate(dict(row))

    def get_by_id(self, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from agent_artifacts where artifact_id=?",
                (artifact_id,),
            ).fetchone()
        return self._inflate(dict(row)) if row is not None else None

    def replace_structured(
        self,
        *,
        artifact_id: str,
        renderer: str,
        document: dict[str, Any] | list[Any],
        schema_version: str,
    ) -> dict[str, Any]:
        """Atomically project the latest validated structured version to its file."""

        artifact = self.get_by_id(artifact_id)
        if artifact is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        if artifact.get("media_type") != self.STRUCTURED_MEDIA_TYPE:
            raise ValueError(f"Artifact is not structured: {artifact_id}")
        renderer = str(renderer or "").strip().casefold()
        if renderer not in self.STRUCTURED_RENDERERS:
            raise ValueError(f"Unsupported structured artifact renderer: {renderer}")
        if not str(schema_version or "").strip():
            raise ValueError("schema_version is required for structured artifacts")
        if not isinstance(document, (dict, list)):
            raise TypeError("Structured artifact document must be an object or array")
        payload = {
            "schema_version": str(schema_version),
            "renderer": renderer,
            "document": document,
        }
        path = Path(str(artifact.get("path") or "")).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Structured artifact path is unavailable or outside the artifact store")
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(encoded)
        temporary.chmod(0o600)
        temporary.replace(path)
        digest = hashlib.sha256(encoded).hexdigest()
        metadata = {
            **dict(artifact.get("metadata") or {}),
            "renderer": renderer,
            "schema_version": str(schema_version),
        }
        with self._connect() as conn:
            conn.execute(
                "update agent_artifacts set sha256=?, metadata_json=? where artifact_id=?",
                (digest, json.dumps(metadata, ensure_ascii=False), artifact_id),
            )
            conn.commit()
        updated = self.get_by_id(artifact_id)
        if updated is None:  # pragma: no cover - guarded by the existing row
            raise KeyError(f"Unknown artifact: {artifact_id}")
        return updated

    def replace_text(self, *, artifact_id: str, content: str) -> dict[str, Any]:
        artifact = self.get_by_id(artifact_id)
        if artifact is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        if not str(artifact.get("media_type") or "").startswith("text/"):
            raise ValueError(f"Artifact is not text: {artifact_id}")
        path = Path(str(artifact.get("path") or "")).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("Text artifact path is unavailable or outside the artifact store")
        encoded = str(content).encode("utf-8")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(encoded)
        temporary.chmod(0o600)
        temporary.replace(path)
        digest = hashlib.sha256(encoded).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "update agent_artifacts set sha256=? where artifact_id=?",
                (digest, artifact_id),
            )
            conn.commit()
        updated = self.get_by_id(artifact_id)
        if updated is None:  # pragma: no cover - guarded by the existing row
            raise KeyError(f"Unknown artifact: {artifact_id}")
        return updated

    def _inflate(self, row: dict[str, Any]) -> dict[str, Any]:
        artifact = {
            **row,
            "metadata": json.loads(str(row.get("metadata_json") or "{}")),
        }
        if artifact.get("media_type") != self.STRUCTURED_MEDIA_TYPE:
            return artifact
        path = Path(str(artifact.get("path") or "")).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return artifact
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return artifact
        if not isinstance(payload, dict):
            return artifact
        return {
            **artifact,
            "renderer": payload.get("renderer") or artifact["metadata"].get("renderer"),
            "schema_version": payload.get("schema_version") or artifact["metadata"].get("schema_version"),
            "document": payload.get("document"),
            "visualization": {
                **(payload.get("document") if isinstance(payload.get("document"), dict) else {"items": payload.get("document")}),
                "renderer": payload.get("renderer") or artifact["metadata"].get("renderer"),
                "schema_version": payload.get("schema_version") or artifact["metadata"].get("schema_version"),
            },
        }

    @staticmethod
    def _version_content(
        *,
        name: str,
        kind: str,
        media_type: str,
        digest: str,
        content: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the path-free immutable v1 payload for a new artifact."""

        if media_type == ArtifactStore.STRUCTURED_MEDIA_TYPE:
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and {"renderer", "schema_version", "document"} <= set(payload):
                return {
                    "renderer": payload["renderer"],
                    "schema_version": payload["schema_version"],
                    "document": payload["document"],
                }
        return {
            "name": name,
            "kind": kind,
            "media_type": media_type or None,
            "sha256": digest,
            "content": content,
            "metadata": dict(metadata),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        return conn
