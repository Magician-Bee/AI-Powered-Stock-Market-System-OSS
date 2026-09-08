from __future__ import annotations

from dataclasses import dataclass
from os import getenv
from typing import Any

from open_stock_ai.config.settings import OpenStockAISettings


@dataclass
class BrokerAccountImportGovernance:
    def build(self, *, settings: OpenStockAISettings, projects: dict[str, Any]) -> dict[str, Any]:
        entries = [
            self._entry(key=key, project=projects.get(key) if isinstance(projects.get(key), dict) else {}, settings=settings)
            for key in ["finrl_trading", "finrl", "ai_trader"]
        ]
        credential_env_names = [
            "FUGLE_TRADE_API_KEY",
            "FUBON_ACCOUNT_ID",
            "FUBON_CERT_PATH",
            "YUSHAN_BROKER_API_KEY",
            "AI_TRADER_API_KEY",
        ]
        credential_env_present = [
            name for name in credential_env_names if bool((getenv(name) or "").strip())
        ]
        blocked_count = sum(1 for item in entries if item["import_status"].startswith("blocked"))
        return {
            "schema_version": "open_stock_ai.broker_account_import_governance.v1",
            "method": "local_broker_account_import_policy",
            "broker_account_imports_enabled": settings.broker_account_imports_enabled,
            "external_credentials_enabled": settings.external_credentials_enabled,
            "mode": settings.mode,
            "live_trading_enabled": settings.live_trading_enabled,
            "paper_ledger_only": settings.mode == "paper" and not settings.live_trading_enabled,
            "credential_env_checked": credential_env_names,
            "credential_env_present": credential_env_present,
            "credential_count": len(credential_env_present),
            "connector_count": len(entries),
            "blocked_count": blocked_count,
            "remote_broker_mutation_allowed": False,
            "paper_outcome_import_allowed": False,
            "decision_log_replay_allowed": True,
            "execution_boundary": "broker_account_imports_disabled_until_credential_governance",
            "entries": entries,
            "available": (
                len(entries) == 3
                and blocked_count == len(entries)
                and not settings.broker_account_imports_enabled
                and not settings.external_credentials_enabled
                and settings.mode == "paper"
                and not settings.live_trading_enabled
                and not credential_env_present
            ),
        }

    def _entry(self, *, key: str, project: dict[str, Any], settings: OpenStockAISettings) -> dict[str, Any]:
        role = self._role(key)
        origin_verified = project.get("origin_verified") is True
        lock_verified = project.get("lock_verified") is True
        blockers = []
        if not origin_verified:
            blockers.append("origin_not_verified")
        if not lock_verified:
            blockers.append("source_lock_not_verified")
        if not settings.broker_account_imports_enabled:
            blockers.append("broker_account_imports_disabled")
        if not settings.external_credentials_enabled:
            blockers.append("external_credentials_disabled")
        if settings.mode != "paper" or settings.live_trading_enabled:
            blockers.append("paper_boundary_not_enforced")
        if role["can_import_realized_orders"] or role["can_mutate_remote_broker"]:
            blockers.append("real_broker_or_account_path_requires_explicit_governance")
        return {
            "key": key,
            "display_name": project.get("display_name") or key,
            "origin_verified": origin_verified,
            "lock_verified": lock_verified,
            "import_role": role,
            "requested": settings.broker_account_imports_enabled,
            "credential_access_requested": settings.external_credentials_enabled,
            "import_status": "blocked_by_policy" if blockers else "eligible_for_future_import_design",
            "blockers": blockers,
            "allowed_boundary": "paper_ledger_replay_only",
            "remote_broker_mutation_allowed": False,
            "paper_outcome_import_allowed": False,
            "decision_log_replay_allowed": True,
        }

    def _role(self, key: str) -> dict[str, Any]:
        roles = {
            "finrl_trading": {
                "category": "paper_trading_and_strategy_runtime",
                "can_import_realized_orders": True,
                "can_mutate_remote_broker": True,
                "primary_risk": "external_strategy_runtime_must_not_write_orders_or_account_state",
            },
            "finrl": {
                "category": "rl_trading_runtime",
                "can_import_realized_orders": True,
                "can_mutate_remote_broker": True,
                "primary_risk": "rl_policy_outputs_must_not_be_treated_as_broker_account_truth",
            },
            "ai_trader": {
                "category": "agent_signal_and_copytrade_platform",
                "can_import_realized_orders": True,
                "can_mutate_remote_broker": True,
                "primary_risk": "remote_agent_or_copytrade_actions_must_not_mutate_open_stock_ai_ledgers",
            },
        }
        return roles.get(
            key,
            {
                "category": "unknown",
                "can_import_realized_orders": False,
                "can_mutate_remote_broker": False,
                "primary_risk": "unknown_import_boundary",
            },
        )
