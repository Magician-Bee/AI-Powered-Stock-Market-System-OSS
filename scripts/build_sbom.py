#!/usr/bin/env python3
"""Build a deterministic CycloneDX JSON SBOM from ``uv.lock``."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any


def build_sbom(lock_path: str | Path) -> dict[str, Any]:
    data = tomllib.loads(Path(lock_path).read_text(encoding="utf-8"))
    components = []
    for package in sorted(data.get("package") or [], key=lambda item: (item.get("name", ""), item.get("version", ""))):
        name = str(package.get("name") or "")
        version = str(package.get("version") or "")
        if not name or not version:
            continue
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{name}@{version}",
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "stock-ai-ecosystem", "version": "0.1.0"}},
        "components": components,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", default="uv.lock", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(build_sbom(args.lock), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
