from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from open_stock_ai.config.settings import OpenStockAISettings


@dataclass
class RuntimeConnectorGovernance:
    def build(self, *, settings: OpenStockAISettings, projects: dict[str, Any]) -> dict[str, Any]:
        entries = [
            self._entry(key=key, project=projects.get(key) if isinstance(projects.get(key), dict) else {}, settings=settings)
            for key in [
                "tradingagents",
                "finrobot",
                "fingpt",
                "finrl_trading",
                "finrl",
                "qlib",
                "ai_trader",
            ]
        ]
        blocked_count = sum(1 for item in entries if item["connector_status"].startswith("blocked"))
        manual_review_count = sum(1 for item in entries if item["manual_review_required"])
        return {
            "schema_version": "open_stock_ai.runtime_connector_governance.v1",
            "method": "local_external_runtime_connector_policy",
            "global_connector_enabled": settings.external_runtime_connectors_enabled,
            "mode": settings.mode,
            "live_trading_enabled": settings.live_trading_enabled,
            "paper_boundary_enforced": settings.mode == "paper" and not settings.live_trading_enabled,
            "connector_count": len(entries),
            "blocked_count": blocked_count,
            "manual_review_count": manual_review_count,
            "remote_order_submission_allowed": False,
            "risk_engine_required": True,
            "paper_executor_required": True,
            "entries": entries,
            "available": len(entries) == 7
            and blocked_count == len(entries)
            and settings.mode == "paper"
            and not settings.live_trading_enabled
            and not settings.external_runtime_connectors_enabled,
            "execution_boundary": "runtime_connectors_disabled_until_governed",
        }

    def _entry(self, *, key: str, project: dict[str, Any], settings: OpenStockAISettings) -> dict[str, Any]:
        license_status = project.get("license_status") or "missing_license_file_review_required"
        origin_verified = project.get("origin_verified") is True
        lock_verified = project.get("lock_verified") is True
        manual_review = license_status != "license_file_detected"
        runtime_role = self._runtime_role(key)
        blockers = []
        if not origin_verified:
            blockers.append("origin_not_verified")
        if not lock_verified:
            blockers.append("source_lock_not_verified")
        if manual_review:
            blockers.append("license_manual_review_required")
        if not settings.external_runtime_connectors_enabled:
            blockers.append("global_runtime_connectors_disabled")
        if settings.mode != "paper" or settings.live_trading_enabled:
            blockers.append("paper_boundary_not_enforced")
        if runtime_role["can_submit_remote_orders"]:
            blockers.append("remote_order_submission_must_remain_disabled")
        connector_status = "blocked_by_policy" if blockers else "eligible_for_future_connector_design"
        if manual_review:
            connector_status = "blocked_missing_license_review"
        return {
            "key": key,
            "display_name": project.get("display_name") or key,
            "origin_verified": origin_verified,
            "lock_verified": lock_verified,
            "license_status": license_status,
            "manual_review_required": manual_review,
            "dependency_manifest_count": project.get("dependency_manifest_count", 0),
            "runtime_role": runtime_role,
            "requested": settings.external_runtime_connectors_enabled,
            "connector_status": connector_status,
            "blockers": blockers,
            "allowed_boundary": "read_only_adapter_contract",
            "risk_engine_required": True,
            "paper_executor_required": True,
            "live_order_submission_allowed": False,
        }

    def _runtime_role(self, key: str) -> dict[str, Any]:
        roles = {
            "tradingagents": {
                "category": "multi_agent_reasoning",
                "can_submit_remote_orders": False,
                "primary_risk": "llm_graph_runtime_and_stateful_agent_side_effects",
            },
            "finrobot": {
                "category": "report_generation",
                "can_submit_remote_orders": False,
                "primary_risk": "external_data_provider_and_report_runtime_dependencies",
            },
            "fingpt": {
                "category": "model_inference_or_training",
                "can_submit_remote_orders": False,
                "primary_risk": "model_runtime_dependency_and_gpu_cost",
            },
            "finrl_trading": {
                "category": "backtest_or_paper_trading_runtime",
                "can_submit_remote_orders": True,
                "primary_risk": "strategy_runtime_must_not_bypass_open_stock_ai_risk",
            },
            "finrl": {
                "category": "rl_training_and_trading_runtime",
                "can_submit_remote_orders": True,
                "primary_risk": "rl_policy_must_not_bypass_open_stock_ai_risk",
            },
            "qlib": {
                "category": "workflow_and_factor_runtime",
                "can_submit_remote_orders": False,
                "primary_risk": "dataset_workflow_runtime_dependency",
            },
            "ai_trader": {
                "category": "agent_signal_publishing",
                "can_submit_remote_orders": True,
                "primary_risk": "remote_signal_publish_must_not_be_treated_as_execution",
            },
        }
        return roles.get(
            key,
            {
                "category": "unknown",
                "can_submit_remote_orders": False,
                "primary_risk": "unknown_runtime_boundary",
            },
        )
