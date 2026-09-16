from __future__ import annotations

"""Authoritative availability rules for point-in-time research data.

Temporal columns say when this installation obtained a value.  That alone is
not evidence that an earlier strategy could have known it.  This registry
separates source-provided timestamps, reviewed exchange publication schedules,
and acquisition-only archives.  Unknown combinations fail closed for exact
historical research.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml

from .contracts import normalize_timestamp


SCHEMA_VERSION = "stock_ai.data_availability_contracts.v1"
SNAPSHOT_SCHEMA_VERSION = "stock_ai.data_availability_contract_snapshot.v1"
AUDIT_SCHEMA_VERSION = "stock_ai.data_availability_audit.v1"
CONTRACT_MANIFEST_SCHEMA_VERSION = "stock_ai.availability_contract_manifest.v1"
DEFAULT_AVAILABILITY_CONTRACTS_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "data_availability_contracts.yaml"
)
AvailabilityBasis = Literal[
    "source_published",
    "market_schedule",
    "acquisition_only",
    "unverified",
]


@dataclass(frozen=True)
class AvailabilityContract:
    source_id: str
    dataset: str
    basis: AvailabilityBasis
    historical_pit_eligible: bool
    reason: str
    timezone_name: str | None = None
    scheduled_time: str | None = None
    requires_published_at: bool = False
    source_retention_days: int | None = None
    contract_scope: str = "source_default"
    contract_sha256: str = ""
    production_contract_covered: bool = False

    def receipt(
        self,
        *,
        observed_at: str | None,
        published_at: str | None,
        acquired_at: str | None,
    ) -> dict[str, Any]:
        """Return a deterministic availability decision for one revision."""

        observed = normalize_timestamp(observed_at)
        published = normalize_timestamp(published_at)
        acquired = normalize_timestamp(acquired_at)
        eligible = self.historical_pit_eligible
        available_at = acquired
        reason = self.reason

        if self.basis == "source_published":
            if published:
                available_at = published
            elif self.requires_published_at:
                eligible = False
                reason = "source_publication_timestamp_missing"
        elif self.basis == "market_schedule":
            scheduled = self._scheduled_available_at(observed)
            if scheduled is None:
                eligible = False
                reason = "market_schedule_observation_time_missing"
            else:
                available_at = scheduled
        elif self.basis == "acquisition_only":
            eligible = False
            reason = "historical_availability_not_provided_by_source"
        else:
            eligible = False
            reason = "availability_contract_unverified"

        return {
            "schema_version": SCHEMA_VERSION,
            "source_id": self.source_id,
            "dataset": self.dataset,
            "basis": self.basis,
            "historical_pit_eligible": eligible,
            "available_at": available_at,
            "reason": reason,
            "published_at_required": self.requires_published_at,
            # This describes how long the official *source* currently exposes
            # historical records.  It is provenance metadata, not a license to
            # discard an already immutable local capture that was obtained when
            # the source made it available.
            "source_retention_days": self.source_retention_days,
            "contract_scope": self.contract_scope,
            "contract_sha256": self.contract_sha256,
            "production_contract_covered": self.production_contract_covered,
        }

    def snapshot(
        self,
        *,
        observed_at: str | None,
        published_at: str | None,
        acquired_at: str | None,
    ) -> dict[str, Any]:
        """Freeze the reviewed contract and its decision for one revision.

        A revision must retain the contract that was active when it arrived.
        Re-resolving a changed registry during research would silently rewrite
        historical availability, so callers persist this whole value with the
        immutable revision rather than only its derived ``available_at``.
        """

        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "contract": {
                "schema_version": SCHEMA_VERSION,
                "source_id": self.source_id,
                "dataset": self.dataset,
                "basis": self.basis,
                "historical_pit_eligible": self.historical_pit_eligible,
                "reason": self.reason,
                "timezone": self.timezone_name,
                "scheduled_time": self.scheduled_time,
                "published_at_required": self.requires_published_at,
                "source_retention_days": self.source_retention_days,
                "contract_scope": self.contract_scope,
                "contract_sha256": self.contract_sha256,
                "production_contract_covered": self.production_contract_covered,
            },
            "decision": self.receipt(
                observed_at=observed_at,
                published_at=published_at,
                acquired_at=acquired_at,
            ),
        }

    def _scheduled_available_at(self, observed_at: str | None) -> str | None:
        if not observed_at or not self.timezone_name or not self.scheduled_time:
            return None
        local = datetime.fromisoformat(observed_at).astimezone(ZoneInfo(self.timezone_name))
        hour, minute = (int(part) for part in self.scheduled_time.split(":", 1))
        scheduled = datetime.combine(local.date(), time(hour, minute), tzinfo=ZoneInfo(self.timezone_name))
        return scheduled.astimezone(timezone.utc).isoformat()


class DataAvailabilityRegistry:
    """Machine-readable PIT availability contracts with fail-closed lookup."""

    def __init__(self, path: str | Path = DEFAULT_AVAILABILITY_CONTRACTS_PATH) -> None:
        self.path = Path(path).expanduser().resolve()
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Invalid availability registry; expected {SCHEMA_VERSION}")
        self.registry_sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        research_scope = payload.get("research_contract_scope") or {}
        if not isinstance(research_scope, dict) or not research_scope:
            raise ValueError("availability registry requires research_contract_scope")
        self.research_contract_scope = {
            str(domain): tuple(str(dataset) for dataset in datasets)
            for domain, datasets in research_scope.items()
        }
        scoped_datasets = [
            dataset
            for datasets in self.research_contract_scope.values()
            for dataset in datasets
        ]
        if len(scoped_datasets) != len(set(scoped_datasets)):
            raise ValueError("research availability datasets must belong to one domain")
        self.research_datasets = frozenset(scoped_datasets)
        self._defaults = {
            str(item["source_id"]): self._contract(
                item, dataset="*", contract_scope="source_default"
            )
            for item in payload.get("source_defaults") or []
        }
        self._contracts = {
            (str(item["source_id"]), str(item["dataset"])): self._contract(
                item,
                contract_scope="dataset_source",
                production_contract_covered=str(item["dataset"]) in self.research_datasets,
            )
            for item in payload.get("contracts") or []
        }
        self._endpoint_contracts = {
            (str(item["source_id"]), str(item["domain"])): self._contract(
                item,
                dataset=str(item["domain"]),
                contract_scope="source_domain",
            )
            for item in payload.get("endpoint_contracts") or []
        }
        self._endpoint_dataset_contracts = {}
        for item in payload.get("endpoint_dataset_contracts") or []:
            source_id = str(item["source_id"])
            dataset_id = str(item["dataset_id"])
            domain = str(item["domain"])
            parent = self._endpoint_contracts.get((source_id, domain))
            if parent is None:
                raise ValueError(
                    "availability dataset endpoint contract references an unreviewed "
                    f"source/domain rule: {(source_id, domain)}"
                )
            contract_item = {
                "source_id": source_id,
                "dataset": dataset_id,
                "basis": parent.basis,
                "historical_pit_eligible": parent.historical_pit_eligible,
                "timezone": parent.timezone_name,
                "scheduled_time": parent.scheduled_time,
                "requires_published_at": parent.requires_published_at,
                "source_retention_days": parent.source_retention_days,
                "reason": parent.reason,
                "review_basis": str(item.get("review_basis") or "source_domain_rule"),
            }
            self._endpoint_dataset_contracts[(source_id, dataset_id)] = self._contract(
                contract_item,
                dataset=dataset_id,
                contract_scope="endpoint_dataset",
            )
        if len(self._defaults) != len(payload.get("source_defaults") or []):
            raise ValueError("availability registry source defaults must be unique")
        certifying_defaults = sorted(
            source_id
            for source_id, contract in self._defaults.items()
            if contract.historical_pit_eligible
        )
        if certifying_defaults:
            raise ValueError(
                "availability source defaults must not certify PIT research: "
                f"{certifying_defaults}"
            )
        if len(self._contracts) != len(payload.get("contracts") or []):
            raise ValueError("availability registry contracts must be unique")
        if len(self._endpoint_contracts) != len(payload.get("endpoint_contracts") or []):
            raise ValueError("availability registry endpoint contracts must be unique")
        if len(self._endpoint_dataset_contracts) != len(payload.get("endpoint_dataset_contracts") or []):
            raise ValueError("availability registry endpoint dataset contracts must be unique")
        missing_datasets = sorted(
            dataset
            for dataset in self.research_datasets
            if not any(contract_dataset == dataset for _, contract_dataset in self._contracts)
        )
        if missing_datasets:
            raise ValueError(
                f"research availability datasets have no explicit source contract: {missing_datasets}"
            )

    @staticmethod
    def _contract(
        item: dict[str, Any],
        *,
        dataset: str | None = None,
        contract_scope: str,
        production_contract_covered: bool = False,
    ) -> AvailabilityContract:
        basis = str(item.get("basis") or "")
        if basis not in {"source_published", "market_schedule", "acquisition_only", "unverified"}:
            raise ValueError(f"unsupported availability basis: {basis}")
        scheduled_time = item.get("scheduled_time")
        timezone_name = item.get("timezone")
        if basis == "market_schedule":
            if not scheduled_time or not timezone_name:
                raise ValueError("market_schedule contract requires timezone and scheduled_time")
            try:
                hour, minute = (int(part) for part in str(scheduled_time).split(":", 1))
                time(hour, minute)
                ZoneInfo(str(timezone_name))
            except (TypeError, ValueError) as exc:
                raise ValueError("market_schedule contract has invalid timezone or scheduled_time") from exc
        normalized_dataset = str(dataset or item["dataset"])
        raw_retention_days = item.get("source_retention_days")
        source_retention_days: int | None
        if raw_retention_days is None:
            source_retention_days = None
        elif isinstance(raw_retention_days, bool):
            raise ValueError("source_retention_days must be a positive integer")
        else:
            try:
                source_retention_days = int(raw_retention_days)
            except (TypeError, ValueError) as exc:
                raise ValueError("source_retention_days must be a positive integer") from exc
            if source_retention_days <= 0:
                raise ValueError("source_retention_days must be a positive integer")
        contract_payload = {
            **dict(item),
            "dataset": normalized_dataset,
            "contract_scope": contract_scope,
            "production_contract_covered": production_contract_covered,
        }
        contract_sha256 = hashlib.sha256(
            json.dumps(
                contract_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return AvailabilityContract(
            source_id=str(item["source_id"]),
            dataset=normalized_dataset,
            basis=basis,  # type: ignore[arg-type]
            historical_pit_eligible=bool(item.get("historical_pit_eligible")),
            reason=str(item.get("reason") or "availability_contract_unverified"),
            timezone_name=str(timezone_name) if timezone_name else None,
            scheduled_time=str(scheduled_time) if scheduled_time else None,
            requires_published_at=bool(item.get("requires_published_at")),
            source_retention_days=source_retention_days,
            contract_scope=contract_scope,
            contract_sha256=contract_sha256,
            production_contract_covered=production_contract_covered,
        )

    def validate_source_coverage(self, source_ids: set[str]) -> None:
        missing = sorted(source_ids - set(self._defaults))
        if missing:
            raise ValueError(f"availability registry is missing source defaults: {missing}")
        unknown_contract_sources = sorted(
            {source_id for source_id, _ in self._contracts} - source_ids
        )
        if unknown_contract_sources:
            raise ValueError(
                f"availability contracts reference unknown sources: {unknown_contract_sources}"
            )

    def validate_endpoint_coverage(self, endpoints: Iterable[Any]) -> None:
        """Require an exact, reviewed availability rule for every active endpoint."""

        active_pairs = {
            (str(endpoint.source_id), str(endpoint.dataset_id))
            for endpoint in endpoints
            if bool(getattr(endpoint, "active", True))
        }
        missing = sorted(active_pairs - set(self._endpoint_dataset_contracts))
        if missing:
            raise ValueError(
                "availability registry is missing active source endpoint dataset contracts: "
                f"{missing}"
            )
        extra = sorted(set(self._endpoint_dataset_contracts) - active_pairs)
        if extra:
            raise ValueError(
                "availability registry endpoint dataset contracts reference inactive or unknown endpoints: "
                f"{extra}"
            )

    def resolve(self, *, source_id: str, dataset: str) -> AvailabilityContract:
        return self._contracts.get(
            (source_id, dataset),
            self._defaults.get(
                source_id,
                AvailabilityContract(
                    source_id=source_id,
                    dataset="*",
                    basis="unverified",
                    historical_pit_eligible=False,
                    reason="availability_contract_unverified",
                    contract_scope="unverified",
                ),
            ),
        )

    def resolve_endpoint(
        self,
        *,
        source_id: str,
        domain: str,
        dataset_id: str | None = None,
    ) -> AvailabilityContract:
        """Resolve an exact endpoint rule, retaining a domain fallback for callers."""

        if dataset_id is not None:
            exact = self._endpoint_dataset_contracts.get((source_id, dataset_id))
            if exact is not None:
                return exact

        return self._endpoint_contracts.get(
            (source_id, domain),
            AvailabilityContract(
                source_id=source_id,
                dataset=domain,
                basis="unverified",
                historical_pit_eligible=False,
                reason="availability_endpoint_contract_missing",
                contract_scope="unverified",
            ),
        )

    def research_coverage(self) -> dict[str, Any]:
        datasets = {
            dataset: sorted(
                source_id
                for source_id, contract_dataset in self._contracts
                if contract_dataset == dataset
            )
            for dataset in sorted(self.research_datasets)
        }
        contract_manifests = {
            dataset: [
                self._contract_manifest(
                    source_id=source_id,
                    dataset=dataset,
                    contract=contract,
                )
                for (source_id, contract_dataset), contract in sorted(self._contracts.items())
                if contract_dataset == dataset
            ]
            for dataset in sorted(self.research_datasets)
        }
        return {
            "schema_version": "stock_ai.data_availability_research_coverage.v1",
            "registry_sha256": self.registry_sha256,
            "domains": {
                domain: list(datasets_for_domain)
                for domain, datasets_for_domain in self.research_contract_scope.items()
            },
            "datasets": datasets,
            "contract_manifests": contract_manifests,
            "missing_datasets": [dataset for dataset, sources in datasets.items() if not sources],
            "passed": all(datasets.values()),
        }

    @staticmethod
    def _contract_manifest(
        *,
        source_id: str,
        dataset: str,
        contract: AvailabilityContract,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": CONTRACT_MANIFEST_SCHEMA_VERSION,
            "source_id": source_id,
            "dataset": dataset,
            "basis": contract.basis,
            "historical_pit_eligible": contract.historical_pit_eligible,
            "reason": contract.reason,
            "timezone": contract.timezone_name,
            "scheduled_time": contract.scheduled_time,
            "published_at_required": contract.requires_published_at,
            "source_retention_days": contract.source_retention_days,
            "contract_scope": contract.contract_scope,
            "contract_sha256": contract.contract_sha256,
            "production_contract_covered": contract.production_contract_covered,
        }
        payload["manifest_sha256"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in payload.items() if key != "manifest_sha256"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return payload

    def endpoint_coverage(self, endpoints: Iterable[Any]) -> dict[str, Any]:
        """Return the explicit-contract audit for every active source endpoint."""

        records = []
        for endpoint in sorted(
            (item for item in endpoints if bool(getattr(item, "active", True))),
            key=lambda item: str(item.dataset_id),
        ):
            contract = self.resolve_endpoint(
                source_id=str(endpoint.source_id),
                domain=str(endpoint.domain),
                dataset_id=str(endpoint.dataset_id),
            )
            records.append(
                {
                    "dataset_id": str(endpoint.dataset_id),
                    "source_id": str(endpoint.source_id),
                    "domain": str(endpoint.domain),
                    "basis": contract.basis,
                    "historical_pit_eligible": contract.historical_pit_eligible,
                    "contract_scope": contract.contract_scope,
                    "contract_sha256": contract.contract_sha256,
                    "reason": contract.reason,
                    "published_at_required": contract.requires_published_at,
                    "source_retention_days": contract.source_retention_days,
                    "production_contract_covered": contract.production_contract_covered,
                }
            )
            records[-1]["contract_manifest_sha256"] = hashlib.sha256(
                json.dumps(
                    records[-1],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        missing = [
            item["dataset_id"]
            for item in records
            if item["contract_scope"] != "endpoint_dataset"
        ]
        return {
            "schema_version": "stock_ai.data_availability_endpoint_coverage.v1",
            "registry_sha256": self.registry_sha256,
            "endpoints": records,
            "missing_endpoint_contracts": missing,
            "passed": not missing,
        }

    def audit_receipt(self, endpoints: Iterable[Any]) -> dict[str, Any]:
        """Return one immutable audit over endpoint and research-dataset scope.

        Endpoint validation used to be available only as a startup assertion or
        an isolated test result.  A research run needs the same evidence in its
        payload so a restored run can prove which availability registry and
        endpoint set it used.  This receipt never upgrades an unverified or
        acquisition-only contract; it only makes the fail-closed audit
        portable and content-addressed.
        """

        endpoint_coverage = self.endpoint_coverage(endpoints)
        research_coverage = self.research_coverage()
        blockers: list[str] = []
        if endpoint_coverage["passed"] is not True:
            blockers.append("active_endpoint_availability_contract_missing")
        if research_coverage["passed"] is not True:
            blockers.append("research_dataset_availability_contract_missing")
        payload: dict[str, Any] = {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "registry_sha256": self.registry_sha256,
            "endpoint_coverage": endpoint_coverage,
            "research_coverage": research_coverage,
            "blockers": blockers,
            "passed": not blockers,
        }
        payload["receipt_sha256"] = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return payload

    @staticmethod
    def verify_audit_receipt(receipt: dict[str, Any]) -> None:
        """Verify an availability audit without consulting the live registry."""

        if receipt.get("schema_version") != AUDIT_SCHEMA_VERSION:
            raise ValueError("invalid data availability audit schema")
        endpoint_records = receipt.get("endpoint_coverage", {}).get("endpoints", [])
        for record in endpoint_records:
            if not isinstance(record, dict):
                raise ValueError("data availability endpoint manifest is invalid")
            supplied_manifest = str(record.get("contract_manifest_sha256") or "").lower()
            manifest_payload = dict(record)
            manifest_payload.pop("contract_manifest_sha256", None)
            expected_manifest = hashlib.sha256(
                json.dumps(
                    manifest_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if supplied_manifest != expected_manifest:
                raise ValueError("data availability contract manifest hash mismatch")
        research_manifests = receipt.get("research_coverage", {}).get("contract_manifests", {})
        for manifests in research_manifests.values():
            for manifest in manifests:
                if not isinstance(manifest, dict):
                    raise ValueError("data availability research manifest is invalid")
                supplied_manifest = str(manifest.get("manifest_sha256") or "").lower()
                manifest_payload = dict(manifest)
                manifest_payload.pop("manifest_sha256", None)
                expected_manifest = hashlib.sha256(
                    json.dumps(
                        manifest_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if supplied_manifest != expected_manifest:
                    raise ValueError("data availability research manifest hash mismatch")
        supplied = str(receipt.get("receipt_sha256") or "").lower()
        if len(supplied) != 64:
            raise ValueError("data availability audit receipt hash is missing")
        payload = dict(receipt)
        payload.pop("receipt_sha256", None)
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if supplied != expected:
            raise ValueError("data availability audit receipt hash mismatch")


@lru_cache(maxsize=1)
def get_data_availability_registry() -> DataAvailabilityRegistry:
    return DataAvailabilityRegistry()
