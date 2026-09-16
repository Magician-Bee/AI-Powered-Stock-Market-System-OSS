from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from open_stock_ai.governance.release_gate import (
    ReleaseGateFailure,
    assert_release_gate,
    evaluate_release_gate,
)


ROOT = Path(__file__).resolve().parents[1]


def test_full_release_gate_is_machine_readable_and_fails_closed() -> None:
    report = evaluate_release_gate()

    assert report["schema_version"] == "stock_ai.release_gate.v1"
    assert report["source_of_truth"] == "config/production_requirement_status.yaml"
    assert report["gate"] == "full_release"
    assert report["passed"] is False
    assert report["counts"] == {"complete": 85, "partial": 39, "unverified": 0, "total": 124}
    assert "Q-003" in report["blocking_requirement_ids"]
    json.dumps(report, ensure_ascii=False, sort_keys=True)

    with pytest.raises(ReleaseGateFailure, match="release_gate_failed:full_release"):
        assert_release_gate()


def test_unknown_gate_is_blocked_without_exception() -> None:
    report = evaluate_release_gate("not-a-real-gate")
    assert report["passed"] is False
    assert report["blocking_requirement_ids"] == ["unknown_gate"]


def test_release_gate_cli_emits_json_and_nonzero_for_blocked_release() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check-release-gate.py", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["schema_version"] == "stock_ai.release_gate.v1"
    assert report["passed"] is False
    assert "Q-003" in report["blocking_requirement_ids"]
