from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
    query_income_statement_history,
    resolve_market_segment,
    validate_period,
)
from .models import CashFlowStatementItem
from .realtime_data import normalize_symbol


CASH_FLOW_HISTORY_SCHEMA_VERSION = "stock_ai.cash_flow_statement_history.v1"
CASH_FLOW_ARCHIVE_PARSER = "stock_ai.mops_cash_flow_archive_parser.v1"
CASH_FLOW_TRANSFORMATION = "stock_ai.cash_flow_statement_normalizer.v1"
CASH_FLOW_DATASET = "fundamentals_quarterly"
CASH_FLOW_SOURCE_ID = "mops_archive"
CASH_FLOW_OBSERVATION_PREFIX = "cash-flow:"


class CashFlowHistoryError(ValueError):
    pass


class CashFlowSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CashFlowArchivePage:
    period: str
    market_segment: str
    source_url: str
    request_parameters: dict[str, str]
    acquired_at: str
    content_type: str
    body: bytes
    item: CashFlowStatementItem


_FIELD_LABELS = {
    "operating_cash_flow": (
        "營業活動之淨現金流入（流出）",
        "營業活動之淨現金流量",
    ),
    "investing_cash_flow": (
        "投資活動之淨現金流入（流出）",
        "投資活動之淨現金流量",
    ),
    "financing_cash_flow": (
        "籌資活動之淨現金流入（流出）",
        "融資活動之淨現金流入（流出）",
        "籌資活動之淨現金流量",
    ),
    "capital_expenditure": (
        "取得不動產、廠房及設備",
        "購置不動產、廠房及設備",
        "取得不動產及設備",
    ),
    "depreciation": ("折舊費用",),
    "amortization": ("攤銷費用",),
}


def archive_endpoint() -> str:
    return source_endpoint("mops_cash_flow_statement_archive")


