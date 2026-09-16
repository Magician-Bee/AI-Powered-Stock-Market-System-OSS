"""Project verified current catalogue identities into the existing master.

Catalogue membership supplies identity and a stated listing day. It supplies
neither an active trading state nor a quote, an expiry, or an issuer record.
Existing lifecycle sources retain authority over their already resolved rows.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .contracts import EntityRecord, SecuritySourceSnapshot
from .security_lifecycle import _existing_entity_id, _stable_entity_id


def catalogue_identity_batch(
    context: dict[str, Any], inventory: list[dict[str, Any]], *, as_of: str,
    reserved_identifiers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure projection; caller obtains context from retained byte verification."""
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_id = {item["entity_id"]: item for item in inventory}
    for item in inventory:
        codes = {str(link["identifier_value"]) for link in item.get("identifiers", [])
                 if link["identifier_type"] == "exchange_code"}
        for code in codes:
            by_code[code].append(item)
    entities, identifiers, revisions, events = [], [], [], []
    counts = {p["source_dataset"]: {"created_count": 0, "refreshed_count": 0,
              "existing_count": 0, "unresolved": []} for p in context["partitions"]}
    for (venue, code), issue in sorted(context["bindings"].items()):
        dataset = issue["source_dataset"]
        report = counts[dataset]
        product_type = issue["product_type"]
        entity_type = {"ordinary_stock": "stock", "etf": "etf", "warrant": "warrant"}.get(product_type, "security")
        listing_type = (entity_type if entity_type in {"etf", "warrant"} else "other"
                        if entity_type == "security" else {"TWSE": "listed", "TPEx": "otc", "TPEx-ESB": "emerging"}[venue])
        state = "unknown" if issue["eligible_as_of"] else "pre_listing"
        snapshot = SecuritySourceSnapshot(
            source_id="twse_isin", source_dataset=dataset, source_url=issue["source_url"],
            venue=venue, listing_type=listing_type, entity_type=entity_type, code=code,
            display_symbol=issue["symbol"], short_name=issue["source_name"], legal_name=issue["source_name"],
            lifecycle_status=state, listed_at=issue["listed_at"], quote_present=False,
            raw_row=issue["product_classification"]["source_row"],
            metadata={"identity_origin": "official_catalogue", "catalogue_only": True,
                      "issuance_identity": issue,
                      "catalogue_missing_fields": ["active_lifecycle", "quote", "issuer_record"]
                      + (["expires_at"] if entity_type == "warrant" else [])},
        )
        existing = None
        try:
            if entity_type == "warrant":
                _, existing = _existing_entity_id([snapshot], {}, by_code)
            else:
                candidates = [item for item in by_code.get(code, []) if item.get("exchange") == venue]
                exact_isin = []
                legacy_overlap = []
                for item in candidates:
                    old_issue = (item.get("metadata") or {}).get("issuance_identity") or {}
                    isins = {link["identifier_value"] for link in item.get("identifiers", [])
                             if link["identifier_type"] == "isin"}
                    if old_issue.get("isin"):
                        isins.add(old_issue["isin"])
                    if isins == {issue["isin"]}:
                        exact_isin.append(item)
                    elif any(link["identifier_type"] == "exchange_code" and link["identifier_value"] == code
                             and (not link.get("valid_to") or link["valid_to"] > issue["listed_at"])
                             for link in item.get("identifiers", [])):
                        # Legacy current owners are already retained by their
                        # lifecycle source. Never overwrite or infer code reuse.
                        if isins:
                            raise ValueError("catalogue_existing_isin_conflict")
                        legacy_overlap.append(item)
                # An exact official ISIN owner resolves the issuance even when
                # a historical pre-ISIN entity reused or overlapped the code.
                # Ambiguity remains fail-closed when no exact ISIN exists.
                same = exact_isin if exact_isin else legacy_overlap
                if len(same) > 1:
                    raise ValueError("catalogue_existing_identity_ambiguous")
                existing = same[0] if same else None
            if existing and not (existing.get("metadata") or {}).get("catalogue_only"):
                report["existing_count"] += 1
                continue
            key = f"{entity_type}:{venue}:isin:{issue['isin']}"
            entity_id = existing["entity_id"] if existing else _stable_entity_id(key)
            if not existing and entity_id in by_id:
                raise ValueError("catalogue_stable_identity_owned_elsewhere")
            if existing and existing["entity_type"] != entity_type:
                raise ValueError("catalogue_existing_product_type_conflict")
        except ValueError as exc:
            report["unresolved"].append({"venue": venue, "code": code, "symbol": issue["symbol"],
                                         "reason": str(exc)})
            continue
        metadata = {**snapshot.metadata, "source_code": code, "display_symbol": issue["symbol"],
                    "short_name": issue["source_name"], "listing_type": listing_type,
                    "quote_present": False, "expires_at": None, "source_datasets": [dataset],
                    "venue_history": [{"venue": venue, "listing_type": listing_type,
                                       "valid_from": issue["listed_at"], "valid_to": None}],
                    "product_classifications": {f"{venue}:{issue['symbol']}": issue["product_classification"]}}
        entities.append(EntityRecord(entity_id=entity_id, entity_type=entity_type,
            canonical_name=issue["source_name"], market="taiwan", exchange=venue, currency="TWD",
            lifecycle_status=state, listed_at=issue["listed_at"], metadata=metadata))
        for kind, value in (("exchange_code", code), ("display_symbol", issue["symbol"]), ("isin", issue["isin"])):
            identifiers.append({"entity_id": entity_id, "source_id": "twse_isin", "identifier_type": kind,
                "identifier_value": value, "valid_from": issue["listed_at"], "valid_to": None,
                "confidence": 1.0, "is_primary": kind == "display_symbol",
                "metadata": {"venue": venue, "listing_type": listing_type, "identity_origin": "official_catalogue",
                    "issuance_identity": {key: issue[key] for key in
                        ("isin", "listed_on", "source_dataset", "raw_payload_id", "raw_sha256", "row_sha256")}}})
        revisions.append({"entity_id": entity_id, "source_id": "twse_isin", "source_dataset": dataset,
            "observation_key": f"catalogue_identity:{dataset}:{venue}", "effective_at": issue["listed_at"],
            "expires_at": None, "payload": {**snapshot.model_dump(mode="json"), "entity_id": entity_id},
            "quality_flags": ["publication_time_unavailable", "catalogue_identity_only", "active_lifecycle_unavailable"]})
        events.append({"entity_id": entity_id, "source_id": "twse_isin", "source_dataset": dataset,
            "event_type": "catalogue_listing_announced", "venue": venue, "listing_type": listing_type,
            "effective_at": issue["listed_at"], "metadata": snapshot.metadata})
        report["refreshed_count" if existing else "created_count"] += 1
    # The registry's immutable ownership key predates venue-qualified source
    # rows. Reject every conflicting incoming owner before the shared writer,
    # so an unresolvable source alias cannot roll back unrelated valid rows.
    owners: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for link in reserved_identifiers or []:
        owners[(link["source_id"], link["identifier_type"], link["identifier_value"],
                link.get("valid_from") or "")].add(link["entity_id"])
    for item in inventory:
        for link in item.get("identifiers", []):
            owners[(link["source_id"], link["identifier_type"], link["identifier_value"],
                    link.get("valid_from") or "")].add(item["entity_id"])
    for link in identifiers:
        owners[(link["source_id"], link["identifier_type"], link["identifier_value"],
                link.get("valid_from") or "")].add(link["entity_id"])
    rejected_ids = {link["entity_id"] for link in identifiers
                    if len(owners[(link["source_id"], link["identifier_type"], link["identifier_value"],
                                   link.get("valid_from") or "")]) > 1}
    for entity in entities:
        if entity.entity_id not in rejected_ids:
            continue
        report = counts[entity.metadata["source_datasets"][0]]
        report["refreshed_count" if entity.entity_id in by_id else "created_count"] -= 1
        report["unresolved"].append({"venue": entity.exchange, "code": entity.metadata["source_code"],
            "symbol": entity.metadata["display_symbol"], "reason": "catalogue_identifier_interval_conflict"})
    entities = [row for row in entities if row.entity_id not in rejected_ids]
    identifiers = [row for row in identifiers if row["entity_id"] not in rejected_ids]
    revisions = [row for row in revisions if row["entity_id"] not in rejected_ids]
    events = [row for row in events if row["entity_id"] not in rejected_ids]
    return {"entities": entities, "identifiers": identifiers, "revisions": revisions, "events": events,
            "partitions": counts}
