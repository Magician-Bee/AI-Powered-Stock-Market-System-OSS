from __future__ import annotations

"""Immutable registry for actual FinRL/Qlib model execution evidence."""

import hashlib
import json
import math
from numbers import Real
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from open_stock_ai.governance.artifact_rollback import ApprovedArtifactRollbackRegistry
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.types import StockRequest

from .experiment_store import ExperimentStore


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _identifier(prefix: str, value: Any) -> str:
    return f"{prefix}-{_canonical_sha256(value)[:32]}"


@dataclass(slots=True)
class ResearchModelRegistry:
    """Store versioned model evidence; never infer it from a report filename."""

    store: SQLiteStore
    experiment_store: ExperimentStore | None = None
    artifact_rollback: ApprovedArtifactRollbackRegistry | None = None

    def __post_init__(self) -> None:
        self.experiment_store = self.experiment_store or ExperimentStore(self.store.path)
        if self.artifact_rollback is not None and self.artifact_rollback.store is not None:
            if Path(self.artifact_rollback.store.path).resolve() != Path(self.store.path).resolve():
                raise ValueError("model_governance_must_share_runtime_database")

    def record_execution(self, request: StockRequest, runtime: dict[str, Any]) -> dict[str, Any]:
        if runtime.get("status") != "executed":
            return {
                "schema_version": "open_stock_ai.research_model_registry_receipt.v1",
                "status": "not_recorded",
                "reason": "model_runtime_not_executed",
            }
        dataset = runtime.get("dataset") if isinstance(runtime.get("dataset"), dict) else {}
        manifest_hash = self._required_value(dataset, "manifest_hash")
        dataset_hash = self._required_hash(dataset, "data_sha256")
        feature_contract = self._feature_contract(dataset)
        runtime_hash = _canonical_sha256(runtime)
        created_at = datetime.now(timezone.utc).isoformat()
        finrl = runtime.get("finrl") if isinstance(runtime.get("finrl"), dict) else {}
        qlib = runtime.get("qlib") if isinstance(runtime.get("qlib"), dict) else {}

        models = [
            self._finrl_version(
                finrl, runtime.get("requested_configuration"), manifest_hash, dataset_hash,
                runtime_hash, feature_contract, created_at,
            ),
            self._qlib_version(
                qlib, runtime.get("requested_configuration"), manifest_hash, dataset_hash,
                runtime_hash, feature_contract, created_at,
            ),
        ]
        saved_models = [self.store.save_research_model_version(model) for model in models]
        model_version_ids = [model["model_version_id"] for model in models]
        reproducibility = self._reproducibility(runtime, models)
        experiment = {
            "schema_version": "open_stock_ai.experiment_entity.v1",
            "experiment_id": _identifier(
                "research-experiment",
                {"runtime_receipt_sha256": runtime_hash, "request": self._request_payload(request)},
            ),
            "kind": "model_training_and_evaluation",
            **self._request_payload(request),
            "dataset_manifest_hash": manifest_hash,
            "dataset_sha256": dataset_hash,
            "runtime_receipt_sha256": runtime_hash,
            "model_version_ids": model_version_ids,
            "reproducibility": reproducibility,
            "created_at": created_at,
            "runtime_receipt": runtime,
        }
        saved_experiment = self.experiment_store.save(experiment)
        promotion = self.promotion_status(model_version_ids)
        return {
            "schema_version": "open_stock_ai.research_model_registry_receipt.v1",
            "status": "recorded",
            "model_versions": models,
            "model_saves": saved_models,
            "experiment": experiment,
            "experiment_save": saved_experiment,
            "promotion": promotion,
        }

    def verify_metric_tolerance_replay(
        self, experiment_id: str, observed_metrics: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Compare a fresh replay's reported metrics to its immutable receipt.

        This does not label a framework bitwise reproducible: accelerator and
        third-party kernel determinism still need a separate attestation.  It
        supplies the honest fallback required for research runs whose metrics,
        rather than artifact bytes, can be compared within declared tolerances.
        """

        experiment = next(
            (item for item in self.experiment_store.list(500)
             if item.get("experiment_id") == experiment_id),
            None,
        )
        if experiment is None:
            raise ValueError("research_experiment_not_found")
        contract = (
            experiment.get("reproducibility", {}).get("metric_tolerance_replay")
            if isinstance(experiment.get("reproducibility"), dict)
            else None
        )
        if not isinstance(contract, dict):
            raise ValueError("research_experiment_metric_tolerance_contract_missing")
        reference = contract.get("reference_metrics")
        tolerance = contract.get("tolerance")
        if not isinstance(reference, dict) or not isinstance(tolerance, dict):
            raise ValueError("research_experiment_metric_tolerance_contract_invalid")
        absolute = float(tolerance.get("absolute") or 0.0)
        relative = float(tolerance.get("relative") or 0.0)
        comparisons: list[dict[str, Any]] = []
        for framework, metrics in sorted(reference.items()):
            expected = metrics if isinstance(metrics, dict) else {}
            observed = observed_metrics.get(framework) if isinstance(observed_metrics, dict) else None
            observed = observed if isinstance(observed, dict) else {}
            for metric, value in sorted(expected.items()):
                candidate = observed.get(metric)
                passed = (
                    isinstance(candidate, Real) and not isinstance(candidate, bool)
                    and math.isfinite(float(candidate))
                    and math.isclose(float(candidate), float(value), rel_tol=relative, abs_tol=absolute)
                )
                comparisons.append({
                    "framework": framework,
                    "metric": metric,
                    "expected": value,
                    "observed": candidate,
                    "passed": passed,
                })
        return {
            "schema_version": "open_stock_ai.research_metric_tolerance_replay.v1",
            "experiment_id": experiment_id,
            "mode": "metric_tolerance",
            "passed": bool(comparisons) and all(item["passed"] for item in comparisons),
            "comparisons": comparisons,
            "tolerance": {"absolute": absolute, "relative": relative},
        }

    def promote_champion(
        self,
        *,
        champion_model_version_id: str,
        challenger_model_version_ids: list[str],
        deployment_mode: str,
        human_promotion_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Record a human decision to deploy a champion with challengers.

        This is intentionally an explicit administrative action, never part of
        training or inference.  A promotion receipt names the exact immutable
        versions, preserves shadow-vs-production intent, and does not turn a
        model into a brokerage execution authority.
        """

        champion = self.store.research_model_version(champion_model_version_id)
        challengers = list(dict.fromkeys(str(value) for value in challenger_model_version_ids))
        if champion is None:
            raise ValueError("promotion_champion_model_not_found")
        if not challengers:
            raise ValueError("promotion_challengers_required")
        if champion_model_version_id in challengers:
            raise ValueError("promotion_champion_cannot_be_challenger")
        if deployment_mode not in {"shadow", "production"}:
            raise ValueError("promotion_mode_invalid")
        challenger_versions = [self.store.research_model_version(value) for value in challengers]
        if any(item is None for item in challenger_versions):
            raise ValueError("promotion_challenger_model_not_found")
        required_human_fields = ("decision_id", "approver_id", "approved_at", "rationale")
        if not isinstance(human_promotion_receipt, dict) or any(
            not str(human_promotion_receipt.get(key) or "").strip() for key in required_human_fields
        ):
            raise ValueError("promotion_human_receipt_required")
        approver_id = str(human_promotion_receipt["approver_id"]).strip()
        if approver_id.casefold() in {"agent", "codex", "model", "ai", "system"}:
            raise ValueError("promotion_human_receipt_must_name_human_approver")
        all_versions = [champion, *[item for item in challenger_versions if item is not None]]
        if any(item.get("approval", {}).get("production_eligible") is True for item in all_versions):
            raise ValueError("promotion_must_not_mutate_model_version_approval")
        created_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "schema_version": "open_stock_ai.model_promotion_receipt.v1",
            "promotion_id": _identifier(
                "model-promotion",
                {
                    "champion": champion_model_version_id,
                    "challengers": challengers,
                    "deployment_mode": deployment_mode,
                    "human_receipt": human_promotion_receipt,
                },
            ),
            "champion_model_version_id": champion_model_version_id,
            "challenger_model_version_ids": challengers,
            "deployment_mode": deployment_mode,
            "human_promotion_receipt": dict(human_promotion_receipt),
            "execution_authority": "none",
            "created_at": created_at,
        }
        rollback_state = None
        if self.artifact_rollback is not None:
            rollback_state = (
                dict(self.artifact_rollback._approved),
                list(self.artifact_rollback._activation_history),
                list(self.artifact_rollback.receipts),
            )
        try:
            with self.store._connect() as connection:
                connection.execute("begin immediate")
                saved = self.store.save_model_promotion_receipt(payload, connection=connection)
                if self.artifact_rollback is not None:
                    self.artifact_rollback.approve(
                        champion_model_version_id,
                        str(champion.get("artifact_sha256") or ""),
                        approved_by=approver_id,
                        approved_at=str(human_promotion_receipt["approved_at"]),
                        metadata={
                            "artifact_scope": "model",
                            "framework": champion.get("framework"),
                            "promotion_id": payload["promotion_id"],
                            "deployment_mode": deployment_mode,
                            "challenger_model_version_ids": challengers,
                        },
                        connection=connection,
                    )
                    self.artifact_rollback.activate(champion_model_version_id, connection=connection)
                connection.commit()
        except Exception:
            if rollback_state is not None and self.artifact_rollback is not None:
                (
                    self.artifact_rollback._approved,
                    self.artifact_rollback._activation_history,
                    self.artifact_rollback.receipts,
                ) = rollback_state
            raise
        return {
            **payload,
            "save": saved,
            "governance": {
                "artifact_rollback_wired": self.artifact_rollback is not None,
                "active_artifact_id": (
                    self.artifact_rollback.current_artifact_id
                    if self.artifact_rollback is not None
                    else None
                ),
            },
        }

    def promotion_status(self, model_version_ids: list[str]) -> dict[str, Any]:
        """Return the latest human receipt relevant to a set of model versions."""

        requested = {str(value) for value in model_version_ids}
        for receipt in self.store.model_promotion_receipts(500):
            champion = str(receipt.get("champion_model_version_id") or "")
            challengers = set(str(value) for value in receipt.get("challenger_model_version_ids") or [])
            if champion in requested or challengers & requested:
                return {
                    "status": "human_promoted",
                    "deployment_mode": receipt.get("deployment_mode"),
                    "promotion_id": receipt.get("promotion_id"),
                    "champion_model_version_id": champion,
                    "challenger_model_version_ids": sorted(challengers),
                    "execution_authority": "none",
                }
        return {
            "status": "research_only_unpromoted",
            "deployment_mode": "none",
            "execution_authority": "none",
        }

    def search_experiments(
        self, *, symbol: str | None = None, framework: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return searchable, comparable experiment entities from the registry."""

        results: list[dict[str, Any]] = []
        for experiment in self.experiment_store.list(limit=500):
            if symbol and experiment.get("request_symbol") != symbol:
                continue
            versions = [self.store.research_model_version(value) for value in experiment.get("model_version_ids") or []]
            versions = [item for item in versions if item is not None]
            if framework and not any(item.get("framework") == framework for item in versions):
                continue
            results.append({
                "schema_version": "open_stock_ai.experiment_entity.v1",
                "experiment_id": experiment.get("experiment_id"),
                "kind": "model_training_and_evaluation",
                "symbol": experiment.get("request_symbol"),
                "market": experiment.get("request_market"),
                "horizon": experiment.get("request_horizon"),
                "created_at": experiment.get("created_at"),
                "model_versions": [
                    {"model_version_id": item.get("model_version_id"), "framework": item.get("framework"),
                     "metrics": item.get("metrics", {}).get("numeric", {})}
                    for item in versions
                ],
                "reproducibility": experiment.get("reproducibility", {}),
                "promotion": self.promotion_status(list(experiment.get("model_version_ids") or [])),
            })
            if len(results) >= max(1, min(limit, 500)):
                break
        return results

    def authorize_oos_inference(
        self,
        *,
        framework: str,
        model_version_id: str,
        evaluation_manifest_hash: str,
        evaluation_dataset_sha256: str,
    ) -> dict[str, Any]:
        """Authorize an OOS adapter to load one immutable, prior fitted model.

        The registry deliberately rejects an evaluation dataset that matches the
        training dataset.  A caller therefore cannot smuggle an in-sample fit
        through the inference path merely by attaching a plausible receipt.
        """

        version = self.store.research_model_version(model_version_id)
        if version is None:
            raise ValueError("oos_model_version_not_found")
        if version.get("framework") != framework:
            raise ValueError("oos_model_framework_mismatch")
        training_manifest_hash = self._required_value(version, "dataset_manifest_hash")
        training_dataset_hash = self._required_hash(version, "dataset_sha256")
        if not self._is_sha256(evaluation_dataset_sha256):
            raise ValueError("oos_evaluation_dataset_hash_invalid")
        if training_dataset_hash == evaluation_dataset_sha256:
            raise ValueError("oos_evaluation_dataset_matches_training_dataset")
        if training_manifest_hash == str(evaluation_manifest_hash or ""):
            raise ValueError("oos_evaluation_manifest_matches_training_manifest")

        artifact = version.get("artifact") if isinstance(version.get("artifact"), dict) else {}
        if framework == "finrl":
            if artifact.get("refit_allowed") is not False:
                raise ValueError("oos_finrl_artifact_is_not_immutable")
            if self._required_hash(artifact, "policy_sha256") != self._required_hash(version, "artifact_sha256"):
                raise ValueError("oos_finrl_artifact_hash_mismatch")
        elif framework == "qlib":
            artifacts = version.get("artifact_set") if isinstance(version.get("artifact_set"), list) else []
            if not artifacts or any(
                not isinstance(item, dict) or not self._is_sha256(str(item.get("sha256") or ""))
                for item in artifacts
            ):
                raise ValueError("oos_qlib_artifact_set_invalid")
        else:
            raise ValueError("oos_model_framework_unsupported")
        return version

    @staticmethod
    def _request_payload(request: StockRequest) -> dict[str, str]:
        return {
            "request_symbol": request.symbol,
            "request_market": request.market,
            "request_horizon": request.horizon,
        }

    def _finrl_version(
        self, finrl: dict[str, Any], requested_configuration: Any, manifest_hash: str, dataset_hash: str,
        runtime_hash: str, feature_contract: dict[str, Any], created_at: str,
    ) -> dict[str, Any]:
        artifact = finrl.get("immutable_artifact") if isinstance(finrl.get("immutable_artifact"), dict) else {}
        artifact_hash = self._required_hash(artifact, "policy_sha256")
        configuration = (
            dict(requested_configuration.get("finrl") or {})
            if isinstance(requested_configuration, dict)
            else {"artifact_name": artifact.get("artifact_name"), "refit_allowed": artifact.get("refit_allowed")}
        )
        config_hash = _canonical_sha256(configuration)
        metrics = self._metrics_from_receipts(finrl.get("training"), finrl.get("inference"))
        identity = {
            "framework": "finrl", "artifact_sha256": artifact_hash, "dataset_sha256": dataset_hash,
            "configuration_sha256": config_hash, "feature_contract_sha256": _canonical_sha256(feature_contract),
        }
        return {
            "schema_version": "open_stock_ai.research_model_version.v1",
            "model_version_id": _identifier("model-finrl", identity),
            "framework": "finrl",
            "artifact_sha256": artifact_hash,
            "dataset_manifest_hash": manifest_hash,
            "dataset_sha256": dataset_hash,
            "configuration_sha256": config_hash,
            "runtime_receipt_sha256": runtime_hash,
            "feature_contract": feature_contract,
            "metrics": metrics,
            "approval": self._research_only_approval(),
            "created_at": created_at,
            "artifact": artifact,
            "training_receipt": finrl.get("training"),
            "inference_receipt": finrl.get("inference"),
        }

    def _qlib_version(
        self, qlib: dict[str, Any], requested_configuration: Any, manifest_hash: str, dataset_hash: str,
        runtime_hash: str, feature_contract: dict[str, Any], created_at: str,
    ) -> dict[str, Any]:
        workflow = qlib.get("workflow") if isinstance(qlib.get("workflow"), dict) else {}
        config_hash = self._required_hash(qlib, "config_sha256")
        configuration = (
            dict(requested_configuration.get("qlib") or {})
            if isinstance(requested_configuration, dict)
            else {"config_sha256": config_hash}
        )
        artifacts = workflow.get("artifacts") if isinstance(workflow.get("artifacts"), list) else []
        if not artifacts or any(
            not isinstance(item, dict) or not self._is_sha256(str(item.get("sha256") or ""))
            for item in artifacts
        ):
            raise ValueError("qlib_artifact_receipt_required")
        artifact_hash = _canonical_sha256(
            sorted(
                [
                    {"path": item.get("path"), "sha256": item.get("sha256")}
                    for item in artifacts if isinstance(item, dict)
                ],
                key=lambda item: (str(item["path"]), str(item["sha256"])),
            )
        )
        metrics = self._metrics_from_receipts(workflow)
        identity = {
            "framework": "qlib", "artifact_sha256": artifact_hash, "dataset_sha256": dataset_hash,
            "configuration_sha256": config_hash, "feature_contract_sha256": _canonical_sha256(feature_contract),
        }
        return {
            "schema_version": "open_stock_ai.research_model_version.v1",
            "model_version_id": _identifier("model-qlib", identity),
            "framework": "qlib",
            "artifact_sha256": artifact_hash,
            "dataset_manifest_hash": manifest_hash,
            "dataset_sha256": dataset_hash,
            "configuration_sha256": config_hash,
            "runtime_receipt_sha256": runtime_hash,
            "feature_contract": feature_contract,
            "metrics": metrics,
            "approval": self._research_only_approval(),
            "created_at": created_at,
            "artifact_set": artifacts,
            "requested_configuration": configuration,
            "workflow_receipt": workflow,
        }

    @staticmethod
    def _reproducibility(runtime: dict[str, Any], models: list[dict[str, Any]]) -> dict[str, Any]:
        receipts = [
            runtime.get("finrl", {}).get("training"),
            runtime.get("finrl", {}).get("inference"),
            runtime.get("qlib", {}).get("workflow"),
        ]
        fingerprints = [
            receipt.get("runtime_fingerprint")
            for receipt in receipts
            if isinstance(receipt, dict) and isinstance(receipt.get("runtime_fingerprint"), dict)
        ]
        lock_hashes = {str(item.get("package_lock_sha256") or "") for item in fingerprints}
        hardware = [item.get("hardware") for item in fingerprints if isinstance(item.get("hardware"), dict)]
        environment_captured = (
            len(fingerprints) == 3 and len(lock_hashes) == 1 and bool(lock_hashes - {""})
            and len(hardware) == 3
        )
        requested = runtime.get("reproducibility") if isinstance(runtime.get("reproducibility"), dict) else {}
        random_seeds = requested.get("random_seeds") if isinstance(requested.get("random_seeds"), dict) else {}
        seed_captured = all(isinstance(random_seeds.get(name), int) for name in ("finrl", "qlib"))
        metric_reference = {
            str(model.get("framework")): dict(model.get("metrics") or {}).get("numeric", {})
            for model in models
        }
        blockers: list[str] = []
        if not environment_captured:
            blockers.append("runtime_package_lock_or_hardware_not_captured")
        if not seed_captured:
            blockers.append("random_seed_not_captured")
        if not blockers:
            blockers.append("framework_determinism_not_attested")
        # A lock makes the worker environment restorable. It does not prove
        # framework-level deterministic kernels, so bitwise remains false.
        return {
            "level": "environment_captured" if environment_captured else "receipt_backed",
            "point_in_time_input_verified": runtime.get("dataset", {}).get("point_in_time_verified") is True,
            "model_artifact_hashes_verified": all(bool(item.get("artifact_sha256")) for item in models),
            "runtime_package_lock_sha256": next(iter(lock_hashes)) if environment_captured else None,
            "runtime_hardware": hardware if environment_captured else [],
            "random_seeds": random_seeds if seed_captured else {},
            "metric_tolerance_replay": {
                "schema_version": "open_stock_ai.research_metric_tolerance_contract.v1",
                "mode": "metric_tolerance",
                "reference_metrics": metric_reference,
                "tolerance": {"absolute": 1e-9, "relative": 1e-6},
                "artifact_sha256": {
                    str(model.get("framework")): str(model.get("artifact_sha256")) for model in models
                },
            },
            "bitwise_reproducible": False,
            "blockers": blockers,
        }

    @classmethod
    def _feature_contract(cls, dataset: dict[str, Any]) -> dict[str, Any]:
        contract = dataset.get("feature_contract") if isinstance(dataset.get("feature_contract"), dict) else {}
        lineage_hash = cls._required_hash(contract, "feature_lineage_sha256")
        code_hashes = contract.get("feature_code_sha256")
        schemas = contract.get("feature_schema_versions")
        if (
            not isinstance(code_hashes, list) or not code_hashes
            or any(not cls._is_sha256(str(value)) for value in code_hashes)
            or not isinstance(schemas, dict) or not schemas
        ):
            raise ValueError("research_registry_feature_contract_invalid")
        return {
            "schema_version": str(contract.get("schema_version") or ""),
            "feature_schema_versions": {
                str(domain): sorted(str(version) for version in versions)
                for domain, versions in sorted(schemas.items()) if isinstance(versions, list)
            },
            "feature_lineage_sha256": lineage_hash,
            "feature_code_sha256": sorted(str(value) for value in code_hashes),
        }

    @staticmethod
    def _research_only_approval() -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.model_approval.v1",
            "status": "research_only_not_human_approved",
            "production_eligible": False,
            "execution_authority": "none",
            "promotion_boundary": "requires_human_model_promotion_receipt",
        }

    @staticmethod
    def _metrics_from_receipts(*receipts: Any) -> dict[str, Any]:
        numeric: dict[str, float] = {}
        for index, receipt in enumerate(receipts):
            if not isinstance(receipt, dict):
                continue
            result = receipt.get("result") if isinstance(receipt.get("result"), dict) else {}
            for key, value in ResearchModelRegistry._numeric_values(result).items():
                numeric[f"receipt_{index}.{key}"] = value
        return {
            "schema_version": "open_stock_ai.model_metrics_receipt.v1",
            "numeric": numeric,
            "status": "reported" if numeric else "runtime_reported_no_numeric_metrics",
        }

    @staticmethod
    def _numeric_values(value: Any, prefix: str = "") -> dict[str, float]:
        if isinstance(value, Real) and not isinstance(value, bool):
            number = float(value)
            return {prefix or "value": number} if math.isfinite(number) else {}
        if not isinstance(value, dict):
            return {}
        result: dict[str, float] = {}
        for key, item in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            result.update(ResearchModelRegistry._numeric_values(item, child_prefix))
        return result

    @staticmethod
    def _required_hash(payload: dict[str, Any], key: str) -> str:
        value = str(payload.get(key) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
            raise ValueError(f"research_registry_invalid_sha256:{key}")
        return value

    @staticmethod
    def _required_value(payload: dict[str, Any], key: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise ValueError(f"research_registry_missing:{key}")
        return value

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())
