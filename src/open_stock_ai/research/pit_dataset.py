from __future__ import annotations

"""Point-in-time research dataset contracts.

The warehouse already retains immutable source revisions.  This module is the
strict bridge from those revisions to research input: a feature may only be
materialized when it was actually available at the decision timestamp.  It
deliberately records unavailable streams instead of backfilling them with data
retrieved today, because a backtest must not turn archival access into
historical knowledge.
"""

import hashlib
import json
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from stock_ai.data_platform.availability import (
    SCHEMA_VERSION as AVAILABILITY_CONTRACT_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
)

from .historical_universe import historical_universe_at


_REQUIRED_DOMAINS = ("prices", "financials", "flows", "events")
FEATURE_RECORD_SCHEMA_VERSION = "open_stock_ai.feature_record.v2"
DATASET_MANIFEST_SCHEMA_VERSION = "open_stock_ai.pit_dataset_manifest.v3"
WAREHOUSE_REVISION_PROJECTION_FORMULA = "stock_ai.warehouse_revision_projection.v1"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _module_code_sha256() -> str:
    """Bind feature lineage to the exact projection code shipped at runtime."""

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@dataclass(frozen=True)
class FeatureRecord:
    """One immutable, source-attributed value available to a research run."""

    entity_id: str
    feature_id: str
    value: Any
    event_time: str
    published_at: str | None
    available_at: str
    effective_at: str
    ingested_at: str
    source_revision_id: str
    transformation_id: str
    transformation_sha: str
    dataset_version: str
    domain: str
    source_id: str = "synthetic_test"
    dataset_id: str = "synthetic"
    availability_basis: str = "source_published"
    historical_pit_eligible: bool = True
    availability_reason: str = "synthetic_fixture"
    availability_contract_sha256: str = "synthetic_fixture"
    production_contract_covered: bool = True
    coverage_receipt_id: str | None = None
    coverage_receipt_sha256: str | None = None
    feature_schema_version: str = FEATURE_RECORD_SCHEMA_VERSION
    formula: str = WAREHOUSE_REVISION_PROJECTION_FORMULA
    formula_sha256: str = _sha256_text(WAREHOUSE_REVISION_PROJECTION_FORMULA)
    code_sha256: str = field(default_factory=_module_code_sha256)
    input_revision_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required_text = {
            "entity_id": self.entity_id,
            "feature_id": self.feature_id,
            "source_revision_id": self.source_revision_id,
            "transformation_id": self.transformation_id,
            "dataset_version": self.dataset_version,
            "domain": self.domain,
            "source_id": self.source_id,
            "dataset_id": self.dataset_id,
        }
        missing = [name for name, value in required_text.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"feature record required fields missing:{','.join(missing)}")
        for name, value in (
            ("event_time", self.event_time),
            ("available_at", self.available_at),
            ("effective_at", self.effective_at),
            ("ingested_at", self.ingested_at),
        ):
            try:
                _time(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"feature record timestamp invalid:{name}") from exc
        if self.published_at is not None:
            try:
                _time(self.published_at)
            except (TypeError, ValueError) as exc:
                raise ValueError("feature record timestamp invalid:published_at") from exc
        if self.source_id != "synthetic_test":
            if len(self.transformation_sha) != 64:
                raise ValueError("warehouse feature transformation_sha must be SHA-256")
            if len(self.availability_contract_sha256) != 64:
                raise ValueError("warehouse feature availability contract must be SHA-256")
        if not str(self.feature_schema_version).strip():
            raise ValueError("feature record schema version is required")
        if not str(self.formula).strip():
            raise ValueError("feature lineage formula is required")
        if self.formula_sha256 != _sha256_text(self.formula):
            raise ValueError("feature lineage formula_sha256 does not match formula")
        if not self.code_sha256 or len(self.code_sha256) != 64:
            raise ValueError("feature lineage code_sha256 must be SHA-256")
        if any(not str(item).strip() for item in self.input_revision_ids):
            raise ValueError("feature lineage input revision id is invalid")

    @property
    def resolved_input_revision_ids(self) -> tuple[str, ...]:
        """Always retain the revision that produced the feature itself."""

        return tuple(sorted({self.source_revision_id, *self.input_revision_ids}))

    def lineage(self) -> dict[str, Any]:
        """Portable feature-level provenance contract.

        The graph is deliberately expressed with revision IDs instead of row
        pointers so an experiment can restore it from immutable warehouse
        partitions on a different machine.
        """

        return {
            "schema_version": "open_stock_ai.feature_lineage.v1",
            "feature_node_id": _sha256({
                "entity_id": self.entity_id,
                "feature_id": self.feature_id,
                "dataset_version": self.dataset_version,
                "source_revision_id": self.source_revision_id,
            }),
            "formula": self.formula,
            "formula_sha256": self.formula_sha256,
            "code_sha256": self.code_sha256,
            "input_revision_ids": list(self.resolved_input_revision_ids),
        }

    @property
    def known_at(self) -> str:
        return _timestamp(max(_time(self.available_at), _time(self.ingested_at)))

    @classmethod
    def from_warehouse(cls, domain: str, item: Mapping[str, Any]) -> "FeatureRecord":
        record = dict(item.get("record") or {})
        event_time = str(item.get("observed_at") or item.get("effective_at") or item.get("available_at") or "")
        available_at = str(item.get("available_at") or "")
        effective_at = str(item.get("effective_at") or event_time or available_at)
        ingested_at = str(item.get("acquired_at") or "")
        if not event_time or not available_at or not effective_at or not ingested_at:
            raise ValueError("warehouse feature record is missing temporal coordinates")
        revision_id = str(item.get("revision_id") or "")
        if not revision_id:
            raise ValueError("warehouse feature record is missing revision_id")
        source_id = str(item.get("source_id") or "")
        dataset = str(item.get("dataset") or "unknown")
        availability = _availability_from_snapshot(
            item.get("availability_contract_snapshot"),
            source_id=source_id,
            dataset=dataset,
            revision_id=revision_id,
        )
        contract_available_at = str(availability.get("available_at") or available_at)
        payload = {
            "domain": domain,
            "dataset": dataset,
            "revision_id": revision_id,
            "record": record,
        }
        transformation_sha = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        coverage_receipt_id = (
            str(record.get("news_history_coverage_receipt_id") or "") or None
        )
        coverage_receipt_sha256 = (
            str(record.get("news_history_coverage_receipt_sha256") or "") or None
        )
        coverage_verified = domain != "events" or (
            record.get("news_history_coverage_verified") is True
            and coverage_receipt_id is not None
            and coverage_receipt_sha256 is not None
            and len(coverage_receipt_sha256) == 64
        )
        raw_inputs = record.get("input_revision_ids")
        input_revision_ids = tuple(
            str(value).strip()
            for value in (raw_inputs if isinstance(raw_inputs, list) else ())
            if str(value).strip()
        )
        return cls(
            entity_id=str(item.get("entity_id") or ""),
            feature_id=f"{domain}:{dataset}:{item.get('observation_key') or revision_id}",
            value=record,
            event_time=event_time,
            published_at=(str(item["published_at"]) if item.get("published_at") else None),
            available_at=contract_available_at,
            effective_at=effective_at,
            ingested_at=ingested_at,
            source_revision_id=revision_id,
            transformation_id=WAREHOUSE_REVISION_PROJECTION_FORMULA,
            transformation_sha=transformation_sha,
            dataset_version=f"{dataset}:{revision_id}",
            domain=domain,
            source_id=source_id,
            dataset_id=dataset,
            availability_basis=str(availability["basis"]),
            historical_pit_eligible=(
                bool(availability["historical_pit_eligible"]) and coverage_verified
            ),
            availability_reason=(
                str(availability["reason"])
                if coverage_verified
                else "news_historical_coverage_receipt_missing"
            ),
            availability_contract_sha256=str(availability["contract_sha256"]),
            production_contract_covered=bool(availability["production_contract_covered"]),
            coverage_receipt_id=coverage_receipt_id,
            coverage_receipt_sha256=coverage_receipt_sha256,
            formula=WAREHOUSE_REVISION_PROJECTION_FORMULA,
            formula_sha256=_sha256_text(WAREHOUSE_REVISION_PROJECTION_FORMULA),
            code_sha256=_module_code_sha256(),
            input_revision_ids=input_revision_ids,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FEATURE_RECORD_SCHEMA_VERSION,
            "feature_schema_version": self.feature_schema_version,
            "lineage": self.lineage(),
            "entity_id": self.entity_id,
            "feature_id": self.feature_id,
            "value": self.value,
            "event_time": self.event_time,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "effective_at": self.effective_at,
            "ingested_at": self.ingested_at,
            "source_revision_id": self.source_revision_id,
            "transformation_id": self.transformation_id,
            "transformation_sha": self.transformation_sha,
            "dataset_version": self.dataset_version,
            "domain": self.domain,
            "source_id": self.source_id,
            "dataset_id": self.dataset_id,
            "availability_basis": self.availability_basis,
            "historical_pit_eligible": self.historical_pit_eligible,
            "availability_reason": self.availability_reason,
            "availability_contract_sha256": self.availability_contract_sha256,
            "production_contract_covered": self.production_contract_covered,
            "coverage_receipt_id": self.coverage_receipt_id,
            "coverage_receipt_sha256": self.coverage_receipt_sha256,
            "known_at": self.known_at,
        }


