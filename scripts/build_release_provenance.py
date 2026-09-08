#!/usr/bin/env python3
"""Build deterministic provenance for an exact source release artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SCHEMA = "stock_ai.release_provenance.v1"


def build_provenance(*, archive: Path, commit_sha: str, signer: str) -> dict[str, str]:
    commit = commit_sha.strip().lower()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("commit_sha must be a 40-character hexadecimal SHA")
    identity = signer.strip()
    if not identity:
        raise ValueError("signer identity is required")
    body = {
        "schema_version": SCHEMA,
        "commit_sha": commit,
        "artifact_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "signer": identity,
    }
    return {
        **body,
        "payload_sha256": hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--signer", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = build_provenance(archive=args.archive, commit_sha=args.commit_sha, signer=args.signer)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
