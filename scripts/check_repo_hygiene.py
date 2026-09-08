#!/usr/bin/env python3
"""Reject generated files and oversized blobs before they enter Git history."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TREE_BYTES = 100 * 1024 * 1024
ALLOWED_GENERATED = {
    ".runtime/.gitkeep",
    "logs/agent/.gitkeep",
    "output/backtests/.gitkeep",
    "output/reports/.gitkeep",
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main() -> int:
    paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in git("ls-files", "-z").split(b"\0")
        if item
    ]
    errors: list[str] = []
    total_bytes = 0

    for path in paths:
        normalized = path.replace("\\", "/")
        forbidden_generated = (
            normalized == "artifacts"
            or normalized.startswith("artifacts/")
            or normalized.startswith(".runtime/")
            or normalized.startswith("logs/")
            or normalized.startswith("output/")
        )
        if forbidden_generated and normalized not in ALLOWED_GENERATED:
            errors.append(f"generated path is tracked: {normalized}")
        if Path(normalized).name.startswith("._"):
            errors.append(f"AppleDouble file is tracked: {normalized}")

        try:
            size = int(git("cat-file", "-s", f":{path}").strip())
        except subprocess.CalledProcessError:
            errors.append(f"cannot inspect tracked blob: {normalized}")
            continue
        total_bytes += size
        if size > MAX_FILE_BYTES:
            errors.append(f"tracked blob exceeds 5 MiB ({size} bytes): {normalized}")

    if total_bytes > MAX_TREE_BYTES:
        errors.append(f"tracked tree exceeds 100 MiB ({total_bytes} bytes)")

    print(f"tracked_files={len(paths)} tracked_bytes={total_bytes}")
    if errors:
        print("repository hygiene check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
