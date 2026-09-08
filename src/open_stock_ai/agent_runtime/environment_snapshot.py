from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .contracts import AgentRunContext
from .context_broker import ContextBroker


@dataclass(frozen=True, slots=True)
class EnvironmentSnapshot:
    snapshot_id: str
    created_at: str
    hash: str
    state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.environment_snapshot.v1",
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "hash": self.hash,
            **self.state,
        }


class EnvironmentSnapshotBuilder:
    def __init__(
        self,
        *,
        project_root: Path,
        capability_manifest: Callable[[], list[dict[str, Any]]],
        ui_snapshot: Callable[[], dict[str, Any]],
        account_snapshot: Callable[[], dict[str, Any]] | None = None,
        external_snapshot: Callable[[], dict[str, Any]] | None = None,
        context_broker: ContextBroker | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.capability_manifest = capability_manifest
        self.ui_snapshot = ui_snapshot
        self.account_snapshot = account_snapshot
        self.external_snapshot = external_snapshot
        self.context_broker = context_broker or ContextBroker()

    def build(
        self,
        context: AgentRunContext,
        *,
        plan: dict[str, Any] | None = None,
        pending_approvals: list[dict[str, Any]] | None = None,
        memories: list[dict[str, Any]] | None = None,
    ) -> EnvironmentSnapshot:
        task_kind = str(context.state.get("task_kind") or "general_answer")
        exposure = self.context_broker.profile(task_kind)
        capability_manifest = self.context_broker.filter_capabilities(
            self.capability_manifest(),
            task_kind=task_kind,
        )
        capability_snapshot = [
            {
                "name": item.get("name"),
                "category": item.get("category"),
                "risk_class": item.get("risk_class"),
                "required_permissions": item.get("required_permissions") or [],
                "execution_backend": item.get("execution_backend"),
            }
            for item in capability_manifest
        ]
        capability_encoded = json.dumps(
            capability_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        state = {
            "session": {"session_id": context.session_id, "run_id": context.run_id},
            "context_exposure": {
                "task_kind": task_kind,
                "ui": exposure.include_ui,
                "market": exposure.include_market,
                "account": exposure.include_account,
                "project": exposure.include_project,
                "external_frameworks": exposure.include_external,
                "memory": exposure.include_memory,
                "capability_prefixes": list(exposure.capability_prefixes),
            },
            "ui": self.ui_snapshot() if exposure.include_ui else {"exposed": False},
            "market": {
                "symbols": list(context.symbols) if exposure.include_market else [],
                "exposed": exposure.include_market,
                "fresh_values_must_come_from_capabilities": True,
            },
            "account": (
                self.account_snapshot()
                if exposure.include_account and self.account_snapshot
                else {"available": False, "exposed": False}
            ),
            "risk": {
                "autonomy": context.autonomy,
                "live_trading": False,
                "paper_execution": context.allow_paper_orders,
                "project_execution": context.allow_project_actions,
                "external_execution": context.allow_external_actions,
            },
            "project": (
                _project_state(
                    self.project_root,
                    active_paths=context.state.get("active_files") or (),
                )
                if exposure.include_project
                else {"exposed": False}
            ),
            "capabilities": {
                "count": len(capability_snapshot),
                "manifest_hash": hashlib.sha256(capability_encoded.encode("utf-8")).hexdigest(),
                "items": capability_snapshot,
            },
            "external_frameworks": (
                self.external_snapshot()
                if exposure.include_external and self.external_snapshot
                else {"exposed": False}
            ),
            "memory": list(memories or []) if exposure.include_memory else [],
            "task": {"plan": plan or {}, "state": dict(context.state.get("task_state") or {})},
            "approvals": list(pending_approvals or []),
        }
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return EnvironmentSnapshot(
            snapshot_id=f"ES-{digest[:24]}",
            created_at=datetime.now(timezone.utc).isoformat(),
            hash=digest,
            state=state,
        )

    @staticmethod
    def diff(before: EnvironmentSnapshot, after: EnvironmentSnapshot) -> dict[str, Any]:
        changed = {
            key: after.state.get(key)
            for key in after.state
            if before.state.get(key) != after.state.get(key)
        }
        return {
            "schema_version": "open_stock_ai.environment_diff.v1",
            "before_hash": before.hash,
            "after_hash": after.hash,
            "changed": changed,
        }


def _project_state(
    project_root: Path,
    *,
    active_paths: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    command = ["git", "status", "--porcelain=v1", "--branch"]
    try:
        result = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"root": str(project_root), "git_available": False, "error": type(exc).__name__}
    lines = result.stdout.splitlines()
    changed_paths = [line[3:] for line in lines[1:] if len(line) > 3]
    requested_paths = [str(item) for item in active_paths if str(item).strip()]
    content_hashes: dict[str, str | None] = {}
    for value in dict.fromkeys([*changed_paths, *requested_paths]):
        candidate = (project_root / value).resolve()
        if candidate != project_root and project_root not in candidate.parents:
            continue
        if not candidate.is_file() or candidate.is_symlink():
            content_hashes[value] = None
            continue
        try:
            if candidate.stat().st_size > 5_000_000:
                content_hashes[value] = "too_large"
            else:
                content_hashes[value] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            content_hashes[value] = None
        if len(content_hashes) >= 200:
            break
    return {
        "root": str(project_root),
        "git_available": result.returncode == 0,
        "branch": lines[0].removeprefix("## ") if lines else None,
        "changed_paths": changed_paths,
        "active_file_hashes": content_hashes,
        "status_hash": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
    }
