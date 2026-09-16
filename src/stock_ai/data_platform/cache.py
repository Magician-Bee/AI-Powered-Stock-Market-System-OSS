from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any
from uuid import uuid4

from .contracts import CachePolicy, normalize_timestamp, utc_now
from .warehouse import MarketDataWarehouse


class CachePolicyService:
    """Evaluate dataset freshness and coordinate one upstream refresh."""

    def __init__(
        self,
        warehouse: MarketDataWarehouse,
        *,
        policies: dict[str, CachePolicy],
        source_update_frequencies: dict[str, int],
    ) -> None:
        self.warehouse = warehouse
        self.policies = policies
        self.source_update_frequencies = source_update_frequencies

    def policy_for(self, dataset: str, *, source_id: str | None = None) -> CachePolicy:
        policy = self.policies.get(dataset)
        if policy is not None:
            return policy
        return CachePolicy(
            dataset=dataset,
            ttl_seconds=(
                self.source_update_frequencies.get(str(source_id), 0)
                if source_id
                else 0
            ),
            stale_while_revalidate_seconds=0,
            refresh_lease_seconds=120,
            serve_stale_on_error=False,
            invalidate_on=[],
        )

    def decision(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str = "all",
        as_of: str | None = None,
        force: bool = False,
        record: bool = True,
    ) -> dict[str, Any]:
        current = normalize_timestamp(as_of or utc_now(), required=True)
        assert current is not None
        policy = self.policy_for(dataset, source_id=source_id)
        policy_payload = policy.model_dump(mode="json")
        entry = self.warehouse.cache_entry(
            source_id=source_id,
            dataset=dataset,
            partition_key=partition_key,
        )
        if entry is None:
            checkpoint = self.warehouse.get_checkpoint(
                source_id=source_id,
                dataset=dataset,
                partition_key=partition_key,
            )
            checkpoint_refreshed = (
                str(checkpoint["last_success_at"])
                if checkpoint and checkpoint.get("status") == "succeeded"
                and checkpoint.get("last_success_at")
                else None
            )
            self.warehouse.ensure_cache_entry(
                source_id=source_id,
                dataset=dataset,
                partition_key=partition_key,
                policy=policy_payload,
                refreshed_at=checkpoint_refreshed,
            )
            entry = self.warehouse.cache_entry(
                source_id=source_id,
                dataset=dataset,
                partition_key=partition_key,
            )
        assert entry is not None
        state = "forced" if force else self._classify(entry, current)
        if record:
            self.warehouse.record_cache_decision(
                source_id=source_id,
                dataset=dataset,
                partition_key=partition_key,
                decision=state,
                decided_at=current,
            )
            entry = self.warehouse.cache_entry(
                source_id=source_id,
                dataset=dataset,
                partition_key=partition_key,
            )
            assert entry is not None
        return {
            "schema_version": "stock_ai.cache_decision.v1",
            "source_id": source_id,
            "dataset": dataset,
            "partition_key": partition_key,
            "as_of": current,
            "state": state,
            "refresh_required": state != "fresh",
            "serve_stale": (
                state == "stale_while_revalidate"
                and bool(policy.serve_stale_on_error)
            ),
            "policy": policy_payload,
            "entry": entry,
        }

    def acquire_refresh(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
        as_of: str,
        owner_id: str | None = None,
    ) -> dict[str, Any] | None:
        policy = self.policy_for(dataset, source_id=source_id)
        lease_id = f"DCL-{uuid4().hex}"
        acquired = self.warehouse.acquire_cache_refresh_lease(
            source_id=source_id,
            dataset=dataset,
            partition_key=partition_key,
            lease_id=lease_id,
            owner_id=owner_id or f"loader-{uuid4().hex}",
            acquired_at=as_of,
            lease_seconds=policy.refresh_lease_seconds,
        )
        if not acquired:
            return None
        return {
            "lease_id": lease_id,
            "source_id": source_id,
            "dataset": dataset,
            "partition_key": partition_key,
            "acquired_at": as_of,
            "lease_seconds": policy.refresh_lease_seconds,
        }

    def release_refresh(self, lease_id: str) -> None:
        self.warehouse.release_cache_refresh_lease(lease_id)

    def mark_refreshed(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
        refreshed_at: str,
    ) -> dict[str, Any]:
        policy = self.policy_for(dataset, source_id=source_id)
        return self.warehouse.mark_cache_refreshed(
            source_id=source_id,
            dataset=dataset,
            partition_key=partition_key,
            policy=policy.model_dump(mode="json"),
            refreshed_at=refreshed_at,
        )

    def invalidate(
        self,
        *,
        dataset: str,
        reason: str,
        source_id: str | None = None,
        partition_key: str | None = None,
        invalidated_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        policy = self.policy_for(dataset, source_id=source_id)
        allowed_reasons = {"manual_refresh", *policy.invalidate_on}
        if reason not in allowed_reasons:
            raise ValueError(
                f"Invalid cache invalidation reason for {dataset}: {reason}; "
                f"expected {sorted(allowed_reasons)}"
            )
        current = normalize_timestamp(invalidated_at or utc_now(), required=True)
        assert current is not None
        entries = self.warehouse.cache_entries(dataset=dataset, limit=10000)
        selected = [
            entry
            for entry in entries
            if (source_id is None or entry["source_id"] == source_id)
            and (partition_key is None or entry["partition_key"] == partition_key)
        ]
        invalidated = [
            item
            for item in (
                self.warehouse.invalidate_cache_entry(
                    source_id=str(entry["source_id"]),
                    dataset=dataset,
                    partition_key=str(entry["partition_key"]),
                    reason=reason,
                    invalidated_at=current,
                    metadata=metadata,
                )
                for entry in selected
            )
            if item is not None
        ]
        return {
            "schema_version": "stock_ai.cache_invalidation.v1",
            "dataset": dataset,
            "source_id": source_id,
            "partition_key": partition_key,
            "reason": reason,
            "invalidated_at": current,
            "count": len(invalidated),
            "entries": invalidated,
        }

    def status(
        self,
        *,
        dataset: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        current = normalize_timestamp(as_of or utc_now(), required=True)
        assert current is not None
        entries = self.warehouse.cache_entries(dataset=dataset, limit=10000)
        items = [
            {
                **entry,
                "state": self._classify(entry, current),
            }
            for entry in entries
        ]
        state_counts = Counter(item["state"] for item in items)
        invalidations = self.warehouse.cache_invalidations(
            dataset=dataset,
            limit=100,
        )
        leases = [
            lease
            for lease in self.warehouse.cache_refresh_leases()
            if dataset is None or lease["dataset"] == dataset
        ]
        return {
            "schema_version": "stock_ai.cache_policy_status.v1",
            "as_of": current,
            "policy_count": (
                1 if dataset and dataset in self.policies
                else len(self.policies) if dataset is None
                else 0
            ),
            "entry_count": len(items),
            "state_counts": dict(state_counts),
            "active_refresh_count": sum(
                1 for lease in leases if str(lease["expires_at"]) > current
            ),
            "invalidation_count": len(invalidations),
            "policies": [
                policy.model_dump(mode="json")
                for key, policy in sorted(self.policies.items())
                if dataset is None or key == dataset
            ],
            "items": items,
            "invalidations": invalidations,
            "leases": leases,
            "status": (
                "passed"
                if not state_counts["expired"] and not state_counts["invalidated"]
                else "attention"
            ),
        }

    @staticmethod
    def _classify(entry: dict[str, Any], as_of: str) -> str:
        refreshed_at = entry.get("refreshed_at")
        if not refreshed_at or str(refreshed_at) > as_of:
            return "empty"
        invalidated_at = entry.get("invalidated_at")
        if (
            invalidated_at
            and str(invalidated_at) <= as_of
            and str(invalidated_at) >= str(refreshed_at)
        ):
            return "invalidated"
        if entry.get("fresh_until") and as_of <= str(entry["fresh_until"]):
            return "fresh"
        if entry.get("stale_until") and as_of <= str(entry["stale_until"]):
            return "stale_while_revalidate"
        return "expired"
