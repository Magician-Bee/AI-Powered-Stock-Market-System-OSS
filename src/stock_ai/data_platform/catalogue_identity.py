"""Read current issuance evidence from retained official catalogues, without fetching.

The v1 classification receipt stays unchanged. This separate projection verifies
the previously uninterpreted listing-date cell. Acquisitions expire after the
registered source interval; the source date may be at most one Taipei calendar
day old. Neither a listing date nor catalogue membership attests historical
publication, an active lifecycle, a company record, or a quote.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import json
import re
import sqlite3

from .product_catalog import CATALOGUES
from .source_registry import get_source_registry, source_endpoint
from .warehouse import content_hash


SCHEMA_VERSION = "stock_ai.catalogue_issue_identity.v1"
_TAIPEI = ZoneInfo("Asia/Taipei")


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("catalogue_identity_timezone_required")
    return parsed.astimezone(timezone.utc)


def _issue_row(receipt: dict[str, Any], *, as_of: datetime) -> dict[str, Any]:
    cells = receipt["source_row"]["cells"]
    value = cells[2]
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}/[0-9]{2}/[0-9]{2}", value):
        raise ValueError("catalogue_listing_date_missing_or_invalid")
    try:
        listed_on = date.fromisoformat(value.replace("/", "-"))
    except ValueError as exc:
        raise ValueError("catalogue_listing_date_missing_or_invalid") from exc
    code, source_name = cells[0].split(maxsplit=1)
    reached = listed_on <= as_of.astimezone(_TAIPEI).date()
    result = {
        "schema_version": SCHEMA_VERSION,
        "venue": receipt["venue"], "code": code, "symbol": receipt["symbol"],
        "isin": receipt["isin"], "issue_isin": receipt["isin"],
        "source_name": source_name, "short_name": source_name,
        "product_type": receipt["product_type"], "market_segment": receipt["market_segment"],
        "listed_on": listed_on.isoformat(),
        "listed_at": datetime.combine(listed_on, time.min, _TAIPEI).astimezone(timezone.utc).isoformat(),
        "date_precision": "day", "eligible_as_of": reached,
        "listing_status": "listed_date_reached" if reached else "pre_listing",
        "source_id": receipt["source_id"], "source_dataset": receipt["source_dataset"],
        "source_url": receipt["source_url"], "acquired_at": receipt["acquired_at"],
        "source_updated_on": receipt["source_updated_on"],
        "raw_payload_id": receipt["raw_payload_id"], "raw_sha256": receipt["raw_sha256"],
        "row_sha256": receipt["row_sha256"],
        "product_classification": receipt,
        "historical_pit_eligible": False,
    }
    result["receipt_sha256"] = content_hash(result)
    return result


def retained_catalogue_issue_bindings(warehouse: Any, *, as_of: str) -> dict[str, Any]:
    """Return exact ``(venue, code)`` bindings and explicit partition failures.

    Only three precise checkpoint/raw-ID queries are used. The warehouse's
    existing retained-byte verifier checks both capture records, hashes and the
    entire parsed catalogue once per capture. No platform constructor, mutation,
    source acquisition, or inventory scan occurs here. ``eligible_as_of`` means
    only that the source listing day has begun, not new-entry eligibility.
    """
    current = _instant(as_of)
    source = get_source_registry().source("twse_isin")
    max_age = source.update_frequency_seconds
    bindings: dict[tuple[str, str], dict[str, Any]] = {}
    partitions: list[dict[str, Any]] = []
    for dataset_id in CATALOGUES:
        item: dict[str, Any] = {"source_dataset": dataset_id, "status": "unavailable", "reasons": [],
                                "count": 0, "rejected_count": 0, "rejection_counts": {}}
        partitions.append(item)
        try:
            # A live SQLite read transaction, never immutable=1 or a warehouse
            # write connection. Reader WAL/SHM coordination remains SQLite-owned.
            connection = sqlite3.connect(Path(warehouse.path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("pragma query_only=on")
                connection.execute("begin")
                checkpoint = connection.execute(
                    "select status,cursor_value,metadata_json from data_ingestion_checkpoints "
                    "where source_id='twse_isin' and dataset='security_master' and partition_key=?",
                    (dataset_id,),
                ).fetchone()
                if checkpoint is None or checkpoint["status"] != "succeeded":
                    raise ValueError("catalogue_checkpoint_not_succeeded")
                metadata = json.loads(checkpoint["metadata_json"])
                if (not isinstance(metadata, dict)
                    or metadata.get("complete_source_datasets") != [dataset_id]
                    or metadata.get("raw_sha256") != checkpoint["cursor_value"]
                    or not isinstance(metadata.get("row_count"), int) or metadata["row_count"] <= 0):
                    raise ValueError("catalogue_checkpoint_binding_invalid")
                raw_id = metadata.get("raw_payload_id")
                if not isinstance(raw_id, str) or not raw_id.startswith("RAW-"):
                    raise ValueError("catalogue_checkpoint_raw_missing")
                raw = connection.execute(
                    "select received_at,request_url from raw_data_payloads where raw_payload_id=?", (raw_id,),
                ).fetchone()
                if raw is None:
                    raise ValueError("catalogue_checkpoint_raw_missing")
                acquired = _instant(raw["received_at"])
                age = (current - acquired).total_seconds()
                item.update(raw_payload_id=raw_id, raw_sha256=metadata["raw_sha256"],
                            acquired_at=raw["received_at"], acquisition_age_seconds=age,
                            max_acquisition_age_seconds=max_age, max_source_age_calendar_days=1)
                if age < 0:
                    raise ValueError("catalogue_acquired_after_as_of")
                if age > max_age:
                    raise ValueError("catalogue_acquisition_stale")
                if raw["request_url"] != source_endpoint(dataset_id):
                    raise ValueError("catalogue_source_url_mismatch")
                proof_binding = {"raw_payload_id": raw_id, "raw_sha256": metadata["raw_sha256"],
                                 "source_dataset": dataset_id, "source_url": raw["request_url"],
                                 "acquired_at": raw["received_at"]}
                rows = warehouse._retained_product_catalogue(proof_binding)
            finally:
                connection.rollback()
                connection.close()
            receipts = [dict(row, raw_payload_id=raw_id) for bucket in rows.values() for row in bucket]
            if len(receipts) != metadata["row_count"]:
                raise ValueError("catalogue_checkpoint_row_count_mismatch")
            dates = {r["source_updated_on"] for r in receipts}
            if len(dates) != 1 or metadata.get("source_updated_on") not in dates:
                raise ValueError("catalogue_checkpoint_source_date_mismatch")
            source_date_text = next(iter(dates))
            source_age = (current.astimezone(_TAIPEI).date() - date.fromisoformat(source_date_text)).days
            item.update(source_updated_on=source_date_text, source_age_calendar_days=source_age,
                        source_row_count=len(receipts))
            if not 0 <= source_age <= 1:
                raise ValueError("catalogue_source_date_stale_or_future")
            isins = Counter((r["venue"], r["isin"]) for r in receipts)
            rejection_counts: Counter[str] = Counter()
            accepted: dict[tuple[str, str], dict[str, Any]] = {}
            for receipt in receipts:
                reason = ("catalogue_row_not_verified" if receipt["status"] != "verified" else
                          "catalogue_duplicate_isin" if isins[(receipt["venue"], receipt["isin"])] != 1 else None)
                if reason is None:
                    try:
                        issue = _issue_row(receipt, as_of=current)
                    except ValueError as exc:
                        reason = str(exc)
                if reason:
                    rejection_counts[reason] += 1
                else:
                    accepted[(issue["venue"], issue["code"])] = issue
            bindings.update(accepted)
            item.update(status="verified" if not rejection_counts else "partial",
                        count=len(accepted), rejected_count=sum(rejection_counts.values()),
                        rejection_counts=dict(rejection_counts))
        except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error) as exc:
            item["reasons"] = [str(exc) if isinstance(exc, ValueError) else "catalogue_retained_proof_unavailable"]
    return {
        "schema_version": "stock_ai.catalogue_issue_bindings.v1", "as_of": current.isoformat(),
        "status": "verified" if all(p["status"] == "verified" for p in partitions) else "partial" if bindings else "unavailable",
        "bindings": bindings, "partitions": partitions,
        "historical_pit_eligible": False,
    }
