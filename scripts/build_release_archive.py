#!/usr/bin/env python3
"""Build a reproducible source release archive for the exact Git commit."""

from __future__ import annotations

import argparse
import gzip
import subprocess
from pathlib import Path


def build_archive(output: Path, *, commit_sha: str = "HEAD") -> None:
    exact = subprocess.check_output(["git", "rev-parse", commit_sha], text=True).strip()
    if len(exact) != 40:
        raise ValueError("release archive requires an exact commit")
    tar = subprocess.check_output([
        "git", "archive", "--format=tar", f"--prefix=stock-ai-{exact}/", exact,
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(tar)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--commit", default="HEAD")
    args = parser.parse_args()
    build_archive(args.output, commit_sha=args.commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
