"""Safe runtime projection for durable deterministic chaos-recovery evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from open_stock_ai.governance.chaos_recovery import (
    CHAOS_SCENARIOS,
    ChaosRecoveryCatalog,
    ChaosRecoveryReceipt,
    DurableChaosRecoveryStore,
)


REQUIRED_RECOVERY_INVARIANTS = (
    "durable_state_recovered",
    "new_orders_blocked_until_safe",
    "operator_receipt_written",
)


class ChaosRecoveryRuntime:
    """Persist and project safe chaos evidence without injecting live faults.

    The desktop runtime has no endpoint that can execute process-kill, network
    partition, disk-full, or timeout injection.  A controlled test/staging
    harness may pass already-observed recovery evidence to ``record_catalog``;
    the Host then validates, persists, and exposes it as an immutable read-only
    projection for the Agent Dock and operational-alert evaluator.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.store = DurableChaosRecoveryStore(str(self.database_path))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def record_catalog(
        self,
        recoveries: Mapping[str, Mapping[str, Any]],
        *,
        seed: int,
        campaign_id: str,
    ) -> tuple[ChaosRecoveryReceipt, ...]:
        """Record a controlled deterministic catalog; never inject a fault itself."""
        normalized_id = str(campaign_id).strip()
        if not normalized_id:
            raise ValueError("chaos campaign id is required")
        catalog = ChaosRecoveryCatalog(seed=seed, clock=lambda: self._now())
        receipts = catalog.run_catalog(
            {
                scenario: {
                    **(
                        dict(recoveries.get(scenario) or {})
                        if isinstance(recoveries.get(scenario), Mapping)
                        else {}
                    ),
                    "campaign_id": normalized_id,
                    "execution_mode": "controlled_deterministic_evidence",
                }
                for scenario in CHAOS_SCENARIOS
            }
        )
        for receipt in receipts:
            self.store.record(receipt, recorded_at=self._now())
        return receipts

    def dashboard(self) -> dict[str, Any]:
        receipts = self.store.receipts(limit=200)
        campaigns = _campaigns(receipts)
        latest = max(campaigns.values(), key=_campaign_sort_key) if campaigns else None
        if latest is None:
            return {
                "schema_version": "open_stock_ai.chaos_recovery_dashboard.v1",
                "status": "not_configured",
                "triggered": False,
                "campaign_id": None,
                "scenario_count": 0,
                "reason": "no_durable_chaos_campaign",
                "required_invariants": list(REQUIRED_RECOVERY_INVARIANTS),
                "receipts": [],
            }
        complete_scenarios = set(latest["scenarios"]) == set(CHAOS_SCENARIOS)
        invariant_failures = sorted(
            {
                invariant
                for receipt in latest["receipts"]
                for invariant in REQUIRED_RECOVERY_INVARIANTS
                if receipt.invariants.get(invariant) is not True
            }
        )
        invalid = [receipt.scenario for receipt in latest["receipts"] if not receipt.verify()]
        recovered = (
            complete_scenarios
            and not invariant_failures
            and not invalid
            and all(receipt.status == "recovered" for receipt in latest["receipts"])
        )
        reasons: list[str] = []
        if not complete_scenarios:
            reasons.append("catalog_incomplete")
        if invariant_failures:
            reasons.extend(f"missing_invariant:{item}" for item in invariant_failures)
        if invalid:
            reasons.extend(f"invalid_receipt:{item}" for item in invalid)
        if any(receipt.status != "recovered" for receipt in latest["receipts"]):
            reasons.append("recovery_blocked")
        return {
            "schema_version": "open_stock_ai.chaos_recovery_dashboard.v1",
            "status": "recovered" if recovered else "blocked",
            "triggered": not recovered,
            "campaign_id": latest["campaign_id"],
            "scenario_count": len(latest["scenarios"]),
            "reason": None if recovered else ", ".join(reasons),
            "required_invariants": list(REQUIRED_RECOVERY_INVARIANTS),
            "receipts": [receipt.as_dict() for receipt in latest["receipts"]],
        }

    def _now(self) -> str:
        current = self.clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()


def _campaigns(receipts: list[ChaosRecoveryReceipt]) -> dict[str, dict[str, Any]]:
    campaigns: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        campaign_id = str(receipt.recovery.get("campaign_id") or f"legacy-seed-{receipt.seed}")
        campaign = campaigns.setdefault(
            campaign_id,
            {"campaign_id": campaign_id, "scenarios": set(), "receipts": [], "observed_at": ""},
        )
        campaign["scenarios"].add(receipt.scenario)
        campaign["receipts"].append(receipt)
        campaign["observed_at"] = max(campaign["observed_at"], str(receipt.recovery.get("observed_at") or ""))
    for campaign in campaigns.values():
        campaign["receipts"].sort(key=lambda receipt: receipt.scenario)
    return campaigns


def _campaign_sort_key(campaign: Mapping[str, Any]) -> tuple[str, str]:
    return (str(campaign.get("observed_at") or ""), str(campaign.get("campaign_id") or ""))
