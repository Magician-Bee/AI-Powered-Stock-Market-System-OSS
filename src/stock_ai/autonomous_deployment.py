"""Host observations linking autonomous decisions and fills to this service.

The source snapshot describes files observed once by this process, not proof of
every byte already loaded by Python. A declared commit and a database location
hash are correlation keys, not independent authentication or database contents.
No credentials, prompts, database contents or absolute paths are retained here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from threading import RLock

from open_stock_ai.execution.trading_plan import content_hash


_SOURCE_SNAPSHOT: dict | None = None
_LOCK = RLock()


def database_path_sha256(path: str | Path) -> str:
    """Identify a Host database location; this is not a hash of its contents."""
    return hashlib.sha256(str(Path(path).expanduser().resolve()).encode("utf-8")).hexdigest()


def initialize_source_snapshot(*, capture_stage: str = "first_autonomous_use") -> dict:
    """Freeze a single process observation, including explicit unreadable files.

    The ASGI lifespan calls this before starting background work. Direct library
    use is distinguished by its first-use label instead of claiming a startup
    observation retroactively. Later edits must not rewrite earlier receipts.
    """
    global _SOURCE_SNAPSHOT
    with _LOCK:
        if _SOURCE_SNAPSHOT is None:
            root = Path(__file__).resolve().parents[2]
            _SOURCE_SNAPSHOT = _source_snapshot(root, capture_stage=capture_stage)
        return deepcopy(_SOURCE_SNAPSHOT)


def _source_snapshot(root: Path, *, capture_stage: str) -> dict:
    files, errors = {}, []
    roots = ((root / "src/open_stock_ai", {".py"}),
             (root / "src/stock_ai", {".py"}),
             (root / "config", {".yaml", ".yml", ".json", ".toml"}))
    for folder, suffixes in roots:
        if not folder.is_dir():
            errors.append({"path": folder.relative_to(root).as_posix(), "error": "missing_source_directory"})
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            relative = path.relative_to(root).as_posix()
            try:
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                errors.append({"path": relative, "error": type(exc).__name__})
    payload = {"schema_version": "open_stock_ai.autonomous_source_snapshot.v1",
               "observed_at": datetime.now(timezone.utc).isoformat(),
               "capture_stage": capture_stage,
               "declared_build_commit": os.getenv("STOCK_AI_BUILD_COMMIT", "working-copy"),
               "instance_id": os.getenv("STOCK_AI_INSTANCE_ID", "untracked"),
               "process_id": os.getpid(), "files": files, "errors": errors,
               "files_sha256": content_hash(files),
               "assurance": "host_filesystem_observation_not_loaded_code_attestation"}
    return {**payload, "receipt_sha256": content_hash(payload)}


def bind_campaign_execution_context(campaign) -> dict:
    """Retain full sources once; keep the per-decision/fill reference compact."""
    snapshot = initialize_source_snapshot()
    source_id = campaign._retain("deployment_source_snapshot", snapshot)
    payload = {"schema_version": "open_stock_ai.autonomous_deployment_receipt.v1",
               "account_id": campaign.broker.account_id, "mode": campaign.broker.mode,
               "observed_at": snapshot["observed_at"], "capture_stage": snapshot["capture_stage"],
               "declared_build_commit": snapshot["declared_build_commit"],
               "instance_id": snapshot["instance_id"], "process_id": snapshot["process_id"],
               "source_snapshot_id": source_id,
               "source_snapshot_sha256": snapshot["receipt_sha256"],
               "source_file_count": len(snapshot["files"]), "source_error_count": len(snapshot["errors"]),
               "database_bindings": {"trading_database_path_sha256": database_path_sha256(campaign.plans.store.path)},
               "assurance": "host_observation_not_independent_attestation"}
    receipt = {**payload, "receipt_sha256": content_hash(payload)}
    receipt_id = campaign._retain("deployment_context", receipt)
    return {"deployment_receipt_id": receipt_id, "deployment_receipt": receipt}