@dataclass(frozen=True)
class DatasetManifest:
    entity_id: str
    as_of: str
    required_domains: tuple[str, ...]
    feature_count: int
    replay_row_count: int
    feature_schema_versions: dict[str, tuple[str, ...]]
    feature_lineage_sha256: str
    partition_hashes: dict[str, str]
    dataset_sha256: str
    coverage: dict[str, dict[str, Any]]
    blockers: tuple[str, ...]
    manifest_hash: str

    @property
    def exact_replay_eligible(self) -> bool:
        return not self.blockers and self.replay_row_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
            "entity_id": self.entity_id,
            "as_of": self.as_of,
            "required_domains": list(self.required_domains),
            "feature_count": self.feature_count,
            "replay_row_count": self.replay_row_count,
            "feature_schema_versions": {
                domain: list(versions)
                for domain, versions in self.feature_schema_versions.items()
            },
            "feature_lineage_sha256": self.feature_lineage_sha256,
            "partition_hashes": dict(self.partition_hashes),
            "dataset_sha256": self.dataset_sha256,
            "coverage": self.coverage,
            "blockers": list(self.blockers),
            "exact_replay_eligible": self.exact_replay_eligible,
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True)
class PointInTimeDataset:
    manifest: DatasetManifest
    features: tuple[FeatureRecord, ...]
    replay_rows: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.manifest.to_dict(),
            "features": [item.to_dict() for item in self.features],
            "replay_rows": list(self.replay_rows),
        }

    def verify_materialization(self) -> dict[str, Any]:
        """Verify that restored features and replay rows match this manifest.

        A manifest hash alone cannot protect a caller that replaces a replay
        row after loading it.  This check recomputes every materialized feature
        partition and the ordered replay partition before an experiment is
        allowed to use the dataset.
        """

        return verify_dataset_materialization(
            self.manifest.to_dict(),
            features=self.features,
            replay_rows=self.replay_rows,
        )

    def feature_provenance_graph(self) -> dict[str, Any]:
        """Return the immutable feature-to-revision graph for this dataset."""

        return feature_provenance_graph(self.features)


