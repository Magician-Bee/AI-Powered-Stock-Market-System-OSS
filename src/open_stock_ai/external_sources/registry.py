from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from open_stock_ai.config.loader import load_yaml_config


EXPECTED_REPOSITORY_LOCKS = {
    "tradingagents": {
        "name": "TradingAgents",
        "path": "external/TradingAgents",
        "origin": "https://github.com/TauricResearch/TradingAgents.git",
        "branch": "main",
        "head": "85946c2f60768ab2dae23a5a36cd927662feef94",
    },
    "finrobot": {
        "name": "FinRobot",
        "path": "external/FinRobot",
        "origin": "https://github.com/AI4Finance-Foundation/FinRobot.git",
        "branch": "master",
        "head": "6a8161ff5cfa66ec3df9c11a0bf7a84a1ac11f01",
    },
    "fingpt": {
        "name": "FinGPT",
        "path": "external/FinGPT",
        "origin": "https://github.com/AI4Finance-Foundation/FinGPT.git",
        "branch": "master",
        "head": "608a496781faf2705b9a59c89f80e6c04b15d76e",
    },
    "finrl_trading": {
        "name": "FinRL-Trading",
        "path": "external/FinRL-Trading",
        "origin": "https://github.com/AI4Finance-Foundation/FinRL-Trading.git",
        "branch": "master",
        "head": "e65d6f0483ead7d2ef4a5fc940cdf960392a25c1",
    },
    "finrl": {
        "name": "FinRL",
        "path": "external/FinRL",
        "origin": "https://github.com/AI4Finance-Foundation/FinRL.git",
        "branch": "master",
        "head": "220f9e490996a6e5c84cfad914ff14f2e0c42d22",
    },
    "qlib": {
        "name": "qlib",
        "path": "external/qlib",
        "origin": "https://github.com/microsoft/qlib.git",
        "branch": "main",
        "head": "d5379c520f66a39953bad76234a7019a72796fd0",
    },
    "ai_trader": {
        "name": "AI-Trader",
        "path": "external/AI-Trader",
        "origin": "https://github.com/HKUDS/AI-Trader.git",
        "branch": "main",
        "head": "d03ff6c056b32ced735adf7c19ed8175adb1c8df",
    },
}

EXPECTED_ORIGINS = {key: spec["origin"] for key, spec in EXPECTED_REPOSITORY_LOCKS.items()}


@dataclass(frozen=True)
class ExternalProjectProfile:
    key: str
    path: str
    exists: bool
    display_name: str | None = None
    clone_command: str | None = None
    expected_origin: str | None = None
    expected_branch: str | None = None
    expected_head: str | None = None
    origin: str | None = None
    branch: str | None = None
    head: str | None = None
    origin_verified: bool = False
    branch_verified: bool = False
    head_verified: bool = False
    lock_verified: bool = False
    lock_evidence_kind: str | None = None
    lock_evidence_path: str | None = None
    readme_title: str | None = None
    readme_excerpt: str | None = None
    license_path: str | None = None
    license_name: str | None = None
    license_status: str = "missing_license_file_review_required"
    license_evidence_path: str | None = None
    license_evidence_kind: str | None = None
    requirement_paths: list[str] = field(default_factory=list)
    dependency_manifest_count: int = 0
    capability_files: list[str] = field(default_factory=list)


