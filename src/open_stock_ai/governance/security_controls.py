"""Machine-readable security control evidence for the production checklist.

The control receipt deliberately distinguishes local control implementation from
external release evidence such as a clean hosted scan or a production signing
key.  This keeps the release gate fail-closed while making the remaining work
explicit and reviewable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SECURITY_CONTROL_SCHEMA = "stock_ai.security_control_receipt.v1"


@dataclass(frozen=True, slots=True)
class SecurityControl:
    identifier: str
    title: str
    acceptance: str
    evidence_paths: tuple[str, ...]
    external_blockers: tuple[str, ...]


SECURITY_CONTROLS: tuple[SecurityControl, ...] = (
    SecurityControl(
        "SEC-003",
        "SAST",
        "CodeQL and Bandit run on the repository and unresolved high/critical findings block release.",
        (".github/workflows/security-supply-chain.yml", "tests/test_security_controls.py"),
        ("hosted_sast_scan_receipt_and_high_critical_owner_triage_are_not_yet_recorded",),
    ),
    SecurityControl(
        "SEC-004",
        "Dependencies",
        "pip-audit and Dependabot provide a dependency supply-chain gate and high CVEs block release.",
        (".github/workflows/security-supply-chain.yml", ".github/dependabot.yml", "uv.lock"),
        ("clean_hosted_dependency_scan_and_cve_exception_ownership_are_not_yet_recorded",),
    ),
    SecurityControl(
        "SEC-005",
        "SBOM",
        "A deterministic CycloneDX SBOM is generated from the pinned uv lockfile and retained as a release artifact.",
        ("scripts/build_sbom.py", "tests/test_security_controls.py", "uv.lock"),
        ("signed_release_sbom_artifact_is_not_yet_published",),
    ),
    SecurityControl(
        "SEC-006",
        "Artifact signing",
        "Release provenance and artifact signatures are verified before release artifacts are accepted.",
        ("src/open_stock_ai/governance/release_signing.py", "tests/test_security_controls.py", ".github/workflows/security-supply-chain.yml"),
        ("production_signing_identity_and_verifiable_release_receipt_are_not_yet_configured",),
    ),
    SecurityControl(
        "SEC-007",
        "Secrets storage",
        "macOS credentials use Keychain and non-macOS development credentials use environment variables without plaintext project fallbacks.",
        ("src/stock_ai/agent_secret_store.py", "tests/test_agent_secret_store.py"),
        ("production_vault_or_hsm_identity_and_secret_rotation_receipts_are_not_yet_configured",),
    ),
    SecurityControl(
        "SEC-008",
        "API auth",
        "API capabilities are bound to explicit identities and least-privilege roles; remote production service identity remains fail-closed until configured.",
        ("src/stock_ai/agent_api.py", "src/stock_ai/broker_tools.py", "tests/test_security_controls.py"),
        ("production_service_identity_rbac_and_remote_token_rotation_are_not_yet_configured",),
    ),
    SecurityControl(
        "SEC-009",
        "Audit log",
        "Security and trading decisions are append-only, hash-addressed and reconstructable from durable audit records.",
        (
            "src/open_stock_ai/integration_audit.py",
            "src/stock_ai/agent_run_store.py",
            "src/open_stock_ai/governance/security_audit.py",
            "src/open_stock_ai/governance/hosted_security_audit_gate.py",
            ".github/workflows/critical-retention-archive.yml",
            "docs/validation/hosted-sec009-offhost-audit-20260908.md",
            "tests/test_security_audit.py",
        ),
        (),
    ),
    SecurityControl(
        "SEC-010",
        "Threat model",
        "A STRIDE/data-flow threat model names trust boundaries, assets, mitigations and residual risks for the production system.",
        ("docs/security/threat-model.md", "tests/test_security_controls.py"),
        ("security_owner_signoff_and_reviewed_production_data_flow_are_not_yet_recorded",),
    ),
)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def build_security_control_receipt(root: str | Path | None = None) -> dict[str, Any]:
    """Build a deterministic receipt from local files without claiming release signoff."""

    project_root = Path(root) if root is not None else Path(__file__).resolve().parents[3]
    controls: list[dict[str, Any]] = []
    for control in SECURITY_CONTROLS:
        present = [path for path in control.evidence_paths if (project_root / path).exists()]
        missing = [path for path in control.evidence_paths if path not in present]
        controls.append(
            {
                "id": control.identifier,
                "title": control.title,
                "acceptance": control.acceptance,
                "evidence_paths": present,
                "missing_evidence_paths": missing,
                "external_blockers": list(control.external_blockers),
                "local_status": "ready" if not missing else "incomplete",
            }
        )
    payload = {"schema_version": SECURITY_CONTROL_SCHEMA, "controls": controls}
    return {**payload, "receipt_sha256": _digest(payload)}


def verify_security_control_receipt(receipt: dict[str, Any]) -> None:
    """Reject altered or incomplete receipt envelopes."""

    if receipt.get("schema_version") != SECURITY_CONTROL_SCHEMA:
        raise ValueError("invalid security control receipt schema")
    supplied = str(receipt.get("receipt_sha256") or "")
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if len(supplied) != 64 or supplied != _digest(payload):
        raise ValueError("security control receipt hash mismatch")
    ids = [str(control.get("id")) for control in receipt.get("controls") or []]
    expected = [control.identifier for control in SECURITY_CONTROLS]
    if ids != expected:
        raise ValueError("security control receipt IDs are incomplete or out of order")


__all__ = [
    "SECURITY_CONTROL_SCHEMA",
    "SECURITY_CONTROLS",
    "SecurityControl",
    "build_security_control_receipt",
    "verify_security_control_receipt",
]
