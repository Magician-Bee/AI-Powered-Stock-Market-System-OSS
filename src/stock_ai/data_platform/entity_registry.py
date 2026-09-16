from __future__ import annotations

from typing import Any, Iterable

from .contracts import normalize_identifier_value, normalize_timestamp, utc_now
from .warehouse import MarketDataWarehouse


IDENTIFIER_TYPES = (
    "display_symbol",
    "exchange_code",
    "unified_business_no",
    "source_symbol",
    "isin",
    "figi",
    "lei",
    "legal_name",
)


class EntityRegistry:
    """Point-in-time resolver for all source and display identifiers."""

    def __init__(self, warehouse: MarketDataWarehouse) -> None:
        self.warehouse = warehouse

    @staticmethod
    def inferred_types(identifier: str) -> tuple[str, ...]:
        value = str(identifier or "").strip()
        upper = value.upper()
        if upper.endswith((".TW", ".TWO")):
            return ("display_symbol", "source_symbol")
        if value.isdigit() and len(value) == 8:
            return ("unified_business_no", "exchange_code")
        return ("exchange_code", "display_symbol", "source_symbol")

    def resolve(
        self,
        identifier: str,
        *,
        source_id: str | None = None,
        identifier_type: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        value = str(identifier or "").strip()
        if not value:
            raise ValueError("identifier is required")
        effective_as_of = normalize_timestamp(as_of, required=True) if as_of else None
        if value.upper().startswith("ENT-"):
            profile = self.warehouse.entity_profile(value)
            return {
                "schema_version": "stock_ai.entity_resolution.v1",
                "status": "resolved" if profile else "unavailable",
                "input": {
                    "identifier": value,
                    "source_id": source_id,
                    "identifier_type": "entity_id",
                    "as_of": effective_as_of,
                },
                "resolution_method": "internal_entity_id" if profile else None,
                "entity": profile,
                "candidates": [profile] if profile else [],
                "candidate_count": 1 if profile else 0,
                "matches": [],
                "warnings": [],
            }
        types: Iterable[str] = (
            (identifier_type,) if identifier_type else self.inferred_types(value)
        )
        matches = self.warehouse.resolve_identifier_candidates(
            value,
            source_id=source_id,
            identifier_types=types,
            as_of=effective_as_of,
        )
        grouped: dict[str, dict[str, Any]] = {}
        for match in matches:
            entity = dict(match["entity"])
            entity_id = str(entity["entity_id"])
            candidate = grouped.setdefault(
                entity_id,
                {**entity, "matched_identifiers": []},
            )
            candidate["matched_identifiers"].append(match["identifier"])
        candidates = list(grouped.values())
        candidates.sort(
            key=lambda item: (
                item.get("lifecycle_status") != "active",
                item.get("updated_at") or "",
            )
        )
        selected: dict[str, Any] | None = None
        method: str | None = None
        warnings: list[str] = []
        if len(candidates) == 1:
            selected = candidates[0]
            method = "source_identifier" if source_id else "unique_identifier"
        elif (len(candidates) > 1 and effective_as_of is None
              and not any(item.get("entity_type") == "warrant" for item in candidates)):
            active = [
                item
                for item in candidates
                if item.get("lifecycle_status") in {"active", "pre_listing", "suspended"}
            ]
            if len(active) == 1:
                selected = active[0]
                method = "unique_current_entity"
                warnings.append("historical_identifier_reuse")
        status = "resolved" if selected else "ambiguous" if candidates else "unavailable"
        normalized_inputs = {
            kind: normalize_identifier_value(kind, value)
            for kind in types
        }
        return {
            "schema_version": "stock_ai.entity_resolution.v1",
            "status": status,
            "input": {
                "identifier": value,
                "source_id": source_id,
                "identifier_type": identifier_type,
                "as_of": effective_as_of,
            },
            "normalized_inputs": normalized_inputs,
            "resolution_method": method,
            "entity": selected,
            "candidates": candidates,
            "candidate_count": len(candidates),
            "matches": matches,
            "warnings": warnings,
        }

    def require(
        self,
        identifier: str,
        *,
        source_id: str | None = None,
        identifier_type: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        result = self.resolve(
            identifier,
            source_id=source_id,
            identifier_type=identifier_type,
            as_of=as_of,
        )
        if result["status"] != "resolved":
            raise LookupError(
                f"Entity identifier {identifier!r} is {result['status']}"
            )
        return dict(result["entity"])

    def status(self) -> dict[str, Any]:
        return {
            **self.warehouse.entity_registry_summary(),
            "generated_at": utc_now(),
            "identifier_types": list(IDENTIFIER_TYPES),
            "ambiguity_policy": (
                "Return every candidate; only select automatically when the "
                "identifier maps to one entity or exactly one current entity. "
                "Distinct warrant entities remain ambiguous within overlapping intervals."
            ),
        }
