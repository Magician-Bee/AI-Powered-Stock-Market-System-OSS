from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import re

from open_stock_ai.agent_runtime import default_external_transport_guard

from .data_platform.contracts import payload_leaf_pointers, utc_now
from .data_platform.service import MarketDataPlatform, get_market_data_platform
from .data_platform.source_registry import get_source_registry, source_endpoint
from .income_statement import (
    ARCHIVE_MARKETS,
    EARLIEST_ARCHIVE_PERIOD,
    MAX_HISTORY_QUARTERS,
    _MopsIncomeStatementHTMLParser,
    _clean_cell,
    _company_name,
    _entity_id_for,
    _fiscal_month,
    _number,
    _period_end,
    _period_index,
    _period_parts,
    _period_start,
    _roc_period,
    _storage_entity_ids,
    archive_periods,
    latest_conservatively_available_period,
    resolve_market_segment,
    validate_period,
)
from .models import BalanceSheetItem
from .realtime_data import normalize_symbol


BALANCE_SHEET_HISTORY_SCHEMA_VERSION = "stock_ai.balance_sheet_history.v1"
BALANCE_SHEET_ARCHIVE_PARSER = "stock_ai.mops_balance_sheet_archive_parser.v1"
BALANCE_SHEET_TRANSFORMATION = "stock_ai.balance_sheet_history_normalizer.v1"
BALANCE_SHEET_DATASET = "fundamentals_quarterly"
BALANCE_SHEET_SOURCE_ID = "mops_archive"
BALANCE_SHEET_OBSERVATION_PREFIX = "balance-sheet:"


class BalanceSheetHistoryError(ValueError):
    pass


class BalanceSheetSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class BalanceSheetArchivePage:
    period: str
    market_segment: str
    source_url: str
    request_parameters: dict[str, str]
    acquired_at: str
    content_type: str
    body: bytes
    item: BalanceSheetItem


_FIELD_LABELS = {
    "cash_and_cash_equivalents": ("現金及約當現金",),
    "total_assets": ("資產總額", "資產總計"),
    "total_liabilities": ("負債總額", "負債總計"),
    "total_equity": ("權益總額", "權益總計"),
    "inventory": ("存貨", "存貨淨額"),
    "accounts_receivable": (
        "應收帳款淨額",
        "應收帳款",
        "應收票據及帳款淨額",
        "應收款項淨額",
    ),
    "current_assets": ("流動資產合計",),
    "current_liabilities": ("流動負債合計",),
}

_INTEREST_BEARING_DEBT_LABELS = (
    "短期借款",
    "應付短期票券",
    "一年內到期之長期借款",
    "一年內到期之長期負債",
    "應付公司債",
    "長期借款",
    "租賃負債－流動",
    "租賃負債－非流動",
)


def archive_endpoint() -> str:
    return source_endpoint("mops_balance_sheet_archive")


def archive_url(period: str, market_segment: str, symbol: str) -> str:
    if market_segment not in ARCHIVE_MARKETS:
        raise BalanceSheetHistoryError(
            f"market_segment must be one of {sorted(ARCHIVE_MARKETS)}"
        )
    normalized = normalize_symbol(symbol)
    code = normalized.split(".", 1)[0]
    roc_year, season = _roc_period(period)
    return (
        f"{archive_endpoint()}?"
        + urlencode(
            {
                "TYPEK": market_segment,
                "co_id": code,
                "year": roc_year,
                "season": season,
            }
        )
    )


def _find_metric(
    rows: list[list[str]],
    labels: tuple[str, ...],
) -> tuple[float | None, str | None]:
    normalized_labels = tuple(_clean_cell(label) for label in labels)
    for label in normalized_labels:
        for row in rows:
            if row and _clean_cell(row[0]) == label:
                return _number(row[1] if len(row) > 1 else None), label
    return None, None


def _sum_metrics(
    rows: list[list[str]],
    labels: tuple[str, ...],
) -> tuple[float | None, list[str]]:
    values: list[float] = []
    matched: list[str] = []
    for label in tuple(_clean_cell(value) for value in labels):
        value, raw_label = _find_metric(rows, (label,))
        if value is not None:
            values.append(float(value))
            matched.append(str(raw_label))
    return (sum(values), matched) if values else (None, [])


