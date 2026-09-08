from __future__ import annotations

import json

from open_stock_ai.external_sources.registry import EXPECTED_REPOSITORY_LOCKS, ExternalProjectRegistry


def test_all_external_snapshot_markers_verify_pinned_origin_branch_and_head(tmp_path):
    project_paths: dict[str, str] = {}
    for key, spec in EXPECTED_REPOSITORY_LOCKS.items():
        project = tmp_path / spec["path"]
        project.mkdir(parents=True)
        (project / ".source-lock.json").write_text(
            json.dumps(
                {
                    "schema_version": "open_stock_ai.external_snapshot_lock.v1",
                    "source_key": key,
                    "origin": spec["origin"],
                    "branch": spec["branch"],
                    "head": spec["head"],
                    "snapshot_mode": "vendored_source_snapshot",
                }
            ),
            encoding="utf-8",
        )
        (project / "README.md").write_text(f"# {spec['name']}\n", encoding="utf-8")
        (project / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        project_paths[key] = str(project)

    registry = ExternalProjectRegistry(project_paths=project_paths, root=tmp_path)
    profiles = {key: registry.profile(key) for key in EXPECTED_REPOSITORY_LOCKS}

    assert set(profiles) == set(EXPECTED_REPOSITORY_LOCKS)
    for key, profile in profiles.items():
        spec = EXPECTED_REPOSITORY_LOCKS[key]
        assert profile.exists is True
        assert profile.origin == spec["origin"]
        assert profile.branch == spec["branch"]
        assert profile.head == spec["head"]
        assert profile.origin_verified is True
        assert profile.branch_verified is True
        assert profile.head_verified is True
        assert profile.lock_verified is True
        assert profile.lock_evidence_kind == "vendored_source_snapshot"
        assert profile.lock_evidence_path == f"{spec['path']}/.source-lock.json"
        assert profile.clone_command is not None
        assert "--no-checkout" in profile.clone_command
        assert spec["head"] in profile.clone_command
