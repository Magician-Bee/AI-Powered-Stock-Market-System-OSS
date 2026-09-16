"""Small, dependency-free contract for release provenance verification."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


RELEASE_PROVENANCE_SCHEMA = "stock_ai.release_provenance.v1"


def build_provenance(*, commit_sha: str, artifact_sha256: str, signer: str) -> dict[str, str]:
    """Create the payload a hosted signing tool must sign.

    This does not invent a signature.  ``signature`` is intentionally absent
    until the configured production identity signs the payload.
    """

    commit = str(commit_sha).strip().lower()
    artifact = str(artifact_sha256).strip().lower()
    identity = str(signer).strip()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("commit_sha must be a 40-character hexadecimal SHA")
    if len(artifact) != 64 or any(character not in "0123456789abcdef" for character in artifact):
        raise ValueError("artifact_sha256 must be a 64-character hexadecimal SHA")
    if not identity:
        raise ValueError("signer identity is required")
    payload = {
        "schema_version": RELEASE_PROVENANCE_SCHEMA,
        "commit_sha": commit,
        "artifact_sha256": artifact,
        "signer": identity,
    }
    payload["payload_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def verify_provenance_payload(payload: Mapping[str, Any]) -> None:
    """Verify the unsigned provenance payload before an external signer runs."""

    expected = build_provenance(
        commit_sha=str(payload.get("commit_sha") or ""),
        artifact_sha256=str(payload.get("artifact_sha256") or ""),
        signer=str(payload.get("signer") or ""),
    )
    if dict(payload) != expected:
        raise ValueError("release provenance payload hash mismatch")


__all__ = ["RELEASE_PROVENANCE_SCHEMA", "build_provenance", "verify_provenance_payload"]
