from __future__ import annotations

from dataclasses import dataclass, field

from open_stock_ai.external_sources.finrl_source import FinRLSource
from open_stock_ai.external_sources.qlib_source import QlibSource
from open_stock_ai.research.model_runtime import ResearchModelRuntime
from open_stock_ai.research.model_registry import ResearchModelRegistry
from open_stock_ai.research.model_drift import evaluate_model_drift
from open_stock_ai.research.pit_dataset import verify_dataset_materialization
from open_stock_ai.research.report_generator import ReportGenerator
from open_stock_ai.types import IntelligenceResult, MarketSnapshot, ResearchResult, StockRequest, TradingSignal


@dataclass
class ResearchEngine:
    finrl: FinRLSource
    qlib: QlibSource
    report_generator: ReportGenerator = field(default_factory=ReportGenerator)
    model_runtime: ResearchModelRuntime = field(default_factory=ResearchModelRuntime)
    model_registry: ResearchModelRegistry | None = None

    def validate(
        self,
        request: StockRequest,
        signal: TradingSignal,
        market_snapshot: MarketSnapshot,
        intelligence: IntelligenceResult,
    ) -> ResearchResult:
        finrl_result = self.finrl.backtest_signal(request, signal, market_snapshot)
        qlib_result = self.qlib.validate_factor(request, signal, market_snapshot)
        model_runtime = self.model_runtime.run(request, market_snapshot, model_registry=self.model_registry)
        model_registry_receipt = (
            self.model_registry.record_execution(request, model_runtime)
            if self.model_registry is not None
            else {"schema_version": "open_stock_ai.research_model_registry_receipt.v1", "status": "not_configured"}
        )
        monitor_payload = (
            market_snapshot.raw.get("model_monitoring")
            if isinstance(market_snapshot.raw, dict) else None
        )
        model_drift = evaluate_model_drift(monitor_payload)
        self._attach_signal_model_provenance(signal, model_runtime, model_registry_receipt, model_drift)
        sharpe = finrl_result.get("sharpe")
        max_drawdown = finrl_result.get("max_drawdown_pct")
        qlib_score = qlib_result.get("score")

        exact_strategy_replay = finrl_result.get("strategy_replay_exact") is True
        empirical_backtest = finrl_result.get("empirical_valid") is True
        lookahead_safe = finrl_result.get("lookahead_safe") is True
        transaction_costs_included = finrl_result.get("transaction_costs_included") is True
        qlib_empirical = qlib_result.get("empirical_valid") is True
        model_runtime_requested = (
            isinstance(market_snapshot.raw, dict)
            and isinstance(market_snapshot.raw.get("research_model_runtime"), dict)
            and market_snapshot.raw["research_model_runtime"].get("enabled") is True
        )
        model_runtime_executed = model_runtime.get("status") == "executed"
        model_drift_accepted = (
            not model_runtime_executed or model_drift.get("status") == "enabled"
        )
        pit_manifest = (
            market_snapshot.raw.get("point_in_time_dataset_manifest")
            if isinstance(market_snapshot.raw, dict)
            and isinstance(market_snapshot.raw.get("point_in_time_dataset_manifest"), dict)
            else {}
        )
        pit_rows = (
            market_snapshot.raw.get("point_in_time_dataset")
            if isinstance(market_snapshot.raw, dict)
            else None
        )
        pit_features = (
            market_snapshot.raw.get("point_in_time_dataset_features")
            if isinstance(market_snapshot.raw, dict)
            else None
        )
        pit_integrity = (
            verify_dataset_materialization(
                pit_manifest,
                features=pit_features,
                replay_rows=pit_rows,
            )
            if isinstance(pit_rows, list) and isinstance(pit_features, list)
            else {"passed": False, "errors": ["dataset_materialization_missing"]}
        )
        pit_dataset_eligible = (
            pit_manifest.get("exact_replay_eligible") is True
            and pit_integrity.get("passed") is True
        )

        metric_thresholds_passed = (
            sharpe is not None
            and sharpe >= 1.0
            and max_drawdown is not None
            and max_drawdown <= 15.0
            and (qlib_score is None or qlib_score >= 0)
        )
        passed = bool(
            exact_strategy_replay
            and empirical_backtest
            and lookahead_safe
            and transaction_costs_included
            and qlib_empirical
            and pit_dataset_eligible
            and metric_thresholds_passed
            and (not model_runtime_requested or model_runtime_executed)
            and model_drift_accepted
        )
        advisory_ready = bool(
            finrl_result.get("baseline_passed")
            or qlib_result.get("score") is not None
            or intelligence.evidence
        )

        blockers: list[str] = []
        if not exact_strategy_replay:
            blockers.extend(finrl_result.get("approval_blockers") or ["exact_strategy_replay_not_verified"])
        if not empirical_backtest:
            blockers.append("backtest_not_empirically_valid")
        if not lookahead_safe:
            blockers.append("lookahead_safety_not_verified")
        if not transaction_costs_included:
            blockers.append("transaction_costs_missing")
        if not pit_dataset_eligible:
            blockers.extend(pit_manifest.get("blockers") or ["point_in_time_dataset_missing"])
            blockers.extend(pit_integrity.get("errors") or [])
        if not qlib_empirical:
            blockers.append("qlib_runtime_model_not_empirically_validated")
        if not metric_thresholds_passed:
            blockers.append("research_metrics_below_threshold_or_missing")
        if model_runtime_requested and not model_runtime_executed:
            blockers.extend(model_runtime.get("blockers") or ["research_model_runtime_not_executed"])
        if not model_drift_accepted:
            blockers.extend(model_drift.get("blockers") or ["model_drift_auto_disabled"])

        validation_status = {
            "schema_version": "open_stock_ai.research_validation_status.v3",
            "method": "empirical_execution_evidence_gate",
            "passed": passed,
            "advisory_ready": advisory_ready,
            "execution_evidence_eligible": passed,
            "exact_strategy_replay": exact_strategy_replay,
            "empirical_backtest": empirical_backtest,
            "lookahead_safe": lookahead_safe,
            "transaction_costs_included": transaction_costs_included,
            "pit_dataset_eligible": pit_dataset_eligible,
            "pit_dataset_manifest_hash": pit_manifest.get("manifest_hash"),
            "pit_dataset_sha256": pit_manifest.get("dataset_sha256"),
            "pit_dataset_integrity": pit_integrity,
            "qlib_empirical": qlib_empirical,
            "metric_thresholds_passed": metric_thresholds_passed,
            "model_runtime_requested": model_runtime_requested,
            "model_runtime_executed": model_runtime_executed,
            "model_drift": model_drift,
            "model_drift_accepted": model_drift_accepted,
            "model_registry_recorded": model_registry_receipt.get("status") == "recorded",
            "blockers": blockers,
        }
        summary = (
            "Empirical research validation passed."
            if passed
            else "Advisory research completed; execution evidence is blocked until point-in-time exact replay and empirical model validation pass."
        )

        adapter_results = [
            item["adapter_result"]
            for item in [finrl_result, qlib_result]
            if isinstance(item.get("adapter_result"), dict)
        ]
        qlib_projection = (
            qlib_result.get("factor_projection")
            if isinstance(qlib_result.get("factor_projection"), dict)
            else {}
        )
        artifact_payload = {
            "request": {"symbol": request.symbol, "market": request.market, "horizon": request.horizon},
            "signal": {
                "symbol": signal.symbol,
                "market": signal.market,
                "action": signal.action,
                "confidence": signal.confidence,
                "horizon": signal.horizon,
                "reason": signal.reason,
            },
            "research": {
                "passed": passed,
                "advisory_ready": advisory_ready,
                "summary": summary,
                "backtest_id": finrl_result.get("backtest_id"),
                "sharpe": sharpe,
                "max_drawdown_pct": max_drawdown,
                "win_rate": finrl_result.get("win_rate"),
                "validation_status": validation_status,
            },
            "risk": {"approved": None},
            "adapter_results": adapter_results,
            "qlib_factor_projection": qlib_projection,
            "model_runtime": model_runtime,
            "model_registry": model_registry_receipt,
            "model_drift": model_drift,
        }
        artifacts = self.report_generator.save_research_artifacts(artifact_payload)
        self._attach_research_artifacts_to_qlib_projection(qlib_result, artifacts)
        return ResearchResult(
            passed=passed,
            summary=summary,
            backtest_id=finrl_result.get("backtest_id"),
            sharpe=sharpe,
            max_drawdown_pct=max_drawdown,
            win_rate=finrl_result.get("win_rate"),
            report_path=artifacts["report_path"] or qlib_result.get("report_path"),
            adapter_results=adapter_results,
            artifacts=artifacts,
            raw={
                "finrl": finrl_result,
                "qlib": qlib_result,
                "model_runtime": model_runtime,
                "model_registry": model_registry_receipt,
                "model_drift": model_drift,
                "artifacts": artifacts,
                "validation_status": validation_status,
            },
        )

    def _attach_research_artifacts_to_qlib_projection(
        self,
        qlib_result: dict,
        artifacts: dict,
    ) -> None:
        projection = qlib_result.get("factor_projection")
        if not isinstance(projection, dict):
            return
        projection["research_report"] = {
            "schema_version": artifacts.get("schema_version"),
            "path": artifacts.get("report_path"),
            "backtest_path": artifacts.get("backtest_path"),
            "artifact_kind": "research_report",
        }
        adapter_result = qlib_result.get("adapter_result")
        if isinstance(adapter_result, dict):
            metrics = adapter_result.get("metrics")
            if isinstance(metrics, dict):
                metrics["factor_projection"] = projection

    @staticmethod
    def _attach_signal_model_provenance(
        signal: TradingSignal,
        model_runtime: dict,
        model_registry_receipt: dict,
        model_drift: dict,
    ) -> None:
        """Make persisted signals traceable without mislabelling baseline rules.

        The strategy remains the source of an advisory rule score.  When an
        actual model runtime was requested, the signal carries the immutable
        model IDs and experiment receipt; absent model execution is recorded
        explicitly rather than silently attaching a made-up model version.
        """

        versions = model_registry_receipt.get("model_versions")
        versions = versions if isinstance(versions, list) else []
        experiment = model_registry_receipt.get("experiment")
        experiment = experiment if isinstance(experiment, dict) else {}
        promotion = model_registry_receipt.get("promotion")
        promotion = promotion if isinstance(promotion, dict) else {}
        signal.decision_schema["model_provenance"] = {
            "schema_version": "open_stock_ai.signal_model_provenance.v1",
            "runtime_status": str(model_runtime.get("status") or "not_requested"),
            "registry_status": str(model_registry_receipt.get("status") or "not_configured"),
            "model_version_ids": [
                str(item.get("model_version_id")) for item in versions
                if isinstance(item, dict) and str(item.get("model_version_id") or "").strip()
            ],
            "experiment_id": experiment.get("experiment_id"),
            "model_output": model_runtime.get("status") == "executed",
            "approval_statuses": [
                str((item.get("approval") or {}).get("status") or "unknown")
                for item in versions if isinstance(item, dict)
            ],
            "drift_monitor_status": str(model_drift.get("status") or "not_configured"),
            "drift_monitor_blockers": list(model_drift.get("blockers") or []),
            "promotion_status": str(promotion.get("status") or "research_only_unpromoted"),
            "deployment_mode": str(promotion.get("deployment_mode") or "none"),
            "promotion_id": promotion.get("promotion_id"),
            "execution_authority": "none",
        }
