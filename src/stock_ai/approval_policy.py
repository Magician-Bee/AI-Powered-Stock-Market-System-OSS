from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ScopedApprovalGrant:
    approval_id: str
    run_id: str
    capabilities: frozenset[str]
    resource_root: Path
    issued_at: datetime
    expires_at: datetime


class ApprovalPolicy:
    """Evaluate native Codex approvals against one explicit, expiring project grant."""

    def issue(
        self,
        *,
        run_id: str,
        capabilities: set[str],
        resource_root: Path,
        ttl_seconds: int = 600,
    ) -> ScopedApprovalGrant:
        now = datetime.now(timezone.utc)
        return ScopedApprovalGrant(
            approval_id=f"AP-{uuid4().hex}",
            run_id=run_id,
            capabilities=frozenset(capabilities),
            resource_root=resource_root.resolve(),
            issued_at=now,
            expires_at=now + timedelta(seconds=max(1, min(ttl_seconds, 3600))),
        )

    def review(
        self,
        grant: ScopedApprovalGrant | None,
        *,
        method: str,
        payload: dict[str, Any],
    ) -> tuple[bool, str]:
        capability = {
            "item/commandExecution/requestApproval": "native.command",
            "item/fileChange/requestApproval": "native.file_change",
        }.get(method)
        if capability is None:
            return False, "unsupported_approval_method"
        if grant is None:
            return False, "no_explicit_grant"
        if datetime.now(timezone.utc) >= grant.expires_at:
            return False, "grant_expired"
        if capability not in grant.capabilities:
            return False, "capability_not_granted"
        if not self._resource_allowed(grant.resource_root, payload):
            return False, "resource_outside_project_scope"
        return True, "explicit_scoped_grant"

    def _resource_allowed(self, root: Path, payload: dict[str, Any]) -> bool:
        serialized = json.dumps(payload, ensure_ascii=False, default=str).casefold()
        if any(token in serialized for token in ("/.ssh", "~/.ssh", "/.gnupg", "/.codex", "credentials.json")):
            return False
        for key, value in _walk(payload):
            lowered_key = key.casefold()
            if any(token in lowered_key for token in ("command", "argv")):
                if not self._command_resources_allowed(root, value):
                    return False
            if not isinstance(value, str) or not any(token in lowered_key for token in ("path", "cwd", "file")):
                continue
            if not _path_within(root, value):
                return False
        return True

    def _command_resources_allowed(self, root: Path, value: Any) -> bool:
        if isinstance(value, list):
            tokens = [str(token) for token in value]
        elif isinstance(value, str):
            if any(operator in value for operator in ("$(", "`", "&&", "||", ";", "|", ">", "<")):
                return False
            try:
                tokens = shlex.split(value)
            except ValueError:
                return False
        else:
            return True
        for token in tokens:
            if token.startswith("~"):
                return False
            if token.startswith("/") and not _path_within(root, token):
                return False
            if token == ".." or token.startswith("../") or "/../" in token:
                return False
        return True


def _path_within(root: Path, value: str) -> bool:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _walk(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, item
            yield from _walk(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]"
            yield path, item
            yield from _walk(item, path)
