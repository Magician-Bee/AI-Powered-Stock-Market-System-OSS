from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from open_stock_ai.governance import LoadTestProfile, PerformanceBudget, RequestSample
from open_stock_ai.governance.hosted_load_gate import (
    run_hosted_load_gate,
    verify_hosted_load_gate_receipt,
)


def _profile() -> LoadTestProfile:
    return LoadTestProfile(
        "stock-ai-health-api",
        target_requests_per_second=4,
        concurrency=2,
        duration_seconds=1,
        budget=PerformanceBudget(
            "stock-ai-health-api",
            max_p95_ms=100,
            max_error_rate=0,
            min_throughput=1,
            allowed_regression=0.25,
            max_p99_ms=150,
        ),
    )


def test_hosted_gate_retains_verified_baseline_candidate_and_receipt(tmp_path) -> None:
    receipt = run_hosted_load_gate(
        _profile(),
        url="http://127.0.0.1:8765/health",
        output_dir=tmp_path,
        commit_sha="a" * 40,
        service_identity={"status": "ok", "system_id": "stock-ai-system", "build_commit": "a" * 40},
        request_fn=lambda _url, _timeout: RequestSample(5, True),
        clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
    )

    assert receipt["passed"] is True
    assert receipt["blockers"] == []
    assert receipt["baseline_artifact"]["metrics"]["p99_ms"] == 5
    assert receipt["candidate_metrics"]["p99_ms"] == 5
    assert verify_hosted_load_gate_receipt(receipt) is True
    assert (tmp_path / "load-profile.json").is_file()
    assert (tmp_path / "load-report.json").is_file()
    assert (tmp_path / "load-gate-receipt.json").is_file()
    assert len(list((tmp_path / "baselines").glob("*.json"))) == 1

    receipt["candidate_metrics"]["p99_ms"] = 999
    assert verify_hosted_load_gate_receipt(receipt) is False


def test_hosted_gate_fails_closed_when_target_or_samples_are_missing(tmp_path) -> None:
    try:
        run_hosted_load_gate(
            _profile(),
            url="http://127.0.0.1:8765/health",
            output_dir=tmp_path / "identity",
            commit_sha="b" * 40,
            service_identity={"status": "ok", "system_id": "other", "build_commit": "b" * 40},
        )
    except ValueError as exc:
        assert "healthy Stock AI service" in str(exc)
    else:
        raise AssertionError("wrong service identity must fail closed")

    receipt = run_hosted_load_gate(
        _profile(),
        url="http://127.0.0.1:8765/health",
        output_dir=tmp_path / "samples",
        commit_sha="b" * 40,
        service_identity={"status": "ok", "system_id": "stock-ai-system", "build_commit": "b" * 40},
        request_fn=lambda _url, _timeout: (_ for _ in ()).throw(OSError("offline")),
    )
    assert receipt["passed"] is False
    assert "baseline_sample_count_below_profile_minimum" in receipt["blockers"]
    assert "candidate_sample_count_below_profile_minimum" in receipt["blockers"]
    assert verify_hosted_load_gate_receipt(receipt) is True


def test_hosted_workflow_runs_exact_service_and_retains_evidence() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/load-regression.yml").read_text()
    assert "STOCK_AI_BUILD_COMMIT: ${{ github.sha }}" in workflow
    assert "python scripts/run_hosted_load_gate.py" in workflow
    assert "--duration 15" in workflow
    assert "--rps 20" in workflow
    assert "--concurrency 8" in workflow
    assert "retention-days: 30" in workflow
    assert "verify_hosted_load_gate_receipt" in workflow
