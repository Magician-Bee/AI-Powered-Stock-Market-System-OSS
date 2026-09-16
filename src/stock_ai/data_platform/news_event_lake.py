from __future__ import annotations

"""Immutable, point-in-time news and event ingestion.

News feeds are observations, not a current-state cache.  A record without its
publisher timestamp may still be displayed in a live UI, but must never be
promoted into a historical research dataset.
"""

import json
from collections import defaultdict
from hashlib import sha256
from typing import Any, Iterable, Mapping

from .availability import get_data_availability_registry
from .contracts import normalize_timestamp
from .news_history_coverage import NewsHistoryCoverageStore
from .service import MarketDataPlatform


SCHEMA_VERSION = "stock_ai.news_event_lake.v1"


def immutable_event_key(item: Mapping[str, Any]) -> str:
    """Build a stable identity independent of RSS ordering or local fetch time."""

    canonical = "\x1f".join(
        str(item.get(field) or "").strip()
        for field in ("source_url", "published_at", "title")
    )
    return "EVT-" + sha256(canonical.encode("utf-8")).hexdigest()[:32]


def ingest_news_events(
    platform: MarketDataPlatform,
    *,
    entity_id: str,
    items: Iterable[Mapping[str, Any]],
    acquired_at: str | None = None,
    coverage_store: NewsHistoryCoverageStore | None = None,
) -> dict[str, Any]:
    """Persist source-attributed news revisions and report PIT eligibility.

    Each source is written as a separate immutable revision stream. Unknown
    hosts and absent original publication times fail closed and are reported,
    rather than being assigned the time this Mac happened to fetch the feed.
    """

    accepted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    coverage_by_source: dict[str, Any] = {}
    rejected: list[dict[str, str]] = []
    for raw in items:
        item = dict(raw)
        published_at = normalize_timestamp(item.get("published_at"))
        if not published_at:
            rejected.append({"reason": "original_publication_timestamp_missing", "title": str(item.get("title") or "")})
            continue
        source = platform.source_registry_service.identify_url(str(item.get("source_url") or ""))
        if source is None:
            rejected.append({"reason": "news_source_not_registered", "title": str(item.get("title") or "")})
            continue
        item["published_at"] = published_at
        item["event_time"] = published_at
        item["event_key"] = immutable_event_key(item)
        item["source_id"] = source.source_id
        coverage = (
            coverage_store.covering_receipt(
                source_id=source.source_id, published_at=published_at
            )
            if coverage_store is not None
            else None
        )
        item["news_history_coverage_verified"] = coverage is not None
        item["news_history_coverage_receipt_id"] = coverage.receipt_id if coverage else None
        item["news_history_coverage_receipt_sha256"] = coverage.receipt_sha256 if coverage else None
        if coverage is not None:
            if source.source_id not in coverage_by_source:
                coverage_by_source[source.source_id] = coverage
            else:
                existing = coverage_by_source[source.source_id]
                if existing is None:
                    pass
                elif existing.receipt_sha256 != coverage.receipt_sha256:
                    # A source response that spans multiple unjoined coverage
                    # claims cannot be certified by the single-source ingestion
                    # path. Keep the rows for live display, but withhold PIT.
                    coverage_by_source[source.source_id] = None
        accepted[source.source_id].append(item)

    written = 0
    pit_eligible = 0
    coverage_audits: dict[str, dict[str, Any]] = {}
    for source_id, source_items in accepted.items():
        coverage = coverage_by_source.get(source_id)
        if coverage is None:
            audit = {
                "schema_version": "stock_ai.news_history_ingestion_coverage_audit.v1",
                "source_id": source_id,
                "provider_event_count": None,
                "ingested_event_count": len(source_items),
                "unique_ingested_event_count": len({str(item["event_key"]) for item in source_items}),
                "blockers": ["coverage_receipt_missing_or_ambiguous"],
                "complete": False,
            }
            audit["audit_sha256"] = sha256(
                json.dumps(audit, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            coverage_audits[source_id] = audit
        else:
            coverage_audits[source_id] = coverage.audit_ingestion(
                event_keys=[str(item["event_key"]) for item in source_items]
            )
        if coverage_store is not None:
            coverage_store.record_ingestion_audit(coverage_audits[source_id])
        coverage_complete = coverage_audits[source_id].get("complete") is True
        for item in source_items:
            item["news_history_coverage_verified"] = coverage_complete
            if not coverage_complete:
                item["news_history_coverage_receipt_id"] = None
                item["news_history_coverage_receipt_sha256"] = None
        contract = get_data_availability_registry().resolve(source_id=source_id, dataset="events")
        availability = [
            contract.receipt(
                observed_at=str(item["event_time"]),
                published_at=str(item["published_at"]),
                acquired_at=None,
            )
            for item in source_items
        ]
        pit_eligible += sum(
            bool(decision["historical_pit_eligible"])
            and item["news_history_coverage_verified"] is True
            for item, decision in zip(source_items, availability)
        )
        platform.ingest_records(
            source_id=source_id,
            dataset="events",
            records=source_items,
            entity_id_for=lambda _item: entity_id,
            observation_key_for=lambda item: str(item["event_key"]),
            observed_at_for=lambda item: str(item["event_time"]),
            published_at_for=lambda item: str(item["published_at"]),
            available_at_for=lambda item: str(contract.receipt(
                observed_at=str(item["event_time"]), published_at=str(item["published_at"]), acquired_at=None,
            )["available_at"] or item["published_at"]),
            effective_at_for=lambda item: str(item["event_time"]),
            transformation_id="stock_ai.news_event_lake_normalizer.v1",
            acquired_at=acquired_at,
        )
        written += len(source_items)
    return {
        "schema_version": SCHEMA_VERSION,
        "accepted_count": written,
        "historical_pit_eligible_count": pit_eligible,
        "coverage_receipt_required_for_pit": True,
        "coverage_audits": coverage_audits,
        "rejected": rejected,
    }
