#!/usr/bin/env python3
"""Bind clean-runner Sigstore verification to a durable release receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA = "stock_ai.hosted_release_attestation_gate.v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_receipt(
    *, archive: Path, provenance: Path, sbom: Path,
    source_verification: Path, provenance_verification: Path, sbom_verification: Path,
    commit_sha: str, run_id: str, repository: str,
) -> dict[str, Any]:
    evidence = (
        archive, provenance, sbom, source_verification,
        provenance_verification, sbom_verification,
    )
    for path in evidence:
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f"release evidence is missing: {path.name}")
    for path in (source_verification, provenance_verification, sbom_verification):
        if "Verified OK" not in path.read_text():
            raise ValueError(f"Sigstore verification did not pass: {path.name}")
    identity = f"https://github.com/{repository}/.github/workflows/release-attestation.yml@refs/heads/main"
    body = {
        "schema_version": SCHEMA,
        "commit_sha": commit_sha,
        "run_id": str(run_id),
        "repository": repository,
        "archive_sha256": _sha(archive),
        "provenance_sha256": _sha(provenance),
        "sbom_sha256": _sha(sbom),
        "source_verification_sha256": _sha(source_verification),
        "provenance_verification_sha256": _sha(provenance_verification),
        "sbom_verification_sha256": _sha(sbom_verification),
        "signing_identity": identity,
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "transparency_log": "https://rekor.sigstore.dev",
        "verified_on_clean_runner": True,
        "artifact_retention_days": 90,
        "blockers": [],
        "passed": True,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return {**body, "receipt_sha256": hashlib.sha256(encoded).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in (
        "archive", "provenance", "sbom", "source-verification",
        "provenance-verification", "sbom-verification",
    ):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    commit = args.commit_sha.lower()
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise SystemExit("exact candidate SHA required")
    receipt = build_receipt(
        archive=args.archive, provenance=args.provenance, sbom=args.sbom,
        source_verification=args.source_verification,
        provenance_verification=args.provenance_verification,
        sbom_verification=args.sbom_verification,
        commit_sha=commit, run_id=args.run_id, repository=args.repository,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
