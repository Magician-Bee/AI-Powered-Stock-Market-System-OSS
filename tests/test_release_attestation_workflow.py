from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.build_release_archive import build_archive
from scripts.build_release_provenance import build_provenance
from scripts.verify_release_attestations import build_receipt


ROOT = Path(__file__).resolve().parents[1]


def test_release_archive_is_reproducible_for_exact_commit(tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    build_archive(first)
    build_archive(second)
    assert first.read_bytes() == second.read_bytes()


def test_release_provenance_binds_archive_commit_and_identity(tmp_path: Path) -> None:
    archive = tmp_path / "release.tgz"
    archive.write_bytes(b"release")
    payload = build_provenance(archive=archive, commit_sha="a" * 40, signer="github-workflow")
    assert payload["artifact_sha256"] == hashlib.sha256(b"release").hexdigest()
    assert payload["commit_sha"] == "a" * 40
    assert len(payload["payload_sha256"]) == 64


def test_release_attestation_receipt_binds_all_clean_runner_evidence(tmp_path: Path) -> None:
    names = (
        "release.tgz", "provenance.json", "sbom.json", "source.txt",
        "provenance.txt", "sbom.txt",
    )
    paths = [tmp_path / name for name in names]
    paths[0].write_bytes(b"release")
    paths[1].write_text('{"schema_version":"stock_ai.release_provenance.v1"}')
    paths[2].write_text('{"bomFormat":"CycloneDX"}')
    for path in paths[3:]:
        path.write_text("Verified OK\n")
    receipt = build_receipt(
        archive=paths[0], provenance=paths[1], sbom=paths[2],
        source_verification=paths[3], provenance_verification=paths[4],
        sbom_verification=paths[5], commit_sha="a" * 40, run_id="92",
        repository="Magician-Bee/AI-Powered-Stock-Market-System",
    )
    assert receipt["passed"] is True
    assert receipt["archive_sha256"] == hashlib.sha256(b"release").hexdigest()
    assert receipt["oidc_issuer"] == "https://token.actions.githubusercontent.com"

    paths[3].write_text("verification failed\n")
    with pytest.raises(ValueError, match="did not pass"):
        build_receipt(
            archive=paths[0], provenance=paths[1], sbom=paths[2],
            source_verification=paths[3], provenance_verification=paths[4],
            sbom_verification=paths[5], commit_sha="a" * 40, run_id="92",
            repository="owner/repo",
        )


def test_release_attestation_workflow_uses_oidc_sigstore_and_clean_runner() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-attestation.yml").read_text()
    assert "id-token: write" in workflow
    assert "attestations: write" not in workflow
    assert workflow.count("sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6") == 2
    assert workflow.count("cosign sign-blob --yes --bundle") == 3
    assert workflow.count("cosign verify-blob") == 3
    assert "--certificate-identity" in workflow
    assert "--certificate-oidc-issuer" in workflow
    assert '(cd "${RELEASE_DIR}" && sha256sum stock-ai-source.tar.gz stock-ai-provenance.json stock-ai-sbom.cdx.json > SHA256SUMS)' in workflow
    assert '(cd "${RELEASE_DIR}" && sha256sum --check SHA256SUMS)' in workflow
    assert "needs: build-and-sign" in workflow
    assert workflow.count("retention-days: 90") == 2
