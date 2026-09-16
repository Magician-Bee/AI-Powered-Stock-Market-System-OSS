from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from open_stock_ai.external_sources.registry import ExternalProjectRegistry, default_registry
from open_stock_ai.research.factor_research import FactorResearch
from open_stock_ai.types import MarketSnapshot, StockRequest, TradingSignal, adapter_result_to_dict


@dataclass
class QlibSource:
    registry: ExternalProjectRegistry | None = None
    factor_research: FactorResearch | None = None

    def load_capability_contract(self) -> dict[str, Any]:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "qlib",
            capability_terms=[
                "workflow",
                "alpha",
                "factor",
                "backtest",
                "dataset",
                "LightGBM",
                "Transformer",
            ],
        )
        project_path = Path(registry.root) / profile.path
        profile_dict = asdict(profile)
        adapter_result = adapter_result_to_dict(
            source_key="qlib",
            source_name="Qlib",
            role="contract",
            method="local_workflow_factor_contract",
            status="verified" if profile.origin_verified else "missing",
            summary=(
                "Qlib workflow, alpha and factor contracts were parsed locally. "
                "This verifies source lineage and available interfaces only; no Qlib training or inference was executed."
            ),
            capability_contract={
                "loaded": profile.origin_verified,
                "runtime_connected": False,
                "model_trained": False,
                "model_inferred": False,
                "execution_evidence_eligible": False,
                "execution_boundary": "read_only_contract_no_qlib_runtime_import",
            },
            external_projects={"qlib": profile_dict},
        )
        return {
            "source": "Qlib",
            "loaded": profile.origin_verified,
            "status": "verified" if profile.origin_verified else "missing",
            "runtime_status": "not_connected",
            "model_trained": False,
            "model_inferred": False,
            "execution_evidence_eligible": False,
            "method": "local_workflow_factor_contract",
            "adapter_result": adapter_result,
            "workflow_contract": self._workflow_contract(project_path),
            "documentation_contract": self._documentation_contract(project_path),
            "execution_boundary": "read_only_contract_no_qlib_runtime_import",
        }

    def validate_factor(self, request: StockRequest, signal: TradingSignal, market_snapshot: MarketSnapshot) -> dict:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "qlib",
            capability_terms=[
                "workflow",
                "alpha",
                "factor",
                "backtest",
                "dataset",
                "LightGBM",
                "Transformer",
            ],
        )
        status = "verified" if profile.origin_verified else "missing"
        result = (self.factor_research or FactorResearch()).evaluate(request, signal, market_snapshot)
        capability_contract = self.load_capability_contract()
        workflow_summary = self._workflow_summary(
            workflow_contract=capability_contract.get("workflow_contract") or {},
            factor_result=result,
            signal=signal,
        )
        factor_projection = self._factor_projection(
            workflow_summary=workflow_summary,
            factor_result=result,
            signal=signal,
        )
        method = "local_factor_score_plus_contract"
        profile_dict = asdict(profile)
        note = (
            "Qlib source tree is mapped for factor research and workflow references; no Qlib runtime, trained model "
            f"or inference is loaded inside OpenStockAIEngine. {result.get('note', '')}"
        )
        metrics = {
            "score": result.get("score"),
            "model_score": result.get("model_score"),
            "rank_ic_proxy": result.get("rank_ic_proxy"),
            "rank_ic_method": result.get("rank_ic_method"),
            "passed": result.get("passed"),
            "advisory_ready": result.get("advisory_ready"),
            "runtime_connected": result.get("runtime_connected"),
            "empirical_valid": result.get("empirical_valid"),
            "execution_evidence_eligible": result.get("execution_evidence_eligible"),
            "factors": result.get("factors", {}),
            "workflow_summary": workflow_summary,
            "factor_projection": factor_projection,
        }
        return {
            **result,
            "status": status,
            "runtime_status": "not_connected",
            "model_trained": False,
            "model_inferred": False,
            "note": note,
            "method": method,
            "workflow_summary": workflow_summary,
            "factor_projection": factor_projection,
            "capability_contract": capability_contract,
            "external_project": profile_dict,
            "adapter_result": adapter_result_to_dict(
                source_key="qlib",
                source_name="Qlib",
                role="research",
                method=method,
                status=status,
                summary=note,
                evidence=[],
                risks=list(result.get("approval_blockers") or []),
                metrics=metrics,
                capability_contract=capability_contract,
                external_projects={"qlib": profile_dict},
            ),
        }

    def _workflow_summary(
        self,
        *,
        workflow_contract: dict[str, Any],
        factor_result: dict[str, Any],
        signal: TradingSignal,
    ) -> dict[str, Any]:
        workflows = workflow_contract.get("sample_workflows")
        sample_workflows = workflows if isinstance(workflows, list) else []
        preferred = self._select_workflow(sample_workflows, signal)
        families = workflow_contract.get("families")
        families = families if isinstance(families, dict) else {}
        return {
            "schema_version": "open_stock_ai.qlib_workflow_summary.v1",
            "method": "local_qlib_workflow_projection",
            "workflow_count": workflow_contract.get("workflow_count", 0),
            "family_count": len(families),
            "selected_workflow": preferred,
            "selected_workflow_is_reference_only": True,
            "factor_score": factor_result.get("score"),
            "factor_passed": factor_result.get("passed"),
            "runtime_connected": False,
            "model_trained": False,
            "model_inferred": False,
            "signal_action": signal.action,
            "execution_boundary": "read_only_contract_no_qlib_runtime_import",
        }

    def _factor_projection(
        self,
        *,
        workflow_summary: dict[str, Any],
        factor_result: dict[str, Any],
        signal: TradingSignal,
    ) -> dict[str, Any]:
        selected = (
            workflow_summary.get("selected_workflow")
            if isinstance(workflow_summary.get("selected_workflow"), dict)
            else {}
        )
        factors = factor_result.get("factors") if isinstance(factor_result.get("factors"), dict) else {}
        score = factor_result.get("score")
        model_score = factor_result.get("model_score")
        rank_ic_proxy = factor_result.get("rank_ic_proxy")
        advisory_passed = factor_result.get("passed") is True
        empirical_valid = factor_result.get("empirical_valid") is True
        execution_evidence_eligible = factor_result.get("execution_evidence_eligible") is True
        blockers = list(factor_result.get("approval_blockers") or [])
        if not empirical_valid and "qlib_model_not_empirically_validated" not in blockers:
            blockers.append("qlib_model_not_empirically_validated")
        return {
            "schema_version": "open_stock_ai.qlib_factor_projection.v1",
            "method": "local_qlib_factor_projection",
            "passed": advisory_passed,
            "advisory_ready": factor_result.get("advisory_ready") is True,
            "runtime_connected": False,
            "model_trained": False,
            "model_inferred": False,
            "empirical_valid": empirical_valid,
            "execution_evidence_eligible": execution_evidence_eligible and empirical_valid,
            "approval_blockers": blockers,
            "score": score,
            "model_score": model_score,
            "rank_ic_proxy": rank_ic_proxy,
            "rank_ic_method": factor_result.get("rank_ic_method") or "single_symbol_forward_return_proxy_not_cross_sectional_ic",
            "factors": factors,
            "thresholds": {
                "minimum_score": -0.35,
                "minimum_model_score": 0.325,
                "minimum_rank_ic_proxy": -0.20,
                "buy_add_requires_positive_score": True,
                "sell_reduce_requires_negative_score": True,
                "requires_trained_model": True,
                "requires_cross_sectional_validation": True,
            },
            "signal_action": signal.action,
            "selected_workflow": selected,
            "selected_workflow_is_reference_only": True,
            "selected_family": selected.get("family"),
            "selected_model": selected.get("model"),
            "selected_dataset": selected.get("dataset"),
            "selected_strategy": selected.get("strategy"),
            "workflow_count": workflow_summary.get("workflow_count", 0),
            "family_count": workflow_summary.get("family_count", 0),
            "workflow_summary_schema_version": workflow_summary.get("schema_version"),
            "research_report": {
                "schema_version": "open_stock_ai.research_artifacts.v1",
                "path": factor_result.get("report_path"),
                "artifact_kind": "research_report",
            },
            "execution_boundary": "read_only_contract_no_qlib_runtime_import",
        }

    def _select_workflow(self, workflows: list[Any], signal: TradingSignal) -> dict[str, Any] | None:
        candidates = [item for item in workflows if isinstance(item, dict)]
        if not candidates:
            return None
        if signal.action in {"buy", "add"}:
            preferred_models = {"LightGBM", "XGBoost", "CatBoostModel", "LSTM", "Transformer"}
        elif signal.action in {"sell", "reduce"}:
            preferred_models = {"LinearModel", "XGBoost", "LightGBM"}
        else:
            preferred_models = {"LightGBM", "LinearModel", "CatBoostModel"}
        for workflow in candidates:
            if workflow.get("model") in preferred_models:
                return workflow
        return candidates[0]

    def _workflow_contract(self, project_path: Path) -> dict[str, Any]:
        benchmark_root = project_path / "examples" / "benchmarks"
        workflow_files = sorted(benchmark_root.rglob("workflow_config*.yaml")) if benchmark_root.exists() else []
        families: dict[str, int] = {}
        samples: list[dict[str, Any]] = []
        for path in workflow_files:
            family = path.parent.name
            families[family] = families.get(family, 0) + 1
            if len(samples) < 12:
                config = self._read_yaml(path)
                task = config.get("task") if isinstance(config, dict) else {}
                task = task if isinstance(task, dict) else {}
                port_analysis = config.get("port_analysis_config") if isinstance(config, dict) else {}
                port_analysis = port_analysis if isinstance(port_analysis, dict) else {}
                samples.append(
                    {
                        "path": self._relative(project_path, path),
                        "family": family,
                        "model": self._class_name(task.get("model")),
                        "dataset": self._class_name(task.get("dataset")),
                        "strategy": self._class_name(port_analysis.get("strategy")),
                        "record": self._record_classes(task.get("record")),
                    }
                )
        return {
            "workflow_count": len(workflow_files),
            "families": families,
            "sample_workflows": samples,
        }

    def _documentation_contract(self, project_path: Path) -> dict[str, Any]:
        docs = [
            project_path / "docs" / "advanced" / "alpha.rst",
            project_path / "docs" / "component" / "workflow.rst",
            project_path / "examples" / "workflow_by_code.py",
            project_path / "examples" / "benchmarks" / "README.md",
        ]
        return {
            "documents": [
                {
                    "path": self._relative(project_path, path),
                    "exists": path.exists(),
                    "title": self._title(path),
                }
                for path in docs
            ],
        }

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, yaml.YAMLError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _class_name(self, value: Any) -> str | None:
        if isinstance(value, dict):
            class_name = value.get("class")
            return str(class_name) if class_name is not None else None
        return None

    def _record_classes(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        records: list[str] = []
        for item in value:
            class_name = self._class_name(item)
            if class_name:
                records.append(class_name)
        return records

    def _title(self, path: Path) -> str | None:
        if not path.exists():
            return None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or None
            if stripped and set(stripped) not in [{"="}, {"-"}]:
                return stripped[:120]
        return None

    def _relative(self, project_path: Path, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(project_path.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)