def _official_comparison_dates(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    return [
        _clean_cell(value)
        for value in rows[0][1:]
        if _clean_cell(value) and _number(value) is None
    ]


def parse_archive_html(
    body: bytes,
    *,
    period: str,
    market_segment: str,
    symbol: str,
    source_url: str,
    request_parameters: dict[str, str],
    acquired_at: str,
) -> BalanceSheetItem:
    year, quarter = _period_parts(period)
    if market_segment not in ARCHIVE_MARKETS:
        raise BalanceSheetHistoryError("unknown MOPS archive market segment")
    text = body.decode("utf-8", errors="replace")
    parser = _MopsIncomeStatementHTMLParser()
    parser.feed(text)
    if not parser.rows or "資產負債表" not in text:
        raise BalanceSheetSourceError(
            f"MOPS archive did not contain a balance sheet: {source_url}"
        )
    metrics: dict[str, float | None] = {}
    raw_labels: dict[str, str | None] = {}
    for field, labels in _FIELD_LABELS.items():
        metrics[field], raw_labels[field] = _find_metric(parser.rows, labels)
    debt, debt_labels = _sum_metrics(parser.rows, _INTEREST_BEARING_DEBT_LABELS)
    metrics["interest_bearing_debt"] = debt
    raw_labels["interest_bearing_debt"] = " + ".join(debt_labels) or None
    if not any(value is not None for value in metrics.values()):
        raise BalanceSheetSourceError(
            f"MOPS archive contained no supported balance-sheet metrics: {source_url}"
        )
    normalized = normalize_symbol(symbol)
    code = normalized.split(".", 1)[0]
    suffix = str(ARCHIVE_MARKETS[market_segment]["symbol_suffix"])
    return BalanceSheetItem(
        period=validate_period(period),
        fiscal_year=year,
        quarter=quarter,
        symbol=f"{code}{suffix}",
        name=_company_name(parser.company_caption),
        statement_scope=parser.statement_scope or "資產負債表",
        official_comparison_dates=_official_comparison_dates(parser.rows),
        source=(
            "MOPS historical individual-company balance sheet "
            f"({ARCHIVE_MARKETS[market_segment]['label']})"
        ),
        source_id=BALANCE_SHEET_SOURCE_ID,
        source_url=source_url,
        source_market=market_segment,
        source_request_parameters=dict(request_parameters),
        raw_field_labels=raw_labels,
        acquired_at=acquired_at,
        published_at=None,
        available_at=acquired_at,
        **metrics,
    )


def fetch_archive_period(
    period: str,
    *,
    market_segment: str,
    symbol: str,
    timeout: float | None = None,
) -> BalanceSheetArchivePage:
    normalized = normalize_symbol(symbol)
    code = normalized.split(".", 1)[0]
    roc_year, season = _roc_period(period)
    request_parameters = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": market_segment,
        "co_id": code,
        "year": roc_year,
        "season": season,
    }
    source_url = archive_url(period, market_segment, normalized)
    dataset = get_source_registry().dataset("mops_balance_sheet_archive")
    attempts = max(1, dataset.failure_strategy.max_attempts)
    request_timeout = float(timeout or dataset.failure_strategy.timeout_seconds)
    backoffs = dataset.failure_strategy.backoff_seconds
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(
                archive_endpoint(),
                data=urlencode(request_parameters).encode("utf-8"),
                headers={
                    "User-Agent": "Mozilla/5.0 StockAI/0.1",
                    "Accept": "text/html,application/xhtml+xml",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response = default_external_transport_guard().call_sync(
                f"source:mops:{dataset.dataset_id}:{period}:{market_segment}:{normalized}:attempt:{attempt}",
                lambda: urlopen(request, timeout=request_timeout),
            )
            with response:
                body = response.read()
                content_type = str(
                    response.headers.get("Content-Type") or "text/html"
                )
            acquired_at = utc_now()
            item = parse_archive_html(
                body,
                period=period,
                market_segment=market_segment,
                symbol=normalized,
                source_url=source_url,
                request_parameters=request_parameters,
                acquired_at=acquired_at,
            )
            return BalanceSheetArchivePage(
                period=period,
                market_segment=market_segment,
                source_url=source_url,
                request_parameters=request_parameters,
                acquired_at=acquired_at,
                content_type=content_type,
                body=body,
                item=item,
            )
        except (HTTPError, URLError, TimeoutError, BalanceSheetSourceError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            if backoffs:
                import time

                time.sleep(float(backoffs[min(attempt, len(backoffs) - 1)]))
    raise BalanceSheetSourceError(
        f"MOPS balance-sheet archive failed for {period}: {last_error}"
    ) from last_error


def _field_provenance(
    record: dict[str, Any],
    temporal: Any,
    raw_payload_id: str,
) -> dict[str, dict[str, Any]]:
    return {
        pointer: {
            "source_id": BALANCE_SHEET_SOURCE_ID,
            "temporal": temporal,
            "updated_at": temporal.acquired_at,
            "raw_payload_id": raw_payload_id,
            "raw_json_pointer": f"/0{pointer}" if pointer != "/" else "/0",
            "quality_status": "valid",
            "quality_flags": [],
            "transformation_id": BALANCE_SHEET_TRANSFORMATION,
            "input_fields": [pointer],
        }
        for pointer in payload_leaf_pointers(record)
    }


def _persist_archive_page(
    page: BalanceSheetArchivePage,
    *,
    platform: MarketDataPlatform,
) -> list[Any]:
    record = page.item.model_dump(mode="json")
    return platform.ingest_records(
        source_id=BALANCE_SHEET_SOURCE_ID,
        dataset=BALANCE_SHEET_DATASET,
        records=[record],
        entity_id_for=lambda row: _entity_id_for(
            str(row["symbol"]),
            market_segment=page.market_segment,
            platform=platform,
        ),
        observation_key_for=lambda row: (
            f"{BALANCE_SHEET_OBSERVATION_PREFIX}{row['period']}"
        ),
        time_basis="fiscal_period",
        fiscal_period_for=lambda row: _fiscal_month(str(row["period"])),
        period_start_for=lambda row: _period_start(str(row["period"])),
        period_end_for=lambda row: _period_end(str(row["period"])),
        observed_at_for=lambda row: _period_end(str(row["period"])),
        published_at_for=lambda row: row.get("published_at"),
        available_at_for=lambda row: str(row.get("available_at") or page.acquired_at),
        effective_at_for=lambda row: _period_end(str(row["period"])),
        request_url=page.source_url,
        raw_response=page.body,
        raw_content_type=page.content_type,
        raw_content_encoding="utf-8",
        raw_parser_id=BALANCE_SHEET_ARCHIVE_PARSER,
        transformation_id=BALANCE_SHEET_TRANSFORMATION,
        acquired_at=page.acquired_at,
        field_provenance_for=_field_provenance,
    )


_COMPARISON_FIELDS = (
    "cash_and_cash_equivalents",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "inventory",
    "accounts_receivable",
)


def _comparison(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "previous_period": previous.get("period") if previous else None,
        "fields": {},
    }
    for field in _COMPARISON_FIELDS:
        current_value = current.get(field)
        previous_value = previous.get(field) if previous else None
        change = (
            float(current_value) - float(previous_value)
            if current_value is not None and previous_value is not None
            else None
        )
        result["fields"][field] = {
            "previous_value": previous_value,
            "change": change,
            "change_percent": (
                change / abs(float(previous_value)) * 100
                if change is not None and float(previous_value) != 0
                else None
            ),
        }
    return result


def query_balance_sheet_history(
    symbol: str,
    *,
    start_period: str = EARLIEST_ARCHIVE_PERIOD,
    end_period: str | None = None,
    market_segment: str | None = None,
    knowledge_at: str | None = None,
    effective_at: str | None = None,
    limit: int = MAX_HISTORY_QUARTERS,
    platform: MarketDataPlatform | None = None,
) -> dict[str, Any]:
    service = platform or get_market_data_platform()
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise BalanceSheetHistoryError("symbol is required")
    end = end_period or latest_conservatively_available_period()
    requested_periods = archive_periods(start_period, end)
    market, resolution_basis = resolve_market_segment(
        normalized,
        platform=service,
        requested=market_segment,
    )
    storage_entity_ids = _storage_entity_ids(
        normalized,
        market_segment=market,
        platform=service,
    )
    rows = [
        row
        for storage_entity_id in storage_entity_ids
        for row in service.standard_query(
            "financials",
            dataset=BALANCE_SHEET_DATASET,
            entity_id=storage_entity_id,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=10000,
        )
    ]
    requested_set = set(requested_periods)
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(row.get("record") or {})
        if record.get("statement_kind") != "balance_sheet":
            continue
        period = str(record.get("period") or "")
        if period not in requested_set:
            continue
        existing = selected.get(period)
        if existing is None or str(row.get("acquired_at") or "") > str(
            existing.get("acquired_at") or ""
        ):
            selected[period] = {**row, "record": record}
    stored_periods = sorted(selected, key=_period_index)
    records = {
        period: BalanceSheetItem.model_validate(
            selected[period]["record"]
        ).model_dump(mode="json")
        for period in stored_periods
    }
    enriched = []
    for index, period in enumerate(stored_periods):
        record = records[period]
        previous = records[stored_periods[index - 1]] if index else None
        enriched.append({**record, "quarter_comparison": _comparison(record, previous)})
    page_limit = max(1, min(int(limit), MAX_HISTORY_QUARTERS))
    items = list(reversed(enriched))[:page_limit]
    missing_periods = [period for period in requested_periods if period not in selected]
    return {
        "schema_version": BALANCE_SHEET_HISTORY_SCHEMA_VERSION,
        "symbol": normalized,
        "entity_id": _entity_id_for(
            normalized,
            market_segment=market,
            platform=service,
        ),
        "storage_entity_ids": storage_entity_ids,
        "market_segment": market,
        "market_resolution_basis": resolution_basis,
        "start_period": requested_periods[0],
        "end_period": requested_periods[-1],
        "unit": "thousand_twd",
        "count": len(items),
        "total_stored_period_count": len(stored_periods),
        "items": items,
        "coverage": {
            "requested_period_count": len(requested_periods),
            "stored_period_count": len(stored_periods),
            "missing_period_count": len(missing_periods),
            "missing_periods": missing_periods,
            "first_stored_period": stored_periods[0] if stored_periods else None,
            "last_stored_period": stored_periods[-1] if stored_periods else None,
            "is_complete": not missing_periods,
            "covered_years": len({_period_parts(period)[0] for period in stored_periods}),
        },
        "source_ids": [BALANCE_SHEET_SOURCE_ID] if selected else [],
        "point_in_time": {
            "knowledge_at": knowledge_at,
            "effective_at": effective_at,
            "mode": "explicit_cutoff" if knowledge_at or effective_at else "current",
        },
        "truthfulness": {
            "reported_values": (
                "All balance-sheet values are official period-end amounts."
            ),
            "quarter_comparison": (
                "Quarter changes compare consecutive saved official period-end "
                "amounts; missing or zero denominators remain null."
            ),
            "archive_publication_time": (
                "The historical statement page does not provide the original "
                "filing timestamp; published_at remains null and available_at "
                "is the actual acquisition time."
            ),
        },
    }


def backfill_balance_sheet_history(
    symbol: str,
    *,
    start_period: str = EARLIEST_ARCHIVE_PERIOD,
    end_period: str | None = None,
    market_segment: str | None = None,
    force_refresh: bool = False,
    max_workers: int = 4,
    platform: MarketDataPlatform | None = None,
) -> dict[str, Any]:
    service = platform or get_market_data_platform()
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise BalanceSheetHistoryError("symbol is required")
    end = end_period or latest_conservatively_available_period()
    requested_periods = archive_periods(start_period, end)
    market, resolution_basis = resolve_market_segment(
        normalized,
        platform=service,
        requested=market_segment,
    )
    partition_keys = {
        period: f"balance-sheet:{market}:{period}:{normalized}"
        for period in requested_periods
    }
    checkpoints = service.warehouse.get_checkpoints(
        source_id=BALANCE_SHEET_SOURCE_ID,
        dataset=BALANCE_SHEET_DATASET,
        partition_keys=partition_keys.values(),
    )
    pending_periods = [
        period
        for period in requested_periods
        if force_refresh
        or checkpoints.get(partition_keys[period], {}).get("status") != "succeeded"
    ]
    failures: list[dict[str, str]] = []
    fetched_periods: list[str] = []
    revision_count = 0
    worker_count = max(1, min(int(max_workers), 4, len(pending_periods) or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                fetch_archive_period,
                period,
                market_segment=market,
                symbol=normalized,
            ): period
            for period in pending_periods
        }
        for future in as_completed(future_map):
            period = future_map[future]
            partition_key = partition_keys[period]
            try:
                page = future.result()
                envelopes = _persist_archive_page(page, platform=service)
            except Exception as exc:
                failures.append(
                    {
                        "period": period,
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                service.warehouse.save_checkpoint(
                    source_id=BALANCE_SHEET_SOURCE_ID,
                    dataset=BALANCE_SHEET_DATASET,
                    partition_key=partition_key,
                    status="failed",
                    error={"type": type(exc).__name__, "message": str(exc)},
                    metadata={"symbol": normalized, "market_segment": market},
                )
                continue
            fetched_periods.append(period)
            revision_count += len(envelopes)
            service.warehouse.save_checkpoint(
                source_id=BALANCE_SHEET_SOURCE_ID,
                dataset=BALANCE_SHEET_DATASET,
                partition_key=partition_key,
                status="succeeded",
                cursor_value=period,
                metadata={
                    "symbol": normalized,
                    "market_segment": market,
                    "source_url": page.source_url,
                    "record_count": 1,
                    "archive_page_preserved": True,
                    "source_method": "POST",
                },
            )
    history = query_balance_sheet_history(
        normalized,
        start_period=start_period,
        end_period=end,
        market_segment=market,
        platform=service,
    )
    return {
        **history,
        "sync": {
            "status": (
                "succeeded"
                if not failures
                else "partial"
                if fetched_periods
                else "failed"
            ),
            "requested_period_count": len(requested_periods),
            "fetched_period_count": len(fetched_periods),
            "skipped_period_count": len(requested_periods) - len(pending_periods),
            "revision_result_count": revision_count,
            "force_refresh": bool(force_refresh),
            "market_resolution_basis": resolution_basis,
            "failed_periods": sorted(
                failures,
                key=lambda item: _period_index(item["period"]),
            ),
        },
    }
