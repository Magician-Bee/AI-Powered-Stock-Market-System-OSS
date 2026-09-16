from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from open_stock_ai.governance.release_signing import build_provenance, verify_provenance_payload
from open_stock_ai.governance.security_controls import (
    SECURITY_CONTROLS,
    build_security_control_receipt,
    verify_security_control_receipt,
)
from scripts.build_sbom import build_sbom
from scripts.check_codeql_sarif import inspect_sarif


ROOT = Path(__file__).resolve().parents[1]


def test_security_control_receipt_covers_sec003_to_sec010_and_is_hash_verified() -> None:
    receipt = build_security_control_receipt(ROOT)
    assert [control["id"] for control in receipt["controls"]] == [
        f"SEC-{index:03d}" for index in range(3, 11)
    ]
    assert all(control["local_status"] == "ready" for control in receipt["controls"])
    assert all(
        control["external_blockers"]
        for control in receipt["controls"]
        if control["id"] != "SEC-009"
    )
    assert next(
        control for control in receipt["controls"] if control["id"] == "SEC-009"
    )["external_blockers"] == []
    verify_security_control_receipt(receipt)

    altered = copy.deepcopy(receipt)
    altered["controls"][0]["title"] = "altered"
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_security_control_receipt(altered)


def test_supply_chain_workflow_has_sast_dependency_and_artifact_gates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "security-supply-chain.yml").read_text(encoding="utf-8")
    assert "github/codeql-action/analyze@v3" in workflow
    assert "upload: false" in workflow
    assert "scripts/check_codeql_sarif.py" in workflow
    assert "bandit -q -r src -lll" in workflow
    assert "uv export --frozen --no-hashes --no-emit-project" in workflow
    assert "pip-audit --strict --desc" in workflow
    assert "--no-deps --disable-pip" in workflow
    assert "scripts/build_sbom.py" in workflow
    assert (ROOT / ".github" / "codeql" / "codeql-config.yml").exists()
    assert (ROOT / ".github" / "dependabot.yml").exists()


def test_codeql_sarif_gate_blocks_high_security_findings(tmp_path: Path) -> None:
    sarif = tmp_path / "python.sarif"
    sarif.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "tool": {
                            "driver": {
                                "rules": [
                                    {"id": "py/example", "properties": {"security-severity": "8.1"}}
                                ]
                            }
                        },
                        "results": [
                            {"ruleId": "py/example", "message": {"text": "unsafe example"}}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    receipt = inspect_sarif([tmp_path], commit_sha="a" * 40)

    assert receipt["passed"] is False
    assert receipt["high_critical_count"] == 1
    assert len(receipt["receipt_sha256"]) == 64


def test_codeql_sarif_gate_fails_closed_when_output_is_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no CodeQL SARIF"):
        inspect_sarif([tmp_path], commit_sha="b" * 40)


def test_local_supply_chain_preflight_excludes_the_local_project_from_pip_audit() -> None:
    documentation = (ROOT / "docs" / "security" / "local-supply-chain-preflight.md").read_text(
        encoding="utf-8"
    )

    assert "--no-emit-project" in documentation
    assert "pip-audit --strict --desc" in documentation
    assert "bandit -q -r src -lll" in documentation
    assert "scripts/build_sbom.py" in documentation
    assert "不取代 SEC-003～SEC-006" in documentation


def test_sbom_is_deterministic_and_contains_pinned_packages(tmp_path: Path) -> None:
    first = build_sbom(ROOT / "uv.lock")
    second = build_sbom(ROOT / "uv.lock")
    assert first == second
    assert first["bomFormat"] == "CycloneDX"
    assert first["components"]
    assert any(item["name"] == "fastapi" for item in first["components"])
    output = tmp_path / "sbom.json"
    output.write_text(json.dumps(first, sort_keys=True), encoding="utf-8")
    assert json.loads(output.read_text(encoding="utf-8"))["specVersion"] == "1.5"


def test_release_provenance_payload_is_strictly_verified() -> None:
    payload = build_provenance(
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        artifact_sha256="a" * 64,
        signer="github-actions://stock-ai-release",
    )
    verify_provenance_payload(payload)
    altered = dict(payload, signer="unknown")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_provenance_payload(altered)


def test_security_control_catalog_has_unique_ids_and_truthful_blockers() -> None:
    ids = [control.identifier for control in SECURITY_CONTROLS]
    assert ids == [f"SEC-{index:03d}" for index in range(3, 11)]
    assert len(set(ids)) == len(ids)
    assert all(
        control.external_blockers
        for control in SECURITY_CONTROLS
        if control.identifier != "SEC-009"
    )
    assert next(
        control for control in SECURITY_CONTROLS if control.identifier == "SEC-009"
    ).external_blockers == ()
