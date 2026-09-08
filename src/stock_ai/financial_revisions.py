from __future__ import annotations

from numbers import Real
from typing import Any

from .data_platform.contracts import normalize_timestamp, utc_now
from .data_platform.service import MarketDataPlatform, get_market_data_platform
from .income_statement import (
    _storage_entity_ids,
    resolve_market_segment,
    validate_period as validate_quarter,
)
from .monthly_revenue import validate_period as validate_month
from .realtime_data import normalize_symbol


FINANCIAL_REVISION_HISTORY_SCHEMA_VERSION = (
    "stock_ai.financial_statement_revision_history.v1"
)


class FinancialRevisionHistoryError(ValueError):
    pass


_STATEMENT_SPECS: dict[str, dict[str, Any]] = {
    "monthly_revenue": {
        "dataset": "revenues_monthly",
        "observation_key": lambda period: period,
        "fields": (
            "current_revenue",
            "previous_revenue",
            "last_year_revenue",
            "mom_change_percent",
            "yoy_change_percent",
            "ytd_revenue",
            "last_ytd_revenue",
            "ytd_change_percent",
        ),
        "record_kinds": (),
        "period_validator": validate_month,
    },
    "income_statement": {
        "dataset": "fundamentals_quarterly",
        "observation_key": lambda period: period,
        "fields": (
            "revenue",
            "gross_profit",
            "operating_income",
            "net_income",
            "eps",
            "current_quarter_revenue",
            "current_quarter_gross_profit",
            "current_quarter_operating_income",
            "current_quarter_net_income",
            "current_quarter_eps",
        ),
        "record_kinds": (None, "income_statement"),
        "period_validator": validate_quarter,
    },
    "balance_sheet": {
        "dataset": "fundamentals_quarterly",
        "observation_key": lambda period: f"balance-sheet:{period}",
        "fields": (
            "cash_and_cash_equivalents",
            "total_assets",
            "total_liabilities",
            "total_equity",
            "inventory",
            "accounts_receivable",
            "current_assets",
            "current_liabilities",
        ),
        "record_kinds": ("balance_sheet",),
        "period_validator": validate_quarter,
    },
    "cash_flow_statement": {
        "dataset": "fundamentals_quarterly",
        "observation_key": lambda period: f"cash-flow:{period}",
        "fields": (
            "operating_cash_flow",
            "investing_cash_flow",
            "financing_cash_flow",
            "capital_expenditure",
            "free_cash_flow",
        ),
        "record_kinds": ("cash_flow_statement",),
        "period_validator": validate_quarter,
    },
}


def _statement_spec(statement_kind: str) -> tuple[str, dict[str, Any]]:
    normalized = str(statement_kind or "").strip().casefold()
    try:
        return normalized, _STATEMENT_SPECS[normalized]
    except KeyError as exc:
        raise FinancialRevisionHistoryError(
            "statement_kind must be one of "
            f"{sorted(_STATEMENT_SPECS)}"
        ) from exc


def _matches_statement(
    payload: dict[str, Any],
    statement_kind: str,
    spec: dict[str, Any],
) -> bool:
    if statement_kind == "monthly_revenue":
        return True
    return payload.get("statement_kind") in set(spec["record_kinds"])