class PointInTimeDatasetBuilder:
    """Build auditable research rows without inventing historical knowledge."""

    def __init__(self, *, required_domains: Sequence[str] = _REQUIRED_DOMAINS) -> None:
        normalized = tuple(dict.fromkeys(str(item).strip() for item in required_domains if str(item).strip()))
        if "prices" not in normalized:
            raise ValueError("PointInTimeDatasetBuilder requires the prices domain")
        self.required_domains = normalized

    def build_from_warehouse(
        self,
        warehouse: Any,
        *,
        entity_id: str,
        as_of: str,
        intelligence_records: Iterable[Mapping[str, Any]] = (),
    ) -> PointInTimeDataset:
        """Read only warehouse revisions known at ``as_of``.

        ``standard_records`` applies the warehouse's published/available/
        acquired/effective predicates before records reach this builder.
        """

        records: dict[str, list[FeatureRecord]] = {}
        for domain in self.required_domains:
            rows = warehouse.standard_records(
                domain,
                entity_id=entity_id,
                knowledge_at=as_of,
                effective_at=as_of,
                limit=10_000,
            )
            records[domain] = [FeatureRecord.from_warehouse(domain, row) for row in rows]
        universe_records = (
            warehouse.historical_universe_records(knowledge_at=as_of)
            if hasattr(warehouse, "historical_universe_records")
            else []
        )
        return self.build_from_records(
            entity_id=entity_id,
            as_of=as_of,
            records_by_domain=records,
            intelligence_records=intelligence_records,
            universe_records=universe_records,
        )

    def build_from_records(
        self,
        *,
        entity_id: str,
        as_of: str,
        records_by_domain: Mapping[str, Iterable[FeatureRecord]],
        intelligence_records: Iterable[Mapping[str, Any]] = (),
        universe_records: Iterable[Mapping[str, Any]] = (),
    ) -> PointInTimeDataset:
        as_of_time = _time(as_of)
        source_records = {
            domain: tuple(records_by_domain.get(domain, ()))
            for domain in self.required_domains
        }
        records = {
            domain: sorted(
                [item for item in source_records[domain] if _feature_known_at(item, as_of_time)],
                key=lambda item: (_time(item.event_time), _time(item.available_at), item.source_revision_id),
            )
            for domain in self.required_domains
        }
        intelligence = sorted(
            [dict(item) for item in intelligence_records if _record_known_at(item, as_of_time)],
            key=lambda item: (_time(str(item.get("available_at"))), _canonical(item)),
        )
        coverage = {
            domain: {
                "count": len(items),
                "known_at_as_of": bool(items),
                "latest_available_at": items[-1].available_at if items else None,
                "latest_ingested_at": items[-1].ingested_at if items else None,
                "excluded_after_as_of": sum(
                    not _feature_known_at(item, as_of_time)
                    for item in source_records[domain]
                ),
                "dataset_versions": sorted({item.dataset_version for item in items}),
                "availability_contract_sha256": sorted(
                    {item.availability_contract_sha256 for item in items}
                ),
                "production_contract_covered": all(
                    item.production_contract_covered for item in items
                ) if items else False,
            }
            for domain, items in records.items()
        }
        universe_source_records = tuple(dict(item) for item in universe_records)
        blockers = [f"pit_required_domain_missing:{domain}" for domain, details in coverage.items() if not details["known_at_as_of"]]
        blockers.extend(
            f"pit_availability_not_certified:{item.source_id}:{item.dataset_id}:{item.availability_basis}"
            for domain in self.required_domains
            for item in records[domain]
            if not item.historical_pit_eligible
        )
        blockers.extend(
            f"pit_production_contract_missing:{item.source_id}:{item.dataset_id}"
            for domain in self.required_domains
            for item in records[domain]
            if not item.production_contract_covered
        )
        if not intelligence:
            blockers.append("pit_intelligence_missing")

        replay_rows: list[dict[str, Any]] = []
        universe_receipts: list[dict[str, Any]] = []
        if not blockers:
            for price in records["prices"]:
                # The decision occurs only after both source availability and
                # local ingestion.  A market event can precede this time (as
                # daily bars do after the close), so event time is never used
                # as a substitute decision timestamp.
                decision_time = _time(price.known_at)
                if (
                    _time(price.event_time) > decision_time
                    or _time(price.effective_at) > decision_time
                ):
                    continue
                universe_membership = historical_universe_at(
                    universe_source_records,
                    as_of=_timestamp(decision_time),
                    required_entity_id=entity_id,
                )
                universe_receipts.append(universe_membership)
                if universe_membership["passed"] is not True:
                    continue
                features = [
                    item
                    for domain in self.required_domains
                    for item in records[domain]
                    if _feature_known_at(item, decision_time)
                    and _time(item.effective_at) <= decision_time
                ]
                if not all(any(item.domain == domain for item in features) for domain in self.required_domains):
                    continue
                intelligence_item = _latest_intelligence(intelligence, decision_time)
                if intelligence_item is None:
                    continue
                price_value = dict(price.value) if isinstance(price.value, dict) else {}
                close = _number(price_value.get("close") or price_value.get("Close") or price_value.get("ClosingPrice"))
                if close is None:
                    continue
                replay_rows.append(
                    {
                        "timestamp": _timestamp(decision_time),
                        "event_time": price.event_time,
                        "published_at": price.published_at,
                        "available_at": price.available_at,
                        "effective_at": price.effective_at,
                        "ingested_at": price.ingested_at,
                        "open": _number(price_value.get("open") or price_value.get("Open") or price_value.get("OpeningPrice")) or close,
                        "high": _number(price_value.get("high") or price_value.get("High") or price_value.get("HighestPrice")) or close,
                        "low": _number(price_value.get("low") or price_value.get("Low") or price_value.get("LowestPrice")) or close,
                        "close": close,
                        "volume": int(_number(price_value.get("volume") or price_value.get("Volume") or price_value.get("TradeVolume") or price_value.get("TradingShares")) or 0),
                        "venue": str(price_value.get("venue") or price_value.get("exchange") or "").upper(),
                        "product_type": str(price_value.get("product_type") or "stock").lower(),
                        "lot_type": str(price_value.get("lot_type") or "board_lot").lower(),
                        "universe_membership": universe_membership,
                        "feature_records": [item.to_dict() for item in features],
                        "pit_intelligence": intelligence_item,
                    }
                )
        coverage["historical_universe"] = {
            "source_record_count": len(universe_source_records),
            "decision_receipt_count": len(universe_receipts),
            "eligible_decision_count": sum(item["passed"] is True for item in universe_receipts),
            "latest_manifest_hash": universe_receipts[-1]["manifest_hash"] if universe_receipts else None,
            "blockers": list(
                dict.fromkeys(
                    blocker
                    for item in universe_receipts
                    for blocker in item["blockers"]
                )
            ),
        }
        if not universe_receipts or any(item["passed"] is not True for item in universe_receipts):
            blockers.append("pit_historical_universe_unavailable")
        if not replay_rows:
            blockers.append("pit_replay_rows_unavailable")
        blockers = list(dict.fromkeys(blockers))
        all_features = tuple(item for domain in self.required_domains for item in records[domain])
        lineage_graph = feature_provenance_graph(all_features)
        if lineage_graph["valid"] is not True:
            blockers.extend(f"pit_feature_lineage_invalid:{error}" for error in lineage_graph["errors"])
            blockers = list(dict.fromkeys(blockers))
        identity = build_dataset_manifest_identity(
            entity_id=entity_id,
            as_of=_timestamp(as_of_time),
            required_domains=self.required_domains,
            coverage=coverage,
            blockers=blockers,
            features=all_features,
            replay_rows=replay_rows,
        )
        manifest = DatasetManifest(
            entity_id=entity_id,
            as_of=_timestamp(as_of_time),
            required_domains=self.required_domains,
            feature_count=len(all_features),
            replay_row_count=len(replay_rows),
            feature_schema_versions={
                domain: tuple(versions)
                for domain, versions in identity["feature_schema_versions"].items()
            },
            feature_lineage_sha256=identity["feature_lineage_sha256"],
            partition_hashes=identity["partition_hashes"],
            dataset_sha256=identity["dataset_sha256"],
            coverage=coverage,
            blockers=tuple(blockers),
            manifest_hash=identity["manifest_hash"],
        )
        return PointInTimeDataset(manifest=manifest, features=all_features, replay_rows=tuple(replay_rows))