class ExternalProjectRegistry:
    """Inspect pinned third-party projects without requiring nested Git metadata.

    A normal Git checkout is preferred. Portable/vendored distributions can instead
    carry ``.source-lock.json`` inside each external project. The marker is checked
    against the repository lock and is never treated as proof that the third-party
    runtime has been executed or empirically validated.
    """

    def __init__(self, project_paths: dict[str, str] | None = None, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        config = load_yaml_config()
        configured_paths = (config.get("external_projects") or {}) if isinstance(config, dict) else {}
        self.project_paths = {**configured_paths, **(project_paths or {})}
        self._profiles: dict[tuple[str, tuple[str, ...]], ExternalProjectProfile] = {}

    def profile(self, key: str, capability_terms: Iterable[str] = ()) -> ExternalProjectProfile:
        normalized_terms = tuple(sorted(term for term in capability_terms if term))
        cache_key = (key, normalized_terms)
        if cache_key in self._profiles:
            return self._profiles[cache_key]

        expected_spec = EXPECTED_REPOSITORY_LOCKS.get(key, {})
        default_path = expected_spec.get("path", f"external/{key}")
        configured_path = self.project_paths.get(key, default_path)
        project_path = self._resolve(configured_path)
        exists = project_path.exists()
        expected_origin = expected_spec.get("origin")
        expected_branch = expected_spec.get("branch")
        expected_head = expected_spec.get("head")
        expected_path = expected_spec.get("path", configured_path)

        source_metadata = self._source_metadata(project_path) if exists else {}
        origin = source_metadata.get("origin")
        branch = source_metadata.get("branch")
        head = source_metadata.get("head")
        lock_evidence_kind = source_metadata.get("kind")
        lock_evidence_path = source_metadata.get("path")

        readme = self._first_existing(project_path, ["README.md", "README_ZH.md", "readme.md"]) if exists else None
        license_path = self._first_existing(project_path, ["LICENSE", "LICENSE.md", "LICENSE.txt"]) if exists else None
        requirement_paths = self._discover(
            project_path,
            ["requirements*.txt", "pyproject.toml", "setup.py", "package.json"],
            8,
        )
        origin_verified = bool(origin and expected_origin and origin == expected_origin)
        branch_verified = bool(branch and expected_branch and branch == expected_branch)
        head_verified = bool(head and expected_head and head == expected_head)
        license_name = self._license_name(license_path)
        license_metadata = self._license_metadata(project_path, readme) if exists and not license_name else {}
        metadata_license_name = license_metadata.get("license_name")
        effective_license_name = license_name or metadata_license_name
        license_status = "license_file_detected" if license_name else (
            "license_metadata_detected_missing_license_text"
            if metadata_license_name
            else "missing_license_file_review_required"
        )
        profile = ExternalProjectProfile(
            key=key,
            path=self._relative(project_path) or str(project_path),
            exists=exists,
            display_name=expected_spec.get("name"),
            clone_command=(
                f"git clone --no-checkout {expected_origin} {expected_path} && "
                f"git -C {expected_path} checkout --detach {expected_head}"
                if expected_origin and expected_head
                else None
            ),
            expected_origin=expected_origin,
            expected_branch=expected_branch,
            expected_head=expected_head,
            origin=origin,
            branch=branch,
            head=head,
            origin_verified=origin_verified,
            branch_verified=branch_verified,
            head_verified=head_verified,
            lock_verified=origin_verified and branch_verified and head_verified,
            lock_evidence_kind=lock_evidence_kind,
            lock_evidence_path=lock_evidence_path,
            readme_title=self._readme_title(readme),
            readme_excerpt=self._readme_excerpt(readme),
            license_path=self._relative(license_path) if license_path else None,
            license_name=effective_license_name,
            license_status=license_status,
            license_evidence_path=(
                self._relative(license_path) if license_path else license_metadata.get("evidence_path")
            ),
            license_evidence_kind="license_file" if license_path else license_metadata.get("evidence_kind"),
            requirement_paths=requirement_paths,
            dependency_manifest_count=len(requirement_paths),
            capability_files=self._discover_capabilities(project_path, normalized_terms),
        )
        self._profiles[cache_key] = profile
        return profile

    def _source_metadata(self, project_path: Path) -> dict[str, str | None]:
        if (project_path / ".git").exists():
            return {
                "origin": self._git(project_path, "remote", "get-url", "origin"),
                "branch": self._git(project_path, "branch", "--show-current"),
                "head": self._git(project_path, "rev-parse", "HEAD"),
                "kind": "git_checkout",
                "path": self._relative(project_path / ".git"),
            }
        marker_path = project_path / ".source-lock.json"
        marker = self._read_json(marker_path)
        if marker:
            return {
                "origin": self._text(marker.get("origin")),
                "branch": self._text(marker.get("branch")),
                "head": self._text(marker.get("head")),
                "kind": self._text(marker.get("snapshot_mode")) or "vendored_source_snapshot",
                "path": self._relative(marker_path),
            }
        return {
            "origin": None,
            "branch": None,
            "head": None,
            "kind": "unverified_directory",
            "path": None,
        }

    def _resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()

    def _relative(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _git(self, path: Path, *args: str) -> str | None:
        if not (path / ".git").exists():
            return None
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _read_json(self, path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _text(self, value: object) -> str | None:
        text = str(value or "").strip()
        return text or None

    def _first_existing(self, root: Path, names: list[str]) -> Path | None:
        for name in names:
            path = root / name
            if path.exists():
                return path
        return None

    def _readme_title(self, path: Path | None) -> str | None:
        if not path:
            return None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or None
        return None

    def _readme_excerpt(self, path: Path | None, limit: int = 280) -> str | None:
        if not path:
            return None
        text = " ".join(
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.strip().startswith(("!", "[!", "<"))
        )
        return text[:limit] if text else None

    def _license_name(self, path: Path | None) -> str | None:
        if not path:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")[:1200].lower()
        if "apache license" in text and "version 2.0" in text:
            return "Apache-2.0"
        if "mit license" in text:
            return "MIT"
        if "gnu general public license" in text:
            return "GPL"
        if "bsd" in text and "redistribution and use" in text:
            return "BSD-style"
        return "custom_or_unclassified"

    def _license_metadata(self, root: Path, readme: Path | None) -> dict[str, str]:
        package_lock = root / "package-lock.json"
        if package_lock.exists():
            payload = self._read_json(package_lock)
            root_package = (payload.get("packages") or {}).get("") if isinstance(payload, dict) else {}
            license_name = root_package.get("license") if isinstance(root_package, dict) else None
            if isinstance(license_name, str) and license_name:
                return {
                    "license_name": license_name,
                    "evidence_path": self._relative(package_lock) or str(package_lock),
                    "evidence_kind": "package_lock_root_license",
                }
        if readme:
            text = readme.read_text(encoding="utf-8", errors="replace")[:4000].lower()
            if "license-mit" in text or "license](https://img.shields.io/badge/license-mit" in text:
                return {
                    "license_name": "MIT",
                    "evidence_path": self._relative(readme) or str(readme),
                    "evidence_kind": "readme_license_badge",
                }
        return {}

    def _discover(self, root: Path, patterns: list[str], limit: int) -> list[str]:
        if not root.exists():
            return []
        found: list[str] = []
        for pattern in patterns:
            for path in sorted(root.rglob(pattern)):
                if ".git" in path.parts or path.is_dir():
                    continue
                rel = self._relative(path)
                if rel and rel not in found:
                    found.append(rel)
                if len(found) >= limit:
                    return found
        return found

    def _discover_capabilities(self, root: Path, terms: Iterable[str], limit: int = 10) -> list[str]:
        if not root.exists():
            return []
        normalized_terms = [term.lower() for term in terms if term]
        if not normalized_terms:
            return []
        found: list[str] = []
        for path in sorted(root.rglob("*")):
            if len(found) >= limit:
                break
            if ".git" in path.parts or path.is_dir():
                continue
            rel = self._relative(path)
            haystack = str(rel or path).lower()
            if any(term in haystack for term in normalized_terms):
                found.append(rel or str(path))
        return found


def default_registry() -> ExternalProjectRegistry:
    return ExternalProjectRegistry()
