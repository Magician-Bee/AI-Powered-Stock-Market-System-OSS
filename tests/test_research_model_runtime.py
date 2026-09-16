from __future__ import annotations

from pathlib import Path
from typing import Any
import sqlite3
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from open_stock_ai.research.model_runtime import ResearchModelRuntime
from open_stock_ai.research.model_registry import ResearchModelRegistry
from open_stock_ai.research.experiment_store import ExperimentStore
from open_stock_ai.research.research_engine import ResearchEngine
from open_stock_ai.research.pit_dataset import FeatureRecord, build_dataset_manifest_identity
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.governance.artifact_rollback import ApprovedArtifactRollbackRegistry
from open_stock_ai.governance.durable_store import SQLiteGovernanceStore
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.types import MarketSnapshot, ResearchResult, StockRequest, TradingSignal


_POLICY_HASH = "a" * 64


def _snapshot(
    config: dict[str, Any], *, day_offset: int = 0
) -> MarketSnapshot:
    start = datetime(2026, 2, 1, tzinfo=timezone.utc) + timedelta(days=day_offset)
    rows = [
        {
            "timestamp": (start + timedelta(days=day)).isoformat(),
            "available_at": (start + timedelta(days=day)).isoformat(),
            "close": 101.0 + day + day_offset,
        }
        for day in range(12)
    ]
    features = [
        FeatureRecord(
            entity_id="EQ-2330", feature_id=f"fixture:{domain}", value={"fixture": domain},
            event_time=start.isoformat(), published_at=start.isoformat(), available_at=start.isoformat(),
            effective_at=start.isoformat(), ingested_at=start.isoformat(), source_revision_id=f"REV-{domain}",
            transformation_id="test.fixture.v1", transformation_sha=f"fixture-{domain}",
            dataset_version="fixture:v1", domain=domain,
        ).to_dict()
        for domain in ("prices", "financials", "flows", "events")
    ]
    manifest = build_dataset_manifest_identity(
        entity_id="EQ-2330",
        as_of=rows[-1]["timestamp"],
        required_domains=("prices", "financials", "flows", "events"),
        coverage={},
        blockers=(),
        features=features,
        replay_rows=rows,
    )
    manifest["exact_replay_eligible"] = True
    runtime_config = deepcopy(config)
    runtime_config["dataset_manifest_hash"] = manifest["manifest_hash"]
    runtime_config.setdefault("qlib", {})["dataset_manifest_hash"] = manifest["manifest_hash"]
    return MarketSnapshot(
        symbol="2330.TW",
        market="TW",
        price=112.0,
        raw={
            "point_in_time_dataset": rows,
            "point_in_time_dataset_features": features,
            "point_in_time_dataset_manifest": manifest,
            "research_model_runtime": runtime_config,
        },
    )