def _latest_intelligence(records: Sequence[dict[str, Any]], decision_time: datetime) -> dict[str, Any] | None:
    eligible = [item for item in records if _time(str(item.get("available_at"))) <= decision_time]
    if not eligible:
        return None
    return dict(eligible[-1])


def _feature_known_at(record: FeatureRecord, as_of: datetime) -> bool:
    """A historical decision needs source availability and local ingestion.

    A reviewed publication schedule can prove when an exchange released a
    datum, but it cannot make a revision fetched today appear in a historical
    local run.  Keeping both coordinates closes the archive/backfill loophole.
    """

    return _time(record.known_at) <= as_of


def _record_known_at(record: Mapping[str, Any], as_of: datetime) -> bool:
    required = ("event_time", "published_at", "available_at", "effective_at", "ingested_at")
    if any(key not in record for key in required):
        return False
    available = record.get("available_at")
    ingested = record.get("ingested_at")
    event = record.get("event_time")
    effective = record.get("effective_at")
    if not all((available, ingested, event, effective)):
        return False
    try:
        published = _time(str(record["published_at"])) if record.get("published_at") else None
        available_time = _time(str(available))
        ingested_time = _time(str(ingested))
        _time(str(event))
        _time(str(effective))
    except (TypeError, ValueError):
        return False
    if published is not None and published > available_time:
        return False
    return max(available_time, ingested_time) <= as_of


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def dataset_partition_hashes(
    *,
    features: Iterable[FeatureRecord | Mapping[str, Any]],
    replay_rows: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    """Return content hashes for every materialized research partition.

    Feature ordering in a warehouse query is not a dataset characteristic, so
    each feature partition is canonically sorted.  Replay row ordering *is*
    the decision sequence and is intentionally retained in its hash.
    """

    by_domain: dict[str, list[dict[str, Any]]] = {}
    for item in features:
        value = item.to_dict() if isinstance(item, FeatureRecord) else dict(item)
        domain = str(value.get("domain") or "")
        if not domain:
            raise ValueError("dataset feature partition requires domain")
        by_domain.setdefault(domain, []).append(value)
    hashes = {
        f"features:{domain}": _sha256(sorted(values, key=_canonical))
        for domain, values in sorted(by_domain.items())
    }
    hashes["replay_rows"] = _sha256([dict(item) for item in replay_rows])
    return hashes


def feature_provenance_graph(
    features: Iterable[FeatureRecord | Mapping[str, Any]],
) -> dict[str, Any]:
    """Materialize a canonical feature provenance graph from stored records.

    Each feature node is connected to every immutable warehouse revision it
    consumed.  Formula and code hashes stay on the feature node so changing a
    formula, adding an input, or running different code produces a different
    graph identity and cannot silently reuse a research result.
    """

    feature_nodes: list[dict[str, Any]] = []
    revision_ids: set[str] = set()
    errors: list[str] = []
    for index, item in enumerate(features):
        value = item.to_dict() if isinstance(item, FeatureRecord) else dict(item)
        lineage = value.get("lineage")
        if not isinstance(lineage, Mapping):
            errors.append(f"lineage_missing:{index}")
            continue
        formula = str(lineage.get("formula") or "")
        formula_sha = str(lineage.get("formula_sha256") or "")
        code_sha = str(lineage.get("code_sha256") or "")
        inputs = lineage.get("input_revision_ids")
        feature_node_id = str(lineage.get("feature_node_id") or "")
        expected_node_id = _sha256({
            "entity_id": value.get("entity_id"),
            "feature_id": value.get("feature_id"),
            "dataset_version": value.get("dataset_version"),
            "source_revision_id": value.get("source_revision_id"),
        })
        if (
            lineage.get("schema_version") != "open_stock_ai.feature_lineage.v1"
            or not formula
            or formula_sha != _sha256_text(formula)
            or len(code_sha) != 64
            or not isinstance(inputs, list)
            or not inputs
            or any(not str(revision).strip() for revision in inputs)
            or feature_node_id != expected_node_id
        ):
            errors.append(f"lineage_invalid:{index}")
            continue
        normalized_inputs = sorted({str(revision) for revision in inputs})
        if str(value.get("source_revision_id") or "") not in normalized_inputs:
            errors.append(f"lineage_source_revision_missing:{index}")
            continue
        revision_ids.update(normalized_inputs)
        feature_nodes.append({
            "node_id": feature_node_id,
            "entity_id": str(value.get("entity_id") or ""),
            "feature_id": str(value.get("feature_id") or ""),
            "dataset_version": str(value.get("dataset_version") or ""),
            "formula": formula,
            "formula_sha256": formula_sha,
            "code_sha256": code_sha,
            "input_revision_ids": normalized_inputs,
        })
    feature_nodes.sort(key=lambda value: value["node_id"])
    revision_nodes = [
        {"node_id": f"revision:{revision_id}", "revision_id": revision_id}
        for revision_id in sorted(revision_ids)
    ]
    edges = [
        {
            "from": feature["node_id"],
            "to": f"revision:{revision_id}",
            "relationship": "consumes_revision",
        }
        for feature in feature_nodes
        for revision_id in feature["input_revision_ids"]
    ]
    payload = {
        "schema_version": "open_stock_ai.feature_provenance_graph.v1",
        "feature_nodes": feature_nodes,
        "revision_nodes": revision_nodes,
        "edges": edges,
        "errors": list(dict.fromkeys(errors)),
    }
    return {
        **payload,
        "valid": not errors,
        "graph_sha256": _sha256(payload),
    }


def build_dataset_manifest_identity(
    *,
    entity_id: str,
    as_of: str,
    required_domains: Sequence[str],
    coverage: Mapping[str, Any],
    blockers: Sequence[str],
    features: Iterable[FeatureRecord | Mapping[str, Any]],
    replay_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create a self-consistent, content-addressed PIT manifest identity.

    This is public so non-warehouse importers and tests cannot invent a
    plausible manifest by hand.  The same constructor is used by the builder
    and by the restore verifier.
    """

    normalized_features = [
        item.to_dict() if isinstance(item, FeatureRecord) else dict(item)
        for item in features
    ]
    normalized_rows = [dict(item) for item in replay_rows]
    domains = tuple(dict.fromkeys(str(item).strip() for item in required_domains if str(item).strip()))
    schemas: dict[str, list[str]] = {}
    for domain in domains:
        schemas[domain] = sorted({
            str(item.get("feature_schema_version") or item.get("schema_version") or "")
            for item in normalized_features
            if str(item.get("domain") or "") == domain
        })
    identity = {
        "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
        "entity_id": str(entity_id),
        "as_of": _timestamp(_time(as_of)),
        "required_domains": domains,
        "feature_count": len(normalized_features),
        "replay_row_count": len(normalized_rows),
        "feature_schema_versions": schemas,
        "feature_lineage_sha256": feature_provenance_graph(normalized_features)["graph_sha256"],
        "partition_hashes": dataset_partition_hashes(
            features=normalized_features,
            replay_rows=normalized_rows,
        ),
        "coverage": dict(coverage),
        "replay_rows": normalized_rows,
        "blockers": list(dict.fromkeys(str(item) for item in blockers)),
    }
    dataset_sha256 = _sha256(identity)
    return {
        **identity,
        "dataset_sha256": dataset_sha256,
        "manifest_hash": _sha256({**identity, "dataset_sha256": dataset_sha256}),
    }


def verify_dataset_materialization(
    manifest: Mapping[str, Any],
    *,
    features: Iterable[FeatureRecord | Mapping[str, Any]],
    replay_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fail closed unless a restored dataset exactly matches its receipt."""

    feature_values = list(features)
    row_values = list(replay_rows)
    expected = manifest.get("partition_hashes")
    errors: list[str] = []
    if manifest.get("schema_version") != DATASET_MANIFEST_SCHEMA_VERSION:
        errors.append("dataset_manifest_schema_invalid")
    if not isinstance(expected, Mapping):
        errors.append("dataset_manifest_partition_hashes_missing")
        expected = {}
    try:
        observed = dataset_partition_hashes(features=feature_values, replay_rows=row_values)
    except (TypeError, ValueError) as exc:
        return {
            "schema_version": "open_stock_ai.pit_dataset_integrity.v1",
            "passed": False,
            "errors": [f"dataset_materialization_invalid:{type(exc).__name__}"],
        }
    required_domains = tuple(str(item) for item in manifest.get("required_domains") or ())
    for domain in required_domains:
        key = f"features:{domain}"
        if key not in expected:
            errors.append(f"dataset_manifest_partition_missing:{key}")
    for key, expected_hash in expected.items():
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            errors.append(f"dataset_manifest_partition_hash_invalid:{key}")
        elif observed.get(str(key)) != expected_hash:
            errors.append(f"dataset_partition_hash_mismatch:{key}")
    for key in observed:
        if key not in expected:
            errors.append(f"dataset_manifest_unexpected_partition:{key}")
    observed_identity = build_dataset_manifest_identity(
        entity_id=str(manifest.get("entity_id") or ""),
        as_of=str(manifest.get("as_of") or ""),
        required_domains=tuple(str(item) for item in manifest.get("required_domains") or ()),
        coverage=manifest.get("coverage") if isinstance(manifest.get("coverage"), Mapping) else {},
        blockers=tuple(str(item) for item in manifest.get("blockers") or ()),
        features=feature_values,
        replay_rows=row_values,
    )
    if manifest.get("feature_count") != observed_identity["feature_count"]:
        errors.append("dataset_manifest_feature_count_mismatch")
    if manifest.get("replay_row_count") != observed_identity["replay_row_count"]:
        errors.append("dataset_manifest_replay_row_count_mismatch")
    if manifest.get("feature_schema_versions") != observed_identity["feature_schema_versions"]:
        errors.append("dataset_manifest_feature_schema_mismatch")
    if manifest.get("feature_lineage_sha256") != observed_identity["feature_lineage_sha256"]:
        errors.append("dataset_manifest_feature_lineage_hash_mismatch")
    observed_lineage = feature_provenance_graph(feature_values)
    if observed_lineage["valid"] is not True:
        errors.extend(f"dataset_feature_lineage_invalid:{error}" for error in observed_lineage["errors"])
    observed_dataset_sha256 = observed_identity["dataset_sha256"]
    if manifest.get("dataset_sha256") != observed_dataset_sha256:
        errors.append("dataset_manifest_identity_hash_mismatch")
    if manifest.get("manifest_hash") != observed_identity["manifest_hash"]:
        errors.append("dataset_manifest_hash_mismatch")
    return {
        "schema_version": "open_stock_ai.pit_dataset_integrity.v1",
        "passed": not errors,
        "observed_partition_hashes": observed,
        "observed_dataset_sha256": observed_dataset_sha256,
        "errors": errors,
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _availability_from_snapshot(
    snapshot_value: Any,
    *,
    source_id: str,
    dataset: str,
    revision_id: str,
) -> dict[str, Any]:
    """Read only the contract recorded with an immutable warehouse revision.

    Legacy rows cannot be certified after the fact, and a registry update must
    never change how an existing revision is replayed. Invalid snapshots are
    likewise withheld from exact PIT instead of being repaired from current
    configuration.
    """

    fallback_hash = hashlib.sha256(
        f"availability-contract-snapshot:{revision_id}".encode("utf-8")
    ).hexdigest()
    unavailable = {
        "basis": "snapshot_missing",
        "historical_pit_eligible": False,
        "reason": "availability_contract_snapshot_missing",
        "contract_sha256": fallback_hash,
        "production_contract_covered": False,
    }
    if not isinstance(snapshot_value, Mapping):
        return unavailable
    contract = snapshot_value.get("contract")
    decision = snapshot_value.get("decision")
    if not isinstance(contract, Mapping) or not isinstance(decision, Mapping):
        return unavailable
    if (
        snapshot_value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or contract.get("schema_version") != AVAILABILITY_CONTRACT_SCHEMA_VERSION
        or contract.get("source_id") != source_id
        or contract.get("dataset") != dataset
        or decision.get("source_id") != source_id
        or decision.get("dataset") != dataset
        or decision.get("contract_sha256") != contract.get("contract_sha256")
        or not isinstance(contract.get("contract_sha256"), str)
        or len(str(contract.get("contract_sha256"))) != 64
    ):
        return {
            **unavailable,
            "basis": "snapshot_invalid",
            "reason": "availability_contract_snapshot_invalid",
        }
    required = (
        "basis",
        "historical_pit_eligible",
        "reason",
        "available_at",
        "contract_sha256",
        "production_contract_covered",
    )
    if any(key not in decision for key in required):
        return {
            **unavailable,
            "basis": "snapshot_invalid",
            "reason": "availability_contract_snapshot_invalid",
        }
    return dict(decision)
