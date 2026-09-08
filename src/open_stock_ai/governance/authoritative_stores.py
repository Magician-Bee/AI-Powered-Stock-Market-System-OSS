"""Authoritative domain-store matrix and fail-closed invariants."""

from __future__ import annotations

import hashlib
import json
import ast
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_PATH = _ROOT / "config" / "authoritative_store_matrix.yaml"
_VALID_STATUSES = {"canonical", "transitional"}


@lru_cache(maxsize=1)
def _definition() -> tuple[dict[str, Any], str]:
    raw = _CONFIG_PATH.read_bytes()
    payload = yaml.safe_load(raw) or {}
    if not isinstance(payload, dict):
        raise ValueError("authoritative store matrix must be a mapping")
    if payload.get("schema_version") != "open_stock_ai.authoritative_store_matrix.v1":
        raise ValueError("unsupported authoritative store matrix schema")
    if payload.get("source_of_truth") != "config/authoritative_store_matrix.yaml":
        raise ValueError("authoritative store matrix source_of_truth is invalid")
    domains = payload.get("domains")
    if not isinstance(domains, list) or not domains:
        raise ValueError("authoritative store matrix requires domains")
    identifiers = [str(item.get("id") or "") for item in domains if isinstance(item, dict)]
    if len(identifiers) != len(domains) or any(not identifier for identifier in identifiers):
        raise ValueError("authoritative store domains require non-empty ids")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("authoritative store domain ids must be unique")
    authoritative_refs: set[tuple[str, str]] = set()
    projection_refs: set[tuple[str, str]] = set()
    for item in domains:
        status = item.get("status")
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid authoritative store status:{item.get('id')}")
        owner = item.get("authoritative_store")
        if not _valid_store_ref(owner):
            raise ValueError(f"invalid authoritative store owner:{item.get('id')}")
        owner_module = str(owner["module"])
        if not (_ROOT / owner_module).is_file():
            raise ValueError(f"authoritative store module does not exist:{item.get('id')}:{owner_module}")
        owner_ref = (owner_module, str(owner["symbol"]))
        if not _module_declares_symbol(_ROOT / owner_module, owner_ref[1]):
            raise ValueError(f"authoritative store symbol does not exist:{item.get('id')}:{owner_ref[1]}")
        authoritative_refs.add(owner_ref)
        migration_required = item.get("migration_required") or []
        if status == "transitional" and (not isinstance(migration_required, list) or not migration_required):
            raise ValueError(f"transitional store requires migration_required:{item.get('id')}")
        if status == "canonical" and migration_required:
            raise ValueError(f"canonical store cannot retain migration blockers:{item.get('id')}")
        projections = item.get("projections")
        if not isinstance(projections, list):
            raise ValueError(f"store projections must be a list:{item.get('id')}")
        for projection in projections:
            if not _valid_store_ref(projection) or projection.get("read_only") is not True:
                raise ValueError(f"projection must be explicitly read-only:{item.get('id')}")
            projection_module = str(projection["module"])
            if not (_ROOT / projection_module).is_file():
                raise ValueError(f"projection module does not exist:{item.get('id')}:{projection_module}")
            projection_symbol = str(projection["symbol"])
            if not _module_declares_symbol(_ROOT / projection_module, projection_symbol):
                raise ValueError(f"projection symbol does not exist:{item.get('id')}:{projection_symbol}")
            projection_refs.add((projection_module, projection_symbol))
        non_authoritative_writers = item.get("non_authoritative_writers") or []
        if not isinstance(non_authoritative_writers, list):
            raise ValueError(f"non_authoritative_writers must be a list:{item.get('id')}")
        if status == "canonical" and non_authoritative_writers:
            raise ValueError(f"canonical store cannot retain non-authoritative writers:{item.get('id')}")
        for writer in non_authoritative_writers:
            if not _valid_store_ref(writer) or writer.get("write_allowed") is not False:
                raise ValueError(f"non-authoritative writer must be explicitly denied:{item.get('id')}")
            writer_module = str(writer["module"])
            writer_symbol = str(writer["symbol"])
            if not (_ROOT / writer_module).is_file() or not _module_declares_symbol(
                _ROOT / writer_module, writer_symbol
            ):
                raise ValueError(f"non-authoritative writer does not exist:{item.get('id')}:{writer_symbol}")
    collision = authoritative_refs & projection_refs
    if collision:
        raise ValueError(f"a projection cannot also be authoritative:{sorted(collision)}")
    payload["domain_ids"] = identifiers
    payload["validated"] = True
    return payload, hashlib.sha256(raw).hexdigest()


def authoritative_store_matrix() -> dict[str, Any]:
    payload, digest = _definition()
    return {
        "schema_version": payload["schema_version"],
        "source_of_truth": payload["source_of_truth"],
        "matrix_sha256": digest,
        "validated": True,
        "domains": json.loads(json.dumps(payload["domains"], ensure_ascii=False)),
    }


def authoritative_store_for(domain_id: str) -> dict[str, Any]:
    """Return one owner record; unknown domains fail closed."""

    for item in authoritative_store_matrix()["domains"]:
        if item["id"] == domain_id:
            return dict(item["authoritative_store"])
    raise KeyError(f"unknown authoritative store domain: {domain_id}")


def is_authoritative_store(domain_id: str, module: str, symbol: str | None = None) -> bool:
    owner = authoritative_store_for(domain_id)
    return owner["module"] == module and (symbol is None or owner["symbol"] == symbol)


def _valid_store_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("module"), str)
        and bool(value["module"].strip())
        and isinstance(value.get("symbol"), str)
        and bool(value["symbol"].strip())
    )


def _module_declares_symbol(path: Path, symbol: str) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return False
    return any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == symbol
        for node in ast.walk(tree)
    )
