from __future__ import annotations

"""Deterministic content identity for policy code and declared configuration."""

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def strategy_source_manifest(strategy: Any = None) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "strategy").glob("*.py")) + [
        root / "types.py", root / "research/model_calibration.py",
        root / "research/pit_dataset.py",
    ]
    sources = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in files if p.is_file()}
    configuration: dict[str, Any] = {}
    if strategy is not None:
        cls = type(strategy)
        sources[f"active_class:{cls.__module__}.{cls.__qualname__}"] = hashlib.sha256(
            inspect.getsource(cls).encode()).hexdigest()
        # Constructor parameters identify policy settings, excluding durable
        # stores and runtime counters. Custom strategies may declare metadata.
        for name in inspect.signature(cls).parameters:
            if name != "artifact_registry" and hasattr(strategy, name):
                value = getattr(strategy, name)
                json.dumps(value, allow_nan=False)  # fail closed on opaque configuration
                configuration[name] = value
        if callable(getattr(strategy, "policy_metadata", None)):
            configuration["policy_metadata"] = strategy.policy_metadata()
    return {"schema_version": "open_stock_ai.strategy_source_manifest.v1",
            "sources": sources, "configuration": configuration}
