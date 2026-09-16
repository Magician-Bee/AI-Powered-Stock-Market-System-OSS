from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable
from uuid import NAMESPACE_URL, uuid5
import os

from .contracts import (
    DataEnvelopeV2,
    DataQuery,
    TemporalCoordinates,
    normalize_timestamp,
    utc_now,
)
from .cache import CachePolicyService
from .entity_registry import EntityRegistry
from .failover import SourceFailoverService
from .observability import SourceObservabilityService
from .observability_notifications import NotificationSender
from .quality import DataQualityService
from .reconciliation import ReconciliationEngine
from .security_lifecycle import (
    consolidate_security_snapshots,
    normalize_security_payloads,
)
from .source_registry import SourceRegistry
from .warehouse import (
    MarketDataWarehouse,
    content_hash,
    revision_identity_payload,
)
from .ui_api import unified_data_api_contract


def stable_entity_id(*, market: str, exchange: str, source_code: str) -> str:
    seed = f"stock-ai:{market.casefold()}:{exchange.casefold()}:{source_code.upper()}"
    return f"ENT-{uuid5(NAMESPACE_URL, seed).hex}"


class MarketDataPlatform:
    """Single entry point for raw capture, normalization and point-in-time reads."""

    def __init__(
        self,
        *,
        database_path: str | Path | None = None,
        source_catalog_path: str | Path | None = None,
        quality_rules_path: str | Path | None = None,
        reconciliation_rules_path: str | Path | None = None,
        observability_rules_path: str | Path | None = None,
        observability_notification_sender: NotificationSender | None = None,
        code_version: str | None = None,
    ) -> None:
        if database_path is None:
            database_path = os.getenv("STOCK_AI_MARKET_DATA_DB")
            if database_path is None:
                from open_stock_ai.agent_runtime.runtime_paths import AgentRuntimePaths

                database_path = AgentRuntimePaths.discover().database
        self.warehouse = MarketDataWarehouse(database_path)
        self.entity_registry = EntityRegistry(self.warehouse)
        self._last_identity_reconciliation = (
            self.warehouse.reconcile_entity_registry_aliases()
        )
        self.code_version = code_version or os.getenv("STOCK_AI_BUILD_COMMIT", "working-copy")
        self.source_registry_service = SourceRegistry(
            source_catalog_path
            or Path(__file__).resolve().parents[3] / "config" / "market_data_sources.yaml"
        )
        self.source_catalog_path = self.source_registry_service.path
        self.sources = self.source_registry_service.sources
        self.source_datasets = self.source_registry_service.datasets
        self.cache_policies = self.source_registry_service.cache_policies
        self.cache_policy_service = CachePolicyService(
            self.warehouse,
            policies=self.cache_policies,
            source_update_frequencies={
                source_id: source.update_frequency_seconds
                for source_id, source in self.sources.items()
            },
        )
        self.data_quality_service = DataQualityService(
            self.warehouse,
            **({"rules_path": quality_rules_path} if quality_rules_path else {}),
        )
        self.reconciliation_engine = ReconciliationEngine(
            self.warehouse,
            **(
                {"rules_path": reconciliation_rules_path}
                if reconciliation_rules_path
                else {}
            ),
        )
        if observability_notification_sender is None:
            from ..operator_notifications import configured_operator_notification_sender

            observability_notification_sender = configured_operator_notification_sender()
        self.source_observability_service = SourceObservabilityService(
            self.warehouse,
            self.source_registry_service,
            **({"rules_path": observability_rules_path} if observability_rules_path else {}),
            notification_sender=observability_notification_sender,
        )
        self._security_master_snapshot_hash: str | None = None
        for source in self.sources.values():
            self.warehouse.register_source(source)
        self.source_failover_service = SourceFailoverService(self)

    def source_registry(self) -> dict[str, Any]:
        registry = self.source_registry_service.as_dict()
        registry["items"] = self.warehouse.list_sources()
        return registry

    def resolve_entity(
        self,
        identifier: str,
        *,
        source_id: str | None = None,
        identifier_type: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        return self.entity_registry.resolve(
            identifier,
            source_id=source_id,
            identifier_type=identifier_type,
            as_of=as_of,
        )

    def cache_ttl(self, dataset: str, *, source_id: str | None = None) -> int:
        return self.cache_policy_service.policy_for(
            dataset,
            source_id=source_id,
        ).ttl_seconds

    def cache_status(
        self,
        *,
        dataset: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        return self.cache_policy_service.status(dataset=dataset, as_of=as_of)

    def invalidate_cache(
        self,
        *,
        dataset: str,
        reason: str,
        source_id: str | None = None,
        partition_key: str | None = None,
        invalidated_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.cache_policy_service.invalidate(
            dataset=dataset,
            reason=reason,
            source_id=source_id,
            partition_key=partition_key,
            invalidated_at=invalidated_at,
            metadata=metadata,
        )

    def sync_security_master_payloads(
        self,
        *,
        twse_companies: list[dict[str, Any]] | None = None,
        twse_quotes: list[dict[str, Any]] | None = None,
        tpex_companies: list[dict[str, Any]] | None = None,
        tpex_quotes: list[dict[str, Any]] | None = None,
        tpex_emerging_companies: list[dict[str, Any]] | None = None,
        tpex_emerging_quotes: list[dict[str, Any]] | None = None,
        twse_etfs: list[dict[str, Any]] | None = None,
        twse_warrants: list[dict[str, Any]] | None = None,
        tpex_warrants: list[dict[str, Any]] | None = None,
        twse_indices: list[dict[str, Any]] | None = None,
        tpex_indices: list[dict[str, Any]] | None = None,
        twse_delisted: list[dict[str, Any]] | None = None,
        tpex_delisted: list[dict[str, Any]] | None = None,
        acquired_at: str | None = None,
    ) -> dict[str, Any]:
        acquired = normalize_timestamp(acquired_at or utc_now(), required=True)
        assert acquired is not None
        payloads = {
            name: rows
            for name, rows in {
                "twse_companies": twse_companies,
                "twse_quotes": twse_quotes,
                "tpex_companies": tpex_companies,
                "tpex_quotes": tpex_quotes,
                "tpex_emerging_companies": tpex_emerging_companies,
                "tpex_emerging_quotes": tpex_emerging_quotes,
                "twse_etfs": twse_etfs,
                "twse_warrants": twse_warrants,
                "tpex_warrants": tpex_warrants,
                "twse_indices": twse_indices,
                "tpex_indices": tpex_indices,
                "twse_delisted": twse_delisted,
                "tpex_delisted": tpex_delisted,
            }.items()
            if rows is not None
        }
        if not payloads:
            raise ValueError("At least one official security-master payload is required")
        from .catalogue_identity import retained_catalogue_issue_bindings
        from zoneinfo import ZoneInfo

        issue_context = (
            retained_catalogue_issue_bindings(self.warehouse, as_of=acquired)
            if any(rows for dataset, rows in payloads.items()
                   if dataset not in {"twse_indices", "tpex_indices", "twse_delisted", "tpex_delisted"})
            else {"bindings": {}, "partitions": []}
        )
        snapshot_hash = content_hash({
            "payloads": payloads,
            "issuance_sources": [{key: value for key, value in partition.items()
                                  if key not in {"acquisition_age_seconds", "source_age_calendar_days"}}
                                 for partition in issue_context["partitions"]],
            "market_day": datetime.fromisoformat(acquired).astimezone(ZoneInfo("Asia/Taipei")).date().isoformat(),
        })
        if snapshot_hash == self._security_master_snapshot_hash:
            stored = self.securities(limit=100000)
            by_exchange: dict[str, int] = {}
            for item in stored:
                exchange = str(item.get("exchange") or "unknown")
                by_exchange[exchange] = by_exchange.get(exchange, 0) + 1
            return {
                "schema_version": "stock_ai.security_master_sync.v2",
                "status": "unchanged",
                "acquired_at": acquired,
                "count": len(stored),
                "by_exchange": by_exchange,
                "lifecycle": self.warehouse.lifecycle_summary(),
                "revision_count": 0,
                "revision_ids": [],
                "snapshot_hash": snapshot_hash,
            }
        raw_groups: dict[str, tuple[str, str, Any]] = {}
        if "twse_companies" in payloads or "twse_quotes" in payloads:
            raw_groups["twse_companies_quotes"] = (
                "twse_openapi",
                self.source_registry_service.endpoint("twse_companies"),
                {
                    "companies": payloads.get("twse_companies", []),
                    "quotes": payloads.get("twse_quotes", []),
                },
            )
        if "tpex_companies" in payloads or "tpex_quotes" in payloads:
            raw_groups["tpex_otc_companies_quotes"] = (
                "tpex_openapi",
                self.source_registry_service.endpoint("tpex_companies"),
                {
                    "companies": payloads.get("tpex_companies", []),
                    "quotes": payloads.get("tpex_quotes", []),
                },
            )
        if "tpex_emerging_companies" in payloads or "tpex_emerging_quotes" in payloads:
            raw_groups["tpex_emerging_companies_quotes"] = (
                "tpex_openapi",
                self.source_registry_service.endpoint("tpex_emerging_companies"),
                {
                    "companies": payloads.get("tpex_emerging_companies", []),
                    "quotes": payloads.get("tpex_emerging_quotes", []),
                },
            )
        dataset_sources = {
            "twse_etfs": ("twse_openapi", self.source_registry_service.endpoint("twse_etfs")),
            "twse_warrants": ("twse_openapi", self.source_registry_service.endpoint("twse_warrants")),
            "tpex_warrants": ("tpex_openapi", self.source_registry_service.endpoint("tpex_warrants")),
            "twse_indices": ("twse_openapi", self.source_registry_service.endpoint("twse_indices")),
            "tpex_indices": (
                "tpex_openapi",
                self.source_registry_service.endpoint("tpex_index", path="tpex_index"),
            ),
            "twse_delisted": ("twse_openapi", self.source_registry_service.endpoint("twse_delisted")),
            "tpex_delisted": ("tpex_official_web", self.source_registry_service.endpoint("tpex_delisted")),
        }
        for dataset, (source_id, request_url) in dataset_sources.items():
            if dataset in payloads:
                raw_groups[dataset] = (source_id, request_url, payloads[dataset])
        raw_payload_ids: dict[str, str] = {}
        for dataset, (source_id, request_url, raw_payload) in raw_groups.items():
            raw_payload_ids[dataset] = self.warehouse.record_raw_payload(
                source_id=source_id,
                payload=raw_payload,
                request_url=request_url,
                requested_at=acquired,
                received_at=acquired,
                metadata={
                    "dataset": "security_master",
                    "source_dataset": dataset,
                    "snapshot_kind": "complete_current_or_official_history",
                },
            )
        snapshots = normalize_security_payloads(payloads, acquired_at=acquired,
                                               issue_bindings=issue_context["bindings"])
        inventory = self.warehouse.identity_inventory()
        normalized = consolidate_security_snapshots(
            snapshots,
            acquired_at=acquired,
            existing_inventory=inventory,
        )
        # The official warrant endpoint is a historical archive, while the
        # daily ISIN catalogue describes current issues. An expired archive
        # row that is absent from the current catalogue cannot be assigned a
        # stable issuance identity because codes are reused. Retain that row
        # and its gap, but do not fail the current-market baseline. Missing or
        # conflicting proof for active/future warrants remains blocking.
        snapshot_index: dict[tuple[str, str, str], list[Any]] = {}
        for snapshot in snapshots:
            snapshot_index.setdefault(
                (snapshot.source_dataset, snapshot.venue, snapshot.code), []
            ).append(snapshot)
        historical_unresolved: list[dict[str, Any]] = []
        blocking_unresolved: list[dict[str, Any]] = []
        for item in normalized["unresolved"]:
            observations = snapshot_index.get(
                (str(item.get("source_dataset")), str(item.get("venue")), str(item.get("code"))),
                [],
            )
            is_unbound_expired_warrant = (
                item.get("reason") == "verified_warrant_issuance_unavailable"
                and bool(observations)
                and all(
                    observation.entity_type == "warrant" and observation.lifecycle_status == "expired"
                    for observation in observations
                )
            )
            (historical_unresolved if is_unbound_expired_warrant else blocking_unresolved).append(item)
        # Catalogues are captured before lifecycle refresh. A newly created
        # non-warrant listing must receive its existing exact row in this same
        # pass; waiting for the next download would leave it unclassified for
        # an entire cache interval. Existing owners retain their overlay.
        existing_ids = {item["entity_id"] for item in inventory}
        for entity in normalized["entities"]:
            if entity.entity_id in existing_ids or entity.entity_type == "warrant":
                continue
            binding = issue_context["bindings"].get((entity.exchange, entity.metadata.get("source_code")))
            if binding and binding["eligible_as_of"]:
                entity.metadata["product_classifications"] = {
                    f"{entity.exchange}:{binding['symbol']}": binding["product_classification"],
                }
        batch = self.warehouse.write_security_lifecycle_batch(
            entities=normalized["entities"],
            identifiers=normalized["identifiers"],
            revisions=normalized["revisions"],
            events=normalized["events"],
            raw_payload_ids=raw_payload_ids,
            acquired_at=acquired,
            code_version=self.code_version,
        )
        self._last_identity_reconciliation = (
            self.warehouse.reconcile_entity_registry_aliases(
                effective_at=acquired,
            )
        )
        counts: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for entity in normalized["entities"]:
            counts[entity.exchange or "unknown"] = counts.get(entity.exchange or "unknown", 0) + 1
            by_type[entity.entity_type] = by_type.get(entity.entity_type, 0) + 1
            by_status[entity.lifecycle_status] = by_status.get(entity.lifecycle_status, 0) + 1
        for dataset, (source_id, _request_url, raw_payload) in raw_groups.items():
            unresolved = [item for item in normalized["unresolved"] if item["source_dataset"] == dataset]
            self.warehouse.save_checkpoint(
                source_id=source_id,
                dataset="security_master",
                partition_key=dataset,
                cursor_value=content_hash(raw_payload),
                status="partial" if unresolved else "succeeded",
                metadata={
                    "row_count": len(raw_payload) if isinstance(raw_payload, list) else None,
                    "entity_count": len(normalized["entities"]),
                    "raw_payload_id": raw_payload_ids[dataset],
                    "unresolved_identity_count": len(unresolved),
                    "unresolved_identities": unresolved,
                    "issuance_sources": issue_context["partitions"] if "warrant" in dataset else [],
                },
            )
        if not blocking_unresolved:
            self._security_master_snapshot_hash = snapshot_hash
        sync_status = (
            "partial"
            if blocking_unresolved
            else "succeeded_with_historical_gaps"
            if historical_unresolved
            else "succeeded"
        )
        return {
            "schema_version": "stock_ai.security_master_sync.v2",
            "status": sync_status,
            "acquired_at": acquired,
            "count": len(normalized["entities"]),
            "source_observation_count": len(snapshots),
            "raw_payload_ids": raw_payload_ids,
            "by_exchange": counts,
            "by_entity_type": by_type,
            "by_status": by_status,
            "lifecycle": self.warehouse.lifecycle_summary(),
            "revision_count": batch["created_revision_count"],
            "reused_revision_count": batch["reused_revision_count"],
            "revision_ids": batch["revision_ids"],
            "created_event_count": batch["created_event_count"],
            "entity_registry_reconciliation": self._last_identity_reconciliation,
            "snapshot_hash": snapshot_hash,
            "identity_resolution": {
                "resolved_warrant_count": by_type.get("warrant", 0),
                "current_baseline_complete": not blocking_unresolved,
                "unresolved_count": len(blocking_unresolved),
                "unresolved": blocking_unresolved,
                "historical_unresolved_count": len(historical_unresolved),
                "historical_unresolved": historical_unresolved,
                "total_unresolved_count": len(normalized["unresolved"]),
                "catalogue_partitions": issue_context["partitions"],
            },
        }

    def sync_product_classifications(
        self,
        receipts: Iterable[dict[str, Any]],
        *,
        acquired_at: str,
        complete_source_datasets: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return self.warehouse.sync_product_classifications(
            receipts,
            acquired_at=acquired_at,
            complete_source_datasets=complete_source_datasets,
        )

    def resolve_product_classification(
        self, *, symbol: str, market: str, now: datetime
    ) -> dict[str, Any]:
        return self.warehouse.resolve_product_classification(
            symbol=symbol, market=market, now=now
        )

    def securities(
        self,
        *,
        query: str = "",
        market: str = "all",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        market_filter = market.strip().casefold()
        type_filter = market_filter if market_filter in {"stock", "etf", "warrant", "index"} else None
        lifecycle_filter = "delisted" if market_filter == "delisted" else None
        rows = self.warehouse.list_entities(
            query=query,
            market="taiwan"
            if market_filter
            in {
                "all",
                "taiwan",
                "twse",
                "listed",
                "tpex",
                "otc",
                "emerging",
                "delisted",
                "stock",
                "etf",
                "warrant",
                "index",
            }
            else market,
            exchange={
                "twse": "TWSE", "listed": "TWSE",
                "tpex": "TPEx", "otc": "TPEx", "emerging": "TPEx-ESB",
            }.get(market_filter),
            entity_type=type_filter,
            lifecycle_status=lifecycle_filter,
            limit=limit,
        )
        product_resolutions = self.warehouse.product_classifications_for_listings(
            listings=(
                (
                    str(row.get("exchange") or ""),
                    str(row.get("display_symbol") or (row.get("metadata") or {}).get("display_symbol") or ""),
                )
                for row in rows
            ),
            now=datetime.now(timezone.utc),
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            if row["market"].casefold() != "taiwan":
                continue
            if market_filter in {"twse", "listed"} and row["exchange"] != "TWSE":
                continue
            if market_filter in {"tpex", "otc"} and row["exchange"] != "TPEx":
                continue
            if market_filter == "emerging" and row["exchange"] != "TPEx-ESB":
                continue
            metadata = row.get("metadata") or {}
            identifier_resolved = bool(row.get("display_symbol"))
            # Keep an unresolved entity visible for research by its stored
            # label, but never turn that label into an active listing again.
            symbol = str(row.get("display_symbol") or metadata.get("display_symbol") or "")
            product = product_resolutions[(str(row.get("exchange") or ""), str(symbol or ""))]
            if not identifier_resolved:
                product = self.warehouse._unknown_product_resolution(
                    symbol, "listing_identifier_unavailable_or_ambiguous"
                )
            elif product["entity_id"] not in {None, row["entity_id"]}:
                product = self.warehouse._unknown_product_resolution(
                    str(symbol or ""), "product_listing_entity_mismatch", status="conflict"
                )
            items.append(
                {
                    "entity_id": row["entity_id"],
                    "symbol": symbol,
                    "name": metadata.get("short_name") or row["canonical_name"],
                    "legal_name": row["canonical_name"],
                    "market": row["market"],
                    "exchange": row["exchange"],
                    "listing_type": metadata.get("listing_type")
                    or (
                        "listed"
                        if row["exchange"] == "TWSE"
                        else "otc"
                        if row["exchange"] == "TPEx"
                        else "emerging"
                        if row["exchange"] == "TPEx-ESB"
                        else "other"
                    ),
                    "entity_type": row["entity_type"],
                    "lifecycle_status": row["lifecycle_status"],
                    "listing_identifier_status": (
                        "resolved" if identifier_resolved else "unavailable_or_ambiguous"
                    ),
                    "industry": row["industry"],
                    "trade_unit": 1000,
                    "day_trade_eligible": None,
                    "margin_eligible": None,
                    "short_eligible": None,
                    "is_etf": row["entity_type"] == "etf",
                    "is_warrant": row["entity_type"] == "warrant",
                    "product_classification": {
                        **product["classification"],
                        "entity_id": product["entity_id"],
                        "lifecycle_status": product["lifecycle_status"],
                    },
                    "list_date": row["listed_at"],
                    "delist_date": row["delisted_at"],
                    "venue_history": metadata.get("venue_history") or [],
                    "trading_status": (
                        "unknown" if not identifier_resolved else "listed_pending_quote"
                        if row["lifecycle_status"] == "pre_listing"
                        else row["lifecycle_status"]
                    ),
                    "source": "Unified Market Warehouse security_master",
                }
            )
        return items

    def ingest_records(
        self,
        *,
        source_id: str,
        dataset: str,
        records: Iterable[dict[str, Any]],
        entity_id_for: Callable[[dict[str, Any]], str],
        observation_key_for: Callable[[dict[str, Any]], str],
        available_at_for: Callable[[dict[str, Any]], str | None],
        effective_at_for: Callable[[dict[str, Any]], str | None],
        time_basis: str = "snapshot",
        trade_date_for: Callable[[dict[str, Any]], str | None] | None = None,
        fiscal_period_for: Callable[[dict[str, Any]], str | None] | None = None,
        period_start_for: Callable[[dict[str, Any]], str | None] | None = None,
        period_end_for: Callable[[dict[str, Any]], str | None] | None = None,
        observed_at_for: Callable[[dict[str, Any]], str | None] | None = None,
        published_at_for: Callable[[dict[str, Any]], str | None] | None = None,
        request_url: str | None = None,
        raw_response: bytes | str | None = None,
        raw_content_type: str = "application/json",
        raw_content_encoding: str = "utf-8",
        raw_parser_id: str | None = None,
        is_fallback: bool = False,
        transformation_id: str = "stock_ai.record_normalizer.v1",
        acquired_at: str | None = None,
        field_provenance_for: Callable[
            [dict[str, Any], TemporalCoordinates, str],
            dict[str, Any],
        ]
        | None = None,
    ) -> list[DataEnvelopeV2]:
        record_list = [dict(item) for item in records]
        acquired = normalize_timestamp(acquired_at or utc_now(), required=True)
        assert acquired is not None
        raw_payload_id = self.warehouse.record_raw_payload(
            source_id=source_id,
            payload=record_list,
            identity_payload=(
                [
                    revision_identity_payload(dataset=dataset, payload=record)
                    for record in record_list
                ]
                if raw_response is None
                else None
            ),
            request_url=request_url or self.sources[source_id].base_url,
            requested_at=acquired,
            received_at=acquired,
            content_type=raw_content_type,
            content_encoding=raw_content_encoding,
            raw_body=raw_response,
            parser_id=raw_parser_id,
            metadata={
                "dataset": dataset,
                "record_count": len(record_list),
                "transformation_id": transformation_id,
            },
        )
        envelopes: list[DataEnvelopeV2] = []
        try:
            for record in record_list:
                available = normalize_timestamp(available_at_for(record) or acquired, required=True)
                effective = normalize_timestamp(effective_at_for(record) or available, required=True)
                observed = normalize_timestamp(observed_at_for(record)) if observed_at_for else None
                published = normalize_timestamp(published_at_for(record)) if published_at_for else None
                assert available is not None and effective is not None
                temporal = TemporalCoordinates(
                    time_basis=time_basis,
                    trade_date=trade_date_for(record) if trade_date_for else None,
                    fiscal_period=(
                        fiscal_period_for(record) if fiscal_period_for else None
                    ),
                    period_start=period_start_for(record) if period_start_for else None,
                    period_end=period_end_for(record) if period_end_for else None,
                    observed_at=observed,
                    published_at=published,
                    available_at=available,
                    acquired_at=acquired,
                    effective_at=effective,
                )
                envelopes.append(
                    self.warehouse.write_revision(
                        dataset=dataset,
                        entity_id=entity_id_for(record),
                        observation_key=observation_key_for(record),
                        source_id=source_id,
                        temporal=temporal,
                        payload=record,
                        raw_payload_id=raw_payload_id,
                        quality_status="valid",
                        quality_flags=[],
                        is_fallback=is_fallback,
                        transformation_id=transformation_id,
                        code_version=self.code_version,
                        field_provenance=(
                            field_provenance_for(
                                record,
                                temporal,
                                raw_payload_id,
                            )
                            if field_provenance_for
                            else None
                        ),
                    )
                )
        except Exception as exc:
            self.warehouse.save_checkpoint(
                source_id=source_id,
                dataset=dataset,
                partition_key="all",
                status="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
                metadata={"raw_payload_id": raw_payload_id},
            )
            raise
        self.warehouse.save_checkpoint(
            source_id=source_id,
            dataset=dataset,
            partition_key="all",
            cursor_value=acquired,
            status="succeeded",
            metadata={"raw_payload_id": raw_payload_id, "record_count": len(record_list)},
        )
        return envelopes

    def query(
        self,
        *,
        dataset: str,
        entity_id: str | None = None,
        as_of: str | None = None,
        knowledge_at: str | None = None,
        effective_at: str | None = None,
        source_id: str | None = None,
        limit: int = 500,
    ) -> list[DataEnvelopeV2]:
        return self.warehouse.query(
            DataQuery(
                dataset=dataset,
                entity_id=entity_id,
                as_of=as_of,
                knowledge_at=knowledge_at,
                effective_at=effective_at,
                source_id=source_id,
                limit=limit,
            )
        )

    def standard_query(
        self,
        domain: str,
        *,
        dataset: str | None = None,
        entity_id: str | None = None,
        knowledge_at: str | None = None,
        effective_at: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return self.warehouse.standard_records(
            domain,
            dataset=dataset,
            entity_id=entity_id,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=limit,
        )

    def revision_history(
        self,
        *,
        dataset: str,
        entity_id: str,
        observation_key: str,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        return self.warehouse.revision_history(
            dataset=dataset,
            entity_id=entity_id,
            observation_key=observation_key,
            source_id=source_id,
        )

    def create_revision_snapshot(
        self,
        *,
        dataset: str,
        knowledge_at: str | None = None,
        effective_at: str | None = None,
        entity_id: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        return self.warehouse.create_revision_snapshot(
            dataset=dataset,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            entity_id=entity_id,
            source_id=source_id,
        )

    def daily_quality_report(
        self,
        *,
        dataset: str,
        partition_key: str = "all",
        report_date: str | None = None,
        knowledge_at: str | None = None,
    ) -> dict[str, Any]:
        return self.data_quality_service.run_daily_report(
            dataset,
            partition_key=partition_key,
            report_date=report_date,
            knowledge_at=knowledge_at,
        )

    def run_daily_quality_reports(
        self,
        *,
        datasets: Iterable[str] | None = None,
        report_date: str | None = None,
        knowledge_at: str | None = None,
    ) -> dict[str, Any]:
        return self.data_quality_service.run_daily_reports(
            datasets=datasets,
            report_date=report_date,
            knowledge_at=knowledge_at,
        )

    def quality_reports(
        self,
        *,
        dataset: str | None = None,
        report_date: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.data_quality_service.list_reports(
            dataset=dataset,
            report_date=report_date,
            limit=limit,
        )

    def quality_report_detail(self, report_id: str) -> dict[str, Any] | None:
        return self.data_quality_service.report(report_id)

    def source_failover_status(self) -> dict[str, Any]:
        return self.source_failover_service.status()

    def source_failover_runs(self, *, limit: int = 100) -> dict[str, Any]:
        return self.source_failover_service.runs(limit=limit)

    def source_failover_run(self, run_id: str) -> dict[str, Any] | None:
        return self.source_failover_service.run(run_id)

    def source_observability(self, *, as_of: str | None = None) -> dict[str, Any]:
        return self.source_observability_service.dashboard(as_of=as_of)

    def notify_source_observability(self, *, as_of: str | None = None) -> dict[str, Any]:
        return self.source_observability_service.notify(as_of=as_of)

    def reconciliation_status(self) -> dict[str, Any]:
        return self.reconciliation_engine.status()

    def reconciliation_runs(self, *, limit: int = 100) -> dict[str, Any]:
        return self.reconciliation_engine.runs(limit=limit)

    def reconciliation_run(self, run_id: str) -> dict[str, Any] | None:
        return self.reconciliation_engine.run_detail(run_id)

    def reconciliation_conflicts(
        self,
        *,
        dataset: str | None = None,
        status: str | None = "open",
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.reconciliation_engine.conflicts(
            dataset=dataset,
            status=status,
            limit=limit,
        )

    def reconcile_sources(self, **kwargs: Any) -> dict[str, Any]:
        return self.reconciliation_engine.run(**kwargs)

    def ingest_with_failover(self, **kwargs: Any) -> dict[str, Any]:
        return self.source_failover_service.ingest(**kwargs)

    def preferred_query(
        self,
        *,
        dataset: str,
        entity_id: str,
        as_of: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        rows = self.query(
            dataset=dataset,
            entity_id=entity_id,
            as_of=as_of,
            limit=limit,
        )
        candidates = [
            (
                self.sources[row.source_id].priority,
                1 if row.is_fallback else 0,
                row,
            )
            for row in rows
            if row.source_id in self.sources and self.sources[row.source_id].active
        ]
        candidates.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[2].temporal.available_at,
            )
        )
        if not candidates:
            return {
                "schema_version": "stock_ai.preferred_data.v1",
                "status": "unavailable",
                "dataset": dataset,
                "entity_id": entity_id,
                "data": [],
                "source_id": None,
                "fallback_used": False,
            }
        selected_priority, selected_fallback_rank, _ = candidates[0]
        selected = [
            row
            for priority, fallback_rank, row in candidates
            if (priority, fallback_rank)
            == (selected_priority, selected_fallback_rank)
        ]
        fallback_used = bool(selected_fallback_rank)
        return {
            "schema_version": "stock_ai.preferred_data.v1",
            "status": "available",
            "dataset": dataset,
            "entity_id": entity_id,
            "data": [row.model_dump(mode="json") for row in selected],
            "source_id": selected[0].source_id,
            "fallback_used": fallback_used,
            "source_difference_preserved": True,
        }

    def status(self, *, operations_summary: bool = False) -> dict[str, Any]:
        warehouse_status = self.warehouse.status(
            operations_summary=operations_summary,
        )
        warehouse_status["cache_policy"] = self.cache_status()
        warehouse_status["reconciliation"] = self.reconciliation_status()
        warehouse_status["source_observability"] = self.source_observability()
        return {
            "schema_version": "stock_ai.market_data_platform_status.v1",
            "single_read_path": "MarketDataPlatform",
            "unified_data_api": unified_data_api_contract(),
            "source_registry": (
                {
                    "schema_version": "stock_ai.source_registry_summary.v1",
                    "count": len(self.sources),
                    "dataset_count": len(self.source_datasets),
                }
                if operations_summary
                else self.source_registry()
            ),
            "entity_registry": self.entity_registry.status(),
            "entity_registry_reconciliation": self._last_identity_reconciliation,
            "warehouse": warehouse_status,
            "security_lifecycle": {
                **self.warehouse.lifecycle_summary(),
                "quality": self.warehouse.security_lifecycle_quality(),
            },
        }


_PLATFORM_INIT_LOCK = RLock()


@lru_cache(maxsize=1)
def _cached_market_data_platform() -> MarketDataPlatform:
    return MarketDataPlatform()


def get_market_data_platform() -> MarketDataPlatform:
    """Return one process-wide platform without concurrent cache-miss construction."""

    with _PLATFORM_INIT_LOCK:
        return _cached_market_data_platform()


get_market_data_platform.cache_clear = _cached_market_data_platform.cache_clear  # type: ignore[attr-defined]
