from __future__ import annotations

from collections.abc import Iterable
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import wraps
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5
import csv
import io
import json
import sqlite3
import time

from open_stock_ai.storage.migrations import ManagedSQLiteConnection, apply_migrations, configure_connection

from .availability import get_data_availability_registry
from .contracts import (
    DataEnvelopeV2,
    DataQuery,
    EntityIdentifierRecord,
    EntityRecord,
    FieldProvenanceV1,
    SourceDefinition,
    TemporalCoordinates,
    normalize_identifier_value,
    normalize_timestamp,
    payload_leaf_pointers,
    utc_now,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


_PRODUCT_SOURCE_VENUES = {
    "twse_isin_listed": "TWSE",
    "tpex_isin_otc": "TPEx",
    "tpex_isin_emerging": "TPEx-ESB",
}
_PRODUCT_TYPES = frozenset({
    "ordinary_stock", "etf", "etn", "depositary_receipt", "preferred_stock",
    "warrant", "bond", "other", "unknown",
})
_SEARCH_IDENTIFIER_TYPES = (
    "display_symbol",
    "exchange_code",
    "unified_business_no",
    "source_symbol",
    "isin",
    "figi",
    "lei",
    "legal_name",
)


_VOLATILE_REVISION_FIELDS = frozenset({"acquired_at", "available_at"})


def revision_identity_payload(*, dataset: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return the business content that determines whether a revision is new.

    Acquisition timestamps are stored in the temporal columns and describe when
    this installation fetched a record; they are not a change to the record
    itself.  Security-master raw rows also carry live quote fields, while the
    normalized payload already contains the lifecycle contract.  Keeping these
    values in ``payload_json`` preserves provenance, but excluding them from the
    digest prevents every refresh from creating a duplicate revision.
    """

    identity = {
        key: value
        for key, value in payload.items()
        if key not in _VOLATILE_REVISION_FIELDS
    }
    if dataset == "security_master":
        identity.pop("raw_row", None)
        metadata = dict(identity.get("metadata") or {})
        issue = metadata.get("issuance_identity")
        if isinstance(issue, dict):
            # Fresh capture receipts attest the same issuance without changing
            # its business identity. Keep the complete proof in payload_json,
            # while the next raw capture/checkpoint retains refresh history.
            metadata["issuance_identity"] = {
                key: issue.get(key) for key in (
                    "schema_version", "venue", "code", "symbol", "isin",
                    "source_name", "product_type", "market_segment",
                    "listed_on", "listed_at", "date_precision",
                )
            }
            identity["metadata"] = metadata
    return identity


def revision_content_hash(*, dataset: str, payload: dict[str, Any]) -> str:
    return content_hash(revision_identity_payload(dataset=dataset, payload=payload))


def availability_contract_snapshot(
    *,
    source_id: str,
    dataset: str,
    temporal: TemporalCoordinates,
) -> dict[str, Any]:
    """Freeze the reviewed availability decision for one revision.

    Do not rewrite the source temporal envelope here. Historical imports can
    contain internally inconsistent acquisition metadata, which remains part
    of their immutable provenance. Exact PIT reads the recorded decision in
    this snapshot and separately requires local acquisition knowledge.
    """

    snapshot = get_data_availability_registry().resolve(
        source_id=source_id,
        dataset=dataset,
    ).snapshot(
        observed_at=temporal.observed_at,
        published_at=temporal.published_at,
        acquired_at=temporal.acquired_at,
    )
    return snapshot


STANDARD_WAREHOUSE_TABLES: dict[str, str] = {
    "prices": "market_prices",
    "financials": "financial_facts",
    "flows": "ownership_flows",
    "events": "market_events",
    "macro": "macro_observations",
}

_WAREHOUSE_WRITE_LOCKS_GUARD = RLock()
_WAREHOUSE_WRITE_LOCKS: dict[Path, RLock] = {}


def _warehouse_write_lock(path: Path) -> RLock:
    with _WAREHOUSE_WRITE_LOCKS_GUARD:
        return _WAREHOUSE_WRITE_LOCKS.setdefault(path, RLock())


def serialized_warehouse_write(method):
    """Serialize and briefly retry writers that target the same SQLite file.

    The UI can request several data sets concurrently.  Most writers are
    serialized in this process, but SQLite may still briefly be locked by a
    just-finished WAL transaction or a second trusted desktop process.  A
    failed transaction is rolled back by ``ManagedSQLiteConnection`` before
    the next attempt, so retrying here keeps a transient persistence conflict
    from becoming a user-visible 500 response.
    """

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._write_lock:
            for attempt in range(3):
                try:
                    return method(self, *args, **kwargs)
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).casefold() or attempt == 2:
                        raise
                    time.sleep(0.2 * (attempt + 1))

    return wrapped


STANDARD_WAREHOUSE_DATASETS: dict[str, frozenset[str]] = {
    "prices": frozenset(
        {"prices_daily", "prices_intraday", "prices_adjusted_daily"}
    ),
    "financials": frozenset(
        {"fundamentals", "fundamentals_quarterly", "revenues_monthly", "valuation_metrics"}
    ),
    "flows": frozenset(
        {
            "institutional_flows",
            "margin_trading",
            "borrowed_short",
            "ownership",
            "tdcc_holding_distribution",
            "derivatives",
            "taifex_derivatives",
        }
    ),
    "events": frozenset(
        {
            "events",
            "company_events",
            "documents",
            "event_impacts",
            "price_adjustment_events",
        }
    ),
    "macro": frozenset({"macro", "macro_series"}),
}


def standard_warehouse_domain(dataset: str) -> str | None:
    return next(
        (
            domain
            for domain, datasets in STANDARD_WAREHOUSE_DATASETS.items()
            if dataset in datasets
        ),
        None,
    )


def build_field_provenance(
    *,
    payload: dict[str, Any],
    source_id: str,
    temporal: TemporalCoordinates,
    updated_at: str,
    raw_payload_id: str | None,
    quality_status: str,
    quality_flags: list[str],
    transformation_id: str,
    overrides: dict[str, FieldProvenanceV1 | dict[str, Any]] | None = None,
) -> dict[str, FieldProvenanceV1]:
    result = {
        pointer: FieldProvenanceV1(
            source_id=source_id,
            temporal=temporal,
            updated_at=updated_at,
            raw_payload_id=raw_payload_id,
            raw_json_pointer=pointer,
            quality_status=quality_status,
            quality_flags=quality_flags,
            transformation_id=transformation_id,
            input_fields=[pointer],
        )
        for pointer in payload_leaf_pointers(payload)
    }
    for pointer, item in (overrides or {}).items():
        result[pointer] = (
            item
            if isinstance(item, FieldProvenanceV1)
            else FieldProvenanceV1.model_validate(item)
        )
    return result


class MarketDataWarehouse:
    """SQLite-backed raw, normalized, revision and lineage source of truth."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = _warehouse_write_lock(self.path)
        self._product_proof_lock = RLock()
        self._product_proof_connection: sqlite3.Connection | None = None
        self._product_proof_file_identity: tuple[int, int] | None = None
        self._product_proof_cache: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        with self._write_lock:
            with self._connect() as conn:
                apply_migrations(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, factory=ManagedSQLiteConnection)
        configure_connection(conn)
        conn.row_factory = sqlite3.Row
        return conn

    @serialized_warehouse_write
    def register_source(self, source: SourceDefinition) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into data_sources (
                    source_id, display_name, authority, base_url, license_status,
                    update_frequency_seconds, reliability_tier, priority,
                    domains_json, failover_source_ids_json, field_contract_json,
                    active, registered_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id) do update set
                    display_name=excluded.display_name,
                    authority=excluded.authority,
                    base_url=excluded.base_url,
                    license_status=excluded.license_status,
                    update_frequency_seconds=excluded.update_frequency_seconds,
                    reliability_tier=excluded.reliability_tier,
                    priority=excluded.priority,
                    domains_json=excluded.domains_json,
                    failover_source_ids_json=excluded.failover_source_ids_json,
                    field_contract_json=excluded.field_contract_json,
                    active=excluded.active,
                    updated_at=excluded.updated_at
                """,
                (
                    source.source_id,
                    source.display_name,
                    source.authority,
                    source.base_url,
                    source.license_status,
                    source.update_frequency_seconds,
                    source.reliability_tier,
                    source.priority,
                    canonical_json(source.domains),
                    canonical_json(source.failover_source_ids),
                    canonical_json(source.field_contract),
                    1 if source.active else 0,
                    now,
                    now,
                ),
            )
            conn.commit()

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select * from data_sources order by priority, source_id"
            ).fetchall()
        return [
            {
                **dict(row),
                "domains": json.loads(row["domains_json"]),
                "failover_source_ids": json.loads(row["failover_source_ids_json"]),
                "field_contract": json.loads(row["field_contract_json"]),
                "active": bool(row["active"]),
            }
            for row in rows
        ]

    @staticmethod
    def _preserve_product_classifications(
        conn: sqlite3.Connection, entity: EntityRecord
    ) -> dict[str, Any]:
        """Lifecycle writers cannot replace the independently refreshed catalog."""

        metadata = dict(entity.metadata)
        row = conn.execute(
            "select metadata_json from market_entities where entity_id=?",
            (entity.entity_id,),
        ).fetchone()
        if row is not None:
            current = json.loads(row["metadata_json"])
            if "product_classifications" in current:
                metadata["product_classifications"] = current["product_classifications"]
        return metadata

    @staticmethod
    def _product_listing_rows(
        conn: sqlite3.Connection, *, at: str, symbol: str | None = None
    ) -> list[sqlite3.Row]:
        return conn.execute(
            """
            select distinct e.*, i.identifier_value as listing_symbol
              from market_entities e join entity_identifiers i on i.entity_id=e.entity_id
             where lower(e.market) in ('taiwan', 'tw')
               and e.exchange in ('TWSE', 'TPEx', 'TPEx-ESB')
               and i.identifier_type='display_symbol'
               and (i.valid_from='' or i.valid_from <= ?)
               and (i.valid_to is null or i.valid_to > ?)
               and i.superseded_at is null
               and not exists (
                   select 1 from entity_identity_merges m where m.from_entity_id=e.entity_id
               )
            """ + (" and i.identifier_value=?" if symbol is not None else ""),
            (at, at, symbol) if symbol is not None else (at, at),
        ).fetchall()

    @serialized_warehouse_write
    def sync_product_classifications(
        self,
        receipts: Iterable[dict[str, Any]],
        *,
        acquired_at: str,
        complete_source_datasets: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Overlay exact listing receipts without altering identities or lifecycle."""

        acquired = normalize_timestamp(acquired_at, required=True)
        assert acquired is not None
        if not isinstance(complete_source_datasets, tuple) or any(
            name not in _PRODUCT_SOURCE_VENUES for name in complete_source_datasets
        ):
            raise ValueError("complete_source_datasets must contain known product catalogs")
        grouped: dict[str, list[dict[str, Any]]] = {}
        present_by_dataset: dict[str, set[str]] = {}
        for value in receipts:
            if not isinstance(value, dict):
                raise ValueError("product classification receipt must be a mapping")
            receipt = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
            dataset = receipt.get("source_dataset")
            venue = receipt.get("venue")
            symbol = receipt.get("symbol")
            suffix = ".TW" if venue == "TWSE" else ".TWO"
            if (
                receipt.get("schema_version") != "stock_ai.product_classification.v1"
                or str(receipt.get("status")) not in {"verified", "unknown", "conflict"}
                or str(receipt.get("product_type")) not in _PRODUCT_TYPES
                or receipt.get("source_id") != "twse_isin"
                or not isinstance(dataset, str)
                or dataset not in _PRODUCT_SOURCE_VENUES
                or venue != _PRODUCT_SOURCE_VENUES.get(dataset)
                or not isinstance(symbol, str)
                or symbol != symbol.strip().upper()
                or not symbol.endswith(suffix)
                or not symbol.removesuffix(suffix).isascii()
                or not symbol.removesuffix(suffix).isalnum()
            ):
                raise ValueError("invalid product classification identity or schema")
            if not isinstance(receipt.get("acquired_at"), str):
                raise ValueError("product classification acquired_at is required")
            normalize_timestamp(receipt["acquired_at"], required=True)
            key = f"{venue}:{symbol}"
            bucket = grouped.setdefault(key, [])
            if receipt not in bucket:
                bucket.append(receipt)
            present_by_dataset.setdefault(str(dataset), set()).add(key)
        # An empty or failed response is never evidence of a complete empty market.
        if any(not present_by_dataset.get(name) for name in complete_source_datasets):
            raise ValueError("a complete product catalog must be nonempty")
        catalog_evidence: dict[str, list[dict[str, Any]]] = {}
        for dataset in complete_source_datasets:
            evidence = {
                canonical_json({
                    field: value[field] for field in (
                        "source_id", "source_dataset", "source_url", "source_updated_on",
                        "acquired_at", "raw_sha256", "raw_payload_id",
                    ) if field in value
                })
                for values in grouped.values() for value in values
                if value["source_dataset"] == dataset
            }
            catalog_evidence[dataset] = [json.loads(item) for item in sorted(evidence)]
        updated = 0
        invalidated = 0
        stale = 0
        unmatched: list[str] = []
        ambiguous: list[str] = []
        with self._connect() as conn:
            conn.execute("begin immediate")
            entities: dict[str, dict[str, Any]] = {}
            listing_ids: dict[str, set[str]] = {}
            for row in self._product_listing_rows(conn, at=acquired):
                item = self._entity_row(row)
                entity_id = str(item["entity_id"])
                entities[entity_id] = item
                key = f"{item['exchange']}:{item['listing_symbol']}"
                listing_ids.setdefault(key, set()).add(entity_id)
            changes: dict[str, dict[str, Any]] = {}
            for key, values in grouped.items():
                owners = listing_ids.get(key, set())
                if not owners:
                    unmatched.append(key)
                    continue
                if len(owners) != 1:
                    ambiguous.append(key)
                    continue
                entity_id = next(iter(owners))
                metadata = changes.setdefault(entity_id, dict(entities[entity_id]["metadata"]))
                classifications = dict(metadata.get("product_classifications") or {})
                values.sort(key=canonical_json)
                receipt = values[0]
                if len(values) > 1:
                    receipt = {
                        **receipt,
                        "status": "conflict",
                        "product_type": "unknown",
                        "acquired_at": max(
                            normalize_timestamp(value["acquired_at"], required=True) for value in values
                        ),
                        "reasons": ["conflicting_product_classification_receipts"],
                        "conflicting_receipts": values,
                    }
                old = classifications.get(key)
                if isinstance(old, dict) and old.get("acquired_at") and (
                    normalize_timestamp(old["acquired_at"], required=True)
                    > max(normalize_timestamp(value["acquired_at"], required=True) for value in values)
                ):
                    stale += 1
                    continue
                classifications[key] = receipt
                metadata["product_classifications"] = classifications
                updated += 1
            if complete_source_datasets:
                # Include old bindings even when their listing has since ended.
                for row in conn.execute("select entity_id, metadata_json from market_entities"):
                    entity_id = str(row["entity_id"])
                    metadata = changes.get(entity_id, json.loads(row["metadata_json"]))
                    classifications = dict(metadata.get("product_classifications") or {})
                    changed = False
                    for key, old in list(classifications.items()):
                        if not isinstance(old, dict):
                            continue
                        dataset = old.get("source_dataset")
                        if dataset not in complete_source_datasets or key in present_by_dataset[dataset]:
                            continue
                        if old.get("acquired_at") and normalize_timestamp(old["acquired_at"], required=True) > acquired:
                            stale += 1
                            continue
                        if "absent_from_complete_catalog" in (old.get("reasons") or []):
                            continue
                        classifications[key] = {
                            "schema_version": "stock_ai.product_classification.v1",
                            "status": "unknown",
                            "product_type": "unknown",
                            "symbol": old.get("symbol"),
                            "venue": old.get("venue"),
                            "source_id": "twse_isin",
                            "source_dataset": dataset,
                            "acquired_at": acquired,
                            "reasons": ["absent_from_complete_catalog"],
                            "absence_evidence": catalog_evidence[dataset],
                            "previous_receipt": old,
                        }
                        changed = True
                        invalidated += 1
                    if changed:
                        metadata["product_classifications"] = classifications
                        changes[entity_id] = metadata
            for entity_id, metadata in changes.items():
                conn.execute(
                    "update market_entities set metadata_json=? where entity_id=?",
                    (canonical_json(metadata), entity_id),
                )
        return {
            "status": "succeeded", "updated_count": updated,
            "invalidated_count": invalidated,
            "stale_ignored_count": stale,
            "unmatched_bindings": sorted(unmatched), "ambiguous_bindings": sorted(ambiguous),
            "complete_source_datasets": list(complete_source_datasets),
            "acquired_at": acquired,
        }

    @staticmethod
    def _unknown_product_resolution(
        symbol: str, reason: str, *, status: str = "unknown"
    ) -> dict[str, Any]:
        return {
            "entity_id": None, "lifecycle_status": None,
            "classification": {
                "schema_version": "stock_ai.product_classification.v1",
                "status": status, "product_type": "unknown", "symbol": symbol,
                "venue": None, "reasons": [reason],
            },
        }

    def _retained_product_catalogue(
        self, receipt: dict[str, Any]
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """Verify locally retained bytes; keep at most three parsed catalogues.

        A persistent read-only connection's data_version detects commits made
        by other connections. After a commit we rehash bytes, reusing parsed
        rows only when the immutable hash and capture context still agree.
        """
        from .product_catalog import MAX_CATALOGUE_BYTES, parse_product_catalogue

        raw_id = receipt.get("raw_payload_id")
        if not isinstance(raw_id, str) or not raw_id.startswith("RAW-"):
            raise ValueError("product_raw_payload_missing")
        with self._product_proof_lock:
            stat = self.path.stat()
            file_identity = (stat.st_dev, stat.st_ino)
            if self._product_proof_file_identity != file_identity and self._product_proof_connection is not None:
                self._product_proof_connection.close()
                self._product_proof_connection = None
                self._product_proof_cache.clear()
            if self._product_proof_connection is None:
                conn = sqlite3.connect(
                    self.path.as_uri() + "?mode=ro", uri=True,
                    timeout=10, check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("pragma query_only=on")
                self._product_proof_connection = conn
                self._product_proof_file_identity = file_identity
            conn = self._product_proof_connection
            conn.execute("begin")
            try:
                rows = conn.execute(
                    """
                    select p.raw_payload_id, p.source_id, p.request_url, p.requested_at,
                           p.received_at, p.http_status, p.payload_hash, p.payload_json,
                           p.metadata_json, l.parser_id,
                           o.raw_object_id, o.source_id as object_source_id,
                           o.metadata_json as object_metadata_json, o.wire_hash,
                           o.byte_length, length(o.body_blob) as actual_byte_length
                      from raw_data_payloads p
                      join raw_payload_objects l on l.raw_payload_id=p.raw_payload_id
                      join raw_data_objects o on o.raw_object_id=l.raw_object_id
                     where p.raw_payload_id=?
                    """, (raw_id,),
                ).fetchall()
                if len(rows) != 1:
                    raise ValueError("product_raw_payload_missing_or_ambiguous")
                raw = dict(rows[0])
                capture = json.loads(raw["metadata_json"])
                object_capture = json.loads(raw["object_metadata_json"])
                payload = json.loads(raw["payload_json"])
                if (
                    raw["source_id"] != "twse_isin" or raw["object_source_id"] != "twse_isin"
                    or raw["request_url"] != receipt.get("source_url")
                    or raw["http_status"] != 200
                    or raw["parser_id"] != "stock_ai.product_classification.v1"
                    or raw["wire_hash"] != receipt.get("raw_sha256")
                    or not 0 < raw["actual_byte_length"] <= MAX_CATALOGUE_BYTES
                    or raw["byte_length"] != raw["actual_byte_length"]
                    or raw["payload_hash"] != content_hash(payload)
                    or payload.get("dataset_id") != receipt.get("source_dataset")
                    or payload.get("wire_sha256") != raw["wire_hash"]
                    or normalize_timestamp(payload.get("acquired_at"), required=True) != raw["received_at"]
                    or normalize_timestamp(receipt.get("acquired_at"), required=True) != raw["received_at"]
                    or raw["requested_at"] > raw["received_at"]
                    or capture.get("dataset") != "security_master"
                    or capture.get("source_dataset") != receipt.get("source_dataset")
                    or capture.get("effective_url") != raw["request_url"]
                    or capture.get("capture_representation") != "exact_source_bytes"
                    or capture.get("capture_truncated") is not False
                    # Content objects are deduplicated by source and byte hash.
                    # Their first HTTP capture is history, not the transport
                    # receipt for this later payload's successful acquisition.
                    or object_capture.get("capture_representation") != "exact_source_bytes"
                ):
                    raise ValueError("product_raw_capture_binding_mismatch")
                version = conn.execute("pragma data_version").fetchone()[0]
                key = (raw_id, raw["wire_hash"])
                context_hash = content_hash(raw)
                cached = self._product_proof_cache.get(key)
                if cached and cached["context_hash"] == context_hash and cached["data_version"] == version:
                    self._product_proof_cache.move_to_end(key)
                    return cached["rows"]
                body_row = conn.execute(
                    "select body_blob from raw_data_objects where raw_object_id=?", (raw["raw_object_id"],)
                ).fetchone()
                body = bytes(body_row["body_blob"])
                if sha256(body).hexdigest() != raw["wire_hash"]:
                    self._product_proof_cache.pop(key, None)
                    raise ValueError("product_raw_hash_mismatch")
                if cached and cached["context_hash"] == context_hash:
                    parsed_rows = cached["rows"]
                else:
                    parsed = parse_product_catalogue(
                        body, dataset_id=receipt["source_dataset"],
                        acquired_at=raw["received_at"], source_url=raw["request_url"],
                    )
                    parsed_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
                    for value in parsed["receipts"]:
                        parsed_rows.setdefault((value["venue"], value["symbol"]), []).append(value)
                self._product_proof_cache[key] = {
                    "context_hash": context_hash, "data_version": version, "rows": parsed_rows,
                }
                self._product_proof_cache.move_to_end(key)
                while len(self._product_proof_cache) > 3:
                    self._product_proof_cache.popitem(last=False)
                return parsed_rows
            finally:
                conn.rollback()

    def _product_receipt_failure(
        self, receipt: dict[str, Any], *, proof_cache: dict[str, Any]
    ) -> str | None:
        try:
            raw_id = receipt.get("raw_payload_id")
            # Cache the same capture only within this listing snapshot. Each
            # receipt still has its source, time and row-derived fields checked.
            binding = canonical_json({key: receipt.get(key) for key in (
                "raw_payload_id", "raw_sha256", "source_dataset", "source_url", "acquired_at",
            )})
            if binding not in proof_cache:
                proof_cache[binding] = self._retained_product_catalogue(receipt)
            rows = proof_cache[binding].get((receipt.get("venue"), receipt.get("symbol")), [])
            if len(rows) != 1:
                return "product_raw_row_missing_or_ambiguous"
            expected = rows[0]
            if any(
                normalize_timestamp(receipt.get(key), required=True) != value
                if key == "acquired_at" else receipt.get(key) != value
                for key, value in expected.items()
            ):
                return "product_raw_row_binding_mismatch"
            if not isinstance(raw_id, str):
                return "product_raw_payload_missing"
        except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error):
            return "product_retained_raw_unverified"
        return None

    def _product_resolution(
        self, *, symbol: str, matches: dict[tuple[str, str], dict[str, Any]],
        proof_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(matches) > 1:
            return self._unknown_product_resolution(symbol, "ambiguous_product_listing", status="conflict")
        if not matches:
            return self._unknown_product_resolution(symbol, "current_product_listing_unavailable")
        entity = next(iter(matches.values()))
        metadata = entity["metadata"]
        key = f"{entity['exchange']}:{symbol}"
        receipt = (metadata.get("product_classifications") or {}).get(key)
        reason = (
            "product_listing_not_active" if entity["lifecycle_status"] != "active"
            else "provisional_product_identity" if metadata.get("provisional") and not metadata.get("source_datasets")
            else "product_classification_unavailable" if not isinstance(receipt, dict)
            else "product_receipt_binding_mismatch" if receipt.get("symbol") != symbol or receipt.get("venue") != entity["exchange"]
            else None
        )
        failure_status = "unknown"
        if reason is None and receipt.get("status") == "verified":
            reason = self._product_receipt_failure(
                receipt, proof_cache=proof_cache if proof_cache is not None else {},
            )
            if reason is None and entity["entity_type"] == "warrant":
                # Catalogue membership belongs to an issued contract. A new
                # row for a reused code cannot classify the old warrant owner.
                issuance = metadata.get("issuance_identity")
                known_isin = issuance.get("isin") if isinstance(issuance, dict) else None
                if receipt.get("product_type") != "warrant":
                    reason = "warrant_product_type_binding_mismatch"
                elif known_isin and receipt.get("isin") != known_isin:
                    reason = "warrant_issuance_isin_binding_mismatch"
                if reason:
                    failure_status = "conflict"
        if reason:
            receipt = self._unknown_product_resolution(symbol, reason, status=failure_status)["classification"]
            receipt["venue"] = entity["exchange"]
        return {
            "entity_id": entity["entity_id"], "lifecycle_status": entity["lifecycle_status"],
            "classification": dict(receipt),
        }

    def product_classifications_for_listings(
        self, *, listings: Iterable[tuple[str, str]], now: datetime
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Project a master list using one local identity snapshot."""

        keys = set(listings)
        matches: dict[tuple[str, str], dict[tuple[str, str], dict[str, Any]]] = {
            key: {} for key in keys
        }
        with self._connect() as conn:
            rows = self._product_listing_rows(conn, at=now.astimezone(timezone.utc).isoformat())
        for row in rows:
            key = (str(row["exchange"]), str(row["listing_symbol"]))
            if key in matches:
                matches[key][(str(row["entity_id"]), key[0])] = self._entity_row(row)
        proof_cache: dict[str, Any] = {}
        return {
            key: self._product_resolution(symbol=key[1], matches=values, proof_cache=proof_cache)
            for key, values in matches.items()
        }

    def resolve_product_classification(
        self, *, symbol: str, market: str, now: datetime
    ) -> dict[str, Any]:
        """Resolve one current exact listing from local data; never fetch a source."""

        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return self._unknown_product_resolution(symbol, "classification_now_requires_timezone")
        if not isinstance(symbol, str) or symbol != symbol.strip().upper():
            return self._unknown_product_resolution(symbol, "noncanonical_product_symbol")
        venues = {
            "twse": {"TWSE"}, "listed": {"TWSE"},
            "tpex": {"TPEx"}, "otc": {"TPEx"},
            "tpex-esb": {"TPEx-ESB"}, "emerging": {"TPEx-ESB"},
            "tw": {"TWSE", "TPEx", "TPEx-ESB"},
            "taiwan": {"TWSE", "TPEx", "TPEx-ESB"},
        }.get(str(market).casefold())
        if venues is None:
            return self._unknown_product_resolution(symbol, "unsupported_product_market")
        suffix_venues = {"TWSE"} if symbol.endswith(".TW") else {"TPEx", "TPEx-ESB"} if symbol.endswith(".TWO") else set()
        venues = venues & suffix_venues
        at = now.astimezone(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = self._product_listing_rows(conn, at=at, symbol=symbol)
        matches = {
            (str(row["entity_id"]), str(row["exchange"])): self._entity_row(row)
            for row in rows
            if row["listing_symbol"] == symbol and row["exchange"] in venues
        }
        return self._product_resolution(symbol=symbol, matches=matches)

    @serialized_warehouse_write
    def upsert_entity(
        self,
        entity: EntityRecord,
        *,
        identifiers: Iterable[dict[str, Any]] = (),
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                insert into market_entities (
                    entity_id, entity_type, canonical_name, market, exchange,
                    currency, sector, industry, lifecycle_status, listed_at,
                    delisted_at, metadata_json, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(entity_id) do update set
                    entity_type=excluded.entity_type,
                    canonical_name=excluded.canonical_name,
                    market=excluded.market,
                    exchange=excluded.exchange,
                    currency=excluded.currency,
                    sector=excluded.sector,
                    industry=excluded.industry,
                    lifecycle_status=excluded.lifecycle_status,
                    listed_at=coalesce(excluded.listed_at, market_entities.listed_at),
                    delisted_at=coalesce(excluded.delisted_at, market_entities.delisted_at),
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    entity.entity_id,
                    entity.entity_type,
                    entity.canonical_name,
                    entity.market,
                    entity.exchange,
                    entity.currency,
                    entity.sector,
                    entity.industry,
                    entity.lifecycle_status,
                    entity.listed_at,
                    entity.delisted_at,
                    canonical_json(self._preserve_product_classifications(conn, entity)),
                    now,
                    now,
                ),
            )
            for item in identifiers:
                identifier = EntityIdentifierRecord.model_validate(
                    {**item, "entity_id": entity.entity_id}
                )
                cursor = conn.execute(
                    """
                    insert into entity_identifiers (
                        source_id, identifier_type, identifier_value, normalized_value,
                        entity_id, valid_from, valid_to, confidence, is_primary,
                        metadata_json, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(source_id, identifier_type, identifier_value, valid_from)
                    do update set
                        normalized_value=excluded.normalized_value,
                        valid_to=excluded.valid_to,
                        confidence=excluded.confidence,
                        is_primary=excluded.is_primary,
                        metadata_json=excluded.metadata_json
                    where entity_identifiers.entity_id=excluded.entity_id
                    """,
                    (
                        identifier.source_id,
                        identifier.identifier_type,
                        identifier.identifier_value,
                        identifier.normalized_value,
                        entity.entity_id,
                        identifier.valid_from or "",
                        identifier.valid_to,
                        identifier.confidence,
                        1 if identifier.is_primary else 0,
                        canonical_json(identifier.metadata),
                        now,
                    ),
                )
                if cursor.rowcount == 0:
                    raise ValueError(
                        "Identifier interval is already assigned to another entity: "
                        f"{identifier.source_id}/{identifier.identifier_type}/"
                        f"{identifier.identifier_value}/{identifier.valid_from or ''}"
                    )
            conn.commit()

    def resolve_entity(
        self,
        identifier_value: str,
        *,
        source_id: str | None = None,
        identifier_type: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any] | None:
        matches = self.resolve_identifier_candidates(
            identifier_value,
            source_id=source_id,
            identifier_types=[identifier_type] if identifier_type else None,
            as_of=as_of,
        )
        entities = {
            str(item["entity"]["entity_id"]): item["entity"]
            for item in matches
        }
        if len(entities) == 1:
            return next(iter(entities.values()))
        active = [
            entity
            for entity in entities.values()
            if entity.get("lifecycle_status") in {"active", "pre_listing", "suspended"}
        ]
        return active[0] if len(active) == 1 else None

    def resolve_identifier_candidates(
        self,
        identifier_value: str,
        *,
        source_id: str | None = None,
        identifier_types: Iterable[str] | None = None,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        types = list(
            identifier_types
            or (
                "display_symbol",
                "exchange_code",
                "unified_business_no",
                "source_symbol",
                "isin",
                "figi",
                "lei",
                "legal_name",
            )
        )
        comparisons = [
            (kind, normalize_identifier_value(kind, identifier_value))
            for kind in types
        ]
        clauses = [
            "(" + " or ".join(
                "(i.identifier_type = ? and i.normalized_value = ?)"
                for _kind, _normalized in comparisons
            ) + ")"
        ]
        parameters: list[Any] = [
            value for pair in comparisons for value in pair
        ]
        if source_id:
            clauses.append("i.source_id = ?")
            parameters.append(source_id)
        # A current lookup is also an interval lookup. Lifecycle labels cannot
        # make an expired or not-yet-listed code belong to the current issue.
        timestamp = normalize_timestamp(as_of or utc_now(), required=True)
        clauses.append("(i.valid_from is null or i.valid_from = '' or i.valid_from <= ?)")
        clauses.append("(i.valid_to is null or i.valid_to > ?)")
        clauses.append("(i.superseded_at is null or i.superseded_at > ?)")
        parameters.extend([timestamp, timestamp, timestamp])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select e.*,
                       i.source_id as matched_source_id,
                       i.identifier_type as matched_identifier_type,
                       i.identifier_value as matched_identifier_value,
                       i.normalized_value as matched_normalized_value,
                       i.valid_from as matched_valid_from,
                       i.valid_to as matched_valid_to,
                       i.confidence as matched_confidence,
                       i.is_primary as matched_is_primary,
                       i.superseded_by_entity_id as matched_superseded_by_entity_id,
                       i.superseded_at as matched_superseded_at,
                       i.metadata_json as identifier_metadata_json
                  from entity_identifiers i
                  join market_entities e on e.entity_id = i.entity_id
                 where {' and '.join(clauses)}
                 order by i.confidence desc, i.is_primary desc,
                          case e.lifecycle_status when 'active' then 1 else 2 end,
                          e.updated_at desc
                """,
                parameters,
            ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            entity = self._entity_row(row)
            identifier = {
                "source_id": entity.pop("matched_source_id"),
                "identifier_type": entity.pop("matched_identifier_type"),
                "identifier_value": entity.pop("matched_identifier_value"),
                "normalized_value": entity.pop("matched_normalized_value"),
                "valid_from": entity.pop("matched_valid_from") or None,
                "valid_to": entity.pop("matched_valid_to"),
                "confidence": float(entity.pop("matched_confidence")),
                "is_primary": bool(entity.pop("matched_is_primary")),
                "superseded_by_entity_id": entity.pop(
                    "matched_superseded_by_entity_id"
                ),
                "superseded_at": entity.pop("matched_superseded_at"),
                "metadata": json.loads(entity.pop("identifier_metadata_json")),
            }
            matches.append({"entity": entity, "identifier": identifier})
        return matches

    def entity_profile(self, entity_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from market_entities where entity_id=?",
                (entity_id,),
            ).fetchone()
            if row is None:
                return None
            entity = self._entity_row(row)
            identifiers = [
                {
                    **dict(item),
                    "valid_from": item["valid_from"] or None,
                    "confidence": float(item["confidence"]),
                    "is_primary": bool(item["is_primary"]),
                    "metadata": json.loads(item["metadata_json"]),
                }
                for item in conn.execute(
                    """
                    select source_id, identifier_type, identifier_value,
                           normalized_value, valid_from, valid_to, confidence,
                           is_primary, superseded_by_entity_id, superseded_at,
                           metadata_json
                      from entity_identifiers
                     where entity_id=?
                     order by identifier_type, source_id, valid_from
                    """,
                    (entity_id,),
                ).fetchall()
            ]
        for identifier in identifiers:
            identifier.pop("metadata_json", None)
        return {**entity, "identifiers": identifiers}

    @serialized_warehouse_write
    def reconcile_entity_registry_aliases(
        self,
        *,
        effective_at: str | None = None,
    ) -> dict[str, Any]:
        """Repair known source semantics and supersede ambiguous legacy aliases."""

        effective = normalize_timestamp(effective_at or utc_now(), required=True)
        assert effective is not None
        repaired_intervals = 0
        unresolved_intervals = 0
        superseded_placeholders = 0
        superseded_aliases = 0
        retained_warrant_ambiguities = 0
        merge_edges: set[tuple[str, str, str, str]] = set()
        with self._connect() as conn:
            conn.execute("begin immediate")
            invalid_warrant_rows = conn.execute(
                """
                select i.rowid, i.source_id, i.identifier_type,
                       i.identifier_value, i.valid_from, i.metadata_json
                  from entity_identifiers i
                  join market_entities e on e.entity_id=i.entity_id
                 where i.source_id='twse_openapi'
                   and e.entity_type='warrant'
                   and i.superseded_at is null
                   and i.valid_from != ''
                   and i.valid_to is not null
                   and i.valid_to <= i.valid_from
                """
            ).fetchall()
            interval_updates: list[tuple[str, str, int]] = []
            blank_interval_keys: set[tuple[str, str, str]] = set()
            if invalid_warrant_rows:
                blank_interval_keys = {
                    (row["source_id"], row["identifier_type"], row["identifier_value"])
                    for row in conn.execute(
                        """
                        select source_id, identifier_type, identifier_value
                          from entity_identifiers where valid_from=''
                        """
                    ).fetchall()
                }
            for row in invalid_warrant_rows:
                key = (row["source_id"], row["identifier_type"], row["identifier_value"])
                if key in blank_interval_keys:
                    # Keep the original legacy row for review. Reassigning it
                    # would either collide with or erase another issuance.
                    unresolved_intervals += 1
                    continue
                blank_interval_keys.add(key)
                metadata = json.loads(row["metadata_json"])
                metadata.update(
                    {
                        "original_valid_from": row["valid_from"],
                        "valid_from_repair": "twse_exercise_start_not_listing",
                    }
                )
                interval_updates.append(
                    ("", canonical_json(metadata), int(row["rowid"]))
                )
            if interval_updates:
                conn.executemany(
                    """
                    update entity_identifiers
                       set valid_from=?, metadata_json=?
                     where rowid=?
                    """,
                    interval_updates,
                )
                repaired_intervals = len(interval_updates)

            business_rows = conn.execute(
                """
                select rowid, normalized_value, metadata_json
                  from entity_identifiers
                 where identifier_type='unified_business_no'
                   and superseded_at is null
                """
            ).fetchall()
            placeholder_updates: list[tuple[str, str, int]] = []
            for row in business_rows:
                normalized = str(row["normalized_value"] or "")
                if len(normalized) != 8 or len(set(normalized)) != 1:
                    continue
                metadata = json.loads(row["metadata_json"])
                metadata["supersession_reason"] = "invalid_business_number_placeholder"
                placeholder_updates.append(
                    (effective, canonical_json(metadata), int(row["rowid"]))
                )
            if placeholder_updates:
                conn.executemany(
                    """
                    update entity_identifiers
                       set superseded_at=?, is_primary=0, metadata_json=?
                     where rowid=?
                    """,
                    placeholder_updates,
                )
                superseded_placeholders = len(placeholder_updates)

            ambiguous_symbols = [
                str(row["normalized_value"])
                for row in conn.execute(
                    """
                    select normalized_value
                      from entity_identifiers
                     where identifier_type='display_symbol'
                       and superseded_at is null
                       and (valid_from='' or valid_from <= ?)
                       and (valid_to is null or valid_to > ?)
                     group by normalized_value
                    having count(distinct entity_id) > 1
                    """,
                    (effective, effective),
                ).fetchall()
            ]
            valid_business_entities = {
                str(row["entity_id"])
                for row in conn.execute(
                    """
                    select distinct entity_id
                      from entity_identifiers
                     where identifier_type='unified_business_no'
                       and superseded_at is null
                    """
                ).fetchall()
            }
            for symbol in ambiguous_symbols:
                rows = conn.execute(
                    """
                    select e.*, i.source_id as matched_source_id
                      from entity_identifiers i
                      join market_entities e on e.entity_id=i.entity_id
                     where i.identifier_type='display_symbol'
                       and i.normalized_value=?
                       and i.superseded_at is null
                       and (i.valid_from='' or i.valid_from <= ?)
                       and (i.valid_to is null or i.valid_to > ?)
                    """,
                    (symbol, effective, effective),
                ).fetchall()
                candidates: dict[str, sqlite3.Row] = {
                    str(row["entity_id"]): row
                    for row in rows
                }
                if len(candidates) < 2:
                    continue
                if any(row["entity_type"] == "warrant" for row in candidates.values()):
                    # A shared exchange code does not establish one warrant
                    # contract, even when imported ISIN labels happen to agree.
                    retained_warrant_ambiguities += 1
                    continue

                def candidate_score(row: sqlite3.Row) -> tuple[Any, ...]:
                    metadata = json.loads(row["metadata_json"])
                    datasets = set(metadata.get("source_datasets") or [])
                    return (
                        str(row["entity_id"]) in valid_business_entities,
                        "twse_etfs" in datasets,
                        str(metadata.get("display_symbol") or "").upper() == symbol,
                        row["lifecycle_status"] == "active",
                        str(row["updated_at"] or ""),
                        str(row["entity_id"]),
                    )

                winner = max(candidates.values(), key=candidate_score)
                winner_id = str(winner["entity_id"])
                code = symbol.rsplit(".", 1)[0]
                for loser_id, loser in candidates.items():
                    if loser_id == winner_id:
                        continue
                    display_rows = conn.execute(
                        """
                        select rowid, metadata_json
                          from entity_identifiers
                         where entity_id=? and identifier_type='display_symbol'
                           and normalized_value=? and superseded_at is null
                        """,
                        (loser_id, symbol),
                    ).fetchall()
                    source_ids = {
                        str(row["source_id"])
                        for row in conn.execute(
                            """
                            select distinct source_id from entity_identifiers
                             where entity_id=? and identifier_type='display_symbol'
                               and normalized_value=? and superseded_at is null
                            """,
                            (loser_id, symbol),
                        ).fetchall()
                    }
                    for row in display_rows:
                        metadata = json.loads(row["metadata_json"])
                        metadata.update(
                            {
                                "supersession_reason": "duplicate_current_display_symbol",
                                "superseded_symbol": symbol,
                            }
                        )
                        conn.execute(
                            """
                            update entity_identifiers
                               set superseded_by_entity_id=?, superseded_at=?,
                                   is_primary=0, metadata_json=?
                             where rowid=?
                            """,
                            (
                                winner_id,
                                effective,
                                canonical_json(metadata),
                                int(row["rowid"]),
                            ),
                        )
                        superseded_aliases += 1
                    for source_id in source_ids:
                        code_rows = conn.execute(
                            """
                            select rowid, metadata_json
                              from entity_identifiers
                             where entity_id=? and source_id=?
                               and identifier_type='exchange_code'
                               and normalized_value=? and superseded_at is null
                            """,
                            (loser_id, source_id, code),
                        ).fetchall()
                        for row in code_rows:
                            metadata = json.loads(row["metadata_json"])
                            metadata.update(
                                {
                                    "supersession_reason": "duplicate_current_display_symbol",
                                    "superseded_symbol": symbol,
                                }
                            )
                            conn.execute(
                                """
                                update entity_identifiers
                                   set superseded_by_entity_id=?, superseded_at=?,
                                       is_primary=0, metadata_json=?
                                 where rowid=?
                                """,
                                (
                                    winner_id,
                                    effective,
                                    canonical_json(metadata),
                                    int(row["rowid"]),
                                ),
                            )
                            superseded_aliases += 1
                    merge_edges.add(
                        (
                            loser_id,
                            winner_id,
                            "duplicate_current_display_symbol",
                            symbol,
                        )
                    )

            merged_from: set[str] = set()
            for loser_id, _winner_id, _reason, _symbol in merge_edges:
                remaining = int(
                    conn.execute(
                        """
                        select count(*) from entity_identifiers
                         where entity_id=? and identifier_type='display_symbol'
                           and superseded_at is null
                        """,
                        (loser_id,),
                    ).fetchone()[0]
                )
                if remaining == 0:
                    merged_from.add(loser_id)
            created_merges = 0
            for loser_id, winner_id, reason, symbol in sorted(merge_edges):
                if loser_id not in merged_from:
                    continue
                merge_id = "EIM-" + content_hash(
                    {
                        "from": loser_id,
                        "to": winner_id,
                        "reason": reason,
                        "symbol": symbol,
                    }
                )[:32]
                cursor = conn.execute(
                    """
                    insert or ignore into entity_identity_merges (
                        merge_id, from_entity_id, to_entity_id, reason,
                        effective_at, metadata_json, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        merge_id,
                        loser_id,
                        winner_id,
                        reason,
                        effective,
                        canonical_json({"display_symbol": symbol}),
                        effective,
                    ),
                )
                created_merges += max(cursor.rowcount, 0)
            conn.commit()
        return {
            "schema_version": "stock_ai.entity_registry_reconciliation.v1",
            "status": "partial" if unresolved_intervals else "succeeded",
            "effective_at": effective,
            "repaired_identifier_interval_count": repaired_intervals,
            "unresolved_identifier_interval_count": unresolved_intervals,
            "retained_warrant_ambiguity_count": retained_warrant_ambiguities,
            "superseded_placeholder_count": superseded_placeholders,
            "superseded_alias_count": superseded_aliases,
            "created_merge_count": created_merges,
        }

    def entity_registry_summary(self) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            entity_count = int(
                conn.execute(
                    """
                    select count(*) from market_entities e
                     where not exists (
                       select 1 from entity_identity_merges m
                        where m.from_entity_id=e.entity_id
                     )
                    """
                ).fetchone()[0]
            )
            physical_entity_count = int(
                conn.execute("select count(*) from market_entities").fetchone()[0]
            )
            identifier_count = int(
                conn.execute(
                    "select count(*) from entity_identifiers where superseded_at is null"
                ).fetchone()[0]
            )
            superseded_identifier_count = int(
                conn.execute(
                    "select count(*) from entity_identifiers where superseded_at is not null"
                ).fetchone()[0]
            )
            by_type = {
                str(row["identifier_type"]): int(row["count"])
                for row in conn.execute(
                    """
                    select identifier_type, count(*) as count
                      from entity_identifiers
                     where superseded_at is null
                     group by identifier_type order by identifier_type
                    """
                ).fetchall()
            }
            by_source = {
                str(row["source_id"]): int(row["count"])
                for row in conn.execute(
                    """
                    select source_id, count(*) as count
                      from entity_identifiers
                     where superseded_at is null
                     group by source_id order by source_id
                    """
                ).fetchall()
            }
            cross_source_entities = int(
                conn.execute(
                    """
                    select count(*) from (
                        select entity_id from entity_identifiers
                         where superseded_at is null
                         group by entity_id
                        having count(distinct source_id) > 1
                    )
                    """
                ).fetchone()[0]
            )
            entities_without_identifiers = int(
                conn.execute(
                    """
                    select count(*) from market_entities e
                     where not exists (
                        select 1 from entity_identity_merges m
                         where m.from_entity_id=e.entity_id
                     )
                       and not exists (
                        select 1 from entity_identifiers i
                         where i.entity_id=e.entity_id and i.superseded_at is null
                     )
                    """
                ).fetchone()[0]
            )
            missing_normalized = int(
                conn.execute(
                    """
                    select count(*) from entity_identifiers
                     where (normalized_value is null or normalized_value='')
                       and superseded_at is null
                    """
                ).fetchone()[0]
            )
            invalid_intervals = int(
                conn.execute(
                    """
                    select count(*) from entity_identifiers
                     where valid_to is not null and valid_to <= valid_from
                       and superseded_at is null
                    """
                ).fetchone()[0]
            )
            malformed_entity_ids = int(
                conn.execute(
                    """
                    select count(*) from market_entities
                     where entity_id not like 'ENT-%' or length(entity_id) != 36
                    """
                ).fetchone()[0]
            )
            ambiguous_current_display_symbols = int(
                conn.execute(
                    """
                    select count(*) from (
                        select normalized_value
                          from entity_identifiers
                         where identifier_type='display_symbol'
                           and superseded_at is null
                           and (valid_from='' or valid_from <= ?)
                           and (valid_to is null or valid_to > ?)
                         group by normalized_value
                        having count(distinct entity_id) > 1
                    )
                    """,
                    (now, now),
                ).fetchone()[0]
            )
            merge_count = int(
                conn.execute(
                    "select count(*) from entity_identity_merges"
                ).fetchone()[0]
            )
        blockers = (
            missing_normalized
            + invalid_intervals
            + malformed_entity_ids
            + ambiguous_current_display_symbols
        )
        return {
            "schema_version": "stock_ai.entity_registry_status.v1",
            "status": "passed" if blockers == 0 else "warning",
            "entity_count": entity_count,
            "physical_entity_count": physical_entity_count,
            "identifier_count": identifier_count,
            "superseded_identifier_count": superseded_identifier_count,
            "identity_merge_count": merge_count,
            "by_identifier_type": by_type,
            "by_source": by_source,
            "cross_source_entity_count": cross_source_entities,
            "entities_without_identifiers": entities_without_identifiers,
            "missing_normalized_identifier_count": missing_normalized,
            "invalid_interval_count": invalid_intervals,
            "malformed_internal_id_count": malformed_entity_ids,
            "ambiguous_current_display_symbol_count": ambiguous_current_display_symbols,
        }

    def list_entities(
        self,
        *,
        query: str = "",
        market: str | None = None,
        exchange: str | None = None,
        entity_type: str | None = None,
        lifecycle_status: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Project bounded entity rows with an unambiguous listing label.

        Source arrival order is not listing validity: a delisting feed may
        insert an old venue alias after the current venue's company feed.
        Keep all historical links intact and resolve current labels at read
        time. Non-current lifecycle rows remain available for investigation.
        """

        clauses = [
            "not exists (select 1 from entity_identity_merges m where m.from_entity_id=e.entity_id)"
        ]
        parameters: list[Any] = []
        if query:
            search_text = query.strip()
            like = f"%{search_text.casefold()}%"
            identifier_terms: list[str] = []
            identifier_parameters: list[Any] = []
            for identifier_type in _SEARCH_IDENTIFIER_TYPES:
                identifier_terms.append(
                    "(i.identifier_type=? and i.normalized_value=?)"
                )
                identifier_parameters.extend([
                    identifier_type,
                    normalize_identifier_value(identifier_type, search_text),
                ])
            with self._connect() as search_conn:
                exact_entity_ids = {
                    str(row["entity_id"])
                    for row in search_conn.execute(
                        f"""
                        select i.entity_id
                          from entity_identifiers i
                         where i.superseded_at is null
                           and ({' or '.join(identifier_terms)})
                        """,
                        identifier_parameters,
                    )
                }
            code_like = (
                any(character.isdigit() for character in search_text)
                or "." in search_text
                or search_text.upper().startswith(("ENT-", "TW"))
                or search_text.isascii()
            )
            if exact_entity_ids:
                placeholders = ",".join("?" for _ in exact_entity_ids)
                clauses.append(f"e.entity_id in ({placeholders})")
                parameters.extend(sorted(exact_entity_ids))
            elif code_like:
                return []
            else:
                # Human-name searches may need a retained legal/source name
                # that is not the canonical label. Scan each table once; never
                # run an identifier scan once per entity.
                clauses.append(
                    """
                    (lower(e.canonical_name) like ? or e.entity_id in (
                        select i.entity_id
                          from entity_identifiers i
                         where i.superseded_at is null
                           and lower(i.identifier_value) like ?
                    ))
                    """
                )
                parameters.extend([like, like])
        if market and market.casefold() not in {"all", ""}:
            clauses.append("lower(e.market) = ?")
            parameters.append(market.casefold())
        if exchange:
            clauses.append("e.exchange = ?")
            parameters.append(exchange)
        if entity_type:
            clauses.append("e.entity_type = ?")
            parameters.append(entity_type)
        if lifecycle_status:
            clauses.append("e.lifecycle_status = ?")
            parameters.append(lifecycle_status)
        parameters.append(max(1, min(limit, 100000)))
        at = utc_now()
        # Distinct sources can spell the same display identifier differently.
        # Follow the registry's canonical value, including legacy empty values.
        display_identifier = "coalesce(nullif(i.normalized_value, ''), upper(trim(i.identifier_value)))"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select e.*,
                       (select case when count(distinct {display_identifier})=1
                                    then max({display_identifier}) end
                          from entity_identifiers i
                         where i.entity_id=e.entity_id and i.identifier_type='display_symbol'
                           and i.superseded_at is null
                           and (
                               e.lifecycle_status in ('delisted', 'expired', 'pre_listing')
                               or ((i.valid_from='' or i.valid_from <= ?)
                                   and (i.valid_to is null or i.valid_to > ?))
                           )
                           and (nullif(json_extract(i.metadata_json, '$.venue'), '') is null
                                or json_extract(i.metadata_json, '$.venue')=e.exchange)
                           and (e.exchange is null
                                or e.exchange != 'TWSE'
                                or {display_identifier} not like '%.TWO')
                           and (e.exchange is null
                                or e.exchange not in ('TPEx', 'TPEx-ESB')
                                or {display_identifier} not like '%.TW')
                       ) as display_symbol
                  from market_entities e
                 where {' and '.join(clauses)}
                 order by
                       case e.entity_type
                         when 'stock' then 1
                         when 'etf' then 2
                         when 'index' then 3
                         when 'warrant' then 4
                         else 5
                       end,
                       case e.lifecycle_status when 'active' then 1 else 2 end,
                       e.exchange, e.canonical_name, e.entity_id
                 limit ?
                """,
                [at, at, *parameters],
            ).fetchall()
        return [self._entity_row(row) for row in rows]

    def identity_inventory(self) -> list[dict[str, Any]]:
        """Read all canonical identities once for a batch resolver."""

        with self._connect() as conn:
            entities = {
                str(row["entity_id"]): self._entity_row(row)
                for row in conn.execute(
                    "select * from market_entities order by updated_at desc"
                ).fetchall()
            }
            for entity in entities.values():
                entity["identifiers"] = []
            for row in conn.execute(
                """
                select source_id, identifier_type, identifier_value, entity_id,
                       valid_from, valid_to, metadata_json
                  from entity_identifiers
                 where superseded_at is null
                 order by created_at
                """
            ).fetchall():
                entity = entities.get(str(row["entity_id"]))
                if entity is None:
                    continue
                entity["identifiers"].append(
                    {
                        **dict(row),
                        "metadata": json.loads(row["metadata_json"]),
                    }
                )
        return list(entities.values())

    @serialized_warehouse_write
    def sync_catalogue_identities(self, *, as_of: str, code_version: str) -> dict[str, Any]:
        """Fill catalogue-only identities under the existing writer lock.

        Re-read verified checkpoints on every attempt, including cache hits.
        This makes a crash after capture or a newly reached listing date
        recoverable without needing to download a catalogue again.
        """
        from .catalogue_identity import retained_catalogue_issue_bindings
        from .catalogue_ingest import catalogue_identity_batch

        context = retained_catalogue_issue_bindings(self, as_of=as_of)
        with self._connect() as conn:
            reserved = [dict(row) for row in conn.execute(
                "select source_id,identifier_type,identifier_value,valid_from,entity_id "
                "from entity_identifiers where superseded_at is not null")]
        batch = catalogue_identity_batch(context, self.identity_inventory(), as_of=as_of,
                                         reserved_identifiers=reserved)
        written = self.write_security_lifecycle_batch(
            entities=batch["entities"], identifiers=batch["identifiers"],
            revisions=batch["revisions"], events=batch["events"],
            raw_payload_ids={p["source_dataset"]: p["raw_payload_id"] for p in context["partitions"]
                             if p.get("raw_payload_id") and p["status"] in {"verified", "partial"}},
            acquired_at=as_of, code_version=code_version,
        ) if batch["entities"] else {"created_revision_count": 0, "created_event_count": 0}
        reports = []
        for partition in context["partitions"]:
            counts = batch["partitions"][partition["source_dataset"]]
            report = {**partition, **counts,
                      "status": "succeeded" if partition["status"] == "verified" and not counts["unresolved"]
                      else "partial" if partition.get("count") else "unavailable"}
            self.save_checkpoint(source_id="twse_isin", dataset="security_master",
                partition_key=partition["source_dataset"] + ":identity", cursor_value=partition.get("raw_sha256"),
                status=report["status"], metadata=report)
            reports.append(report)
        return {"schema_version": "stock_ai.catalogue_identity_ingest.v1", "as_of": as_of,
                "status": "succeeded" if all(p["status"] == "succeeded" for p in reports) else "partial",
                "created_count": sum(p["created_count"] for p in reports),
                "refreshed_count": sum(p["refreshed_count"] for p in reports),
                "existing_count": sum(p["existing_count"] for p in reports),
                "partitions": reports, "write": written}

    @serialized_warehouse_write
    def write_security_lifecycle_batch(
        self,
        *,
        entities: Iterable[EntityRecord],
        identifiers: Iterable[dict[str, Any]],
        revisions: Iterable[dict[str, Any]],
        events: Iterable[dict[str, Any]],
        raw_payload_ids: dict[str, str],
        acquired_at: str,
        code_version: str,
    ) -> dict[str, Any]:
        """Persist a full security snapshot in one transaction.

        The official warrant masters contain tens of thousands of rows; opening
        a SQLite connection per entity would turn a bounded refresh into minutes
        of avoidable transaction overhead.
        """

        entity_rows = list(entities)
        identifier_rows = list(identifiers)
        validated_identifiers = [
            EntityIdentifierRecord.model_validate(item)
            for item in identifier_rows
        ]
        revision_rows = list(revisions)
        event_rows = list(events)
        now = utc_now()
        created_revision_ids: list[str] = []
        reused_revision_count = 0
        created_event_count = 0
        split_legacy_identifier_count = 0
        with self._connect() as conn:
            conn.execute("begin immediate")
            for entity in entity_rows:
                conn.execute(
                    """
                    insert into market_entities (
                        entity_id, entity_type, canonical_name, market, exchange,
                        currency, sector, industry, lifecycle_status, listed_at,
                        delisted_at, metadata_json, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(entity_id) do update set
                        entity_type=excluded.entity_type,
                        canonical_name=excluded.canonical_name,
                        market=excluded.market,
                        exchange=excluded.exchange,
                        currency=excluded.currency,
                        sector=excluded.sector,
                        industry=excluded.industry,
                        lifecycle_status=excluded.lifecycle_status,
                        listed_at=coalesce(excluded.listed_at, market_entities.listed_at),
                        delisted_at=excluded.delisted_at,
                        metadata_json=excluded.metadata_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        entity.entity_id,
                        entity.entity_type,
                        entity.canonical_name,
                        entity.market,
                        entity.exchange,
                        entity.currency,
                        entity.sector,
                        entity.industry,
                        entity.lifecycle_status,
                        entity.listed_at,
                        entity.delisted_at,
                        canonical_json(self._preserve_product_classifications(conn, entity)),
                        now,
                        now,
                    ),
                )
            existing_identifier_owners = {
                (
                    str(row["source_id"]),
                    str(row["identifier_type"]),
                    str(row["identifier_value"]),
                    str(row["valid_from"]),
                ): (
                    str(row["entity_id"]),
                    str(row["superseded_by_entity_id"])
                    if row["superseded_by_entity_id"]
                    else None,
                )
                for row in conn.execute(
                    """
                    select source_id, identifier_type, identifier_value,
                           valid_from, entity_id, superseded_by_entity_id
                      from entity_identifiers
                    """
                ).fetchall()
            }
            legacy_placeholder_entities: set[str] = set()
            for row in conn.execute(
                """
                select entity_id, normalized_value
                  from entity_identifiers
                 where identifier_type='unified_business_no'
                """
            ).fetchall():
                normalized = str(row["normalized_value"] or "")
                if len(normalized) == 8 and len(set(normalized)) == 1:
                    legacy_placeholder_entities.add(str(row["entity_id"]))
            incoming_identifier_owners: dict[tuple[str, str, str, str], str] = {}
            skipped_identifier_indexes: set[int] = set()
            for index, identifier in enumerate(validated_identifiers):
                key = (
                    identifier.source_id,
                    identifier.identifier_type,
                    identifier.identifier_value,
                    identifier.valid_from or "",
                )
                incoming_owner = incoming_identifier_owners.get(key)
                existing_owner = existing_identifier_owners.get(key)
                owner = incoming_owner or (existing_owner[0] if existing_owner else None)
                superseded_by = existing_owner[1] if existing_owner else None
                if owner is not None and owner != identifier.entity_id:
                    if superseded_by == identifier.entity_id:
                        already_current = conn.execute(
                            """
                            select 1 from entity_identifiers
                             where entity_id=? and source_id=?
                               and identifier_type=? and normalized_value=?
                               and superseded_at is null
                               and (valid_to is null or valid_to > ?)
                             limit 1
                            """,
                            (
                                identifier.entity_id,
                                identifier.source_id,
                                identifier.identifier_type,
                                identifier.normalized_value,
                                acquired_at,
                            ),
                        ).fetchone()
                        if already_current is not None:
                            skipped_identifier_indexes.add(index)
                            continue
                        identifier = identifier.model_copy(
                            update={"valid_from": acquired_at}
                        )
                        validated_identifiers[index] = identifier
                        key = (
                            identifier.source_id,
                            identifier.identifier_type,
                            identifier.identifier_value,
                            identifier.valid_from or "",
                        )
                        owner = incoming_identifier_owners.get(key)
                    elif (
                        owner in legacy_placeholder_entities
                        and (
                            identifier.valid_to is None
                            or identifier.valid_to > acquired_at
                        )
                    ):
                        legacy_rows = conn.execute(
                            """
                            select rowid, metadata_json
                              from entity_identifiers
                             where entity_id=? and source_id=?
                               and identifier_type=? and normalized_value=?
                               and superseded_at is null
                            """,
                            (
                                owner,
                                identifier.source_id,
                                identifier.identifier_type,
                                identifier.normalized_value,
                            ),
                        ).fetchall()
                        for row in legacy_rows:
                            metadata = json.loads(row["metadata_json"])
                            metadata.update(
                                {
                                    "supersession_reason": (
                                        "legacy_business_number_placeholder_split"
                                    ),
                                    "legacy_owner_entity_id": owner,
                                }
                            )
                            conn.execute(
                                """
                                update entity_identifiers
                                   set superseded_by_entity_id=?, superseded_at=?,
                                       is_primary=0, metadata_json=?
                                 where rowid=?
                                """,
                                (
                                    identifier.entity_id,
                                    acquired_at,
                                    canonical_json(metadata),
                                    int(row["rowid"]),
                                ),
                            )
                            split_legacy_identifier_count += 1
                        identifier = identifier.model_copy(
                            update={
                                "valid_from": acquired_at,
                                "metadata": {
                                    **identifier.metadata,
                                    "identity_transition": (
                                        "legacy_business_number_placeholder_split"
                                    ),
                                },
                            }
                        )
                        validated_identifiers[index] = identifier
                        key = (
                            identifier.source_id,
                            identifier.identifier_type,
                            identifier.identifier_value,
                            identifier.valid_from or "",
                        )
                        owner = incoming_identifier_owners.get(key)
                    if owner is not None and owner != identifier.entity_id:
                        raise ValueError(
                            "Identifier interval is already assigned to another entity: "
                            f"{identifier.source_id}/{identifier.identifier_type}/"
                            f"{identifier.identifier_value}/{identifier.valid_from or ''}"
                        )
                incoming_identifier_owners[key] = identifier.entity_id
            conn.executemany(
                """
                insert into entity_identifiers (
                    source_id, identifier_type, identifier_value, normalized_value,
                    entity_id, valid_from, valid_to, confidence, is_primary,
                    metadata_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id, identifier_type, identifier_value, valid_from)
                do update set
                    normalized_value=excluded.normalized_value,
                    valid_to=excluded.valid_to,
                    confidence=excluded.confidence,
                    is_primary=excluded.is_primary,
                    metadata_json=excluded.metadata_json
                """,
                [
                    (
                        identifier.source_id,
                        identifier.identifier_type,
                        identifier.identifier_value,
                        identifier.normalized_value,
                        identifier.entity_id,
                        identifier.valid_from or "",
                        identifier.valid_to,
                        identifier.confidence,
                        1 if identifier.is_primary else 0,
                        canonical_json(identifier.metadata),
                        now,
                    )
                    for index, identifier in enumerate(validated_identifiers)
                    if index not in skipped_identifier_indexes
                ],
            )
            # A catalogue-only code has an unknown end until a lifecycle
            # source supplies an expiry for this exact issue. Close only that
            # owner's source-qualified aliases; its permanent ISIN remains.
            from zoneinfo import ZoneInfo
            for entity in entity_rows:
                issue = entity.metadata.get("issuance_identity") or {}
                expiry = entity.metadata.get("expires_at")
                if entity.entity_type != "warrant" or not expiry or not issue.get("isin"):
                    continue
                next_day = datetime.fromisoformat(expiry).date() + timedelta(days=1)
                end = datetime.combine(next_day, datetime.min.time(), ZoneInfo("Asia/Taipei")).astimezone(timezone.utc).isoformat()
                conn.execute("update entity_identifiers set valid_to=?, is_primary=0 "
                    "where entity_id=? and source_id='twse_isin' "
                    "and identifier_type in ('exchange_code','display_symbol') "
                    "and json_extract(metadata_json, '$.issuance_identity.isin')=? "
                    "and superseded_at is null", (end, entity.entity_id, issue["isin"]))
            for item in revision_rows:
                payload = dict(item["payload"])
                payload_digest = revision_content_hash(
                    dataset="security_master",
                    payload=payload,
                )
                current = conn.execute(
                    """
                    select * from data_revisions
                     where dataset='security_master' and entity_id=?
                       and observation_key=? and source_id=?
                     order by revision desc limit 1
                    """,
                    (
                        item["entity_id"],
                        item["observation_key"],
                        item["source_id"],
                    ),
                ).fetchone()
                if current and current["payload_hash"] == payload_digest:
                    reused_revision_count += 1
                    continue
                revision_number = int(current["revision"]) + 1 if current else 1
                revision_id = f"DRV-{uuid4().hex}"
                source_dataset = str(item["source_dataset"])
                raw_payload_id = raw_payload_ids.get(source_dataset)
                transformation_id = "stock_ai.security_lifecycle_normalizer.v2"
                transformation = {
                    "transformation_id": transformation_id,
                    "code_version": code_version,
                    "parameters": {
                        "source_dataset": source_dataset,
                        "identity_contract": (
                            "official_catalogue_venue_isin_identity_only"
                            if source_dataset in _PRODUCT_SOURCE_VENUES
                            else
                            "warrant_venue_isin_with_verified_legacy_adoption"
                            if payload.get("entity_type") == "warrant"
                            else "business_no_then_code_and_name"
                        ),
                    },
                }
                lifecycle_temporal = TemporalCoordinates(
                    time_basis="lifecycle",
                    observed_at=acquired_at,
                    published_at=None,
                    available_at=acquired_at,
                    acquired_at=acquired_at,
                    effective_at=normalize_timestamp(
                        item["effective_at"],
                        required=True,
                    ),
                    expires_at=normalize_timestamp(item.get("expires_at")),
                )
                availability_snapshot = availability_contract_snapshot(
                    source_id=str(item["source_id"]),
                    dataset="security_master",
                    temporal=lifecycle_temporal,
                )
                lifecycle_flags = list(item.get("quality_flags") or [])
                lifecycle_provenance = build_field_provenance(
                    payload=payload,
                    source_id=str(item["source_id"]),
                    temporal=lifecycle_temporal,
                    updated_at=now,
                    raw_payload_id=raw_payload_id,
                    quality_status="valid",
                    quality_flags=lifecycle_flags,
                    transformation_id=transformation_id,
                )
                conn.execute(
                    """
                    insert into data_revisions (
                        revision_id, dataset, entity_id, observation_key, source_id,
                        temporal_contract_version, time_basis, trade_date,
                        fiscal_period, period_start, period_end,
                        revision, observed_at, published_at, available_at, acquired_at,
                        effective_at, expires_at, payload_hash, raw_payload_id,
                        quality_status, quality_flags_json, is_fallback,
                        supersedes_revision_id, transformation_json, payload_json,
                        field_provenance_json, availability_contract_snapshot_json, created_at
                    ) values (
                        ?, 'security_master', ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, null, ?, ?,
                        ?, ?, ?, ?,
                        'valid', ?, 0,
                        ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        revision_id,
                        item["entity_id"],
                        item["observation_key"],
                        item["source_id"],
                        lifecycle_temporal.schema_version,
                        lifecycle_temporal.time_basis,
                        lifecycle_temporal.trade_date,
                        lifecycle_temporal.fiscal_period,
                        lifecycle_temporal.period_start,
                        lifecycle_temporal.period_end,
                        revision_number,
                        lifecycle_temporal.observed_at,
                        lifecycle_temporal.available_at,
                        lifecycle_temporal.acquired_at,
                        lifecycle_temporal.effective_at,
                        lifecycle_temporal.expires_at,
                        payload_digest,
                        raw_payload_id,
                        canonical_json(lifecycle_flags),
                        current["revision_id"] if current else None,
                        canonical_json(transformation),
                        canonical_json(payload),
                        canonical_json(
                            {
                                pointer: provenance.model_dump(mode="json")
                                for pointer, provenance in lifecycle_provenance.items()
                            }
                        ),
                        canonical_json(availability_snapshot),
                        now,
                    ),
                )
                conn.execute(
                    """
                    insert into data_lineage_edges (
                        lineage_id, output_revision_id, input_revision_id,
                        raw_payload_id, transformation_id, code_version,
                        parameters_json, created_at
                    ) values (?, ?, null, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"DL-{uuid4().hex}",
                        revision_id,
                        raw_payload_id,
                        transformation_id,
                        code_version,
                        canonical_json(transformation["parameters"]),
                        now,
                    ),
                )
                created_revision_ids.append(revision_id)
            for item in event_rows:
                source_dataset = str(item["source_dataset"])
                effective_at = normalize_timestamp(item["effective_at"], required=True)
                event_seed = ":".join(
                    (
                        str(item["entity_id"]),
                        str(item["event_type"]),
                        str(item.get("venue") or ""),
                        str(effective_at),
                        str(item["source_id"]),
                    )
                )
                event_id = f"ELC-{uuid5(NAMESPACE_URL, event_seed).hex}"
                before = conn.total_changes
                conn.execute(
                    """
                    insert or ignore into entity_lifecycle_events (
                        event_id, entity_id, event_type, venue, listing_type,
                        effective_at, published_at, acquired_at, source_id,
                        raw_payload_id, metadata_json, created_at
                    ) values (?, ?, ?, ?, ?, ?, null, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        item["entity_id"],
                        item["event_type"],
                        item.get("venue"),
                        item.get("listing_type"),
                        effective_at,
                        acquired_at,
                        item["source_id"],
                        raw_payload_ids.get(source_dataset),
                        canonical_json(item.get("metadata") or {}),
                        now,
                    ),
                )
                if conn.total_changes > before:
                    created_event_count += 1
            conn.commit()
        return {
            "entity_count": len(entity_rows),
            "identifier_count": len(identifier_rows),
            "created_revision_count": len(created_revision_ids),
            "reused_revision_count": reused_revision_count,
            "revision_ids": created_revision_ids,
            "created_event_count": created_event_count,
            "split_legacy_identifier_count": split_legacy_identifier_count,
            "skipped_current_identifier_count": len(skipped_identifier_indexes),
        }

    def entity_lifecycle(self, entity_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            entity_row = conn.execute(
                "select * from market_entities where entity_id=?",
                (entity_id,),
            ).fetchone()
            events = [
                {
                    **dict(row),
                    "metadata": json.loads(row["metadata_json"]),
                }
                for row in conn.execute(
                    """
                    select * from entity_lifecycle_events
                     where entity_id=?
                     order by effective_at, event_type, venue
                    """,
                    (entity_id,),
                ).fetchall()
            ]
        return {
            "schema_version": "stock_ai.entity_lifecycle.v1",
            "entity": self._entity_row(entity_row) if entity_row else None,
            "event_count": len(events),
            "events": events,
        }

    def historical_universe_records(self, *, knowledge_at: str) -> list[dict[str, Any]]:
        """Project lifecycle intervals without inventing historical knowledge.

        Security-master endpoints presently do not always publish the original
        disclosure time.  Their rows are still returned with ``available_at``
        set to ``None`` so the PIT universe gate can record why a replay is
        unavailable, rather than silently using the current security list.
        ``knowledge_at`` is validated here for an auditable caller contract;
        filtering belongs to the PIT receipt, which must count excluded rows.
        """

        normalize_timestamp(knowledge_at, required=True)
        start_events = {
            "entered_emerging",
            "listed",
            "listed_etf",
            "listed_otc",
            "listed_twse",
            "listed_warrant",
        }
        end_events = {"delisted", "expires"}
        with self._connect() as conn:
            rows = conn.execute(
                """
                select l.event_id, l.entity_id, l.event_type, l.venue,
                       l.listing_type, l.effective_at, l.published_at,
                       l.acquired_at, l.source_id, e.entity_type,
                       e.metadata_json
                  from entity_lifecycle_events l
                  join market_entities e on e.entity_id = l.entity_id
                 order by l.entity_id, l.effective_at, l.event_id
                """
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            grouped.setdefault(str(item["entity_id"]), []).append(item)

        records: list[dict[str, Any]] = []
        for entity_id, events in grouped.items():
            starts = [item for item in events if item["event_type"] in start_events]
            ends = [item for item in events if item["event_type"] in end_events]
            for start in starts:
                start_time = normalize_timestamp(start["effective_at"], required=True)
                matching_end = next(
                    (
                        item
                        for item in ends
                        if normalize_timestamp(item["effective_at"], required=True) > start_time
                    ),
                    None,
                )
                metadata = dict(start["metadata"])
                records.append(
                    {
                        "entity_id": entity_id,
                        "symbol": metadata.get("display_symbol"),
                        "entity_type": start["entity_type"],
                        "listing_type": start["listing_type"] or "unknown",
                        "effective_from": start_time,
                        "effective_until": (
                            normalize_timestamp(matching_end["effective_at"], required=True)
                            if matching_end is not None
                            else None
                        ),
                        "effective_until_available_at": (
                            matching_end["published_at"] if matching_end is not None else None
                        ),
                        "effective_until_ingested_at": (
                            matching_end["acquired_at"] if matching_end is not None else None
                        ),
                        "available_at": start["published_at"],
                        "ingested_at": start["acquired_at"],
                        "historical_pit_eligible": start["published_at"] is not None,
                        # Current security-master snapshots cannot prove that
                        # every historical listing/delisting is represented.
                        # A future certified source may set this true only
                        # after a coverage receipt is stored.
                        "universe_scope_id": "taiwan-listed-and-otc-securities",
                        "universe_coverage_complete": False,
                        "source_revision_id": (
                            f"{start['event_id']}:{matching_end['event_id']}"
                            if matching_end is not None
                            else str(start["event_id"])
                        ),
                        "source_id": start["source_id"],
                    }
                )
        return records

    def lifecycle_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            by_type = {
                str(row["entity_type"]): int(row["count"])
                for row in conn.execute(
                    """
                    select e.entity_type, count(*) as count
                      from market_entities e
                     where e.market='taiwan'
                       and not exists (
                         select 1 from entity_identity_merges m
                          where m.from_entity_id=e.entity_id
                       )
                     group by entity_type
                    """
                ).fetchall()
            }
            by_status = {
                str(row["lifecycle_status"]): int(row["count"])
                for row in conn.execute(
                    """
                    select e.lifecycle_status, count(*) as count
                      from market_entities e
                     where e.market='taiwan'
                       and not exists (
                         select 1 from entity_identity_merges m
                          where m.from_entity_id=e.entity_id
                       )
                     group by lifecycle_status
                    """
                ).fetchall()
            }
            by_exchange = {
                str(row["exchange"] or "unknown"): int(row["count"])
                for row in conn.execute(
                    """
                    select e.exchange, count(*) as count
                      from market_entities e
                     where e.market='taiwan'
                       and not exists (
                         select 1 from entity_identity_merges m
                          where m.from_entity_id=e.entity_id
                       )
                     group by e.exchange
                    """
                ).fetchall()
            }
            event_count = int(
                conn.execute("select count(*) from entity_lifecycle_events").fetchone()[0]
            )
            latest_event = conn.execute(
                "select max(effective_at) from entity_lifecycle_events"
            ).fetchone()[0]
            by_listing_type = {
                str(row["listing_type"]): int(row["count"])
                for row in conn.execute(
                    """
                    select listing_type, count(distinct entity_id) as count
                      from entity_lifecycle_events
                     group by listing_type
                    """
                ).fetchall()
                if row["listing_type"]
            }
        return {
            "schema_version": "stock_ai.security_lifecycle_summary.v1",
            "by_entity_type": by_type,
            "by_status": by_status,
            "by_exchange": by_exchange,
            "by_listing_type": by_listing_type,
            "event_count": event_count,
            "latest_effective_at": latest_event,
        }

    def security_lifecycle_quality(self) -> dict[str, Any]:
        required_listing_types = {
            "listed",
            "otc",
            "emerging",
            "etf",
            "warrant",
            "index",
            "delisted",
        }
        summary = self.lifecycle_summary()
        with self._connect() as conn:
            missing_display_symbols = int(
                conn.execute(
                    """
                    select count(*) from market_entities e
                     where e.market='taiwan'
                       and not exists (
                         select 1 from entity_identity_merges m
                          where m.from_entity_id=e.entity_id
                       )
                       and not exists (
                         select 1 from entity_identifiers i
                          where i.entity_id=e.entity_id
                            and i.identifier_type='display_symbol'
                            and i.superseded_at is null
                       )
                    """
                ).fetchone()[0]
            )
            invalid_intervals = int(
                conn.execute(
                    """
                    select count(*) from entity_identifiers
                     where valid_from is not null and valid_from != ''
                       and valid_to is not null
                       and valid_to < valid_from
                       and superseded_at is null
                    """
                ).fetchone()[0]
            )
            orphan_events = int(
                conn.execute(
                    """
                    select count(*) from entity_lifecycle_events l
                      left join market_entities e on e.entity_id=l.entity_id
                     where e.entity_id is null
                    """
                ).fetchone()[0]
            )
        observed_types = set(summary["by_listing_type"])
        missing_coverage = sorted(required_listing_types - observed_types)
        issue_count = missing_display_symbols + invalid_intervals + orphan_events
        return {
            "schema_version": "stock_ai.security_lifecycle_quality.v1",
            "status": (
                "failed"
                if orphan_events or missing_coverage
                else "warning"
                if issue_count
                else "passed"
            ),
            "required_listing_types": sorted(required_listing_types),
            "observed_listing_types": sorted(observed_types),
            "missing_coverage": missing_coverage,
            "missing_display_symbol_count": missing_display_symbols,
            "invalid_identifier_interval_count": invalid_intervals,
            "orphan_event_count": orphan_events,
            "event_count": summary["event_count"],
        }

    @serialized_warehouse_write
    def record_raw_payload(
        self,
        *,
        source_id: str,
        payload: Any,
        request_url: str | None = None,
        requested_at: str | None = None,
        received_at: str | None = None,
        http_status: int | None = 200,
        content_type: str | None = "application/json",
        content_encoding: str = "utf-8",
        raw_body: bytes | str | None = None,
        parser_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        identity_payload: Any | None = None,
    ) -> str:
        stored_payload = payload if identity_payload is None else identity_payload
        payload_hash = content_hash(stored_payload)
        raw_payload_id = "RAW-" + sha256(
            f"{source_id}:{payload_hash}".encode("utf-8")
        ).hexdigest()[:32]
        requested = normalize_timestamp(requested_at or utc_now(), required=True)
        received = normalize_timestamp(received_at or utc_now(), required=True)
        assert requested is not None and received is not None
        media_type = str(content_type or "application/octet-stream").split(";", 1)[0].strip()
        selected_parser = parser_id or (
            "stock_ai.raw.csv.v1"
            if "csv" in media_type
            else "stock_ai.raw.json.v1"
            if "json" in media_type
            else "stock_ai.raw.text.v1"
        )
        capture_metadata = dict(metadata or {})
        if raw_body is None:
            body = canonical_json(stored_payload).encode("utf-8")
            capture_metadata.setdefault(
                "capture_representation",
                "canonical_json_reconstruction",
            )
            content_encoding = "utf-8"
        elif isinstance(raw_body, str):
            body = raw_body.encode(content_encoding)
            capture_metadata.setdefault("capture_representation", "exact_source_bytes")
        else:
            body = bytes(raw_body)
            capture_metadata.setdefault("capture_representation", "exact_source_bytes")
        wire_hash = sha256(body).hexdigest()
        raw_object_id = "RDO-" + sha256(
            f"{source_id}:{wire_hash}".encode("utf-8")
        ).hexdigest()[:32]
        now = utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                """
                select raw_payload_id from raw_data_payloads
                 where source_id=? and payload_hash=?
                """,
                (source_id, payload_hash),
            ).fetchone()
            if existing is not None:
                raw_payload_id = str(existing["raw_payload_id"])
            conn.execute(
                """
                insert or ignore into raw_data_payloads (
                    raw_payload_id, source_id, request_url, requested_at,
                    received_at, http_status, content_type, payload_hash,
                    payload_json, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_payload_id,
                    source_id,
                    request_url,
                    requested,
                    received,
                    http_status,
                    media_type,
                    payload_hash,
                    canonical_json(stored_payload),
                    canonical_json(capture_metadata),
                ),
            )
            conn.execute(
                """
                insert or ignore into raw_data_objects (
                    raw_object_id, source_id, request_url, requested_at,
                    received_at, http_status, media_type, content_encoding,
                    wire_hash, byte_length, body_blob, metadata_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_object_id,
                    source_id,
                    request_url,
                    requested,
                    received,
                    http_status,
                    media_type,
                    content_encoding,
                    wire_hash,
                    len(body),
                    body,
                    canonical_json(capture_metadata),
                    now,
                ),
            )
            conn.execute(
                """
                insert or ignore into raw_payload_objects (
                    raw_payload_id, raw_object_id, parser_id, linked_at
                ) values (?, ?, ?, ?)
                """,
                (raw_payload_id, raw_object_id, selected_parser, now),
            )
            conn.commit()
        return raw_payload_id

    def raw_payload(self, raw_payload_id: str, *, include_body: bool = False) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select p.raw_payload_id, p.source_id, p.request_url,
                       p.requested_at, p.received_at, p.http_status,
                       p.content_type, p.payload_hash, p.metadata_json,
                       o.raw_object_id, o.media_type, o.content_encoding,
                       o.wire_hash, o.byte_length, o.body_blob,
                       l.parser_id
                  from raw_data_payloads p
                  join raw_payload_objects l on l.raw_payload_id=p.raw_payload_id
                  join raw_data_objects o on o.raw_object_id=l.raw_object_id
                 where p.raw_payload_id=?
                 order by o.received_at desc, o.raw_object_id desc
                 limit 1
                """,
                (raw_payload_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        body = bytes(payload.pop("body_blob"))
        payload["metadata"] = json.loads(payload.pop("metadata_json"))
        payload["integrity_status"] = (
            "passed" if sha256(body).hexdigest() == payload["wire_hash"] else "failed"
        )
        if include_body:
            payload["body"] = body
        return payload

    def list_raw_payloads(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            identifiers = [
                str(row["raw_payload_id"])
                for row in conn.execute(
                    """
                    select distinct p.raw_payload_id, max(o.received_at) as latest
                      from raw_data_payloads p
                      join raw_payload_objects l on l.raw_payload_id=p.raw_payload_id
                      join raw_data_objects o on o.raw_object_id=l.raw_object_id
                     group by p.raw_payload_id
                     order by latest desc
                     limit ?
                    """,
                    (max(1, min(int(limit), 1000)),),
                ).fetchall()
            ]
        return [
            item
            for raw_payload_id in identifiers
            if (item := self.raw_payload(raw_payload_id)) is not None
        ]

    @serialized_warehouse_write
    def reprocess_raw_payload(
        self,
        raw_payload_id: str,
        *,
        dataset: str | None = None,
        transformation_id: str = "stock_ai.raw_cleaning_replay.v1",
        code_version: str = "working-copy",
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw = self.raw_payload(raw_payload_id, include_body=True)
        if raw is None:
            raise KeyError(raw_payload_id)
        run_id = f"RPR-{uuid4().hex}"
        started_at = utc_now()
        selected_dataset = dataset or str(raw["metadata"].get("dataset") or "") or None
        try:
            body = raw.pop("body")
            if sha256(body).hexdigest() != raw["wire_hash"]:
                raise ValueError("raw object SHA-256 integrity check failed")
            encoding = str(raw["content_encoding"] or "utf-8")
            if raw["parser_id"] == "stock_ai.raw.json.v1":
                cleaned = json.loads(body.decode(encoding))
            elif raw["parser_id"] == "stock_ai.raw.csv.v1":
                cleaned = list(csv.DictReader(io.StringIO(body.decode(encoding))))
            elif raw["parser_id"] == "stock_ai.raw.text.v1":
                cleaned = body.decode(encoding)
            else:
                raise ValueError(f"Unsupported immutable raw parser: {raw['parser_id']}")
            output_hash = content_hash(cleaned)
            if output_hash != raw["payload_hash"]:
                raise ValueError(
                    "cleaning replay output hash does not match the captured parsed payload"
                )
            record_count = len(cleaned) if isinstance(cleaned, (list, dict)) else 1
            completed_at = utc_now()
            self._store_raw_reprocessing_run(
                run_id=run_id,
                raw_payload_id=raw_payload_id,
                raw_object_id=str(raw["raw_object_id"]),
                dataset=selected_dataset,
                parser_id=str(raw["parser_id"]),
                transformation_id=transformation_id,
                code_version=code_version,
                input_wire_hash=str(raw["wire_hash"]),
                output_payload_hash=output_hash,
                output_record_count=record_count,
                status="passed",
                error={},
                parameters=parameters or {},
                started_at=started_at,
                completed_at=completed_at,
            )
            return {
                "schema_version": "stock_ai.raw_reprocessing_result.v1",
                "run_id": run_id,
                "raw_payload_id": raw_payload_id,
                "raw_object_id": raw["raw_object_id"],
                "dataset": selected_dataset,
                "parser_id": raw["parser_id"],
                "transformation_id": transformation_id,
                "input_wire_hash": raw["wire_hash"],
                "output_payload_hash": output_hash,
                "output_record_count": record_count,
                "status": "passed",
                "started_at": started_at,
                "completed_at": completed_at,
            }
        except Exception as exc:
            completed_at = utc_now()
            self._store_raw_reprocessing_run(
                run_id=run_id,
                raw_payload_id=raw_payload_id,
                raw_object_id=str(raw["raw_object_id"]),
                dataset=selected_dataset,
                parser_id=str(raw["parser_id"]),
                transformation_id=transformation_id,
                code_version=code_version,
                input_wire_hash=str(raw["wire_hash"]),
                output_payload_hash=None,
                output_record_count=None,
                status="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
                parameters=parameters or {},
                started_at=started_at,
                completed_at=completed_at,
            )
            raise

    @serialized_warehouse_write
    def _store_raw_reprocessing_run(
        self,
        *,
        run_id: str,
        raw_payload_id: str,
        raw_object_id: str,
        dataset: str | None,
        parser_id: str,
        transformation_id: str,
        code_version: str,
        input_wire_hash: str,
        output_payload_hash: str | None,
        output_record_count: int | None,
        status: str,
        error: dict[str, Any],
        parameters: dict[str, Any],
        started_at: str,
        completed_at: str,
    ) -> None:
        values = (
            run_id,
            raw_payload_id,
            raw_object_id,
            dataset,
            parser_id,
            transformation_id,
            code_version,
            input_wire_hash,
            output_payload_hash,
            output_record_count,
            status,
            canonical_json(error),
            canonical_json(parameters),
            started_at,
            completed_at,
        )
        for attempt in range(3):
            try:
                with self._connect() as conn:
                    conn.execute("pragma busy_timeout = 15000")
                    conn.execute(
                        """
                        insert into raw_reprocessing_runs (
                            run_id, raw_payload_id, raw_object_id, dataset,
                            parser_id, transformation_id, code_version,
                            input_wire_hash, output_payload_hash,
                            output_record_count, status, error_json,
                            parameters_json, started_at, completed_at
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    conn.commit()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))

    @serialized_warehouse_write
    def write_daily_price_revision_batch(
        self,
        *,
        entity_id: str,
        source_id: str,
        records: Iterable[dict[str, Any]],
        raw_payload_id: str,
        acquired_at: str,
        is_fallback: bool,
        transformation_id: str,
        code_version: str,
    ) -> dict[str, int]:
        """Persist a complete daily-price import with one SQLite transaction."""

        record_list = [dict(record) for record in records]
        acquired = normalize_timestamp(acquired_at, required=True)
        assert acquired is not None
        now = utc_now()
        created = 0
        reused = 0
        with self._connect() as conn:
            observation_keys = [str(record["date"]) for record in record_list]
            existing_by_key: dict[str, sqlite3.Row] = {}
            if observation_keys:
                existing_rows = conn.execute(
                    """
                    with ranked as (
                        select *,
                               row_number() over (
                                   partition by observation_key
                                   order by revision desc, created_at desc
                               ) as row_rank
                          from data_revisions
                         where dataset='prices_daily' and entity_id=?
                           and source_id=? and observation_key>=?
                           and observation_key<=?
                    )
                    select * from ranked where row_rank=1
                    """,
                    (
                        entity_id,
                        source_id,
                        min(observation_keys),
                        max(observation_keys),
                    ),
                ).fetchall()
                existing_by_key = {
                    str(row["observation_key"]): row for row in existing_rows
                }
            for payload in record_list:
                observation_key = str(payload["date"])
                payload_digest = content_hash(payload)
                current = existing_by_key.get(observation_key)
                if current and current["payload_hash"] == payload_digest:
                    reused += 1
                    continue
                temporal = TemporalCoordinates(
                    time_basis="trade_date",
                    trade_date=observation_key,
                    observed_at=observation_key,
                    # Store the reviewed market availability separately from
                    # the local acquisition time.  Research still requires
                    # both, so a present-day import cannot leak backwards.
                    available_at=acquired,
                    acquired_at=acquired,
                    effective_at=observation_key,
                )
                availability_snapshot = availability_contract_snapshot(
                    source_id=source_id,
                    dataset="prices_daily",
                    temporal=temporal,
                )
                contract_available_at = normalize_timestamp(
                    availability_snapshot["decision"].get("available_at"),
                    required=True,
                )
                assert contract_available_at is not None
                if contract_available_at <= temporal.acquired_at:
                    temporal = TemporalCoordinates.model_validate(
                        {
                            **temporal.model_dump(mode="json"),
                            "available_at": contract_available_at,
                        }
                    )
                revision = int(current["revision"]) + 1 if current else 1
                revision_id = f"DRV-{uuid4().hex}"
                supersedes = str(current["revision_id"]) if current else None
                provenance = build_field_provenance(
                    payload=payload,
                    source_id=source_id,
                    temporal=temporal,
                    updated_at=now,
                    raw_payload_id=raw_payload_id,
                    quality_status="valid",
                    quality_flags=[],
                    transformation_id=transformation_id,
                )
                conn.execute(
                    """
                    insert into data_revisions (
                        revision_id, dataset, entity_id, observation_key, source_id,
                        temporal_contract_version, time_basis, trade_date,
                        fiscal_period, period_start, period_end,
                        revision, observed_at, published_at, available_at, acquired_at,
                        effective_at, expires_at, payload_hash, raw_payload_id,
                        quality_status, quality_flags_json, is_fallback,
                        supersedes_revision_id, transformation_json, payload_json,
                        field_provenance_json, availability_contract_snapshot_json, created_at
                    ) values (?, 'prices_daily', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, 'valid', '[]', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        entity_id,
                        observation_key,
                        source_id,
                        temporal.schema_version,
                        temporal.time_basis,
                        temporal.trade_date,
                        temporal.fiscal_period,
                        temporal.period_start,
                        temporal.period_end,
                        revision,
                        temporal.observed_at,
                        temporal.published_at,
                        temporal.available_at,
                        temporal.acquired_at,
                        temporal.effective_at,
                        temporal.expires_at,
                        payload_digest,
                        raw_payload_id,
                        1 if is_fallback else 0,
                        supersedes,
                        canonical_json(
                            {
                                "transformation_id": transformation_id,
                                "code_version": code_version,
                                "parameters": {"batch": "complete_daily_history"},
                            }
                        ),
                        canonical_json(payload),
                        canonical_json(
                            {
                                pointer: item.model_dump(mode="json")
                                for pointer, item in provenance.items()
                            }
                        ),
                        canonical_json(availability_snapshot),
                        now,
                    ),
                )
                self._project_standard_record(
                    conn,
                    revision_id=revision_id,
                    dataset="prices_daily",
                    entity_id=entity_id,
                    observation_key=observation_key,
                    source_id=source_id,
                    revision=revision,
                    temporal=temporal,
                    quality_status="valid",
                    is_fallback=is_fallback,
                    payload=payload,
                    created_at=now,
                )
                conn.execute(
                    """
                    insert into data_lineage_edges (
                        lineage_id, output_revision_id, input_revision_id,
                        raw_payload_id, transformation_id, code_version,
                        parameters_json, created_at
                    ) values (?, ?, null, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"DL-{uuid4().hex}",
                        revision_id,
                        raw_payload_id,
                        transformation_id,
                        code_version,
                        canonical_json({"batch": "complete_daily_history"}),
                        now,
                    ),
                )
                created += 1
            conn.commit()
        return {
            "record_count": len(record_list),
            "created_revision_count": created,
            "reused_revision_count": reused,
        }

    @serialized_warehouse_write
    def write_adjusted_price_revision_batch(
        self,
        *,
        entity_id: str,
        source_id: str,
        records: Iterable[dict[str, Any]],
        acquired_at: str,
        transformation_id: str,
        code_version: str,
    ) -> dict[str, int]:
        """Persist raw/front/back adjusted daily projections in one transaction."""

        source_records = [dict(record) for record in records]
        acquired = normalize_timestamp(acquired_at, required=True)
        assert acquired is not None
        now = utc_now()
        created = 0
        reused = 0
        with self._connect() as conn:
            observation_keys = [str(record["date"]) for record in source_records]
            existing_by_key: dict[str, list[sqlite3.Row]] = {}
            if observation_keys:
                rows = conn.execute(
                    """
                    select *
                      from data_revisions
                     where dataset='prices_adjusted_daily' and entity_id=?
                       and source_id=? and observation_key>=?
                       and observation_key<=?
                     order by observation_key, revision desc
                    """,
                    (
                        entity_id,
                        source_id,
                        min(observation_keys),
                        max(observation_keys),
                    ),
                ).fetchall()
                for row in rows:
                    existing_by_key.setdefault(
                        str(row["observation_key"]), []
                    ).append(row)
            for source_record in source_records:
                input_revision_ids = [
                    str(value)
                    for value in source_record.pop("_input_revision_ids", [])
                    if str(value)
                ]
                payload = source_record
                observation_key = str(payload["date"])
                payload_digest = content_hash(payload)
                previous_rows = existing_by_key.get(observation_key, [])
                if any(row["payload_hash"] == payload_digest for row in previous_rows):
                    reused += 1
                    continue
                current = previous_rows[0] if previous_rows else None
                revision = int(current["revision"]) + 1 if current else 1
                revision_id = f"DRV-{uuid4().hex}"
                supersedes = str(current["revision_id"]) if current else None
                temporal = TemporalCoordinates(
                    time_basis="trade_date",
                    trade_date=observation_key,
                    observed_at=observation_key,
                    available_at=acquired,
                    acquired_at=acquired,
                    effective_at=observation_key,
                )
                availability_snapshot = availability_contract_snapshot(
                    source_id=source_id,
                    dataset="prices_adjusted_daily",
                    temporal=temporal,
                )
                provenance = build_field_provenance(
                    payload=payload,
                    source_id=source_id,
                    temporal=temporal,
                    updated_at=now,
                    raw_payload_id=None,
                    quality_status="valid",
                    quality_flags=[],
                    transformation_id=transformation_id,
                )
                conn.execute(
                    """
                    insert into data_revisions (
                        revision_id, dataset, entity_id, observation_key, source_id,
                        temporal_contract_version, time_basis, trade_date,
                        fiscal_period, period_start, period_end,
                        revision, observed_at, published_at, available_at, acquired_at,
                        effective_at, expires_at, payload_hash, raw_payload_id,
                        quality_status, quality_flags_json, is_fallback,
                        supersedes_revision_id, transformation_json, payload_json,
                        field_provenance_json, availability_contract_snapshot_json, created_at
                    ) values (?, 'prices_adjusted_daily', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, null, 'valid', '[]', 0, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        entity_id,
                        observation_key,
                        source_id,
                        temporal.schema_version,
                        temporal.time_basis,
                        temporal.trade_date,
                        temporal.fiscal_period,
                        temporal.period_start,
                        temporal.period_end,
                        revision,
                        temporal.observed_at,
                        temporal.published_at,
                        temporal.available_at,
                        temporal.acquired_at,
                        temporal.effective_at,
                        temporal.expires_at,
                        payload_digest,
                        supersedes,
                        canonical_json(
                            {
                                "transformation_id": transformation_id,
                                "code_version": code_version,
                                "parameters": {
                                    "factor_set_id": payload.get("factor_set_id"),
                                    "price_bases": [
                                        "unadjusted",
                                        "forward_adjusted",
                                        "backward_adjusted",
                                    ],
                                },
                            }
                        ),
                        canonical_json(payload),
                        canonical_json(
                            {
                                pointer: item.model_dump(mode="json")
                                for pointer, item in provenance.items()
                            }
                        ),
                        canonical_json(availability_snapshot),
                        now,
                    ),
                )
                self._project_standard_record(
                    conn,
                    revision_id=revision_id,
                    dataset="prices_adjusted_daily",
                    entity_id=entity_id,
                    observation_key=observation_key,
                    source_id=source_id,
                    revision=revision,
                    temporal=temporal,
                    quality_status="valid",
                    is_fallback=False,
                    payload=payload,
                    created_at=now,
                )
                for input_revision_id in input_revision_ids:
                    conn.execute(
                        """
                        insert into data_lineage_edges (
                            lineage_id, output_revision_id, input_revision_id,
                            raw_payload_id, transformation_id, code_version,
                            parameters_json, created_at
                        ) values (?, ?, ?, null, ?, ?, ?, ?)
                        """,
                        (
                            f"DL-{uuid4().hex}",
                            revision_id,
                            input_revision_id,
                            transformation_id,
                            code_version,
                            canonical_json(
                                {
                                    "factor_set_id": payload.get("factor_set_id"),
                                }
                            ),
                            now,
                        ),
                    )
                created += 1
            conn.commit()
        return {
            "record_count": len(source_records),
            "created_revision_count": created,
            "reused_revision_count": reused,
        }

    @serialized_warehouse_write
    def write_revision(
        self,
        *,
        dataset: str,
        entity_id: str,
        observation_key: str,
        source_id: str,
        temporal: TemporalCoordinates,
        payload: dict[str, Any],
        raw_payload_id: str | None,
        quality_status: str = "valid",
        quality_flags: list[str] | None = None,
        field_provenance: dict[
            str,
            FieldProvenanceV1 | dict[str, Any],
        ]
        | None = None,
        is_fallback: bool = False,
        transformation_id: str = "stock_ai.normalizer.v1",
        code_version: str = "working-copy",
        parameters: dict[str, Any] | None = None,
        input_revision_ids: Iterable[str] = (),
    ) -> DataEnvelopeV2:
        availability_snapshot = availability_contract_snapshot(
            source_id=source_id,
            dataset=dataset,
            temporal=temporal,
        )
        payload_digest = revision_content_hash(dataset=dataset, payload=payload)
        now = utc_now()
        flags = list(quality_flags or [])
        provenance = build_field_provenance(
            payload=payload,
            source_id=source_id,
            temporal=temporal,
            updated_at=now,
            raw_payload_id=raw_payload_id,
            quality_status=quality_status,
            quality_flags=flags,
            transformation_id=transformation_id,
            overrides=field_provenance,
        )
        with self._connect() as conn:
            current = conn.execute(
                """
                select * from data_revisions
                 where dataset=? and entity_id=? and observation_key=? and source_id=?
                 order by revision desc limit 1
                """,
                (dataset, entity_id, observation_key, source_id),
            ).fetchone()
            if current and current["payload_hash"] == payload_digest:
                return self._revision_row(current)
            revision = int(current["revision"]) + 1 if current else 1
            revision_id = f"DRV-{uuid4().hex}"
            supersedes = current["revision_id"] if current else None
            transformation = {
                "transformation_id": transformation_id,
                "code_version": code_version,
                "parameters": parameters or {},
            }
            conn.execute(
                """
                insert into data_revisions (
                    revision_id, dataset, entity_id, observation_key, source_id,
                    temporal_contract_version, time_basis, trade_date,
                    fiscal_period, period_start, period_end,
                    revision, observed_at, published_at, available_at, acquired_at,
                    effective_at, expires_at, payload_hash, raw_payload_id,
                    quality_status, quality_flags_json, is_fallback,
                    supersedes_revision_id, transformation_json, payload_json,
                    field_provenance_json, availability_contract_snapshot_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    dataset,
                    entity_id,
                    observation_key,
                    source_id,
                    temporal.schema_version,
                    temporal.time_basis,
                    temporal.trade_date,
                    temporal.fiscal_period,
                    temporal.period_start,
                    temporal.period_end,
                    revision,
                    temporal.observed_at,
                    temporal.published_at,
                    temporal.available_at,
                    temporal.acquired_at,
                    temporal.effective_at,
                    temporal.expires_at,
                    payload_digest,
                    raw_payload_id,
                    quality_status,
                    canonical_json(flags),
                    1 if is_fallback else 0,
                    supersedes,
                    canonical_json(transformation),
                    canonical_json(payload),
                    canonical_json(
                        {
                            pointer: item.model_dump(mode="json")
                            for pointer, item in provenance.items()
                        }
                    ),
                    canonical_json(availability_snapshot),
                    now,
                ),
            )
            self._project_standard_record(
                conn,
                revision_id=revision_id,
                dataset=dataset,
                entity_id=entity_id,
                observation_key=observation_key,
                source_id=source_id,
                revision=revision,
                temporal=temporal,
                quality_status=quality_status,
                is_fallback=is_fallback,
                payload=payload,
                created_at=now,
            )
            lineage_inputs = list(input_revision_ids) or [None]
            for input_revision_id in lineage_inputs:
                conn.execute(
                    """
                    insert into data_lineage_edges (
                        lineage_id, output_revision_id, input_revision_id,
                        raw_payload_id, transformation_id, code_version,
                        parameters_json, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"DL-{uuid4().hex}",
                        revision_id,
                        input_revision_id,
                        raw_payload_id,
                        transformation_id,
                        code_version,
                        canonical_json(parameters or {}),
                        now,
                    ),
                )
            conn.commit()
            row = conn.execute(
                "select * from data_revisions where revision_id=?",
                (revision_id,),
            ).fetchone()
        assert row is not None
        return self._revision_row(row)

    @staticmethod
    def _project_standard_record(
        conn: sqlite3.Connection,
        *,
        revision_id: str,
        dataset: str,
        entity_id: str,
        observation_key: str,
        source_id: str,
        revision: int,
        temporal: TemporalCoordinates,
        quality_status: str,
        is_fallback: bool,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        domain = standard_warehouse_domain(dataset)
        if domain is None:
            return
        table_name = STANDARD_WAREHOUSE_TABLES[domain]
        conn.execute(
            f"""
            insert or ignore into {table_name} (
                revision_id, dataset, entity_id, observation_key, source_id,
                revision, observed_at, published_at, available_at, acquired_at,
                effective_at, expires_at, quality_status, is_fallback,
                record_json, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                dataset,
                entity_id,
                observation_key,
                source_id,
                revision,
                temporal.observed_at,
                temporal.published_at,
                temporal.available_at,
                temporal.acquired_at,
                temporal.effective_at,
                temporal.expires_at,
                quality_status,
                1 if is_fallback else 0,
                canonical_json(payload),
                created_at,
            ),
        )

    def standard_records(
        self,
        domain: str,
        *,
        dataset: str | None = None,
        entity_id: str | None = None,
        knowledge_at: str | None = None,
        effective_at: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        table_name = STANDARD_WAREHOUSE_TABLES.get(domain)
        if table_name is None:
            raise ValueError(
                f"Unknown warehouse domain: {domain}; expected {sorted(STANDARD_WAREHOUSE_TABLES)}"
            )
        knowledge = normalize_timestamp(knowledge_at or utc_now(), required=True)
        effective = normalize_timestamp(effective_at or knowledge, required=True)
        assert knowledge is not None and effective is not None
        clauses = [
            "(standard.published_at is null or standard.published_at <= ?)",
            "standard.available_at <= ?",
            "standard.acquired_at <= ?",
            "standard.effective_at <= ?",
            "(standard.expires_at is null or standard.expires_at > ?)",
        ]
        parameters: list[Any] = [knowledge, knowledge, knowledge, effective, effective]
        if dataset:
            if dataset not in STANDARD_WAREHOUSE_DATASETS[domain]:
                raise ValueError(f"{dataset} is not registered in warehouse domain {domain}")
            clauses.append("standard.dataset = ?")
            parameters.append(dataset)
        if entity_id:
            clauses.append("standard.entity_id = ?")
            parameters.append(entity_id)
        parameters.append(max(1, min(int(limit), 10000)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                with ranked as (
                    select standard.*, revision.availability_contract_snapshot_json,
                           row_number() over (
                               partition by standard.dataset, standard.entity_id,
                                            standard.observation_key, standard.source_id
                               order by standard.revision desc
                           ) as row_rank
                      from {table_name} as standard
                      left join data_revisions as revision
                        on revision.revision_id=standard.revision_id
                     where {' and '.join(clauses)}
                )
                select * from ranked where row_rank=1
                 order by coalesce(observed_at, effective_at) desc,
                          entity_id, observation_key, source_id
                 limit ?
                """,
                parameters,
            ).fetchall()
        return [
            {
                **{
                    key: value
                    for key, value in dict(row).items()
                    if key not in {
                        "record_json",
                        "row_rank",
                        "availability_contract_snapshot_json",
                    }
                },
                "domain": domain,
                "record": json.loads(row["record_json"]),
                "availability_contract_snapshot": json.loads(
                    row["availability_contract_snapshot_json"] or "{}"
                ),
            }
            for row in rows
        ]

    def daily_price_history(
        self,
        *,
        entity_id: str,
        start_date: str,
        end_date: str,
        after: str | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Return one source-prioritized current revision per daily candle.

        Source revisions remain immutable and independently queryable. This read
        projection only chooses the non-fallback, highest-priority current source
        for each trade date; it never overwrites a lower-priority revision.
        """

        page_limit = max(1, min(int(limit), 5000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select p.*, coalesce(s.priority, 9999) as source_priority
                  from market_prices p
                  left join data_sources s on s.source_id=p.source_id
                 where p.dataset='prices_daily'
                   and p.entity_id=?
                   and p.observation_key>=?
                   and p.observation_key<=?
                   and p.revision=(
                       select max(current.revision)
                         from market_prices current
                        where current.dataset=p.dataset
                          and current.entity_id=p.entity_id
                          and current.observation_key=p.observation_key
                          and current.source_id=p.source_id
                   )
                 order by p.observation_key asc, p.is_fallback asc,
                          source_priority asc, p.revision desc, p.created_at desc
                """,
                (entity_id, start_date, end_date),
            ).fetchall()
        selected_by_date: dict[str, sqlite3.Row] = {}
        for row in rows:
            selected_by_date.setdefault(str(row["observation_key"]), row)
        selected = list(selected_by_date.values())
        total = len(selected)
        if after:
            selected = [
                row for row in selected if str(row["observation_key"]) > after
            ]
        has_more = len(selected) > page_limit
        page_rows = selected[:page_limit]
        items = [
            {
                **{
                    key: value
                    for key, value in dict(row).items()
                    if key
                    not in {
                        "record_json",
                        "source_priority",
                    }
                },
                "record": json.loads(row["record_json"]),
            }
            for row in page_rows
        ]
        return {
            "items": items,
            "total_count": total,
            "has_more": has_more,
            "next_cursor": (
                str(page_rows[-1]["observation_key"]) if has_more and page_rows else None
            ),
        }

    def adjusted_price_history(
        self,
        *,
        entity_id: str,
        source_id: str,
        factor_set_id: str,
        start_date: str,
        end_date: str,
        after: str | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        """Return one immutable adjusted projection per requested trade date."""

        page_limit = max(1, min(int(limit), 5000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                with matching as (
                    select p.*,
                           row_number() over (
                               partition by p.observation_key
                               order by p.revision desc, p.created_at desc
                           ) as row_rank
                      from market_prices p
                     where p.dataset='prices_adjusted_daily'
                       and p.entity_id=? and p.source_id=?
                       and p.observation_key>=? and p.observation_key<=?
                       and json_extract(p.record_json, '$.factor_set_id')=?
                )
                select * from matching
                 where row_rank=1
                 order by observation_key asc
                """,
                (
                    entity_id,
                    source_id,
                    start_date,
                    end_date,
                    factor_set_id,
                ),
            ).fetchall()
        total = len(rows)
        if after:
            rows = [row for row in rows if str(row["observation_key"]) > after]
        has_more = len(rows) > page_limit
        page_rows = rows[:page_limit]
        return {
            "items": [
                {
                    **{
                        key: value
                        for key, value in dict(row).items()
                        if key not in {"record_json", "row_rank"}
                    },
                    "record": json.loads(row["record_json"]),
                }
                for row in page_rows
            ],
            "total_count": total,
            "has_more": has_more,
            "next_cursor": (
                str(page_rows[-1]["observation_key"])
                if has_more and page_rows
                else None
            ),
        }

    def query(self, request: DataQuery) -> list[DataEnvelopeV2]:
        clauses = [
            "dataset = ?",
            "(published_at is null or published_at <= ?)",
            "available_at <= ?",
            "acquired_at <= ?",
            "effective_at <= ?",
            "(expires_at is null or expires_at > ?)",
        ]
        parameters: list[Any] = [
            request.dataset,
            request.knowledge_at,
            request.knowledge_at,
            request.knowledge_at,
            request.effective_at,
            request.effective_at,
        ]
        if request.entity_id:
            clauses.append("entity_id = ?")
            parameters.append(request.entity_id)
        if request.source_id:
            clauses.append("source_id = ?")
            parameters.append(request.source_id)
        parameters.append(request.limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                with ranked as (
                    select *,
                           row_number() over (
                               partition by dataset, entity_id, observation_key, source_id
                               order by revision desc
                           ) as row_rank
                      from data_revisions
                     where {' and '.join(clauses)}
                )
                select * from ranked
                 where row_rank=1
                 order by coalesce(observed_at, effective_at) desc,
                          entity_id, observation_key, source_id
                 limit ?
                """,
                parameters,
            ).fetchall()
        return [self._revision_row(row) for row in rows]

    def revision_history(
        self,
        *,
        dataset: str,
        entity_id: str,
        observation_key: str,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        clauses = [
            "dataset=?",
            "entity_id=?",
            "observation_key=?",
        ]
        parameters: list[Any] = [dataset, entity_id, observation_key]
        if source_id:
            clauses.append("source_id=?")
            parameters.append(source_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select * from data_revisions
                 where {' and '.join(clauses)}
                 order by source_id, revision, acquired_at, revision_id
                """,
                parameters,
            ).fetchall()

        issues: list[dict[str, Any]] = []
        previous_by_source: dict[str, sqlite3.Row] = {}
        for row in rows:
            source = str(row["source_id"])
            previous = previous_by_source.get(source)
            expected_revision = int(previous["revision"]) + 1 if previous else 1
            expected_supersedes = str(previous["revision_id"]) if previous else None
            if int(row["revision"]) != expected_revision:
                issues.append(
                    {
                        "revision_id": row["revision_id"],
                        "type": "revision_sequence_gap",
                        "expected": expected_revision,
                        "actual": int(row["revision"]),
                    }
                )
            if row["supersedes_revision_id"] != expected_supersedes:
                issues.append(
                    {
                        "revision_id": row["revision_id"],
                        "type": "supersedes_mismatch",
                        "expected": expected_supersedes,
                        "actual": row["supersedes_revision_id"],
                    }
                )
            previous_by_source[source] = row
        return {
            "schema_version": "stock_ai.revision_history.v1",
            "dataset": dataset,
            "entity_id": entity_id,
            "observation_key": observation_key,
            "source_id": source_id,
            "status": "passed" if not issues else "failed",
            "count": len(rows),
            "issues": issues,
            "items": [
                self._revision_row(row).model_dump(mode="json")
                for row in rows
            ],
        }

    @serialized_warehouse_write
    def create_revision_snapshot(
        self,
        *,
        dataset: str,
        knowledge_at: str | None = None,
        effective_at: str | None = None,
        entity_id: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        knowledge = normalize_timestamp(knowledge_at or utc_now(), required=True)
        effective = normalize_timestamp(effective_at or knowledge, required=True)
        assert knowledge is not None and effective is not None
        clauses = [
            "dataset = ?",
            "(published_at is null or published_at <= ?)",
            "available_at <= ?",
            "acquired_at <= ?",
            "effective_at <= ?",
            "(expires_at is null or expires_at > ?)",
        ]
        parameters: list[Any] = [
            dataset,
            knowledge,
            knowledge,
            knowledge,
            effective,
            effective,
        ]
        if entity_id:
            clauses.append("entity_id = ?")
            parameters.append(entity_id)
        if source_id:
            clauses.append("source_id = ?")
            parameters.append(source_id)
        now = utc_now()
        with self._connect() as conn:
            conn.execute("begin")
            rows = conn.execute(
                f"""
                with ranked as (
                    select *,
                           row_number() over (
                               partition by dataset, entity_id, observation_key, source_id
                               order by revision desc
                           ) as row_rank
                      from data_revisions
                     where {' and '.join(clauses)}
                )
                select * from ranked
                 where row_rank=1
                 order by entity_id, observation_key, source_id, revision_id
                """,
                parameters,
            ).fetchall()
            ordered_revisions = [
                {
                    "revision_id": str(row["revision_id"]),
                    "payload_hash": str(row["payload_hash"]),
                }
                for row in rows
            ]
            manifest = {
                "schema_version": "stock_ai.revision_snapshot_manifest.v1",
                "query": {
                    "dataset": dataset,
                    "knowledge_at": knowledge,
                    "effective_at": effective,
                    "entity_id": entity_id,
                    "source_id": source_id,
                },
                "item_count": len(ordered_revisions),
                "ordered_revision_hash": content_hash(ordered_revisions),
            }
            manifest_hash = content_hash(manifest)
            snapshot_id = f"DSN-{manifest_hash[:32]}"
            conn.execute(
                """
                insert or ignore into data_revision_snapshots (
                    snapshot_id, dataset, knowledge_at, effective_at, entity_id,
                    source_id, item_count, manifest_hash, manifest_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    dataset,
                    knowledge,
                    effective,
                    entity_id,
                    source_id,
                    len(rows),
                    manifest_hash,
                    canonical_json(manifest),
                    now,
                ),
            )
            conn.executemany(
                """
                insert or ignore into data_revision_snapshot_items (
                    snapshot_id, ordinal, revision_id, payload_hash
                ) values (?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        ordinal,
                        item["revision_id"],
                        item["payload_hash"],
                    )
                    for ordinal, item in enumerate(ordered_revisions)
                ],
            )
            conn.commit()
        result = self.revision_snapshot(snapshot_id, limit=1)
        assert result is not None
        return {
            **result["snapshot"],
            "integrity": result["integrity"],
        }

    def revision_snapshot(
        self,
        snapshot_id: str,
        *,
        offset: int = 0,
        limit: int = 500,
    ) -> dict[str, Any] | None:
        bounded_offset = max(0, int(offset))
        bounded_limit = max(1, min(int(limit), 10000))
        with self._connect() as conn:
            snapshot = conn.execute(
                "select * from data_revision_snapshots where snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                return None
            all_pairs = [
                {
                    "revision_id": str(row["revision_id"]),
                    "payload_hash": str(row["payload_hash"]),
                }
                for row in conn.execute(
                    """
                    select revision_id, payload_hash
                      from data_revision_snapshot_items
                     where snapshot_id=? order by ordinal
                    """,
                    (snapshot_id,),
                ).fetchall()
            ]
            rows = conn.execute(
                """
                select r.*
                  from data_revision_snapshot_items i
                  join data_revisions r on r.revision_id=i.revision_id
                 where i.snapshot_id=?
                 order by i.ordinal
                 limit ? offset ?
                """,
                (snapshot_id, bounded_limit, bounded_offset),
            ).fetchall()
        manifest = json.loads(snapshot["manifest_json"])
        observed_ordered_hash = content_hash(all_pairs)
        observed_manifest_hash = content_hash(
            {
                **manifest,
                "item_count": len(all_pairs),
                "ordered_revision_hash": observed_ordered_hash,
            }
        )
        integrity = {
            "status": (
                "passed"
                if len(all_pairs) == int(snapshot["item_count"])
                and observed_ordered_hash == manifest["ordered_revision_hash"]
                and observed_manifest_hash == snapshot["manifest_hash"]
                else "failed"
            ),
            "expected_item_count": int(snapshot["item_count"]),
            "observed_item_count": len(all_pairs),
            "expected_manifest_hash": snapshot["manifest_hash"],
            "observed_manifest_hash": observed_manifest_hash,
        }
        return {
            "schema_version": "stock_ai.revision_snapshot.v1",
            "snapshot": {
                **dict(snapshot),
                "manifest": manifest,
            },
            "integrity": integrity,
            "offset": bounded_offset,
            "limit": bounded_limit,
            "count": len(rows),
            "items": [
                self._revision_row(row).model_dump(mode="json")
                for row in rows
            ],
        }

    @serialized_warehouse_write
    def record_lineage_artifact(
        self,
        *,
        artifact_type: str,
        name: str,
        value: Any,
        inputs: Iterable[dict[str, Any]],
        transformation_id: str,
        code_version: str = "working-copy",
        entity_id: str | None = None,
        observation_key: str | None = None,
        quality_status: str = "valid",
        parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one derived value only when every input reaches immutable raw data."""

        normalized_type = artifact_type.strip()
        normalized_name = name.strip()
        normalized_transformation = transformation_id.strip()
        normalized_code_version = code_version.strip()
        if not normalized_type or not normalized_name:
            raise ValueError("artifact_type and name are required")
        if not normalized_transformation or not normalized_code_version:
            raise ValueError("transformation_id and code_version are required")
        if quality_status not in {"valid", "warning", "invalid", "unavailable"}:
            raise ValueError("unsupported quality_status")

        normalized_inputs: list[dict[str, Any]] = []
        for position, candidate in enumerate(inputs):
            if not isinstance(candidate, dict):
                raise ValueError("lineage inputs must be objects")
            kind = str(candidate.get("kind") or "").strip().casefold()
            input_id = str(
                candidate.get("id")
                or candidate.get("revision_id")
                or candidate.get("artifact_id")
                or ""
            ).strip()
            if kind not in {"revision", "artifact"} or not input_id:
                raise ValueError("each input requires kind revision/artifact and id")
            fields = [
                str(field).strip()
                for field in candidate.get("fields", [])
                if str(field).strip()
            ]
            if any(not field.startswith("/") for field in fields):
                raise ValueError("input fields must use RFC 6901 JSON pointers")
            role = str(candidate.get("role") or f"input_{position + 1}").strip()
            if not role:
                raise ValueError("input role cannot be empty")
            graph = self.lineage_graph(input_id)
            if graph["target"] is None:
                raise KeyError(input_id)
            if graph["completeness"]["status"] != "complete":
                raise ValueError(
                    f"lineage input does not reach verified raw data: {input_id}"
                )
            allowed_fields = (
                set(graph["target"].get("field_pointers", []))
                if kind == "revision"
                else set(payload_leaf_pointers(graph["target"].get("value")))
            )
            unknown_fields = set(fields) - allowed_fields
            if unknown_fields:
                raise ValueError(
                    f"input fields do not exist on {input_id}: {sorted(unknown_fields)}"
                )
            normalized_inputs.append(
                {
                    "kind": kind,
                    "id": input_id,
                    "role": role,
                    "fields": sorted(set(fields)),
                }
            )
        if not normalized_inputs:
            raise ValueError("at least one complete lineage input is required")
        normalized_inputs.sort(
            key=lambda item: (
                item["kind"],
                item["id"],
                item["role"],
                canonical_json(item["fields"]),
            )
        )

        artifact_document = {
            "artifact_type": normalized_type,
            "name": normalized_name,
            "entity_id": entity_id,
            "observation_key": observation_key,
            "value": value,
            "quality_status": quality_status,
            "inputs": normalized_inputs,
            "transformation_id": normalized_transformation,
            "code_version": normalized_code_version,
            "parameters": parameters or {},
            "metadata": metadata or {},
        }
        artifact_hash = content_hash(artifact_document)
        artifact_id = f"DLA-{artifact_hash[:32]}"
        now = utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "select artifact_id from data_lineage_artifacts where artifact_hash=?",
                (artifact_hash,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    insert into data_lineage_artifacts (
                        artifact_id, artifact_type, name, entity_id,
                        observation_key, value_json, quality_status,
                        artifact_hash, metadata_json, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        normalized_type,
                        normalized_name,
                        entity_id,
                        observation_key,
                        canonical_json(value),
                        quality_status,
                        artifact_hash,
                        canonical_json(metadata or {}),
                        now,
                    ),
                )
                for item in normalized_inputs:
                    input_artifact_id = (
                        item["id"] if item["kind"] == "artifact" else None
                    )
                    input_revision_id = (
                        item["id"] if item["kind"] == "revision" else None
                    )
                    edge_document = {
                        "output_artifact_id": artifact_id,
                        "input_artifact_id": input_artifact_id,
                        "input_revision_id": input_revision_id,
                        "input_role": item["role"],
                        "input_fields": item["fields"],
                        "transformation_id": normalized_transformation,
                        "code_version": normalized_code_version,
                        "parameters": parameters or {},
                    }
                    conn.execute(
                        """
                        insert into data_artifact_lineage_edges (
                            lineage_id, output_artifact_id, input_artifact_id,
                            input_revision_id, input_role, input_fields_json,
                            transformation_id, code_version, parameters_json,
                            created_at
                        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"DLE-{content_hash(edge_document)[:32]}",
                            artifact_id,
                            input_artifact_id,
                            input_revision_id,
                            item["role"],
                            canonical_json(item["fields"]),
                            normalized_transformation,
                            normalized_code_version,
                            canonical_json(parameters or {}),
                            now,
                        ),
                    )
                conn.commit()
            else:
                artifact_id = str(existing["artifact_id"])
        artifact = self.lineage_artifact(artifact_id)
        assert artifact is not None
        return artifact

    def lineage_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from data_lineage_artifacts where artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                return None
            edges = conn.execute(
                """
                select * from data_artifact_lineage_edges
                 where output_artifact_id=?
                 order by created_at, lineage_id
                """,
                (artifact_id,),
            ).fetchall()
        return {
            **self._lineage_artifact_row(row),
            "inputs": [
                {
                    **dict(edge),
                    "kind": (
                        "artifact"
                        if edge["input_artifact_id"] is not None
                        else "revision"
                    ),
                    "input_id": (
                        edge["input_artifact_id"]
                        or edge["input_revision_id"]
                    ),
                    "input_fields": json.loads(edge["input_fields_json"]),
                    "parameters": json.loads(edge["parameters_json"]),
                }
                for edge in edges
            ],
        }

    def list_lineage_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if artifact_type:
            clauses.append("artifact_type=?")
            parameters.append(artifact_type)
        if entity_id:
            clauses.append("entity_id=?")
            parameters.append(entity_id)
        parameters.append(max(1, min(int(limit), 1000)))
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select * from data_lineage_artifacts
                 {where}
                 order by created_at desc, artifact_id desc
                 limit ?
                """,
                parameters,
            ).fetchall()
        return [self._lineage_artifact_row(row) for row in rows]

    def lineage_graph(self, target_id: str) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        edge_keys: set[tuple[str, str, str, str]] = set()
        queue: list[tuple[str, int]] = [(target_id, 0)]
        visited: set[str] = set()
        raw_payload_ids: set[str] = set()
        missing_node_ids: set[str] = set()
        missing_raw_revision_ids: set[str] = set()
        max_depth = 0

        def add_edge(edge: dict[str, Any]) -> None:
            key = (
                str(edge["from"]),
                str(edge["to"]),
                str(edge["edge_type"]),
                str(edge.get("lineage_id") or ""),
            )
            if key not in edge_keys:
                edge_keys.add(key)
                edges.append(edge)

        with self._connect() as conn:
            while queue:
                current_id, depth = queue.pop(0)
                if current_id in visited:
                    continue
                visited.add(current_id)
                max_depth = max(max_depth, depth)
                artifact = conn.execute(
                    "select * from data_lineage_artifacts where artifact_id=?",
                    (current_id,),
                ).fetchone()
                if artifact is not None:
                    artifact_node = self._lineage_artifact_row(artifact)
                    nodes[current_id] = {
                        "id": current_id,
                        "type": "artifact",
                        **artifact_node,
                        "field_pointers": payload_leaf_pointers(
                            artifact_node["value"]
                        ),
                    }
                    artifact_edges = conn.execute(
                        """
                        select * from data_artifact_lineage_edges
                         where output_artifact_id=?
                         order by created_at, lineage_id
                        """,
                        (current_id,),
                    ).fetchall()
                    if not artifact_edges:
                        missing_node_ids.add(f"input:{current_id}")
                    for row in artifact_edges:
                        input_id = str(
                            row["input_artifact_id"]
                            or row["input_revision_id"]
                        )
                        add_edge(
                            {
                                "lineage_id": row["lineage_id"],
                                "from": input_id,
                                "to": current_id,
                                "edge_type": "derived_from",
                                "input_role": row["input_role"],
                                "input_fields": json.loads(
                                    row["input_fields_json"]
                                ),
                                "transformation_id": row["transformation_id"],
                                "code_version": row["code_version"],
                                "parameters": json.loads(
                                    row["parameters_json"]
                                ),
                                "created_at": row["created_at"],
                            }
                        )
                        queue.append((input_id, depth + 1))
                    continue

                revision = conn.execute(
                    "select * from data_revisions where revision_id=?",
                    (current_id,),
                ).fetchone()
                if revision is None:
                    missing_node_ids.add(current_id)
                    continue
                envelope = self._revision_row(revision).model_dump(mode="json")
                nodes[current_id] = {
                    "id": current_id,
                    "type": "revision",
                    **envelope,
                    "field_pointers": sorted(envelope["field_provenance"]),
                }
                revision_edges = conn.execute(
                    """
                    select * from data_lineage_edges
                     where output_revision_id=?
                     order by created_at, lineage_id
                    """,
                    (current_id,),
                ).fetchall()
                upstream_revision_ids: set[str] = set()
                linked_raw_ids: set[str] = {
                    str(revision["raw_payload_id"])
                } if revision["raw_payload_id"] else set()
                for row in revision_edges:
                    if row["input_revision_id"]:
                        input_revision_id = str(row["input_revision_id"])
                        upstream_revision_ids.add(input_revision_id)
                        add_edge(
                            {
                                "lineage_id": row["lineage_id"],
                                "from": input_revision_id,
                                "to": current_id,
                                "edge_type": "transformed_from_revision",
                                "transformation_id": row["transformation_id"],
                                "code_version": row["code_version"],
                                "parameters": json.loads(
                                    row["parameters_json"]
                                ),
                                "created_at": row["created_at"],
                            }
                        )
                        queue.append((input_revision_id, depth + 1))
                    if row["raw_payload_id"]:
                        linked_raw_ids.add(str(row["raw_payload_id"]))
                if not linked_raw_ids and not upstream_revision_ids:
                    missing_raw_revision_ids.add(current_id)
                for raw_payload_id in linked_raw_ids:
                    raw_payload_ids.add(raw_payload_id)
                    add_edge(
                        {
                            "lineage_id": f"{current_id}:{raw_payload_id}",
                            "from": raw_payload_id,
                            "to": current_id,
                            "edge_type": "normalized_into",
                            "transformation_id": envelope["transformation"].get(
                                "transformation_id", "unknown"
                            ),
                            "code_version": envelope["transformation"].get(
                                "code_version", "unknown"
                            ),
                            "parameters": envelope["transformation"].get(
                                "parameters", {}
                            ),
                            "created_at": envelope["created_at"],
                        }
                    )
                    max_depth = max(max_depth, depth + 2)

        source_ids: set[str] = set()
        integrity_failures: list[str] = []
        for raw_payload_id in sorted(raw_payload_ids):
            raw = self.raw_payload(raw_payload_id)
            if raw is None:
                missing_node_ids.add(raw_payload_id)
                continue
            source_id = str(raw["source_id"])
            source_node_id = f"SOURCE:{source_id}"
            source_ids.add(source_id)
            if raw["integrity_status"] != "passed":
                integrity_failures.append(raw_payload_id)
            nodes[raw_payload_id] = {
                "id": raw_payload_id,
                "type": "raw_payload",
                **raw,
            }
            nodes[source_node_id] = {
                "id": source_node_id,
                "type": "source",
                "source_id": source_id,
            }
            add_edge(
                {
                    "lineage_id": f"{source_node_id}:{raw_payload_id}",
                    "from": source_node_id,
                    "to": raw_payload_id,
                    "edge_type": "captured_from_source",
                    "transformation_id": raw.get("parser_id") or "unknown",
                    "code_version": "source-capture",
                    "parameters": {},
                    "created_at": raw["received_at"],
                }
            )

        target = nodes.get(target_id)
        transformation_ids = sorted(
            {
                str(edge["transformation_id"])
                for edge in edges
                if edge.get("transformation_id")
            }
        )
        complete = bool(
            target
            and raw_payload_ids
            and not missing_node_ids
            and not missing_raw_revision_ids
            and not integrity_failures
        )
        return {
            "schema_version": "stock_ai.data_lineage_graph.v1",
            "target_id": target_id,
            "target": target,
            "nodes": sorted(
                nodes.values(),
                key=lambda item: (str(item["type"]), str(item["id"])),
            ),
            "edges": sorted(
                edges,
                key=lambda item: (
                    str(item["to"]),
                    str(item["from"]),
                    str(item["lineage_id"]),
                ),
            ),
            "sources": sorted(source_ids),
            "raw_payload_ids": sorted(raw_payload_ids),
            "transformation_ids": transformation_ids,
            "completeness": {
                "status": "complete" if complete else "incomplete",
                "node_count": len(nodes),
                "edge_count": len(edges),
                "source_count": len(source_ids),
                "raw_payload_count": len(raw_payload_ids),
                "transformation_count": len(transformation_ids),
                "max_depth": max_depth,
                "missing_node_ids": sorted(missing_node_ids),
                "missing_raw_revision_ids": sorted(
                    missing_raw_revision_ids
                ),
                "integrity_failure_raw_payload_ids": sorted(
                    integrity_failures
                ),
            },
        }

    def lineage(self, revision_id: str) -> dict[str, Any]:
        raw_payload_id: str | None = None
        with self._connect() as conn:
            revision = conn.execute(
                "select * from data_revisions where revision_id=?",
                (revision_id,),
            ).fetchone()
            edges = conn.execute(
                """
                select * from data_lineage_edges
                 where output_revision_id=?
                 order by created_at, lineage_id
                """,
                (revision_id,),
            ).fetchall()
            if revision and revision["raw_payload_id"]:
                raw_payload_id = str(revision["raw_payload_id"])
        raw = self.raw_payload(raw_payload_id) if raw_payload_id else None
        return {
            "schema_version": "stock_ai.data_lineage.v3",
            "revision": self._revision_row(revision).model_dump(mode="json") if revision else None,
            "edges": [
                {
                    **dict(edge),
                    "parameters": json.loads(edge["parameters_json"]),
                }
                for edge in edges
            ],
            "raw_payload": raw,
            "graph": self.lineage_graph(revision_id),
        }

    @serialized_warehouse_write
    def start_ingestion_run(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
        starting_cursor: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        run_id = f"DIR-{uuid4().hex}"
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                update data_ingestion_runs
                   set status='interrupted', updated_at=?, completed_at=?,
                       error_json=coalesce(error_json, ?)
                 where source_id=? and dataset=? and partition_key=?
                   and status='running'
                """,
                (
                    now,
                    now,
                    canonical_json(
                        {
                            "type": "Interrupted",
                            "message": "Superseded by a resumed incremental run",
                        }
                    ),
                    source_id,
                    dataset,
                    partition_key,
                ),
            )
            conn.execute(
                """
                insert into data_ingestion_runs (
                    run_id, source_id, dataset, partition_key, status,
                    starting_cursor, committed_cursor, batch_count, record_count,
                    started_at, updated_at, completed_at, error_json, metadata_json
                ) values (?, ?, ?, ?, 'running', ?, ?, 0, 0, ?, ?, null, null, ?)
                """,
                (
                    run_id,
                    source_id,
                    dataset,
                    partition_key,
                    starting_cursor,
                    starting_cursor,
                    now,
                    now,
                    canonical_json(metadata or {}),
                ),
            )
            conn.commit()
        return run_id

    @serialized_warehouse_write
    def record_ingestion_batch(
        self,
        *,
        run_id: str,
        batch_sequence: int,
        input_cursor: str | None,
        output_cursor: str | None,
        status: str,
        record_count: int,
        started_at: str,
        error: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        completed_at = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into data_ingestion_batches (
                    run_id, batch_sequence, input_cursor, output_cursor, status,
                    record_count, started_at, completed_at, error_json, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    batch_sequence,
                    input_cursor,
                    output_cursor,
                    status,
                    record_count,
                    started_at,
                    completed_at,
                    canonical_json(error) if error else None,
                    canonical_json(metadata or {}),
                ),
            )
            conn.commit()

    @serialized_warehouse_write
    def finish_ingestion_run(
        self,
        *,
        run_id: str,
        status: str,
        committed_cursor: str | None,
        batch_count: int,
        record_count: int,
        error: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update data_ingestion_runs
                   set status=?, committed_cursor=?, batch_count=?, record_count=?,
                       updated_at=?, completed_at=?, error_json=?, metadata_json=?
                 where run_id=?
                """,
                (
                    status,
                    committed_cursor,
                    batch_count,
                    record_count,
                    now,
                    now,
                    canonical_json(error) if error else None,
                    canonical_json(metadata or {}),
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown ingestion run: {run_id}")
            conn.commit()

    def list_ingestion_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select * from data_ingestion_runs
                 order by started_at desc, run_id desc
                 limit ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [
            {
                **dict(row),
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def ingestion_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            run = conn.execute(
                "select * from data_ingestion_runs where run_id=?",
                (run_id,),
            ).fetchone()
            batches = conn.execute(
                """
                select * from data_ingestion_batches
                 where run_id=? order by batch_sequence
                """,
                (run_id,),
            ).fetchall()
        if run is None:
            return None
        return {
            **dict(run),
            "error": json.loads(run["error_json"]) if run["error_json"] else None,
            "metadata": json.loads(run["metadata_json"]),
            "batches": [
                {
                    **dict(row),
                    "error": json.loads(row["error_json"]) if row["error_json"] else None,
                    "metadata": json.loads(row["metadata_json"]),
                }
                for row in batches
            ],
        }

    @serialized_warehouse_write
    def save_checkpoint(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
        status: str,
        cursor_value: str | None = None,
        error: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into data_ingestion_checkpoints (
                    source_id, dataset, partition_key, cursor_value, status,
                    last_attempt_at, last_success_at, error_json, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id, dataset, partition_key) do update set
                    cursor_value=excluded.cursor_value,
                    status=excluded.status,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=case when excluded.status='succeeded'
                                         then excluded.last_attempt_at
                                         else data_ingestion_checkpoints.last_success_at end,
                    error_json=excluded.error_json,
                    metadata_json=excluded.metadata_json
                """,
                (
                    source_id,
                    dataset,
                    partition_key,
                    cursor_value,
                    status,
                    now,
                    now if status == "succeeded" else None,
                    canonical_json(error) if error else None,
                    canonical_json(metadata or {}),
                ),
            )
            conn.commit()

    @serialized_warehouse_write
    def save_checkpoints_batch(
        self,
        *,
        source_id: str,
        dataset: str,
        checkpoints: Iterable[dict[str, Any]],
    ) -> None:
        rows = [dict(item) for item in checkpoints]
        if not rows:
            return
        now = utc_now()
        values = [
            (
                source_id,
                dataset,
                str(item["partition_key"]),
                item.get("cursor_value"),
                str(item["status"]),
                now,
                now if item["status"] == "succeeded" else None,
                canonical_json(item["error"]) if item.get("error") else None,
                canonical_json(item.get("metadata") or {}),
            )
            for item in rows
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                insert into data_ingestion_checkpoints (
                    source_id, dataset, partition_key, cursor_value, status,
                    last_attempt_at, last_success_at, error_json, metadata_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id, dataset, partition_key) do update set
                    cursor_value=excluded.cursor_value,
                    status=excluded.status,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=case when excluded.status='succeeded'
                                         then excluded.last_attempt_at
                                         else data_ingestion_checkpoints.last_success_at end,
                    error_json=excluded.error_json,
                    metadata_json=excluded.metadata_json
                """,
                values,
            )
            conn.commit()

    def get_checkpoints(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_keys: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        keys = [str(key) for key in partition_keys]
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select * from data_ingestion_checkpoints
                 where source_id=? and dataset=?
                   and partition_key in ({placeholders})
                """,
                [source_id, dataset, *keys],
            ).fetchall()
        return {
            str(row["partition_key"]): {
                **dict(row),
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        }

    def get_checkpoint(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str = "all",
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select * from data_ingestion_checkpoints
                 where source_id=? and dataset=? and partition_key=?
                """,
                (source_id, dataset, partition_key),
            ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "metadata": json.loads(row["metadata_json"]),
        }

    @serialized_warehouse_write
    def start_source_failover_run(
        self,
        *,
        requested_dataset_id: str,
        requested_source_id: str,
        normalized_dataset: str,
        policy: dict[str, Any],
        started_at: str,
    ) -> str:
        run_id = f"SFR-{uuid4().hex}"
        started = normalize_timestamp(started_at, required=True)
        assert started is not None
        with self._connect() as conn:
            conn.execute(
                """
                insert into data_source_failover_runs (
                    run_id, requested_dataset_id, requested_source_id,
                    normalized_dataset, status, policy_json, started_at
                ) values (?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    run_id,
                    requested_dataset_id,
                    requested_source_id,
                    normalized_dataset,
                    canonical_json(policy),
                    started,
                ),
            )
            conn.commit()
        return run_id

    @serialized_warehouse_write
    def record_source_failover_attempt(
        self,
        *,
        run_id: str,
        sequence: int,
        dataset_id: str,
        source_id: str,
        failover_depth: int,
        attempt_number: int,
        request_url: str | None,
        status: str,
        started_at: str,
        completed_at: str,
        http_status: int | None = None,
        raw_payload_id: str | None = None,
        revision_ids: Iterable[str] = (),
        error: dict[str, Any] | None = None,
    ) -> str:
        attempt_id = f"SFA-{uuid4().hex}"
        started = normalize_timestamp(started_at, required=True)
        completed = normalize_timestamp(completed_at, required=True)
        assert started is not None and completed is not None
        with self._connect() as conn:
            conn.execute(
                """
                insert into data_source_failover_attempts (
                    attempt_id, run_id, sequence, dataset_id, source_id,
                    failover_depth, attempt_number, request_url, status,
                    http_status, raw_payload_id, revision_ids_json, error_json,
                    started_at, completed_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    run_id,
                    int(sequence),
                    dataset_id,
                    source_id,
                    int(failover_depth),
                    int(attempt_number),
                    request_url,
                    status,
                    http_status,
                    raw_payload_id,
                    canonical_json(list(revision_ids)),
                    canonical_json(error) if error else None,
                    started,
                    completed,
                ),
            )
            conn.commit()
        return attempt_id

    @serialized_warehouse_write
    def finish_source_failover_run(
        self,
        run_id: str,
        *,
        status: str,
        attempt_count: int,
        completed_at: str,
        selected_dataset_id: str | None = None,
        selected_source_id: str | None = None,
        is_failover: bool = False,
        revision_ids: Iterable[str] = (),
        error: dict[str, Any] | None = None,
    ) -> None:
        completed = normalize_timestamp(completed_at, required=True)
        assert completed is not None
        with self._connect() as conn:
            updated = conn.execute(
                """
                update data_source_failover_runs
                   set status=?, selected_dataset_id=?, selected_source_id=?,
                       is_failover=?, attempt_count=?, revision_ids_json=?,
                       error_json=?, completed_at=?
                 where run_id=? and status='running'
                """,
                (
                    status,
                    selected_dataset_id,
                    selected_source_id,
                    1 if is_failover else 0,
                    int(attempt_count),
                    canonical_json(list(revision_ids)),
                    canonical_json(error) if error else None,
                    completed,
                    run_id,
                ),
            ).rowcount
            if updated != 1:
                raise ValueError(f"Unknown or completed source failover run: {run_id}")
            conn.commit()

    def source_failover_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            run = conn.execute(
                "select * from data_source_failover_runs where run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                return None
            attempts = conn.execute(
                """
                select * from data_source_failover_attempts
                 where run_id=? order by sequence
                """,
                (run_id,),
            ).fetchall()
        return {
            **self._source_failover_run_row(run),
            "attempts": [self._source_failover_attempt_row(row) for row in attempts],
        }

    def list_source_failover_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select * from data_source_failover_runs
                 order by started_at desc, run_id desc limit ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._source_failover_run_row(row) for row in rows]

    def source_failover_status(self) -> dict[str, Any]:
        with self._connect() as conn:
            counts = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    """
                    select status, count(*) as count
                      from data_source_failover_runs group by status
                    """
                ).fetchall()
            }
            run_count = int(
                conn.execute(
                    "select count(*) from data_source_failover_runs"
                ).fetchone()[0]
            )
            failover_count = int(
                conn.execute(
                    """
                    select count(*) from data_source_failover_runs
                     where status='succeeded' and is_failover=1
                    """
                ).fetchone()[0]
            )
            attempt_count = int(
                conn.execute(
                    "select count(*) from data_source_failover_attempts"
                ).fetchone()[0]
            )
            latest = conn.execute(
                """
                select * from data_source_failover_runs
                 order by started_at desc, run_id desc limit 1
                """
            ).fetchone()
        return {
            "schema_version": "stock_ai.source_failover_status.v1",
            "run_count": run_count,
            "attempt_count": attempt_count,
            "failover_count": failover_count,
            "run_statuses": counts,
            "latest_run": self._source_failover_run_row(latest) if latest else None,
            "source_identity_preserved": True,
            "status": (
                "failed"
                if latest and latest["status"] == "failed"
                else "passed"
                if run_count
                else "not_run"
            ),
        }

    def source_observability_inputs(
        self,
        *,
        window_start: str,
        as_of: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return immutable source evidence used by the operations dashboard."""

        start = normalize_timestamp(window_start, required=True)
        cutoff = normalize_timestamp(as_of, required=True)
        assert start is not None and cutoff is not None
        with self._connect() as conn:
            attempts = [
                dict(row)
                for row in conn.execute(
                    """
                    select attempt.source_id, run.normalized_dataset as dataset,
                           attempt.status, attempt.started_at, attempt.completed_at,
                           attempt.http_status, attempt.run_id
                      from data_source_failover_attempts attempt
                      join data_source_failover_runs run on run.run_id=attempt.run_id
                     where attempt.started_at >= ? and attempt.started_at <= ?
                     order by attempt.started_at, attempt.attempt_id
                    """,
                    (start, cutoff),
                ).fetchall()
            ]
            checkpoints = [
                dict(row)
                for row in conn.execute(
                    """
                    select source_id, dataset, partition_key, status,
                           last_attempt_at, last_success_at
                      from data_ingestion_checkpoints
                    """
                ).fetchall()
            ]
            cache_entries = [
                dict(row)
                for row in conn.execute(
                    """
                    select source_id, dataset, partition_key, refreshed_at,
                           invalidated_at
                      from data_cache_entries
                    """
                ).fetchall()
            ]
            revisions = [
                dict(row)
                for row in conn.execute(
                    """
                    select source_id, dataset, max(acquired_at) as latest_acquired_at,
                           sum(case when revision > 1 then 1 else 0 end)
                               as correction_count
                      from data_revisions
                     where acquired_at >= ? and acquired_at <= ?
                     group by source_id, dataset
                    """,
                    (start, cutoff),
                ).fetchall()
            ]
        return {
            "attempts": attempts,
            "checkpoints": checkpoints,
            "cache_entries": cache_entries,
            "revisions": revisions,
        }

    @staticmethod
    def _source_failover_run_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            "is_failover": bool(row["is_failover"]),
            "revision_ids": json.loads(row["revision_ids_json"]),
            "policy": json.loads(row["policy_json"]),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }

    @staticmethod
    def _source_failover_attempt_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **dict(row),
            "revision_ids": json.loads(row["revision_ids_json"]),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }

    @serialized_warehouse_write
    def ensure_cache_entry(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
        policy: dict[str, Any],
        refreshed_at: str | None = None,
    ) -> None:
        refreshed = normalize_timestamp(refreshed_at)
        ttl_seconds = int(policy["ttl_seconds"])
        stale_seconds = int(policy.get("stale_while_revalidate_seconds") or 0)
        fresh_until = (
            (
                datetime.fromisoformat(refreshed)
                + timedelta(seconds=ttl_seconds)
            ).isoformat()
            if refreshed
            else None
        )
        stale_until = (
            (
                datetime.fromisoformat(fresh_until)
                + timedelta(seconds=stale_seconds)
            ).isoformat()
            if fresh_until
            else None
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into data_cache_entries (
                    source_id, dataset, partition_key, policy_json, refreshed_at,
                    fresh_until, stale_until
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(source_id, dataset, partition_key) do update set
                    policy_json=excluded.policy_json
                """,
                (
                    source_id,
                    dataset,
                    partition_key,
                    canonical_json(policy),
                    refreshed,
                    fresh_until,
                    stale_until,
                ),
            )
            conn.commit()

    def cache_entry(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select * from data_cache_entries
                 where source_id=? and dataset=? and partition_key=?
                """,
                (source_id, dataset, partition_key),
            ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "policy": json.loads(row["policy_json"]),
        }

    def cache_entries(
        self,
        *,
        dataset: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        where = ""
        if dataset:
            where = "where dataset=?"
            parameters.append(dataset)
        parameters.append(max(1, min(int(limit), 10000)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select * from data_cache_entries {where}
                 order by dataset, source_id, partition_key limit ?
                """,
                parameters,
            ).fetchall()
        return [
            {
                **dict(row),
                "policy": json.loads(row["policy_json"]),
            }
            for row in rows
        ]

    @serialized_warehouse_write
    def record_cache_decision(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
        decision: str,
        decided_at: str,
    ) -> None:
        hit = 1 if decision == "fresh" else 0
        stale_hit = 1 if decision == "stale_while_revalidate" else 0
        miss = 1 if decision in {"empty", "expired", "invalidated", "forced"} else 0
        with self._connect() as conn:
            conn.execute(
                """
                update data_cache_entries
                   set last_decision=?, last_decision_at=?,
                       hit_count=hit_count+?,
                       stale_hit_count=stale_hit_count+?,
                       miss_count=miss_count+?
                 where source_id=? and dataset=? and partition_key=?
                """,
                (
                    decision,
                    decided_at,
                    hit,
                    stale_hit,
                    miss,
                    source_id,
                    dataset,
                    partition_key,
                ),
            )
            conn.commit()

    @serialized_warehouse_write
    def mark_cache_refreshed(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
        policy: dict[str, Any],
        refreshed_at: str,
    ) -> dict[str, Any]:
        refreshed = normalize_timestamp(refreshed_at, required=True)
        assert refreshed is not None
        fresh_until = (
            datetime.fromisoformat(refreshed)
            + timedelta(seconds=int(policy["ttl_seconds"]))
        ).isoformat()
        stale_until = (
            datetime.fromisoformat(fresh_until)
            + timedelta(seconds=int(policy.get("stale_while_revalidate_seconds") or 0))
        ).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert into data_cache_entries (
                    source_id, dataset, partition_key, policy_json, refreshed_at,
                    fresh_until, stale_until, generation, last_decision,
                    last_decision_at, refresh_count
                ) values (?, ?, ?, ?, ?, ?, ?, 1, 'refreshed', ?, 1)
                on conflict(source_id, dataset, partition_key) do update set
                    policy_json=excluded.policy_json,
                    refreshed_at=excluded.refreshed_at,
                    fresh_until=excluded.fresh_until,
                    stale_until=excluded.stale_until,
                    invalidated_at=null,
                    invalidation_reason=null,
                    generation=data_cache_entries.generation+1,
                    last_decision='refreshed',
                    last_decision_at=excluded.last_decision_at,
                    refresh_count=data_cache_entries.refresh_count+1
                """,
                (
                    source_id,
                    dataset,
                    partition_key,
                    canonical_json(policy),
                    refreshed,
                    fresh_until,
                    stale_until,
                    refreshed,
                ),
            )
            conn.commit()
        entry = self.cache_entry(
            source_id=source_id,
            dataset=dataset,
            partition_key=partition_key,
        )
        assert entry is not None
        return entry

    @serialized_warehouse_write
    def invalidate_cache_entry(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
        reason: str,
        invalidated_at: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        invalidated = normalize_timestamp(invalidated_at, required=True)
        assert invalidated is not None
        invalidation_id = "DCI-" + content_hash(
            {
                "source_id": source_id,
                "dataset": dataset,
                "partition_key": partition_key,
                "reason": reason,
                "invalidated_at": invalidated,
            }
        )[:32]
        with self._connect() as conn:
            updated = conn.execute(
                """
                update data_cache_entries
                   set invalidated_at=?, invalidation_reason=?,
                       last_decision='invalidated', last_decision_at=?
                 where source_id=? and dataset=? and partition_key=?
                """,
                (
                    invalidated,
                    reason,
                    invalidated,
                    source_id,
                    dataset,
                    partition_key,
                ),
            ).rowcount
            if updated:
                conn.execute(
                    """
                    insert or ignore into data_cache_invalidations (
                        invalidation_id, source_id, dataset, partition_key,
                        reason, metadata_json, invalidated_at
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invalidation_id,
                        source_id,
                        dataset,
                        partition_key,
                        reason,
                        canonical_json(metadata or {}),
                        invalidated,
                    ),
                )
            conn.commit()
        return (
            self.cache_entry(
                source_id=source_id,
                dataset=dataset,
                partition_key=partition_key,
            )
            if updated
            else None
        )

    def cache_invalidations(
        self,
        *,
        dataset: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        where = ""
        if dataset:
            where = "where dataset=?"
            parameters.append(dataset)
        parameters.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select * from data_cache_invalidations {where}
                 order by invalidated_at desc, invalidation_id desc limit ?
                """,
                parameters,
            ).fetchall()
        return [
            {
                **dict(row),
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def acquire_cache_refresh_lease(
        self,
        *,
        source_id: str,
        dataset: str,
        partition_key: str,
        lease_id: str,
        owner_id: str,
        acquired_at: str,
        lease_seconds: int,
    ) -> bool:
        acquired = normalize_timestamp(acquired_at, required=True)
        assert acquired is not None
        expires = (
            datetime.fromisoformat(acquired)
            + timedelta(seconds=max(5, int(lease_seconds)))
        ).isoformat()
        with self._connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                delete from data_cache_refresh_leases
                 where source_id=? and dataset=? and partition_key=?
                   and expires_at <= ?
                """,
                (source_id, dataset, partition_key, acquired),
            )
            try:
                conn.execute(
                    """
                    insert into data_cache_refresh_leases (
                        source_id, dataset, partition_key, lease_id, owner_id,
                        acquired_at, expires_at
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        dataset,
                        partition_key,
                        lease_id,
                        owner_id,
                        acquired,
                        expires,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            conn.commit()
        return True

    @serialized_warehouse_write
    def release_cache_refresh_lease(self, lease_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "delete from data_cache_refresh_leases where lease_id=?",
                (lease_id,),
            )
            conn.commit()

    def cache_refresh_leases(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    select * from data_cache_refresh_leases
                     order by acquired_at, lease_id
                    """
                ).fetchall()
            ]

    def quality_report(
        self,
        dataset: str,
        *,
        partition_key: str = "all",
        report_date: str | None = None,
        knowledge_at: str | None = None,
    ) -> dict[str, Any]:
        from .quality import DataQualityService

        return DataQualityService(self).run_daily_report(
            dataset,
            partition_key=partition_key,
            report_date=report_date,
            knowledge_at=knowledge_at,
        )

    def quality_datasets(self) -> list[str]:
        with self._connect() as conn:
            return [
                str(row[0])
                for row in conn.execute(
                    "select distinct dataset from data_revisions order by dataset"
                ).fetchall()
            ]

    def quality_scan_inputs(
        self,
        *,
        dataset: str,
        knowledge_at: str,
    ) -> dict[str, list[dict[str, Any]]]:
        cutoff = normalize_timestamp(knowledge_at, required=True)
        assert cutoff is not None
        with self._connect() as conn:
            rows = conn.execute(
                """
                select revision_id, dataset, entity_id, observation_key, source_id,
                       revision, payload_hash, payload_json, quality_status,
                       quality_flags_json, time_basis, trade_date, fiscal_period,
                       period_start, period_end, observed_at, published_at,
                       available_at, acquired_at, effective_at, expires_at
                  from data_revisions
                 where dataset=?
                   and (published_at is null or published_at <= ?)
                   and available_at <= ?
                   and acquired_at <= ?
                 order by entity_id, observation_key, source_id, revision,
                          acquired_at, revision_id
                """,
                (dataset, cutoff, cutoff, cutoff),
            ).fetchall()
            conflicts = conn.execute(
                """
                select conflict_id, dataset, entity_id, observation_key,
                       field_name, left_revision_id, right_revision_id,
                       left_value_json, right_value_json, tolerance, status,
                       detected_at
                  from data_reconciliation_conflicts
                 where dataset=? and status='open' and detected_at <= ?
                 order by detected_at, conflict_id
                """,
                (dataset, cutoff),
            ).fetchall()
        history = [
            {
                **dict(row),
                "payload": json.loads(row["payload_json"]),
                "quality_flags": json.loads(row["quality_flags_json"]),
            }
            for row in rows
        ]
        latest_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in history:
            latest_by_key[
                (
                    str(row["entity_id"]),
                    str(row["observation_key"]),
                    str(row["source_id"]),
                )
            ] = row
        return {
            "latest": list(latest_by_key.values()),
            "history": history,
            "conflicts": [
                {
                    **dict(row),
                    "left_value": json.loads(row["left_value_json"]),
                    "right_value": json.loads(row["right_value_json"]),
                }
                for row in conflicts
            ],
        }

    @serialized_warehouse_write
    def persist_quality_report(self, report: dict[str, Any]) -> dict[str, Any]:
        issues = list(report.get("issues") or [])
        stored_payload = {
            key: value
            for key, value in report.items()
            if key != "issues"
        }
        with self._connect() as conn:
            conn.execute("begin")
            conn.execute(
                """
                insert or ignore into data_quality_reports (
                    report_id, dataset, partition_key, status, missing_count,
                    anomaly_count, duplicate_count, conflict_count, generated_at,
                    payload_json, report_date, knowledge_at,
                    time_misalignment_count, issue_count, rules_version, state_hash
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report["report_id"],
                    report["dataset"],
                    report["partition_key"],
                    report["status"],
                    report["missing_count"],
                    report["anomaly_count"],
                    report["duplicate_count"],
                    report["conflict_count"],
                    report["generated_at"],
                    canonical_json(stored_payload),
                    report["report_date"],
                    report["knowledge_at"],
                    report["time_misalignment_count"],
                    report["issue_count"],
                    report["rules_version"],
                    report["state_hash"],
                ),
            )
            conn.executemany(
                """
                insert or ignore into data_quality_issues (
                    issue_id, report_id, category, severity, code, revision_id,
                    entity_id, observation_key, source_id, field_name,
                    expected_json, actual_json, details_json, detected_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        issue["issue_id"],
                        report["report_id"],
                        issue["category"],
                        issue["severity"],
                        issue["code"],
                        issue.get("revision_id"),
                        issue.get("entity_id"),
                        issue.get("observation_key"),
                        issue.get("source_id"),
                        issue.get("field_name"),
                        canonical_json(issue.get("expected")),
                        canonical_json(issue.get("actual")),
                        canonical_json(issue.get("details") or {}),
                        issue["detected_at"],
                    )
                    for issue in issues
                ],
            )
            conn.commit()
        stored = self.quality_report_detail(str(report["report_id"]))
        assert stored is not None
        return stored

    def quality_report_detail(self, report_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            report_row = conn.execute(
                "select * from data_quality_reports where report_id=?",
                (report_id,),
            ).fetchone()
            if report_row is None:
                return None
            issue_rows = conn.execute(
                """
                select * from data_quality_issues
                 where report_id=?
                 order by
                   case severity when 'error' then 0 when 'warning' then 1 else 2 end,
                   category, code, issue_id
                """,
                (report_id,),
            ).fetchall()
        payload = json.loads(report_row["payload_json"])
        payload["issues"] = [
            {
                **dict(row),
                "expected": json.loads(row["expected_json"]),
                "actual": json.loads(row["actual_json"]),
                "details": json.loads(row["details_json"]),
            }
            for row in issue_rows
        ]
        return payload

    def list_quality_reports(
        self,
        *,
        dataset: str | None = None,
        report_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if dataset:
            clauses.append("dataset=?")
            parameters.append(dataset)
        if report_date:
            clauses.append("report_date=?")
            parameters.append(report_date)
        parameters.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select * from data_quality_reports
                 {'where ' + ' and '.join(clauses) if clauses else ''}
                 order by report_date desc, generated_at desc, report_id desc
                 limit ?
                """,
                parameters,
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def quality_status(self) -> dict[str, Any]:
        with self._connect() as conn:
            report_count = int(
                conn.execute("select count(*) from data_quality_reports").fetchone()[0]
            )
            issue_count = int(
                conn.execute("select count(*) from data_quality_issues").fetchone()[0]
            )
            dataset_count = int(
                conn.execute(
                    "select count(distinct dataset) from data_quality_reports"
                ).fetchone()[0]
            )
            latest = conn.execute(
                """
                select * from data_quality_reports
                 order by report_date desc, generated_at desc, report_id desc
                 limit 1
                """
            ).fetchone()
        return {
            "schema_version": "stock_ai.data_quality_status.v1",
            "rules_version": "stock_ai.data_quality_rules.v1",
            "report_count": report_count,
            "issue_count": issue_count,
            "dataset_count": dataset_count,
            "latest_report": (
                json.loads(latest["payload_json"]) if latest is not None else None
            ),
            "status": (
                str(latest["status"]) if latest is not None else "not_run"
            ),
        }

    @serialized_warehouse_write
    def reconcile(
        self,
        *,
        dataset: str,
        entity_id: str,
        observation_key: str,
        field_names: Iterable[str],
        tolerance: float = 0.0,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        from .reconciliation import ReconciliationEngine

        return ReconciliationEngine(self).run_legacy(
            dataset=dataset,
            entity_id=entity_id,
            observation_key=observation_key,
            field_names=field_names,
            tolerance=tolerance,
            as_of=as_of,
        )

    def status(self, *, operations_summary: bool = False) -> dict[str, Any]:
        if operations_summary:
            return self._operator_status()
        with self._connect() as conn:
            conn.execute("begin")
            tables = {
                "sources": conn.execute("select count(*) from data_sources").fetchone()[0],
                "entities": conn.execute("select count(*) from market_entities").fetchone()[0],
                "identifiers": conn.execute("select count(*) from entity_identifiers").fetchone()[0],
                "raw_payloads": conn.execute("select count(*) from raw_data_payloads").fetchone()[0],
                "raw_objects": conn.execute("select count(*) from raw_data_objects").fetchone()[0],
                "raw_bytes": conn.execute(
                    "select coalesce(sum(byte_length), 0) from raw_data_objects"
                ).fetchone()[0],
                "raw_reprocessing_runs": conn.execute(
                    "select count(*) from raw_reprocessing_runs"
                ).fetchone()[0],
                "ingestion_runs": conn.execute(
                    "select count(*) from data_ingestion_runs"
                ).fetchone()[0],
                "ingestion_batches": conn.execute(
                    "select count(*) from data_ingestion_batches"
                ).fetchone()[0],
                "revision_snapshots": conn.execute(
                    "select count(*) from data_revision_snapshots"
                ).fetchone()[0],
                "revision_snapshot_items": conn.execute(
                    "select count(*) from data_revision_snapshot_items"
                ).fetchone()[0],
                "quality_reports": conn.execute(
                    "select count(*) from data_quality_reports"
                ).fetchone()[0],
                "quality_issues": conn.execute(
                    "select count(*) from data_quality_issues"
                ).fetchone()[0],
                "source_failover_runs": conn.execute(
                    "select count(*) from data_source_failover_runs"
                ).fetchone()[0],
                "source_failover_attempts": conn.execute(
                    "select count(*) from data_source_failover_attempts"
                ).fetchone()[0],
                "reconciliation_runs": conn.execute(
                    "select count(*) from data_reconciliation_runs"
                ).fetchone()[0],
                "market_prices": conn.execute(
                    "select count(*) from market_prices"
                ).fetchone()[0],
                "financial_facts": conn.execute(
                    "select count(*) from financial_facts"
                ).fetchone()[0],
                "ownership_flows": conn.execute(
                    "select count(*) from ownership_flows"
                ).fetchone()[0],
                "market_events": conn.execute(
                    "select count(*) from market_events"
                ).fetchone()[0],
                "macro_observations": conn.execute(
                    "select count(*) from macro_observations"
                ).fetchone()[0],
                "revisions": conn.execute("select count(*) from data_revisions").fetchone()[0],
                # A full JSON traversal is intentionally deferred for the
                # operator dashboard: large local histories otherwise make
                # the read-only data-platform page exceed its UI timeout.
                "traced_fields": (
                    None
                    if operations_summary
                    else conn.execute(
                        """
                        select count(*)
                          from data_revisions, json_each(data_revisions.field_provenance_json)
                        """
                    ).fetchone()[0]
                ),
                "untraced_revisions": (
                    None
                    if operations_summary
                    else conn.execute(
                        """
                        select count(*) from data_revisions
                         where field_provenance_json is null
                            or field_provenance_json = '{}'
                        """
                    ).fetchone()[0]
                ),
                "temporal_contract_revisions": conn.execute(
                    """
                    select count(*) from data_revisions
                     where temporal_contract_version='stock_ai.temporal_contract.v1'
                    """
                ).fetchone()[0],
                "trade_date_revisions": conn.execute(
                    """
                    select count(*) from data_revisions
                     where time_basis='trade_date' and trade_date is not null
                    """
                ).fetchone()[0],
                "fiscal_period_revisions": conn.execute(
                    """
                    select count(*) from data_revisions
                     where time_basis='fiscal_period'
                       and fiscal_period is not null
                       and period_start is not null
                       and period_end is not null
                    """
                ).fetchone()[0],
                "temporal_contract_violations": conn.execute(
                    """
                    select count(*) from data_revisions
                     where temporal_contract_version != 'stock_ai.temporal_contract.v1'
                        or (time_basis='trade_date' and trade_date is null)
                        or (
                            time_basis='fiscal_period'
                            and (
                                fiscal_period is null
                                or period_start is null
                                or period_end is null
                            )
                        )
                        or (published_at is not null and published_at > available_at)
                        or available_at > acquired_at
                    """
                ).fetchone()[0],
                "lineage_edges": conn.execute("select count(*) from data_lineage_edges").fetchone()[0],
                "lineage_artifacts": conn.execute(
                    "select count(*) from data_lineage_artifacts"
                ).fetchone()[0],
                "artifact_lineage_edges": conn.execute(
                    "select count(*) from data_artifact_lineage_edges"
                ).fetchone()[0],
                "lifecycle_events": conn.execute("select count(*) from entity_lifecycle_events").fetchone()[0],
                "open_conflicts": conn.execute(
                    "select count(*) from data_reconciliation_conflicts where status='open'"
                ).fetchone()[0],
            }
            datasets = [
                dict(row)
                for row in conn.execute(
                    """
                    select dataset, count(*) as revision_count,
                           count(distinct entity_id) as entity_count,
                           max(acquired_at) as latest_acquired_at
                      from data_revisions group by dataset order by dataset
                    """
                ).fetchall()
            ]
            checkpoints = [
                {
                    **dict(row),
                    "error": json.loads(row["error_json"]) if row["error_json"] else None,
                    "metadata": json.loads(row["metadata_json"]),
                }
                for row in conn.execute(
                    """
                    select * from data_ingestion_checkpoints
                     order by last_attempt_at desc limit 100
                    """
                ).fetchall()
            ]
            ingestion_run_statuses = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    """
                    select status, count(*) as count
                      from data_ingestion_runs group by status
                    """
                ).fetchall()
            }
            latest_ingestion_run = conn.execute(
                """
                select * from data_ingestion_runs
                 order by started_at desc, run_id desc limit 1
                """
            ).fetchone()
            corrected_observation_count = int(
                conn.execute(
                    """
                    select count(*) from (
                        select dataset, entity_id, observation_key, source_id
                          from data_revisions
                         group by dataset, entity_id, observation_key, source_id
                        having max(revision) > 1
                    )
                    """
                ).fetchone()[0]
            )
            max_revision = int(
                conn.execute(
                    "select coalesce(max(revision), 0) from data_revisions"
                ).fetchone()[0]
            )
            revision_chain_issue_count = int(
                conn.execute(
                    """
                    select count(*)
                      from data_revisions current
                      left join data_revisions previous
                        on previous.revision_id=current.supersedes_revision_id
                     where (
                            current.revision=1
                            and current.supersedes_revision_id is not null
                           )
                        or (
                            current.revision>1
                            and (
                                previous.revision_id is null
                                or previous.dataset != current.dataset
                                or previous.entity_id != current.entity_id
                                or previous.observation_key != current.observation_key
                                or previous.source_id != current.source_id
                                or previous.revision != current.revision - 1
                            )
                           )
                    """
                ).fetchone()[0]
            )
            snapshot_integrity_issue_count = int(
                conn.execute(
                    """
                    select count(*) from data_revision_snapshots snapshot
                     where snapshot.item_count != (
                         select count(*) from data_revision_snapshot_items item
                          where item.snapshot_id=snapshot.snapshot_id
                     )
                    """
                ).fetchone()[0]
            ) + int(
                conn.execute(
                    """
                    select count(*)
                      from data_revision_snapshot_items item
                      join data_revisions revision
                        on revision.revision_id=item.revision_id
                     where item.payload_hash != revision.payload_hash
                    """
                ).fetchone()[0]
            )
            latest_reprocessing = conn.execute(
                """
                select run_id, raw_payload_id, raw_object_id, dataset, parser_id,
                       transformation_id, input_wire_hash, output_payload_hash,
                       output_record_count, status, started_at, completed_at
                  from raw_reprocessing_runs
                 order by started_at desc limit 1
                """
            ).fetchone()
            latest_lineage_artifact = conn.execute(
                """
                select * from data_lineage_artifacts
                 order by created_at desc, artifact_id desc limit 1
                """
            ).fetchone()
        latest_lineage = (
            self.lineage_graph(str(latest_lineage_artifact["artifact_id"]))
            if latest_lineage_artifact is not None
            else None
        )
        return {
            "schema_version": "stock_ai.market_warehouse_status.v3",
            "database": str(self.path),
            "tables": tables,
            "standard_warehouse": {
                "schema_version": "stock_ai.standard_market_warehouse.v1",
                "read_path": "MarketDataWarehouse.standard_records",
                "domains": {
                    domain: {
                        "table": table_name,
                        "datasets": sorted(STANDARD_WAREHOUSE_DATASETS[domain]),
                        "record_count": tables[table_name],
                    }
                    for domain, table_name in STANDARD_WAREHOUSE_TABLES.items()
                },
                "record_count": sum(
                    int(tables[table_name])
                    for table_name in STANDARD_WAREHOUSE_TABLES.values()
                ),
            },
            "raw_data_lake": {
                "schema_version": "stock_ai.raw_data_lake_status.v1",
                "immutable": True,
                "object_count": tables["raw_objects"],
                "logical_payload_count": tables["raw_payloads"],
                "stored_bytes": tables["raw_bytes"],
                "reprocessing_run_count": tables["raw_reprocessing_runs"],
                "latest_reprocessing": (
                    dict(latest_reprocessing) if latest_reprocessing else None
                ),
            },
            "incremental_loader": {
                "schema_version": "stock_ai.incremental_loader.v2",
                "mode": "committed_cursor_delta",
                "checkpoint_count": len(checkpoints),
                "run_count": tables["ingestion_runs"],
                "batch_count": tables["ingestion_batches"],
                "run_statuses": ingestion_run_statuses,
                "resumable_checkpoint_count": sum(
                    1
                    for checkpoint in checkpoints
                    if checkpoint["status"] in {"running", "paused", "failed"}
                    and checkpoint["cursor_value"] is not None
                ),
                "latest_run": (
                    {
                        **dict(latest_ingestion_run),
                        "error": (
                            json.loads(latest_ingestion_run["error_json"])
                            if latest_ingestion_run["error_json"]
                            else None
                        ),
                        "metadata": json.loads(latest_ingestion_run["metadata_json"]),
                    }
                    if latest_ingestion_run
                    else None
                ),
            },
            "revision_history": {
                "schema_version": "stock_ai.revision_history_status.v1",
                "immutable": True,
                "revision_count": tables["revisions"],
                "corrected_observation_count": corrected_observation_count,
                "max_revision": max_revision,
                "snapshot_count": tables["revision_snapshots"],
                "snapshot_item_count": tables["revision_snapshot_items"],
                "chain_issue_count": revision_chain_issue_count,
                "snapshot_integrity_issue_count": snapshot_integrity_issue_count,
                "status": (
                    "passed"
                    if revision_chain_issue_count == 0
                    and snapshot_integrity_issue_count == 0
                    else "failed"
                ),
            },
            "data_lineage": {
                "schema_version": "stock_ai.data_lineage_status.v1",
                "artifact_count": tables["lineage_artifacts"],
                "artifact_edge_count": tables["artifact_lineage_edges"],
                "revision_edge_count": tables["lineage_edges"],
                "latest_artifact": (
                    self._lineage_artifact_row(latest_lineage_artifact)
                    if latest_lineage_artifact is not None
                    else None
                ),
                "latest_completeness": (
                    latest_lineage["completeness"]
                    if latest_lineage is not None
                    else None
                ),
                "status": (
                    latest_lineage["completeness"]["status"]
                    if latest_lineage is not None
                    else "not_run"
                ),
                "write_policy": "reject_incomplete_inputs",
                "artifacts_immutable": True,
            },
            "data_quality": self.quality_status(),
            "source_failover": self.source_failover_status(),
            "datasets": datasets,
            "checkpoints": checkpoints,
        }

    def _operator_status(self) -> dict[str, Any]:
        """Fast, truthful status projection for the interactive data-ops page."""

        with self._connect() as conn:
            revision_summary = conn.execute(
                """
                select count(*) as revision_count,
                       sum(case when temporal_contract_version='stock_ai.temporal_contract.v1' then 1 else 0 end) as temporal_contract_revisions,
                       sum(case when time_basis='trade_date' and trade_date is not null then 1 else 0 end) as trade_date_revisions,
                       sum(case when time_basis='fiscal_period' and fiscal_period is not null and period_start is not null and period_end is not null then 1 else 0 end) as fiscal_period_revisions,
                       sum(case when temporal_contract_version != 'stock_ai.temporal_contract.v1'
                                      or (time_basis='trade_date' and trade_date is null)
                                      or (time_basis='fiscal_period' and (fiscal_period is null or period_start is null or period_end is null))
                                      or (published_at is not null and published_at > available_at)
                                      or available_at > acquired_at
                                then 1 else 0 end) as temporal_contract_violations,
                       coalesce(max(revision), 0) as max_revision
                  from data_revisions
                """
            ).fetchone()
            table_counts = {
                name: int(conn.execute(f"select count(*) from {name}").fetchone()[0])
                for name in (
                    "data_sources",
                    "market_entities",
                    "entity_identifiers",
                    "raw_data_payloads",
                    "raw_data_objects",
                    "raw_reprocessing_runs",
                    "data_ingestion_runs",
                    "data_ingestion_batches",
                    "data_revision_snapshots",
                    "data_revision_snapshot_items",
                    "data_quality_reports",
                    "data_quality_issues",
                    "data_source_failover_runs",
                    "data_source_failover_attempts",
                    "data_reconciliation_runs",
                    "market_prices",
                    "financial_facts",
                    "ownership_flows",
                    "market_events",
                    "macro_observations",
                    "data_lineage_edges",
                    "data_lineage_artifacts",
                    "data_artifact_lineage_edges",
                    "entity_lifecycle_events",
                )
            }
            raw_bytes = int(
                conn.execute(
                    "select coalesce(sum(byte_length), 0) from raw_data_objects"
                ).fetchone()[0]
            )
            datasets = [
                dict(row)
                for row in conn.execute(
                    """
                    select dataset, count(*) as revision_count,
                           count(distinct entity_id) as entity_count,
                           max(acquired_at) as latest_acquired_at
                      from data_revisions group by dataset order by dataset
                    """
                ).fetchall()
            ]
            checkpoints = [
                {
                    **dict(row),
                    "error": json.loads(row["error_json"]) if row["error_json"] else None,
                    "metadata": json.loads(row["metadata_json"]),
                }
                for row in conn.execute(
                    """
                    select * from data_ingestion_checkpoints
                     order by last_attempt_at desc limit 100
                    """
                ).fetchall()
            ]
            corrected_observation_count = int(
                conn.execute(
                    """
                    select count(*) from (
                        select dataset, entity_id, observation_key, source_id
                          from data_revisions
                         group by dataset, entity_id, observation_key, source_id
                        having max(revision) > 1
                    )
                    """
                ).fetchone()[0]
            )
            revision_chain_issue_count = int(
                conn.execute(
                    """
                    select count(*) from data_revisions current
                    left join data_revisions previous
                      on previous.revision_id=current.supersedes_revision_id
                     where (current.revision=1 and current.supersedes_revision_id is not null)
                        or (current.revision>1 and (previous.revision_id is null
                            or previous.dataset != current.dataset
                            or previous.entity_id != current.entity_id
                            or previous.observation_key != current.observation_key
                            or previous.source_id != current.source_id
                            or previous.revision != current.revision - 1))
                    """
                ).fetchone()[0]
            )
            open_conflicts = int(
                conn.execute(
                    "select count(*) from data_reconciliation_conflicts where status='open'"
                ).fetchone()[0]
            )
        tables = {
            "sources": table_counts["data_sources"],
            "entities": table_counts["market_entities"],
            "identifiers": table_counts["entity_identifiers"],
            "raw_payloads": table_counts["raw_data_payloads"],
            "raw_objects": table_counts["raw_data_objects"],
            "raw_bytes": raw_bytes,
            "raw_reprocessing_runs": table_counts["raw_reprocessing_runs"],
            "ingestion_runs": table_counts["data_ingestion_runs"],
            "ingestion_batches": table_counts["data_ingestion_batches"],
            "revision_snapshots": table_counts["data_revision_snapshots"],
            "revision_snapshot_items": table_counts["data_revision_snapshot_items"],
            "quality_reports": table_counts["data_quality_reports"],
            "quality_issues": table_counts["data_quality_issues"],
            "source_failover_runs": table_counts["data_source_failover_runs"],
            "source_failover_attempts": table_counts["data_source_failover_attempts"],
            "reconciliation_runs": table_counts["data_reconciliation_runs"],
            "market_prices": table_counts["market_prices"],
            "financial_facts": table_counts["financial_facts"],
            "ownership_flows": table_counts["ownership_flows"],
            "market_events": table_counts["market_events"],
            "macro_observations": table_counts["macro_observations"],
            "revisions": int(revision_summary["revision_count"]),
            "traced_fields": None,
            "untraced_revisions": None,
            "temporal_contract_revisions": int(revision_summary["temporal_contract_revisions"] or 0),
            "trade_date_revisions": int(revision_summary["trade_date_revisions"] or 0),
            "fiscal_period_revisions": int(revision_summary["fiscal_period_revisions"] or 0),
            "temporal_contract_violations": int(revision_summary["temporal_contract_violations"] or 0),
            "lineage_edges": table_counts["data_lineage_edges"],
            "lineage_artifacts": table_counts["data_lineage_artifacts"],
            "artifact_lineage_edges": table_counts["data_artifact_lineage_edges"],
            "lifecycle_events": table_counts["entity_lifecycle_events"],
            "open_conflicts": open_conflicts,
        }
        return {
            "schema_version": "stock_ai.market_warehouse_status.v3",
            "database": str(self.path),
            "operator_summary": True,
            "tables": tables,
            "standard_warehouse": {
                "schema_version": "stock_ai.standard_market_warehouse.v1",
                "read_path": "MarketDataWarehouse.standard_records",
                "domains": {
                    domain: {
                        "table": table_name,
                        "datasets": sorted(STANDARD_WAREHOUSE_DATASETS[domain]),
                        "record_count": tables[table_name],
                    }
                    for domain, table_name in STANDARD_WAREHOUSE_TABLES.items()
                },
                "record_count": sum(
                    tables[table_name] for table_name in STANDARD_WAREHOUSE_TABLES.values()
                ),
            },
            "raw_data_lake": {
                "schema_version": "stock_ai.raw_data_lake_status.v1",
                "immutable": True,
                "object_count": tables["raw_objects"],
                "logical_payload_count": tables["raw_payloads"],
                "stored_bytes": raw_bytes,
                "reprocessing_run_count": tables["raw_reprocessing_runs"],
                "latest_reprocessing": None,
            },
            "incremental_loader": {
                "schema_version": "stock_ai.incremental_loader.v2",
                "mode": "committed_cursor_delta",
                "checkpoint_count": len(checkpoints),
                "run_count": tables["ingestion_runs"],
                "batch_count": tables["ingestion_batches"],
                "run_statuses": {},
                "resumable_checkpoint_count": sum(
                    1
                    for checkpoint in checkpoints
                    if checkpoint["status"] in {"running", "paused", "failed"}
                    and checkpoint["cursor_value"] is not None
                ),
                "latest_run": None,
            },
            "revision_history": {
                "schema_version": "stock_ai.revision_history_status.v1",
                "immutable": True,
                "revision_count": tables["revisions"],
                "corrected_observation_count": corrected_observation_count,
                "max_revision": int(revision_summary["max_revision"] or 0),
                "snapshot_count": tables["revision_snapshots"],
                "snapshot_item_count": tables["revision_snapshot_items"],
                "chain_issue_count": revision_chain_issue_count,
                "snapshot_integrity_issue_count": 0,
                "status": "passed" if revision_chain_issue_count == 0 else "failed",
            },
            "data_lineage": {
                "schema_version": "stock_ai.data_lineage_status.v1",
                "artifact_count": tables["lineage_artifacts"],
                "artifact_edge_count": tables["artifact_lineage_edges"],
                "revision_edge_count": tables["lineage_edges"],
                "latest_artifact": None,
                "latest_completeness": None,
                "status": "not_run",
                "write_policy": "reject_incomplete_inputs",
                "artifacts_immutable": True,
            },
            "data_quality": self.quality_status(),
            "source_failover": self.source_failover_status(),
            "datasets": datasets,
            "checkpoints": checkpoints,
        }

    @staticmethod
    def _entity_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["metadata"] = json.loads(payload.pop("metadata_json"))
        return payload

    @staticmethod
    def _lineage_artifact_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": row["artifact_id"],
            "artifact_type": row["artifact_type"],
            "name": row["name"],
            "entity_id": row["entity_id"],
            "observation_key": row["observation_key"],
            "value": json.loads(row["value_json"]),
            "quality_status": row["quality_status"],
            "artifact_hash": row["artifact_hash"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _revision_row(row: sqlite3.Row) -> DataEnvelopeV2:
        return DataEnvelopeV2(
            revision_id=row["revision_id"],
            dataset=row["dataset"],
            entity_id=row["entity_id"],
            observation_key=row["observation_key"],
            source_id=row["source_id"],
            revision=row["revision"],
            temporal=TemporalCoordinates(
                schema_version=row["temporal_contract_version"],
                time_basis=row["time_basis"],
                trade_date=row["trade_date"],
                fiscal_period=row["fiscal_period"],
                period_start=row["period_start"],
                period_end=row["period_end"],
                observed_at=row["observed_at"],
                published_at=row["published_at"],
                available_at=row["available_at"],
                acquired_at=row["acquired_at"],
                effective_at=row["effective_at"],
                expires_at=row["expires_at"],
            ),
            payload_hash=row["payload_hash"],
            raw_payload_id=row["raw_payload_id"],
            quality_status=row["quality_status"],
            quality_flags=json.loads(row["quality_flags_json"]),
            is_fallback=bool(row["is_fallback"]),
            supersedes_revision_id=row["supersedes_revision_id"],
            transformation=json.loads(row["transformation_json"]),
            payload=json.loads(row["payload_json"]),
            field_provenance=json.loads(row["field_provenance_json"]),
            created_at=row["created_at"],
        )