def archive_url(period: str, market_segment: str, symbol: str) -> str:
    if market_segment not in ARCHIVE_MARKETS:
        raise CashFlowHistoryError(
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
    for label in tuple(_clean_cell(value) for value in labels):
        for row in rows:
            if row and _clean_cell(row[0]) == label:
                return _number(row[1] if len(row) > 1 else None), label
    return None, None


def parse_archive_html(
    body: bytes,
    *,
    period: str,
    market_segment: str,
    symbol: str,
    source_url: str,
    request_parameters: dict[str, str],
    acquired_at: str,
) -> CashFlowStatementItem:
    year, quarter = _period_parts(period)
    text = body.decode("utf-8", errors="replace")
    parser = _MopsIncomeStatementHTMLParser()
    parser.feed(text)
    if not parser.rows or "現金流量表" not in text:
        raise CashFlowSourceError(
            f"MOPS archive did not contain a cash-flow statement: {source_url}"
        )
    metrics: dict[str, float | None] = {}
    raw_labels: dict[str, str | None] = {}
    for field, labels in _FIELD_LABELS.items():
        metrics[field], raw_labels[field] = _find_metric(parser.rows, labels)
    if not any(value is not None for value in metrics.values()):
        raise CashFlowSourceError(
            f"MOPS archive contained no supported cash-flow metrics: {source_url}"
        )
    operating = metrics["operating_cash_flow"]
    capex = metrics["capital_expenditure"]
    depreciation = metrics.pop("depreciation", None)
    amortization = metrics.pop("amortization", None)
    depreciation_and_amortization = (
        sum(
            float(value)
            for value in (depreciation, amortization)
            if value is not None
        )
        if depreciation is not None or amortization is not None
        else None
    )
    raw_labels["depreciation_and_amortization"] = " + ".join(
        label
        for label in (
            raw_labels.pop("depreciation", None),
            raw_labels.pop("amortization", None),
        )
        if label
    ) or None
    free_cash_flow = (
        float(operating) - abs(float(capex))
        if operating is not None and capex is not None
        else None
    )
    normalized = normalize_symbol(symbol)
    code = normalized.split(".", 1)[0]
    suffix = str(ARCHIVE_MARKETS[market_segment]["symbol_suffix"])
    return CashFlowStatementItem(
        period=validate_period(period),
        fiscal_year=year,
        quarter=quarter,
        symbol=f"{code}{suffix}",
        name=_company_name(parser.company_caption),
        statement_scope=parser.statement_scope or "現金流量表",
        free_cash_flow=free_cash_flow,
        depreciation_and_amortization=depreciation_and_amortization,
        source=(
            "MOPS historical individual-company cash-flow statement "
            f"({ARCHIVE_MARKETS[market_segment]['label']})"
        ),
        source_id=CASH_FLOW_SOURCE_ID,
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
) -> CashFlowArchivePage:
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
    dataset = get_source_registry().dataset("mops_cash_flow_statement_archive")
    attempts = max(1, dataset.failure_strategy.max_attempts)
    request_timeout = float(timeout or dataset.failure_strategy.timeout_seconds)
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
            return CashFlowArchivePage(
                period=period,
                market_segment=market_segment,
                source_url=source_url,
                request_parameters=request_parameters,
                acquired_at=acquired_at,
                content_type=content_type,
                body=body,
                item=item,
            )
        except (HTTPError, URLError, TimeoutError, CashFlowSourceError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            backoffs = dataset.failure_strategy.backoff_seconds
            if backoffs:
                import time

                time.sleep(float(backoffs[min(attempt, len(backoffs) - 1)]))
    raise CashFlowSourceError(
        f"MOPS cash-flow archive failed for {period}: {last_error}"
    ) from last_error


def _field_provenance(
    record: dict[str, Any],
    temporal: Any,
    raw_payload_id: str,
) -> dict[str, dict[str, Any]]:
    return {
        pointer: {
            "source_id": CASH_FLOW_SOURCE_ID,
            "temporal": temporal,
            "updated_at": temporal.acquired_at,
            "raw_payload_id": raw_payload_id,
            "raw_json_pointer": f"/0{pointer}" if pointer != "/" else "/0",
            "quality_status": "valid",
            "quality_flags": [],
            "transformation_id": CASH_FLOW_TRANSFORMATION,
            "input_fields": [pointer],
        }
        for pointer in payload_leaf_pointers(record)
    }


def _persist_archive_page(
    page: CashFlowArchivePage,
    *,
    platform: MarketDataPlatform,
) -> list[Any]:
    return platform.ingest_records(
        source_id=CASH_FLOW_SOURCE_ID,
        dataset=CASH_FLOW_DATASET,
        records=[page.item.model_dump(mode="json")],
        entity_id_for=lambda row: _entity_id_for(
            str(row["symbol"]),
            market_segment=page.market_segment,
            platform=platform,
        ),
        observation_key_for=lambda row: f"{CASH_FLOW_OBSERVATION_PREFIX}{row['period']}",
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
        raw_parser_id=CASH_FLOW_ARCHIVE_PARSER,
        transformation_id=CASH_FLOW_TRANSFORMATION,
        acquired_at=page.acquired_at,
        field_provenance_for=_field_provenance,
    )


def _profit_quality(
    operating_cash_flow: float | None,
    net_income: float | None,
) -> dict[str, Any]:
    if operating_cash_flow is None or net_income is None or net_income == 0:
        return {
            "status": "insufficient_data",
            "operating_cash_flow_to_net_income": None,
            "net_income": net_income,
        }
    ratio = float(operating_cash_flow) / float(net_income)
    status = (
        "strong_cash_conversion"
        if ratio >= 1
        else "aligned"
        if ratio >= 0.8
        else "weak_cash_conversion"
        if ratio >= 0
        else "negative_operating_cash_flow"
    )
    return {
        "status": status,
        "operating_cash_flow_to_net_income": ratio,
        "net_income": net_income,
    }


def query_cash_flow_history(
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
        raise CashFlowHistoryError("symbol is required")
    end = end_period or latest_conservatively_available_period()
    requested_periods = archive_periods(start_period, end)
    market, resolution_basis = resolve_market_segment(
        normalized,
        platform=service,
        requested=market_segment,
    )
    entity_ids = _storage_entity_ids(
        normalized,
        market_segment=market,
        platform=service,
    )
    rows = [
        row
        for entity_id in entity_ids
        for row in service.standard_query(
            "financials",
            dataset=CASH_FLOW_DATASET,
            entity_id=entity_id,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=10000,
        )
    ]
    requested_set = set(requested_periods)
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(row.get("record") or {})
        if record.get("statement_kind") != "cash_flow_statement":
            continue
        period = str(record.get("period") or "")
        if period not in requested_set:
            continue
        existing = selected.get(period)
        if existing is None or str(row.get("acquired_at") or "") > str(
            existing.get("acquired_at") or ""
        ):
            selected[period] = {**row, "record": record}
    income = query_income_statement_history(
        normalized,
        start_period=requested_periods[0],
        end_period=requested_periods[-1],
        market_segment=market,
        knowledge_at=knowledge_at,
        effective_at=effective_at,
        limit=MAX_HISTORY_QUARTERS,
        platform=service,
    )
    income_by_period = {
        str(item["period"]): item for item in income.get("items") or []
    }
    stored_periods = sorted(selected, key=_period_index)
    page_limit = max(1, min(int(limit), MAX_HISTORY_QUARTERS))
    items = []
    for period in reversed(stored_periods):
        record = CashFlowStatementItem.model_validate(
            selected[period]["record"]
        ).model_dump(mode="json")
        net_income = (income_by_period.get(period) or {}).get("net_income")
        items.append(
            {
                **record,
                "profit_quality": _profit_quality(
                    record.get("operating_cash_flow"),
                    net_income,
                ),
            }
        )
    missing = [period for period in requested_periods if period not in selected]
    return {
        "schema_version": CASH_FLOW_HISTORY_SCHEMA_VERSION,
        "symbol": normalized,
        "entity_id": _entity_id_for(
            normalized,
            market_segment=market,
            platform=service,
        ),
        "storage_entity_ids": entity_ids,
        "market_segment": market,
        "market_resolution_basis": resolution_basis,
        "start_period": requested_periods[0],
        "end_period": requested_periods[-1],
        "unit": "thousand_twd",
        "count": len(items[:page_limit]),
        "total_stored_period_count": len(stored_periods),
        "items": items[:page_limit],
        "coverage": {
            "requested_period_count": len(requested_periods),
            "stored_period_count": len(stored_periods),
            "missing_period_count": len(missing),
            "missing_periods": missing,
            "first_stored_period": stored_periods[0] if stored_periods else None,
            "last_stored_period": stored_periods[-1] if stored_periods else None,
            "is_complete": not missing,
            "covered_years": len({_period_parts(period)[0] for period in stored_periods}),
        },
        "source_ids": [CASH_FLOW_SOURCE_ID] if selected else [],
        "point_in_time": {
            "knowledge_at": knowledge_at,
            "effective_at": effective_at,
            "mode": "explicit_cutoff" if knowledge_at or effective_at else "current",
        },
        "truthfulness": {
            "statement_values": "Official year-to-date cumulative cash flows.",
            "free_cash_flow": (
                "operating_cash_flow - abs(capital_expenditure); null when "
                "either official input is missing."
            ),
            "profit_quality": (
                "Cash conversion compares official operating cash flow with "
                "the saved official net income for the same cumulative period."
            ),
        },
    }


def backfill_cash_flow_history(
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
        raise CashFlowHistoryError("symbol is required")
    end = end_period or latest_conservatively_available_period()
    periods = archive_periods(start_period, end)
    market, resolution_basis = resolve_market_segment(
        normalized,
        platform=service,
        requested=market_segment,
    )
    keys = {
        period: f"cash-flow:{market}:{period}:{normalized}"
        for period in periods
    }
    checkpoints = service.warehouse.get_checkpoints(
        source_id=CASH_FLOW_SOURCE_ID,
        dataset=CASH_FLOW_DATASET,
        partition_keys=keys.values(),
    )
    pending = [
        period
        for period in periods
        if force_refresh or checkpoints.get(keys[period], {}).get("status") != "succeeded"
    ]
    failures: list[dict[str, str]] = []
    fetched: list[str] = []
    revision_count = 0
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 4, len(pending) or 1))) as executor:
        futures = {
            executor.submit(
                fetch_archive_period,
                period,
                market_segment=market,
                symbol=normalized,
            ): period
            for period in pending
        }
        for future in as_completed(futures):
            period = futures[future]
            try:
                page = future.result()
                envelopes = _persist_archive_page(page, platform=service)
            except Exception as exc:
                failures.append(
                    {"period": period, "type": type(exc).__name__, "message": str(exc)}
                )
                service.warehouse.save_checkpoint(
                    source_id=CASH_FLOW_SOURCE_ID,
                    dataset=CASH_FLOW_DATASET,
                    partition_key=keys[period],
                    status="failed",
                    error={"type": type(exc).__name__, "message": str(exc)},
                    metadata={"symbol": normalized, "market_segment": market},
                )
                continue
            fetched.append(period)
            revision_count += len(envelopes)
            service.warehouse.save_checkpoint(
                source_id=CASH_FLOW_SOURCE_ID,
                dataset=CASH_FLOW_DATASET,
                partition_key=keys[period],
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
    history = query_cash_flow_history(
        normalized,
        start_period=start_period,
        end_period=end,
        market_segment=market,
        platform=service,
    )
    return {
        **history,
        "sync": {
            "status": "succeeded" if not failures else "partial" if fetched else "failed",
            "requested_period_count": len(periods),
            "fetched_period_count": len(fetched),
            "skipped_period_count": len(periods) - len(pending),
            "revision_result_count": revision_count,
            "force_refresh": bool(force_refresh),
            "market_resolution_basis": resolution_basis,
            "failed_periods": sorted(
                failures,
                key=lambda item: _period_index(item["period"]),
            ),
        },
    }
