"""Read-only product projections using the execution admission contract."""
from datetime import datetime, timezone
from typing import Any

from open_stock_ai.execution.product_admission import assess_new_entry_product


PRODUCT_TYPES = ("ordinary_stock", "etf", "etn", "depositary_receipt", "preferred_stock",
                 "warrant", "bond", "other", "unknown")


def product_assessment(value: Any, *, now: datetime | None = None) -> dict[str, Any]:
    row = value if isinstance(value, dict) else value.model_dump(mode="json")
    classification = row.get("product_classification")
    return assess_new_entry_product(
        symbol=str(row.get("symbol") or ""), market="TW",
        expected_entity_id=row.get("entity_id"), now=now or datetime.now(timezone.utc),
        product_snapshot={"entity_id": row.get("entity_id"), "lifecycle_status": row.get("lifecycle_status"),
                          "classification": classification if isinstance(classification, dict) else {}},
    )


def product_counts(features: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    instant = now or datetime.now(timezone.utc)
    types = dict.fromkeys(PRODUCT_TYPES, 0)
    statuses = dict.fromkeys(("verified", "unknown", "conflict"), 0)
    reasons: dict[str, int] = {}
    eligible = 0
    for row in features:
        assessment = product_assessment(row, now=instant)
        classification = assessment.get("classification") or {}
        verified = assessment.get("classification_verified") is True
        status = "verified" if verified else "conflict" if classification.get("status") == "conflict" else "unknown"
        product_type = classification.get("product_type") if verified else "unknown"
        types[product_type if product_type in types else "unknown"] += 1
        statuses[status] += 1
        eligible += assessment.get("allowed") is True
        for reason in assessment.get("reasons") or []:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {"product_type_counts": types, "product_classification_status_counts": statuses,
            "product_classification_reason_counts": reasons,
            "product_classification_scope": "snapshot_receipts_assessed_at_read_time",
            "ordinary_stock_count": types["ordinary_stock"], "etf_count": types["etf"],
            "investable_count": eligible, "excluded_product_count": len(features) - eligible}
