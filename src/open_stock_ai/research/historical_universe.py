from __future__ import annotations

"""Point-in-time historical universe receipts.

Research may not use today's active-security list as a proxy for a historical
universe.  This module accepts only membership intervals backed by an original
availability and local-ingestion time; otherwise it returns an explicit
unavailable receipt instead of silently dropping delisted securities.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


def historical_universe_at(
    records: Iterable[Mapping[str, Any]],
    *,
    as_of: str,
    required_entity_id: str | None = None,
) -> dict[str, object]:
    """Restore membership known at ``as_of`` and fail closed on weak lineage."""

    cutoff = _time(as_of)
    source_rows = [dict(item) for item in records]
    eligible: list[dict[str, object]] = []
    blockers: list[str] = []
    excluded_after_cutoff = 0
    hidden_future_end_events = 0
    scope_ids: set[str] = set()
    coverage_declarations_complete = True
    for index, source in enumerate(source_rows):
        entity_id = str(source.get("entity_id") or "").strip()
        symbol = str(source.get("symbol") or "").strip().upper()
        revision_id = str(source.get("source_revision_id") or "").strip()
        scope_id = str(source.get("universe_scope_id") or "").strip()
        if not entity_id or not symbol or not revision_id:
            blockers.append(f"historical_universe_identity_missing:{index}")
            continue
        if not scope_id:
            blockers.append(f"historical_universe_coverage_incomplete:{index}")
            coverage_declarations_complete = False
        else:
            scope_ids.add(scope_id)
        if source.get("universe_coverage_complete") is not True:
            blockers.append(f"historical_universe_coverage_incomplete:{index}")
            coverage_declarations_complete = False
        try:
            effective_from = _time(source.get("effective_from") or source.get("listed_at"))
        except (TypeError, ValueError):
            blockers.append(f"historical_universe_effective_from_missing:{index}")
            continue
        try:
            available_at = _time(source.get("available_at") or source.get("published_at"))
            ingested_at = _time(source.get("ingested_at") or source.get("acquired_at"))
        except (TypeError, ValueError):
            blockers.append(f"historical_universe_availability_missing:{index}")
            continue
        if source.get("historical_pit_eligible") is not True:
            blockers.append(f"historical_universe_source_not_pit_eligible:{index}")
            continue
        if available_at > cutoff or ingested_at > cutoff:
            # Rows not yet knowable at this cutoff must not create blockers
            # based on lifecycle details learned later.
            excluded_after_cutoff += 1
            continue
        effective_until = None
        effective_until_available_at = None
        effective_until_ingested_at = None
        if source.get("effective_until") or source.get("delisted_at") or source.get("expires_at"):
            try:
                effective_until = _time(
                    source.get("effective_until") or source.get("delisted_at") or source.get("expires_at")
                )
            except (TypeError, ValueError):
                blockers.append(f"historical_universe_effective_until_invalid:{index}")
                continue
            try:
                effective_until_available_at = _time(
                    source.get("effective_until_available_at")
                    or source.get("delisting_published_at")
                )
                effective_until_ingested_at = _time(
                    source.get("effective_until_ingested_at")
                    or source.get("delisting_ingested_at")
                )
            except (TypeError, ValueError):
                blockers.append(f"historical_universe_effective_until_availability_missing:{index}")
                continue
        if effective_until is not None and effective_until <= effective_from:
            blockers.append(f"historical_universe_invalid_interval:{index}")
            continue
        known_effective_until = effective_until
        if effective_until is not None and (
            effective_until_available_at > cutoff or effective_until_ingested_at > cutoff
        ):
            # The end event exists in today's warehouse, but it was not yet
            # knowable at this historical cutoff.  Keep the member active and
            # do not leak the future delisting date into the receipt.
            known_effective_until = None
            hidden_future_end_events += 1
        eligible.append(
            {
                "entity_id": entity_id,
                "symbol": symbol,
                "entity_type": str(source.get("entity_type") or "stock"),
                "listing_type": str(source.get("listing_type") or "unknown"),
                "effective_from": effective_from.isoformat(),
                "effective_until": known_effective_until.isoformat() if known_effective_until else None,
                "effective_until_available_at": (
                    effective_until_available_at.isoformat()
                    if known_effective_until is not None and effective_until_available_at is not None
                    else None
                ),
                "effective_until_ingested_at": (
                    effective_until_ingested_at.isoformat()
                    if known_effective_until is not None and effective_until_ingested_at is not None
                    else None
                ),
                "available_at": available_at.isoformat(),
                "ingested_at": ingested_at.isoformat(),
                "source_revision_id": revision_id,
                "universe_scope_id": scope_id,
            }
        )

    active = [
        item
        for item in eligible
        if _time(str(item["effective_from"])) <= cutoff
        and (item["effective_until"] is None or _time(str(item["effective_until"])) > cutoff)
    ]
    active.sort(key=lambda item: (str(item["symbol"]), str(item["entity_id"])))
    entity_ids = {str(item["entity_id"]) for item in active}
    required_member = required_entity_id in entity_ids if required_entity_id else None
    if not eligible:
        blockers.append("historical_universe_no_known_membership_records")
    if len(scope_ids) > 1:
        blockers.append("historical_universe_scope_mismatch")
    if required_entity_id and required_member is not True:
        blockers.append("historical_universe_required_entity_not_member")
    payload = {
        "as_of": cutoff.isoformat(),
        "required_entity_id": required_entity_id,
        "eligible": eligible,
        "active": active,
        "blockers": list(dict.fromkeys(blockers)),
    }
    return {
        "schema_version": "open_stock_ai.historical_universe.v1",
        "method": "known_at_membership_intervals_with_delist_inclusion",
        "as_of": cutoff.isoformat(),
        "required_entity_id": required_entity_id,
        "required_entity_in_universe": required_member,
        "source_record_count": len(source_rows),
        "known_record_count": len(eligible),
        "excluded_after_knowledge_cutoff": excluded_after_cutoff,
        "hidden_future_end_event_count": hidden_future_end_events,
        "universe_scope_id": next(iter(scope_ids)) if len(scope_ids) == 1 else None,
        "universe_coverage_complete": (
            bool(eligible) and len(scope_ids) == 1 and coverage_declarations_complete
        ),
        "member_count": len(active),
        "members": active,
        "point_in_time_verified": not blockers,
        "passed": not blockers,
        "blockers": list(dict.fromkeys(blockers)),
        "manifest_hash": hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest(),
    }


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