def _numeric(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _field_changes(
    before: dict[str, Any] | None,
    after: dict[str, Any],
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    if before is None:
        return []
    changes: list[dict[str, Any]] = []
    for field in fields:
        old_value = before.get(field)
        new_value = after.get(field)
        if old_value == new_value:
            continue
        absolute_change = (
            float(new_value) - float(old_value)
            if _numeric(old_value) and _numeric(new_value)
            else None
        )
        percent_change = (
            absolute_change / abs(float(old_value)) * 100
            if absolute_change is not None and float(old_value) != 0
            else None
        )
        changes.append(
            {
                "field": field,
                "before": old_value,
                "after": new_value,
                "absolute_change": absolute_change,
                "percent_change": percent_change,
            }
        )
    return changes


def query_financial_revision_history(
    symbol: str,
    *,
    statement_kind: str,
    period: str,
    market_segment: str | None = None,
    source_id: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    platform: MarketDataPlatform | None = None,
) -> dict[str, Any]:
    service = platform or get_market_data_platform()
    normalized_symbol = normalize_symbol(symbol)
    if not normalized_symbol:
        raise FinancialRevisionHistoryError("symbol is required")
    normalized_kind, spec = _statement_spec(statement_kind)
    try:
        normalized_period = spec["period_validator"](period)
        knowledge = normalize_timestamp(
            knowledge_at or utc_now(),
            required=True,
        )
        effective = normalize_timestamp(
            effective_at or knowledge,
            required=True,
        )
    except ValueError as exc:
        raise FinancialRevisionHistoryError(str(exc)) from exc
    assert knowledge is not None and effective is not None
    market, resolution_basis = resolve_market_segment(
        normalized_symbol,
        platform=service,
        requested=market_segment,
    )
    entity_ids = _storage_entity_ids(
        normalized_symbol,
        market_segment=market,
        platform=service,
    )
    observation_key = spec["observation_key"](normalized_period)

    history_items: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        history = service.revision_history(
            dataset=spec["dataset"],
            entity_id=entity_id,
            observation_key=observation_key,
            source_id=source_id,
        )
        issues.extend(
            {
                **issue,
                "entity_id": entity_id,
            }
            for issue in history.get("issues") or []
        )
        for item in history.get("items") or []:
            payload = dict(item.get("payload") or {})
            if _matches_statement(payload, normalized_kind, spec):
                history_items[str(item["revision_id"])] = {
                    **item,
                    "payload": payload,
                }

    selected_ids: set[str] = set()
    selected_versions: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        for row in service.standard_query(
            "financials",
            dataset=spec["dataset"],
            entity_id=entity_id,
            knowledge_at=knowledge,
            effective_at=effective,
            limit=10000,
        ):
            if str(row.get("observation_key") or "") != observation_key:
                continue
            if source_id and str(row.get("source_id") or "") != source_id:
                continue
            record = dict(row.get("record") or {})
            if not _matches_statement(record, normalized_kind, spec):
                continue
            revision_id = str(row["revision_id"])
            selected_ids.add(revision_id)
            selected_versions.append(
                {
                    "revision_id": revision_id,
                    "revision": int(row["revision"]),
                    "entity_id": str(row["entity_id"]),
                    "source_id": str(row["source_id"]),
                    "available_at": row.get("available_at"),
                    "acquired_at": row.get("acquired_at"),
                    "source_url": record.get("source_url"),
                }
            )

    previous_by_source: dict[tuple[str, str], dict[str, Any]] = {}
    versions: list[dict[str, Any]] = []
    ordered_history = sorted(
        history_items.values(),
        key=lambda item: (
            str(item.get("source_id") or ""),
            int(item.get("revision") or 0),
            str((item.get("temporal") or {}).get("acquired_at") or ""),
        ),
    )
    for item in ordered_history:
        chain_key = (
            str(item.get("entity_id") or ""),
            str(item.get("source_id") or ""),
        )
        previous = previous_by_source.get(chain_key)
        payload = dict(item["payload"])
        changes = _field_changes(
            dict(previous["payload"]) if previous else None,
            payload,
            tuple(spec["fields"]),
        )
        revision_id = str(item["revision_id"])
        versions.append(
            {
                "revision_id": revision_id,
                "revision": int(item["revision"]),
                "supersedes_revision_id": item.get("supersedes_revision_id"),
                "entity_id": str(item["entity_id"]),
                "source_id": str(item["source_id"]),
                "source_url": payload.get("source_url"),
                "raw_payload_id": item.get("raw_payload_id"),
                "payload_hash": item.get("payload_hash"),
                "published_at": (item.get("temporal") or {}).get("published_at"),
                "available_at": (item.get("temporal") or {}).get("available_at"),
                "acquired_at": (item.get("temporal") or {}).get("acquired_at"),
                "effective_at": (item.get("temporal") or {}).get("effective_at"),
                "quality_status": item.get("quality_status"),
                "is_fallback": bool(item.get("is_fallback")),
                "selected_for_point_in_time": revision_id in selected_ids,
                "comparison_status": (
                    "original"
                    if previous is None
                    else "restated"
                    if changes
                    else "metadata_only_revision"
                ),
                "change_count": len(changes),
                "changes": changes,
                "record": payload,
            }
        )
        previous_by_source[chain_key] = item

    restatements = [
        version
        for version in versions
        if version["comparison_status"] == "restated"
    ]
    return {
        "schema_version": FINANCIAL_REVISION_HISTORY_SCHEMA_VERSION,
        "symbol": normalized_symbol,
        "statement_kind": normalized_kind,
        "period": normalized_period,
        "dataset": spec["dataset"],
        "observation_key": observation_key,
        "entity_ids": entity_ids,
        "market_segment": market,
        "market_resolution_basis": resolution_basis,
        "source_id": source_id,
        "tracked_fields": list(spec["fields"]),
        "status": "passed" if not issues else "failed",
        "version_count": len(versions),
        "restatement_count": len(restatements),
        "has_restatement": bool(restatements),
        "issues": issues,
        "versions": versions,
        "point_in_time": {
            "knowledge_at": knowledge,
            "effective_at": effective,
            "selection_mode": (
                "explicit_cutoff"
                if knowledge_at or effective_at
                else "current"
            ),
            "selected_revision_ids": sorted(selected_ids),
            "selected_versions": sorted(
                selected_versions,
                key=lambda item: (item["source_id"], item["revision"]),
            ),
            "status": "available" if selected_versions else "not_available",
        },
        "truthfulness": {
            "difference_basis": (
                "Differences compare consecutive immutable revisions from the "
                "same entity and source; parallel sources are never diffed as "
                "if one superseded the other."
            ),
            "historical_research": (
                "The point-in-time selection requires published_at (when "
                "known), available_at and acquired_at to be no later than "
                "knowledge_at, and effective_at to be no later than the "
                "requested effective cutoff."
            ),
            "publication_time": (
                "Historical MOPS pages that do not disclose the original "
                "filing timestamp retain published_at=null; acquisition time "
                "is not relabeled as the original filing time."
            ),
        },
    }
