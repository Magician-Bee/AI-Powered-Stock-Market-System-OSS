from __future__ import annotations

"""Content-addressed provenance for rules and promoted strategy artifacts."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from open_stock_ai.storage.sqlite_store import SQLiteStore


_STRATEGY_ID = "open_stock_ai.strategy_v2"
_RULE_CONFIGURATION = {
    "sentiment_weight": 0.35,
    "forecast_weight": 0.10,
    "fundamental_weight": 0.25,
    "technical_weight": 0.20,
    "buy_threshold": 0.35,
    "sell_threshold": -0.35,
}


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class StrategyArtifactRegistry:
    """Make the bundled rule engine explicitly a baseline, never a promoted model."""

    store: SQLiteStore | None = None

    def baseline_artifact(self) -> dict[str, Any]:
        source_path = Path(__file__).with_name("strategy_engine.py")
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        configuration_hash = _sha256(_RULE_CONFIGURATION)
        artifact_id = f"strategy-artifact-{_sha256([_STRATEGY_ID, source_hash, configuration_hash])[:32]}"
        payload = {
            "schema_version": "open_stock_ai.strategy_artifact.v1",
            "artifact_id": artifact_id,
            "strategy_id": _STRATEGY_ID,
            "artifact_kind": "baseline_rule",
            "source_sha256": source_hash,
            "configuration_sha256": configuration_hash,
            "approval_status": "baseline_only",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rules": _RULE_CONFIGURATION,
            "production_eligible": False,
            "execution_evidence_eligible": False,
            "promotion_boundary": "requires_exact_evaluation_shadow_and_human_approval",
        }
        if self.store is not None:
            saved = self.store.save_strategy_artifact(payload)
            payload["registry"] = saved
        else:
            payload["registry"] = {"saved": False, "reason": "registry_not_configured"}
        return payload

    def approved_rule_artifact(
        self,
        *,
        proposal: dict[str, Any],
        version_id: str,
        approved_by: str,
        approved_at: str,
    ) -> dict[str, Any]:
        """Build the immutable receipt for a human-approved paper strategy.

        Promotion persists this payload in the same database transaction as its
        ``strategy_versions`` row.  A policy cannot become active while its
        approval evidence is absent.
        """

        proposal_id = str(proposal.get("proposal_id") or "").strip()
        strategy_hash = str(proposal.get("strategy_version_hash") or "").strip().lower()
        data_hash = str(proposal.get("data_version_hash") or "").strip().lower()
        rules = proposal.get("rules") if isinstance(proposal.get("rules"), list) else []
        if not proposal_id or not version_id or not approved_by or not approved_at:
            raise ValueError("approved_strategy_artifact_identity_required")
        if not self._is_sha256(strategy_hash) or not self._is_sha256(data_hash):
            raise ValueError("approved_strategy_artifact_hash_required")
        if not rules or any(not isinstance(rule, str) or not rule.strip() for rule in rules):
            raise ValueError("approved_strategy_artifact_rules_required")
        strategy_id = f"open_stock_ai.policy_proposal.{proposal_id}"
        configuration = {
            "rules": [rule.strip() for rule in rules],
            "data_sha256": data_hash,
            "evaluation": proposal.get("evaluation") or {},
            "shadow": proposal.get("shadow") or {},
        }
        configuration_hash = _sha256(configuration)
        artifact_id = f"strategy-artifact-{_sha256([strategy_id, strategy_hash, configuration_hash])[:32]}"
        return {
            "schema_version": "open_stock_ai.strategy_artifact.v1",
            "artifact_id": artifact_id,
            "strategy_id": strategy_id,
            "artifact_kind": "approved_rule",
            "source_sha256": strategy_hash,
            "configuration_sha256": configuration_hash,
            "approval_status": "human_approved_paper",
            "created_at": approved_at,
            "rules": configuration["rules"],
            "production_eligible": True,
            "execution_evidence_eligible": False,
            "promotion_boundary": "paper_only_no_live_brokerage",
            "approval_receipt": {
                "proposal_id": proposal_id,
                "version_id": version_id,
                "approved_by": approved_by,
                "approved_at": approved_at,
                "strategy_sha256": strategy_hash,
                "data_sha256": data_hash,
            },
            "evaluation": configuration["evaluation"],
            "shadow": configuration["shadow"],
        }

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
