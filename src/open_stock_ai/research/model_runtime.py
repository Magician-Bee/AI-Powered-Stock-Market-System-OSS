from __future__ import annotations

"""Explicit, receipt-backed access to the isolated FinRL and Qlib runtimes.

The normal interactive research path deliberately stays advisory.  A caller has
to attach ``research_model_runtime`` to a point-in-time dataset snapshot before
this adapter is allowed to train or load a model.  This prevents a live quote
from being silently converted into a fitted model or from being presented as
out-of-sample evidence.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.research.model_registry import ResearchModelRegistry
from open_stock_ai.research.differential_metrics import qlib_product_differential_receipt
from open_stock_ai.research.pit_dataset import (
    DATASET_MANIFEST_SCHEMA_VERSION,
    verify_dataset_materialization,
)
from open_stock_ai.types import MarketSnapshot, StockRequest
from stock_ai.external_project_tools import ExternalProjectToolProvider


WorkflowRunner = Callable[[str, dict[str, Any], AgentRunContext], dict[str, Any]]


@dataclass
class ResearchModelRuntime:
    """Run real models only from an explicit immutable PIT research request."""

    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[3])
    runner: WorkflowRunner | None = None

    def run(
        self,
        request: StockRequest,
        market_snapshot: MarketSnapshot,
        *,
        model_registry: ResearchModelRegistry | None = None,
    ) -> dict[str, Any]:
        config = market_snapshot.raw.get("research_model_runtime") if isinstance(market_snapshot.raw, dict) else None
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return self._not_requested()

        manifest = self._manifest(market_snapshot)
        rows = market_snapshot.raw.get("point_in_time_dataset") if isinstance(market_snapshot.raw, dict) else None
        features = market_snapshot.raw.get("point_in_time_dataset_features") if isinstance(market_snapshot.raw, dict) else None
        if (
            manifest.get("exact_replay_eligible") is not True
            or not isinstance(rows, list)
            or not isinstance(features, list)
        ):
            return self._blocked("point_in_time_dataset_not_execution_eligible", manifest=manifest)
        integrity = verify_dataset_materialization(
            manifest,
            features=features,
            replay_rows=rows,
        )
        if integrity.get("passed") is not True:
            return self._blocked(
                "point_in_time_dataset_integrity_failed",
                manifest=manifest,
                dataset_hash=str(integrity.get("observed_dataset_sha256") or "") or None,
                error={"errors": list(integrity.get("errors") or [])},
            )
        dataset_hash = str(manifest.get("dataset_sha256") or "")
        requested_hash = str(config.get("dataset_manifest_hash") or "")
        if not requested_hash or requested_hash != manifest.get("manifest_hash"):
            return self._blocked("dataset_manifest_hash_mismatch", manifest=manifest, dataset_hash=dataset_hash)

        prices = self._prices(rows)
        if len(prices) < 8:
            return self._blocked("insufficient_point_in_time_prices", manifest=manifest, dataset_hash=dataset_hash)
        mode = str(config.get("mode") or "").strip()
        if mode not in {"fit_and_infer", "reuse_and_infer"}:
            return self._blocked("research_model_runtime_mode_invalid", manifest=manifest, dataset_hash=dataset_hash)

        run_id = f"research-{request.symbol.replace('.', '-')}-{dataset_hash[:16]}"
        context = AgentRunContext(
            run_id=run_id,
            autonomy="external_execute",
            symbols=(request.symbol,),
            allow_external_actions=True,
        )
        try:
            registered_models = self._authorize_oos_models(
                mode=mode,
                config=config,
                registry=model_registry,
                manifest_hash=str(manifest.get("manifest_hash") or ""),
                dataset_hash=dataset_hash,
            )
            finrl = self._run_finrl(config, prices, dataset_hash, context, registered_model=registered_models.get("finrl"))
            qlib = self._run_qlib(config, manifest, context, registered_model=registered_models.get("qlib"))
            differential = self._differential_receipts(finrl, qlib, context)
        except (OSError, RuntimeError, ValueError, PermissionError) as exc:
            return self._blocked(
                "external_model_runtime_failed",
                manifest=manifest,
                dataset_hash=dataset_hash,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
        return {
            "schema_version": "open_stock_ai.research_model_runtime.v1",
            "status": "executed",
            "mode": mode,
            "requested_configuration": {
                "finrl": dict(config.get("finrl") or {}),
                "qlib": dict(config.get("qlib") or {}),
            },
            "dataset": {
                "manifest_hash": manifest.get("manifest_hash"),
                "data_sha256": dataset_hash,
                "point_in_time_verified": True,
                "row_count": len(rows),
                "feature_contract": self._feature_contract(manifest, features),
            },
            "reproducibility": self._reproducibility_contract(config),
            "finrl": finrl,
            "qlib": qlib,
            "differential_validation": differential,
            "execution_boundary": "isolated_framework_compute_no_live_brokerage",
        }

    def _run_finrl(
        self,
        config: dict[str, Any],
        prices: list[list[float]],
        dataset_hash: str,
        context: AgentRunContext,
        *,
        registered_model: dict[str, Any] | None,
    ) -> dict[str, Any]:
        finrl_config = config.get("finrl") if isinstance(config.get("finrl"), dict) else {}
        mode = str(config.get("mode"))
        artifact_name = str(finrl_config.get("artifact_name") or "").strip()
        algorithm = str(finrl_config.get("algorithm") or "ppo").lower()
        if not artifact_name:
            raise ValueError("finrl_artifact_name_required")
        if mode == "fit_and_infer":
            policy_path = self.project_root / ".runtime" / "external-workflows" / "policies" / "finrl" / f"{artifact_name}.zip"
            if policy_path.exists():
                raise ValueError("finrl_artifact_already_exists_use_reuse_and_infer")
            training = self._execute(
                "external.finrl.train_policy",
                {
                    "prices": prices,
                    "algorithm": algorithm,
                    "artifact_name": artifact_name,
                    "total_timesteps": int(finrl_config.get("total_timesteps") or 64),
                    "seed": int(finrl_config.get("seed") or 0),
                    "initial_capital": float(finrl_config.get("initial_capital") or 100_000),
                    "max_stock": int(finrl_config.get("max_stock") or 10),
                    "model_kwargs": dict(finrl_config.get("model_kwargs") or {}),
                    "timeout_seconds": int(finrl_config.get("timeout_seconds") or 900),
                },
                context,
            )
            training_hash = str(((training.get("result") or {}).get("policy_sha256")) or "")
            training_provenance = training.get("model_provenance") if isinstance(training.get("model_provenance"), dict) else {}
            if (
                training.get("schema_version") != "open_stock_ai.external_full_workflow.v1"
                or training.get("project") != "finrl"
                or training.get("action") != "train_policy"
                or training.get("executed_function") != "DRLAgent.get_model/train_model"
                or training_provenance.get("training_executed") is not True
                or not self._is_sha256(training_hash)
            ):
                raise ValueError("finrl_training_receipt_incomplete")
        else:
            if registered_model is None:
                raise ValueError("oos_finrl_registry_authorization_required")
            artifact = registered_model.get("artifact") if isinstance(registered_model.get("artifact"), dict) else {}
            if str(artifact.get("artifact_name") or "") != artifact_name:
                raise ValueError("oos_finrl_artifact_name_mismatch")
            training = dict(registered_model.get("training_receipt") or {})
            training_hash = self._required_model_hash(artifact, "policy_sha256")
        inference = self._execute(
            "external.finrl.predict_actions",
            {
                "prices": prices,
                "algorithm": algorithm,
                "artifact_name": artifact_name,
                "initial_capital": float(finrl_config.get("initial_capital") or 100_000),
                "max_stock": int(finrl_config.get("max_stock") or 10),
                "timeout_seconds": int(finrl_config.get("timeout_seconds") or 900),
            },
            context,
        )
        inference_hash = str(((inference.get("result") or {}).get("policy_sha256")) or "")
        if not self._is_sha256(inference_hash) or inference_hash != training_hash:
            raise ValueError("finrl_policy_hash_mismatch")
        return {
            "training": training,
            "inference": inference,
            "immutable_artifact": {
                "artifact_name": artifact_name,
                "policy_sha256": inference_hash,
                "dataset_sha256": dataset_hash,
                "refit_allowed": False,
            },
        }

    def _differential_receipts(
        self,
        finrl: dict[str, Any],
        qlib: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        finrl_result = (finrl.get("inference") or {}).get("result")
        qlib_result = ((qlib.get("workflow") or {}).get("result"))
        finrl_returns = self._finrl_returns(finrl_result if isinstance(finrl_result, dict) else {})
        qlib_path = (qlib_result or {}).get("portfolio_return_path") if isinstance(qlib_result, dict) else None
        if not isinstance(qlib_path, dict) or not isinstance(qlib_path.get("returns"), list):
            raise ValueError("qlib_portfolio_return_path_missing")
        qlib_returns = [float(value) for value in qlib_path["returns"]]
        qlib_periods = int(qlib_path.get("periods_per_year") or 0)
        if qlib_periods < 1:
            raise ValueError("qlib_portfolio_return_path_periods_missing")
        receipts: dict[str, dict[str, Any]] = {}
        for subject, returns, periods_per_year in (
            ("finrl_policy_rollout", finrl_returns, 252),
            ("qlib_portfolio_workflow", qlib_returns, qlib_periods),
        ):
            reference = self._execute(
                "external.qlib.reference_risk_metrics",
                {"returns": returns, "periods_per_year": periods_per_year},
                context,
            )
            receipt = qlib_product_differential_receipt(
                returns,
                reference,
                periods_per_year=periods_per_year,
                subject=subject,
            )
            if receipt["passed"] is not True:
                raise ValueError(f"qlib_differential_metric_mismatch:{subject}")
            receipts[subject] = receipt
        return {
            "schema_version": "open_stock_ai.research_model_differential_validation.v1",
            "status": "passed",
            "reference_framework": "qlib",
            "receipts": receipts,
        }

    @staticmethod
    def _finrl_returns(result: dict[str, Any]) -> list[float]:
        initial = float(result.get("initial_total_asset") or 0.0)
        steps = result.get("steps")
        if initial <= 0.0 or not isinstance(steps, list) or len(steps) < 2:
            raise ValueError("finrl_rollout_return_path_missing")
        assets = [initial]
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("finrl_rollout_step_invalid")
            total_asset = float(step.get("total_asset") or 0.0)
            if total_asset <= 0.0:
                raise ValueError("finrl_rollout_total_asset_invalid")
            assets.append(total_asset)
        return [current / previous - 1.0 for previous, current in zip(assets, assets[1:])]

    def _run_qlib(
        self,
        config: dict[str, Any],
        manifest: dict[str, Any],
        context: AgentRunContext,
        *,
        registered_model: dict[str, Any] | None,
    ) -> dict[str, Any]:
        qlib_config = config.get("qlib") if isinstance(config.get("qlib"), dict) else {}
        config_path = str(qlib_config.get("config_path") or "").strip()
        provider_uri = str(qlib_config.get("provider_uri") or "").strip()
        config_hash = str(qlib_config.get("config_sha256") or "").strip()
        if not config_path or not provider_uri or not self._is_sha256(config_hash):
            raise ValueError("qlib_config_path_provider_uri_and_config_sha256_required")
        supplied_manifest = str(qlib_config.get("dataset_manifest_hash") or "")
        if supplied_manifest != str(manifest.get("manifest_hash") or ""):
            raise ValueError("qlib_dataset_manifest_hash_mismatch")
        if str(config.get("mode")) == "reuse_and_infer":
            if registered_model is None:
                raise ValueError("oos_qlib_registry_authorization_required")
            # Qlib's current external worker exposes complete workflow training
            # only.  Do not reuse that training entry point as an OOS shortcut:
            # until a separately verified load/predict worker exists, refuse
            # rather than refitting the registered model on evaluation data.
            raise ValueError("oos_qlib_immutable_inference_adapter_unavailable")
        payload = {
            "config_path": config_path,
            "provider_uri": provider_uri,
            "experiment_name": str(qlib_config.get("experiment_name") or f"research-{context.run_id}"),
            "seed": int(qlib_config.get("seed") or 0),
            "timeout_seconds": int(qlib_config.get("timeout_seconds") or 900),
        }
        workflow = self._execute("external.qlib.backtest_model", payload, context)
        result = workflow.get("result") if isinstance(workflow.get("result"), dict) else {}
        if (
            workflow.get("executed_function") != "qlib.cli.run.workflow"
            or result.get("workflow_completed") is not True
            or result.get("config_sha256") != config_hash
        ):
            raise ValueError("qlib_workflow_receipt_incomplete")
        return {
            "workflow": workflow,
            "dataset_manifest_hash": supplied_manifest,
            "config_sha256": config_hash,
        }

    @staticmethod
    def _feature_contract(manifest: dict[str, Any], features: list[Any]) -> dict[str, Any]:
        """Bind a trained artifact to the schema and code that produced it."""

        code_hashes = sorted({
            str((item.get("lineage") or {}).get("code_sha256") or "")
            for item in features
            if isinstance(item, dict)
        } - {""})
        if not code_hashes or any(not ResearchModelRuntime._is_sha256(value) for value in code_hashes):
            raise ValueError("point_in_time_feature_code_lineage_missing")
        schema_versions = manifest.get("feature_schema_versions")
        if not isinstance(schema_versions, dict) or not schema_versions:
            raise ValueError("point_in_time_feature_schema_missing")
        lineage_hash = str(manifest.get("feature_lineage_sha256") or "")
        if not ResearchModelRuntime._is_sha256(lineage_hash):
            raise ValueError("point_in_time_feature_lineage_hash_missing")
        return {
            "schema_version": "open_stock_ai.research_feature_contract.v1",
            "feature_schema_versions": {
                str(domain): sorted(str(version) for version in versions)
                for domain, versions in sorted(schema_versions.items())
                if isinstance(versions, list)
            },
            "feature_lineage_sha256": lineage_hash,
            "feature_code_sha256": code_hashes,
        }

    @staticmethod
    def _reproducibility_contract(config: dict[str, Any]) -> dict[str, Any]:
        finrl = config.get("finrl") if isinstance(config.get("finrl"), dict) else {}
        qlib = config.get("qlib") if isinstance(config.get("qlib"), dict) else {}
        return {
            "schema_version": "open_stock_ai.research_reproducibility_contract.v1",
            "random_seeds": {
                "finrl": int(finrl.get("seed") or 0),
                "qlib": int(qlib.get("seed") or 0),
            },
            "deterministic_controls": {
                "finrl_inference": "model_predict_deterministic_true",
                "qlib_workflow": "worker_seeded_before_workflow",
            },
        }

    def _execute(self, name: str, arguments: dict[str, Any], context: AgentRunContext) -> dict[str, Any]:
        if self.runner is not None:
            return self.runner(name, arguments, context)
        provider = ExternalProjectToolProvider(project_root=self.project_root)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(provider.execute(name, arguments, context))
        raise RuntimeError("research_model_runtime_requires_sync_worker_context")

    @staticmethod
    def _required_model_hash(payload: dict[str, Any], key: str) -> str:
        value = str(payload.get(key) or "")
        if not ResearchModelRuntime._is_sha256(value):
            raise ValueError(f"immutable_model_artifact_hash_invalid:{key}")
        return value

    @staticmethod
    def _authorize_oos_models(
        *,
        mode: str,
        config: dict[str, Any],
        registry: ResearchModelRegistry | None,
        manifest_hash: str,
        dataset_hash: str,
    ) -> dict[str, dict[str, Any]]:
        if mode != "reuse_and_infer":
            return {}
        if registry is None:
            raise ValueError("oos_model_registry_required")
        versions = config.get("model_version_ids") if isinstance(config.get("model_version_ids"), dict) else {}
        authorized: dict[str, dict[str, Any]] = {}
        for framework in ("finrl", "qlib"):
            model_version_id = str(versions.get(framework) or "").strip()
            if not model_version_id:
                raise ValueError(f"oos_{framework}_model_version_id_required")
            authorized[framework] = registry.authorize_oos_inference(
                framework=framework,
                model_version_id=model_version_id,
                evaluation_manifest_hash=manifest_hash,
                evaluation_dataset_sha256=dataset_hash,
            )
        return authorized

    @staticmethod
    def _prices(rows: list[Any]) -> list[list[float]]:
        prices: list[list[float]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("point_in_time_row_invalid")
            close = row.get("close")
            if not isinstance(close, (int, float)) or isinstance(close, bool) or close <= 0:
                raise ValueError("point_in_time_close_invalid")
            prices.append([float(close)])
        return prices

    @staticmethod
    def _manifest(snapshot: MarketSnapshot) -> dict[str, Any]:
        manifest = snapshot.raw.get("point_in_time_dataset_manifest") if isinstance(snapshot.raw, dict) else None
        if not isinstance(manifest, dict):
            return {}
        value = dict(manifest)
        if value.get("schema_version") != DATASET_MANIFEST_SCHEMA_VERSION:
            return {}
        return value

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())

    @staticmethod
    def _not_requested() -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.research_model_runtime.v1",
            "status": "not_requested",
            "execution_boundary": "explicit_opt_in_required_no_model_execution",
        }

    @staticmethod
    def _blocked(
        blocker: str,
        *,
        manifest: dict[str, Any] | None = None,
        dataset_hash: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "open_stock_ai.research_model_runtime.v1",
            "status": "blocked",
            "blockers": [blocker],
            "dataset": {"manifest_hash": (manifest or {}).get("manifest_hash"), "data_sha256": dataset_hash},
            "execution_boundary": "explicit_opt_in_required_no_model_execution",
        }
        if error is not None:
            payload["error"] = error
        return payload
