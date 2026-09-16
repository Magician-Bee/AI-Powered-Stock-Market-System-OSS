from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
BASELINE_COMMIT = "afaca27297b583c6d7f18cb6af675a3a39290c70"


def test_core_safety_scans_complete_git_history_with_gitleaks() -> None:
    workflow = (ROOT / ".github" / "workflows" / "core-safety.yml").read_text(
        encoding="utf-8"
    )

    assert "fetch-depth: 0" in workflow
    assert "gitleaks/gitleaks-action@v2" in workflow
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in workflow


def test_secret_scan_baseline_is_limited_to_reviewed_vendored_fingerprints() -> None:
    entries = [
        line.strip()
        for line in (ROOT / ".gitleaksignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert len(entries) == 14
    assert len(set(entries)) == len(entries)
    assert entries[0].startswith("81797f44cff22f80ed1b1cadafb75a1d079509de:tests/")
    assert entries[1].startswith("6a2c2199ae97504700637ed3052a03850fc6f849:tests/")
    assert all(entry.startswith(f"{BASELINE_COMMIT}:external/") for entry in entries[2:])
    assert all(":generic-api-key:" in entry for entry in entries)


def test_secret_history_audit_documentation_keeps_rotation_and_rewrite_as_owner_actions() -> None:
    documentation = (ROOT / "docs" / "security" / "secret-scanning.md").read_text(
        encoding="utf-8"
    )

    assert "Recorded local full-history audit — 2026-08-21" in documentation
    assert "544 commits" in documentation
    assert "680 commits" in documentation
    assert "731 commits" in documentation
    assert "45.89 MB" in documentation
    assert "767 commits" in documentation
    assert "46.24 MB" in documentation
    assert "zero Gitleaks findings" in documentation
    assert "no leaks found" in documentation
    assert "empty JSON finding list" in documentation
    assert "**not** evidence" in documentation
    assert "revoked or rotated" in documentation
    assert "history rewrite" in documentation
