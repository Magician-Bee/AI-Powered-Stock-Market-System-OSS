from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.strategy.strategy_registry import StrategyArtifactRegistry

from .evaluation import PolicyEvaluation
from .policy_proposal import PolicyProposalManager
from open_stock_ai.governance.artifact_rollback import ApprovedArtifactRollbackRegistry


class PromotionGate:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        artifact_rollback: ApprovedArtifactRollbackRegistry | None = None,
    ) -> None:
        self.store = store
        self.proposals = PolicyProposalManager(store)
        self.artifact_rollback = artifact_rollback
        if artifact_rollback is not None and artifact_rollback.store is not None:
            rollback_path = Path(artifact_rollback.store.path).resolve()
            store_path = Path(store.path).resolve()
            if rollback_path != store_path:
                raise ValueError("promotion_governance_must_share_runtime_database")

    def evaluate(self, proposal_id: str, evidence: dict[str, Any], risk_review: dict[str, Any]) -> dict[str, Any]:
        proposal = self._require(proposal_id, {"proposed", "rejected"})
        result = PolicyEvaluation().evaluate(evidence, risk_review=risk_review)
        status = "evaluated" if result["passed"] else "rejected"
        self._update(
            proposal_id,
            status=status,
            evaluation_json=result,
            strategy_version_hash=result.get("strategy_version_hash"),
            data_version_hash=result.get("data_version_hash"),
        )
        return {"proposal": self.proposals.get(proposal_id), "evaluation": result}

    def begin_shadow(self, proposal_id: str) -> dict[str, Any]:
        self._require(proposal_id, {"evaluated"})
        shadow = {"started_at": _now(), "sample_size": 0, "passed": False, "risk_violations": 0}
        self._update(proposal_id, status="shadow", shadow_json=shadow)
        return self.proposals.get(proposal_id) or {}

    def record_shadow(self, proposal_id: str, *, sample_size: int, return_pct: float, max_drawdown_pct: float, risk_violations: int) -> dict[str, Any]:
        self._require(proposal_id, {"shadow"})
        shadow = {
            "updated_at": _now(),
            "sample_size": int(sample_size),
            "return_pct": float(return_pct),
            "max_drawdown_pct": float(max_drawdown_pct),
            "risk_violations": int(risk_violations),
            "passed": int(sample_size) >= 20 and float(max_drawdown_pct) <= 15.0 and int(risk_violations) == 0,
        }
        self._update(proposal_id, shadow_json=shadow)
        return self.proposals.get(proposal_id) or {}

    def promote(self, proposal_id: str, *, approved_by: str) -> dict[str, Any]:
        proposal = self._require(proposal_id, {"shadow"})
        actor = str(approved_by or "").strip()
        if not actor or actor.casefold() in {"agent", "codex", "model", "ai"}:
            raise PermissionError("human_approval_required_for_policy_promotion")
        if (proposal.get("shadow") or {}).get("passed") is not True:
            raise PermissionError("shadow_paper_gate_not_passed")
        now = _now()
        version_id = f"SV-{uuid4().hex}"
        artifact = StrategyArtifactRegistry().approved_rule_artifact(
            proposal=proposal,
            version_id=version_id,
            approved_by=actor,
            approved_at=now,
        )
        rollback_state = None
        if self.artifact_rollback is not None:
            rollback_state = (
                dict(self.artifact_rollback._approved),
                list(self.artifact_rollback._activation_history),
                list(self.artifact_rollback.receipts),
            )
        try:
            with self.store._connect() as conn:
                conn.execute("begin immediate")
                conn.execute(
                    """
                    insert into strategy_versions (
                        version_id, proposal_id, created_at, status, strategy_hash,
                        data_hash, rules_json, approval_json
                    ) values (?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        version_id, proposal_id, now, proposal["strategy_version_hash"],
                        proposal["data_version_hash"], json.dumps(proposal["rules"], ensure_ascii=False),
                        json.dumps({"approved_by": actor, "approved_at": now}, ensure_ascii=False),
                    ),
                )
                artifact_save = self.store.save_strategy_artifact(artifact, connection=conn)
                if self.artifact_rollback is not None:
                    self.artifact_rollback.approve(
                        artifact["artifact_id"],
                        artifact["configuration_sha256"],
                        approved_by=actor,
                        approved_at=now,
                        metadata={
                            "artifact_scope": "strategy",
                            "strategy_id": artifact["strategy_id"],
                            "version_id": version_id,
                            "approval_status": artifact["approval_status"],
                        },
                        connection=conn,
                    )
                    self.artifact_rollback.activate(artifact["artifact_id"], connection=conn)
                conn.execute(
                    "update policy_proposals set status='promoted', approved_by=?, promoted_at=?, updated_at=? where proposal_id=?",
                    (actor, now, now, proposal_id),
                )
                conn.commit()
        except Exception:
            if rollback_state is not None and self.artifact_rollback is not None:
                (
                    self.artifact_rollback._approved,
                    self.artifact_rollback._activation_history,
                    self.artifact_rollback.receipts,
                ) = rollback_state
            raise
        return {
            "proposal": self.proposals.get(proposal_id),
            "version_id": version_id,
            "active": True,
            "strategy_artifact": {**artifact, "registry": artifact_save},
            "governance": {
                "artifact_rollback_wired": self.artifact_rollback is not None,
                "active_artifact_id": (
                    self.artifact_rollback.current_artifact_id
                    if self.artifact_rollback is not None
                    else None
                ),
            },
        }

    def _require(self, proposal_id: str, allowed: set[str]) -> dict[str, Any]:
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise ValueError("policy_proposal_not_found")
        if proposal["status"] not in allowed:
            raise ValueError(f"invalid_policy_proposal_status:{proposal['status']}")
        return proposal

    def _update(self, proposal_id: str, *, status: str | None = None, **values: Any) -> None:
        assignments = ["updated_at = ?"]
        params: list[Any] = [_now()]
        if status:
            assignments.append("status = ?")
            params.append(status)
        for key, value in values.items():
            assignments.append(f"{key} = ?")
            params.append(json.dumps(value, ensure_ascii=False, default=str) if key.endswith("_json") else value)
        params.append(proposal_id)
        with self.store._connect() as conn:
            conn.execute(f"update policy_proposals set {', '.join(assignments)} where proposal_id = ?", params)
            conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
