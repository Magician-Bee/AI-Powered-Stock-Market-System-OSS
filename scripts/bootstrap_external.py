#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install pinned external research projects.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--project", action="append", default=[], help="Source key to install; repeatable.")
    parser.add_argument("--force", action="store_true", help="Replace an existing unverified directory.")
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="Install into .runtime/external instead of the vendored external paths.",
    )
    return parser.parse_args()


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 900) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / "config" / "external_sources.lock.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    projects = payload.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise RuntimeError(f"No projects found in {path}")
    return payload


def install_project(root: Path, key: str, spec: dict[str, Any], *, force: bool, runtime: bool) -> dict[str, Any]:
    origin = str(spec["origin"])
    branch = str(spec["branch"])
    head = str(spec["head"])
    configured_path = Path(str(spec["path"]))
    destination = root / (Path(".runtime/external") / configured_path.name if runtime else configured_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and not (destination / ".git").exists():
        marker = destination / ".source-lock.json"
        if marker.is_file() and not force:
            current = json.loads(marker.read_text(encoding="utf-8"))
            if current.get("origin") == origin and current.get("head") == head:
                return {"key": key, "status": "already_locked_snapshot", "path": str(destination)}
        if not force:
            raise RuntimeError(
                f"{destination} exists without Git metadata. Use --force only after confirming it may be replaced."
            )
        shutil.rmtree(destination)

    if not destination.exists():
        run(["git", "clone", "--no-checkout", "--filter=blob:none", origin, str(destination)])
    else:
        current_origin = run(["git", "remote", "get-url", "origin"], cwd=destination)
        if current_origin != origin:
            raise RuntimeError(f"Origin mismatch for {key}: {current_origin} != {origin}")

    run(["git", "fetch", "--depth", "1", "origin", head], cwd=destination)
    run(["git", "checkout", "--detach", head], cwd=destination)
    actual_head = run(["git", "rev-parse", "HEAD"], cwd=destination)
    if actual_head != head:
        raise RuntimeError(f"HEAD mismatch for {key}: {actual_head} != {head}")

    marker_payload = {
        "schema_version": "open_stock_ai.external_snapshot_lock.v1",
        "source_key": key,
        "origin": origin,
        "branch": branch,
        "head": head,
        "snapshot_mode": "git_checkout",
    }
    (destination / ".source-lock.json").write_text(
        json.dumps(marker_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"key": key, "status": "installed", "path": str(destination), "head": actual_head}


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    manifest = load_manifest(root)
    projects: dict[str, dict[str, Any]] = manifest["projects"]
    selected = args.project or list(projects)
    unknown = [key for key in selected if key not in projects]
    if unknown:
        raise RuntimeError(f"Unknown project keys: {', '.join(unknown)}")

    results = [
        install_project(root, key, projects[key], force=args.force, runtime=args.runtime)
        for key in selected
    ]
    print(json.dumps({"schema_version": manifest.get("schema_version"), "items": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