def _runner(calls: list[tuple[str, dict[str, Any]]]):
    def run(name: str, arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "external.finrl.train_policy":
            return {
                "schema_version": "open_stock_ai.external_full_workflow.v1",
                "project": "finrl",
                "action": "train_policy",
                "executed_function": "DRLAgent.get_model/train_model",
                "execution_boundary": "external_compute_and_services_no_live_brokerage",
                "result": {"policy_sha256": _POLICY_HASH},
                "model_provenance": {"training_executed": True},
            }
        if name == "external.finrl.predict_actions":
            return {
                "schema_version": "open_stock_ai.external_full_workflow.v1",
                "project": "finrl",
                "action": "predict_actions",
                "executed_function": "MODELS.load/model.predict",
                "execution_boundary": "external_compute_and_services_no_live_brokerage",
                "result": {
                    "policy_sha256": _POLICY_HASH,
                    "steps_executed": 3,
                    "initial_total_asset": 100.0,
                    "steps": [{"total_asset": 101.0}, {"total_asset": 99.0}, {"total_asset": 102.0}],
                },
                "model_provenance": {"policy_loaded": True, "policy_inference_executed": True},
            }
        if name == "external.qlib.reference_risk_metrics":
            values = [float(value) for value in arguments["returns"]]
            equity = peak = 1.0
            drawdown = 0.0
            for value in values:
                equity *= 1.0 + value
                peak = max(peak, equity)
                drawdown = min(drawdown, equity / peak - 1.0)
            return {
                "schema_version": "open_stock_ai.external_full_workflow.v1",
                "project": "qlib",
                "action": "reference_risk_metrics",
                "executed_function": "qlib.contrib.evaluate.risk_analysis",
                "execution_boundary": "external_compute_and_services_no_live_brokerage",
                "result": {
                    "mode": "product",
                    "periods_per_year": arguments.get("periods_per_year", 252),
                    "sample_count": len(values),
                    "return_path_sha256": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
                    "metrics": {
                        "annualized_return": equity ** (arguments.get("periods_per_year", 252) / len(values)) - 1.0,
                        "max_drawdown": drawdown,
                    },
                },
                "model_provenance": {"reference_execution": True},
            }
        assert name == "external.qlib.backtest_model"
        return {
            "schema_version": "open_stock_ai.external_full_workflow.v1",
            "project": "qlib",
            "action": "backtest_model",
            "executed_function": "qlib.cli.run.workflow",
            "execution_boundary": "external_compute_and_services_no_live_brokerage",
            "result": {
                "workflow_completed": True,
                "config_sha256": "c" * 64,
                "portfolio_return_path": {
                    "periods_per_year": 238,
                    "returns": [0.01, -0.02, 0.03],
                },
            },
            "artifacts": [{"path": ".runtime/qlib/model.pkl", "sha256": "b" * 64}],
            "model_provenance": {"training_executed": True, "inference_executed": True},
        }

    return run


def _config() -> dict[str, Any]:
    return {
        "enabled": True,
        "mode": "fit_and_infer",
        "dataset_manifest_hash": "pit-manifest-001",
        "finrl": {"artifact_name": "pit-2330-immutable", "total_timesteps": 64},
        "qlib": {
            "config_path": "config/qlib/workflow_smoke_linear_Alpha158.yaml",
            "provider_uri": "/tmp/qlib-provider",
            "dataset_manifest_hash": "pit-manifest-001",
            "config_sha256": "c" * 64,
        },
    }


def _runtime_fingerprint() -> dict[str, Any]:
    package_lock = [{"name": "fixture-runtime", "version": "1.0.0"}]
    return {
        "schema_version": "open_stock_ai.external_worker_runtime_fingerprint.v2",
        "python_implementation": "CPython",
        "python_version": "3.12.0",
        "platform": "fixture-platform",
        "machine": "fixture-machine",
        "hardware": {"architecture": "64bit", "processor": "fixture", "cpu_count": 8},
        "package_lock": package_lock,
        "package_lock_sha256": hashlib.sha256(
            json.dumps(package_lock, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }


def _attach_runtime_fingerprints(runtime: dict[str, Any]) -> None:
    for receipt in (
        runtime["finrl"]["training"], runtime["finrl"]["inference"], runtime["qlib"]["workflow"],
    ):
        receipt["runtime_fingerprint"] = _runtime_fingerprint()


def test_runtime_executes_canonical_finrl_and_qlib_with_pit_only_prices(tmp_path):
    calls: list[tuple[str, dict[str, Any]]] = []
    receipt = ResearchModelRuntime(project_root=tmp_path, runner=_runner(calls)).run(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot(_config())
    )

    assert receipt["status"] == "executed"
    assert [name for name, _ in calls] == [
        "external.finrl.train_policy",
        "external.finrl.predict_actions",
        "external.qlib.backtest_model",
        "external.qlib.reference_risk_metrics",
        "external.qlib.reference_risk_metrics",
    ]
    assert calls[0][1]["prices"] == [[101.0], [102.0], [103.0], [104.0], [105.0], [106.0], [107.0], [108.0], [109.0], [110.0], [111.0], [112.0]]
    assert receipt["finrl"]["immutable_artifact"]["policy_sha256"] == _POLICY_HASH
    assert receipt["finrl"]["immutable_artifact"]["refit_allowed"] is False
    assert receipt["qlib"]["workflow"]["executed_function"] == "qlib.cli.run.workflow"
    assert receipt["differential_validation"]["status"] == "passed"


def test_runtime_rejects_artifact_reuse_without_the_formal_registry_boundary(tmp_path):
    calls: list[tuple[str, dict[str, Any]]] = []
    config = _config()
    config["mode"] = "reuse_and_infer"
    config["model_version_ids"] = {"finrl": "model-finrl-any", "qlib": "model-qlib-any"}

    receipt = ResearchModelRuntime(project_root=tmp_path, runner=_runner(calls)).run(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot(config)
    )

    assert receipt["status"] == "blocked"
    assert receipt["blockers"] == ["external_model_runtime_failed"]
    assert receipt["error"]["message"] == "oos_model_registry_required"
    assert calls == []


def test_runtime_rejects_replay_rows_that_do_not_match_the_materialized_manifest(tmp_path):
    calls: list[tuple[str, dict[str, Any]]] = []
    snapshot = _snapshot(_config())
    snapshot.raw["point_in_time_dataset"][0]["close"] = 999.0

    receipt = ResearchModelRuntime(project_root=tmp_path, runner=_runner(calls)).run(
        StockRequest(symbol="2330.TW", market="TW"), snapshot
    )

    assert receipt["status"] == "blocked"
    assert receipt["blockers"] == ["point_in_time_dataset_integrity_failed"]
    assert "dataset_partition_hash_mismatch:replay_rows" in receipt["error"]["errors"]
    assert calls == []


def test_risk_gate_accepts_only_complete_immutable_actual_model_receipts():
    calls: list[tuple[str, dict[str, Any]]] = []
    receipt = ResearchModelRuntime(project_root=Path("/tmp"), runner=_runner(calls)).run(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot(_config())
    )
    research = ResearchResult(passed=True, summary="validated", raw={"model_runtime": receipt})
    signal = TradingSignal(
        symbol="2330.TW", market="TW", action="hold", confidence=0.9, horizon="swing", reason="test", rule_score=0.9
    )

    decision = RiskEngine().evaluate(StockRequest(symbol="2330.TW", market="TW"), signal, research)
    gates = {item["code"]: item for item in decision.gate_checks}
    assert gates["finrl_backtest_evidence"]["passed"] is True
    assert gates["qlib_factor_evidence"]["passed"] is True

    broken = dict(receipt)
    broken["finrl"] = dict(receipt["finrl"])
    broken["finrl"]["immutable_artifact"] = dict(receipt["finrl"]["immutable_artifact"])
    broken["finrl"]["immutable_artifact"]["policy_sha256"] = "not-a-hash"
    rejected = RiskEngine().evaluate(
        StockRequest(symbol="2330.TW", market="TW"), signal, ResearchResult(passed=True, summary="bad", raw={"model_runtime": broken})
    )
    assert next(item for item in rejected.gate_checks if item["code"] == "finrl_backtest_evidence")["passed"] is False


def test_registry_persists_immutable_models_and_receipt_backed_experiment(tmp_path):
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime = ResearchModelRuntime(project_root=tmp_path, runner=_runner(calls)).run(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot(_config())
    )
    store = SQLiteStore(tmp_path / "research.sqlite")
    registry = ResearchModelRegistry(store=store)

    first = registry.record_execution(StockRequest(symbol="2330.TW", market="TW"), runtime)
    second = registry.record_execution(StockRequest(symbol="2330.TW", market="TW"), runtime)

    assert first["status"] == "recorded"
    assert {item["framework"] for item in first["model_versions"]} == {"finrl", "qlib"}
    reproducibility = first["experiment"]["reproducibility"]
    assert reproducibility["level"] == "receipt_backed"
    assert reproducibility["point_in_time_input_verified"] is True
    assert reproducibility["model_artifact_hashes_verified"] is True
    assert reproducibility["runtime_package_lock_sha256"] is None
    assert reproducibility["random_seeds"] == {"finrl": 0, "qlib": 0}
    assert reproducibility["bitwise_reproducible"] is False
    assert reproducibility["blockers"] == ["runtime_package_lock_or_hardware_not_captured"]
    assert [item["already_exists"] for item in second["model_saves"]] == [True, True]
    assert second["experiment_save"]["already_exists"] is True
    assert len(store.research_model_versions()) == 2
    assert len(store.research_experiment_receipts()) == 1
    assert isinstance(registry.experiment_store, ExperimentStore)
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="research model versions are immutable"):
            conn.execute("update research_model_versions set framework='other'")


def test_registry_binds_feature_code_metrics_approval_and_metric_tolerance_replay(tmp_path):
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime = ResearchModelRuntime(project_root=tmp_path, runner=_runner(calls)).run(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot(_config())
    )
    _attach_runtime_fingerprints(runtime)
    registry = ResearchModelRegistry(SQLiteStore(tmp_path / "research.sqlite"))

    recorded = registry.record_execution(StockRequest(symbol="2330.TW", market="TW"), runtime)

    for version in recorded["model_versions"]:
        assert version["feature_contract"]["feature_schema_versions"]
        assert len(version["feature_contract"]["feature_lineage_sha256"]) == 64
        assert version["feature_contract"]["feature_code_sha256"]
        assert version["metrics"]["schema_version"] == "open_stock_ai.model_metrics_receipt.v1"
        assert version["approval"] == {
            "schema_version": "open_stock_ai.model_approval.v1",
            "status": "research_only_not_human_approved",
            "production_eligible": False,
            "execution_authority": "none",
            "promotion_boundary": "requires_human_model_promotion_receipt",
        }
    reproducibility = recorded["experiment"]["reproducibility"]
    assert reproducibility["level"] == "environment_captured"
    assert reproducibility["random_seeds"] == {"finrl": 0, "qlib": 0}
    assert len(reproducibility["runtime_hardware"]) == 3
    reference = reproducibility["metric_tolerance_replay"]["reference_metrics"]
    replay = registry.verify_metric_tolerance_replay(recorded["experiment"]["experiment_id"], reference)
    assert replay["passed"] is True
    altered = {framework: dict(metrics) for framework, metrics in reference.items()}
    altered["finrl"]["receipt_1.steps_executed"] = 99
    assert registry.verify_metric_tolerance_replay(recorded["experiment"]["experiment_id"], altered)["passed"] is False


def test_signal_model_provenance_references_immutable_registry_versions(tmp_path):
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime = ResearchModelRuntime(project_root=tmp_path, runner=_runner(calls)).run(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot(_config())
    )
    recorded = ResearchModelRegistry(SQLiteStore(tmp_path / "research.sqlite")).record_execution(
        StockRequest(symbol="2330.TW", market="TW"), runtime
    )
    signal = TradingSignal(
        symbol="2330.TW", market="TW", action="hold", confidence=0.4, horizon="swing", reason="fixture"
    )

    ResearchEngine._attach_signal_model_provenance(
        signal, runtime, recorded,
        {"status": "not_configured", "blockers": ["model_monitoring_receipt_not_supplied"]},
    )

    provenance = signal.decision_schema["model_provenance"]
    assert provenance["runtime_status"] == "executed"
    assert provenance["registry_status"] == "recorded"
    assert set(provenance["model_version_ids"]) == {
        version["model_version_id"] for version in recorded["model_versions"]
    }
    assert provenance["experiment_id"] == recorded["experiment"]["experiment_id"]
    assert provenance["drift_monitor_status"] == "not_configured"
    assert provenance["execution_authority"] == "none"


def test_human_champion_promotion_is_append_only_and_never_grants_execution(tmp_path):
    calls: list[tuple[str, dict[str, Any]]] = []
    request = StockRequest(symbol="2330.TW", market="TW")
    runtime = ResearchModelRuntime(project_root=tmp_path, runner=_runner(calls)).run(request, _snapshot(_config()))
    path = tmp_path / "research.sqlite"
    registry = ResearchModelRegistry(
        SQLiteStore(path),
        artifact_rollback=ApprovedArtifactRollbackRegistry(SQLiteGovernanceStore(path)),
    )
    recorded = registry.record_execution(request, runtime)
    versions = {item["framework"]: item["model_version_id"] for item in recorded["model_versions"]}

    promotion = registry.promote_champion(
        champion_model_version_id=versions["finrl"],
        challenger_model_version_ids=[versions["qlib"]],
        deployment_mode="shadow",
        human_promotion_receipt={
            "decision_id": "HUMAN-2026-01", "approver_id": "operator-42",
            "approved_at": "2026-02-12T00:00:00+00:00", "rationale": "validated shadow comparison",
        },
    )

    assert promotion["deployment_mode"] == "shadow"
    assert promotion["execution_authority"] == "none"
    assert promotion["governance"]["artifact_rollback_wired"] is True
    assert promotion["governance"]["active_artifact_id"] == versions["finrl"]
    restarted = ApprovedArtifactRollbackRegistry(SQLiteGovernanceStore(path))
    assert restarted.current_artifact_id == versions["finrl"]
    status = registry.promotion_status(list(versions.values()))
    assert status["status"] == "human_promoted"
    assert status["champion_model_version_id"] == versions["finrl"]
    with sqlite3.connect(tmp_path / "research.sqlite") as conn:
        with pytest.raises(sqlite3.IntegrityError, match="model promotion receipts are immutable"):
            conn.execute("update model_promotion_receipts set deployment_mode='production'")


def test_experiment_entities_are_searchable_and_comparable_with_promotion_state(tmp_path):
    calls: list[tuple[str, dict[str, Any]]] = []
    request = StockRequest(symbol="2330.TW", market="TW")
    runtime = ResearchModelRuntime(project_root=tmp_path, runner=_runner(calls)).run(request, _snapshot(_config()))
    registry = ResearchModelRegistry(SQLiteStore(tmp_path / "research.sqlite"))
    recorded = registry.record_execution(request, runtime)
    entities = registry.search_experiments(symbol="2330.TW", framework="qlib")

    assert len(entities) == 1
    assert entities[0]["schema_version"] == "open_stock_ai.experiment_entity.v1"
    assert entities[0]["experiment_id"] == recorded["experiment"]["experiment_id"]
    assert entities[0]["kind"] == "model_training_and_evaluation"
    assert entities[0]["promotion"]["status"] == "research_only_unpromoted"
    assert entities[0]["model_versions"][1]["framework"] == "qlib"


def test_registry_rejects_qlib_receipt_without_artifact_hash(tmp_path):
    calls: list[tuple[str, dict[str, Any]]] = []
    runtime = ResearchModelRuntime(project_root=tmp_path, runner=_runner(calls)).run(
        StockRequest(symbol="2330.TW", market="TW"), _snapshot(_config())
    )
    runtime["qlib"]["workflow"]["artifacts"] = [{"path": "unverifiable.pkl"}]

    with pytest.raises(ValueError, match="qlib_artifact_receipt_required"):
        ResearchModelRegistry(SQLiteStore(tmp_path / "research.sqlite")).record_execution(
            StockRequest(symbol="2330.TW", market="TW"), runtime
        )


def test_oos_registry_boundary_forbids_refit_and_rejects_training_dataset_reuse(tmp_path):
    request = StockRequest(symbol="2330.TW", market="TW")
    training_calls: list[tuple[str, dict[str, Any]]] = []
    runtime = ResearchModelRuntime(project_root=tmp_path, runner=_runner(training_calls)).run(
        request, _snapshot(_config())
    )
    registry = ResearchModelRegistry(SQLiteStore(tmp_path / "research.sqlite"))
    recorded = registry.record_execution(request, runtime)
    model_version_ids = {item["framework"]: item["model_version_id"] for item in recorded["model_versions"]}

    oos_config = _config()
    oos_config["mode"] = "reuse_and_infer"
    oos_config["dataset_manifest_hash"] = "pit-manifest-oos-002"
    oos_config["qlib"]["dataset_manifest_hash"] = "pit-manifest-oos-002"
    oos_config["model_version_ids"] = model_version_ids
    oos_calls: list[tuple[str, dict[str, Any]]] = []
    receipt = ResearchModelRuntime(project_root=tmp_path, runner=_runner(oos_calls)).run(
        request,
        _snapshot(oos_config, day_offset=20),
        model_registry=registry,
    )

    # FinRL may only load/predict; Qlib's only available worker is a fitting
    # workflow, so the formal OOS boundary must reject it rather than refit.
    assert receipt["status"] == "blocked"
    assert receipt["error"]["message"] == "oos_qlib_immutable_inference_adapter_unavailable"
    assert [name for name, _ in oos_calls] == ["external.finrl.predict_actions"]

    same_dataset_calls: list[tuple[str, dict[str, Any]]] = []
    same_dataset = ResearchModelRuntime(project_root=tmp_path, runner=_runner(same_dataset_calls)).run(
        request,
        _snapshot({**oos_config, "dataset_manifest_hash": "pit-manifest-001"}),
        model_registry=registry,
    )
    assert same_dataset["status"] == "blocked"
    assert same_dataset["error"]["message"] == "oos_evaluation_dataset_matches_training_dataset"
    assert same_dataset_calls == []
