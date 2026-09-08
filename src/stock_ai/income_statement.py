from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import re

from open_stock_ai.agent_runtime import default_external_transport_guard

from .data_platform.contracts import payload_leaf_pointers, utc_now
from .data_platform.service import (
    MarketDataPlatform,
    get_market_data_platform,
    stable_entity_id,
)
from .data_platform.source_registry import get_source_registry, source_endpoint
from .models import IncomeStatementItem
from .realtime_data import normalize_symbol


INCOME_STATEMENT_HISTORY_SCHEMA_VERSION = "stock_ai.income_statement_history.v1"
INCOME_STATEMENT_ARCHIVE_PARSER = "stock_ai.mops_income_statement_archive_parser.v1"
INCOME_STATEMENT_TRANSFORMATION = "stock_ai.income_statement_history_normalizer.v1"
INCOME_STATEMENT_DATASET = "fundamentals_quarterly"
INCOME_STATEMENT_SOURCE_ID = "mops_archive"
EARLIEST_ARCHIVE_PERIOD = "2013-Q1"
MAX_HISTORY_QUARTERS = 64
ARCHIVE_MARKETS = {
    "sii": {"exchange": "TWSE", "symbol_suffix": ".TW", "label": "上市"},
    "otc": {"exchange": "TPEx", "symbol_suffix": ".TWO", "label": "上櫃"},
}
_PERIOD_PATTERN = re.compile(r"^(\d{4})-Q([1-4])$")
_SECURITY_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4,8}$", re.IGNORECASE)


class IncomeStatementHistoryError(ValueError):
    pass


class IncomeStatementSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class IncomeStatementArchivePage:
    period: str
    market_segment: str
    source_url: str
    request_parameters: dict[str, str]
    acquired_at: str
    content_type: str
    body: bytes
    item: IncomeStatementItem


def _clean_cell(value: Any) -> str:
    return " ".join(
        str(value or "")
        .replace("\xa0", " ")
        .replace("\u3000", " ")
        .replace("／", "∕")
        .split()
    ).strip()


def _number(value: Any) -> float | None:
    text = _clean_cell(value).replace(",", "").replace("%", "")
    if text.casefold() in {"", "-", "--", "n/a", "na", "不適用", "無"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return float(text)
    except ValueError:
        return None


class _MopsIncomeStatementHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.statement_scope: str | None = None
        self.company_caption: str | None = None
        self._capture_heading: str | None = None
        self._heading_text: list[str] = []
        self._table_depth = 0
        self._target_table_depth: int | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.rows: list[list[str]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        lower = tag.casefold()
        attributes = {key.casefold(): value or "" for key, value in attrs}
        if lower == "table":
            self._table_depth += 1
            classes = set(attributes.get("class", "").split())
            if "hasBorder" in classes and self._target_table_depth is None:
                self._target_table_depth = self._table_depth
        elif lower in {"h2", "h4"}:
            self._capture_heading = lower
            self._heading_text = []
        elif (
            lower == "tr"
            and self._target_table_depth is not None
            and self._table_depth == self._target_table_depth
        ):
            self._row = []
        elif lower in {"td", "th"} and self._row is not None:
            self._cell = []
        elif lower == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._capture_heading is not None:
            self._heading_text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower in {"h2", "h4"} and self._capture_heading == lower:
            text = _clean_cell("".join(self._heading_text))
            if lower == "h2" and text and self.statement_scope is None:
                self.statement_scope = text
            elif lower == "h4" and "本資料由" in text and self.company_caption is None:
                self.company_caption = text
            self._capture_heading = None
            self._heading_text = []
        elif lower in {"td", "th"} and self._cell is not None:
            if self._row is not None:
                self._row.append(_clean_cell("".join(self._cell)))
            self._cell = None
        elif lower == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif lower == "table":
            if self._target_table_depth == self._table_depth:
                self._target_table_depth = None
            self._table_depth = max(0, self._table_depth - 1)


def validate_period(period: str, *, field_name: str = "period") -> str:
    value = str(period or "").strip().upper()
    if not _PERIOD_PATTERN.fullmatch(value):
        raise IncomeStatementHistoryError(
            f"{field_name} must use YYYY-Q1 through YYYY-Q4"
        )
    return value


def _period_parts(period: str) -> tuple[int, int]:
    match = _PERIOD_PATTERN.fullmatch(validate_period(period))
    assert match is not None
    return int(match.group(1)), int(match.group(2))


def _period_index(period: str) -> int:
    year, quarter = _period_parts(period)
    return year * 4 + quarter - 1


def iter_periods(start_period: str, end_period: str) -> list[str]:
    start = validate_period(start_period, field_name="start_period")
    end = validate_period(end_period, field_name="end_period")
    start_index = _period_index(start)
    end_index = _period_index(end)
    if end_index < start_index:
        raise IncomeStatementHistoryError(
            "end_period must not precede start_period"
        )
    count = end_index - start_index + 1
    if count > MAX_HISTORY_QUARTERS:
        raise IncomeStatementHistoryError(
            "income statement history is limited to "
            f"{MAX_HISTORY_QUARTERS} quarters per request"
        )
    return [
        f"{index // 4:04d}-Q{index % 4 + 1}"
        for index in range(start_index, end_index + 1)
    ]


def archive_periods(start_period: str, end_period: str) -> list[str]:
    periods = iter_periods(start_period, end_period)
    if _period_index(periods[0]) < _period_index(EARLIEST_ARCHIVE_PERIOD):
        raise IncomeStatementHistoryError(
            f"MOPS IFRS income statement archive starts at {EARLIEST_ARCHIVE_PERIOD}"
        )
    return periods


def _quarter_end(year: int, quarter: int) -> date:
    month_day = {
        1: (3, 31),
        2: (6, 30),
        3: (9, 30),
        4: (12, 31),
    }
    month, day = month_day[quarter]
    return date(year, month, day)


def latest_conservatively_available_period(now: datetime | None = None) -> str:
    current_date = (now or datetime.now(timezone.utc)).astimezone().date()
    cutoff = current_date - timedelta(days=90)
    for year in range(current_date.year, 2012, -1):
        for quarter in range(4, 0, -1):
            if _quarter_end(year, quarter) <= cutoff:
                return f"{year:04d}-Q{quarter}"
    return EARLIEST_ARCHIVE_PERIOD


def _period_start(period: str) -> str:
    year, quarter = _period_parts(period)
    month = (quarter - 1) * 3 + 1
    return f"{year:04d}-{month:02d}-01"


def _period_end(period: str) -> str:
    year, quarter = _period_parts(period)
    return _quarter_end(year, quarter).isoformat()


def _fiscal_month(period: str) -> str:
    return _period_end(period)[:7]


def _roc_period(period: str) -> tuple[str, str]:
    year, quarter = _period_parts(period)
    if year < 1912:
        raise IncomeStatementHistoryError("period predates the Minguo calendar")
    return str(year - 1911), f"{quarter:02d}"


def archive_endpoint() -> str:
    return source_endpoint("mops_income_statement_archive")


def archive_request_endpoints() -> tuple[str, ...]:
    primary = archive_endpoint()
    current = primary.replace("://mopsov.", "://mops.")
    return tuple(dict.fromkeys((primary, current)))


def archive_url(period: str, market_segment: str, symbol: str) -> str:
    if market_segment not in ARCHIVE_MARKETS:
        raise IncomeStatementHistoryError(
            f"market_segment must be one of {sorted(ARCHIVE_MARKETS)}"
        )
    normalized = normalize_symbol(symbol)
    code = normalized.split(".", 1)[0]
    if not _SECURITY_CODE_PATTERN.fullmatch(code):
        raise IncomeStatementHistoryError("symbol must contain a valid security code")
    roc_year, season = _roc_period(period)
    query = urlencode(
        {
            "TYPEK": market_segment,
            "co_id": code,
            "year": roc_year,
            "season": season,
        }
    )
    return f"{archive_endpoint()}?{query}"


def resolve_market_segment(
    symbol: str,
    *,
    platform: MarketDataPlatform | None = None,
    requested: str | None = None,
) -> tuple[str, str]:
    if requested:
        value = requested.strip().casefold()
        if value not in ARCHIVE_MARKETS:
            raise IncomeStatementHistoryError(
                f"market_segment must be one of {sorted(ARCHIVE_MARKETS)}"
            )
        return value, "explicit"
    normalized = normalize_symbol(symbol)
    service = platform or get_market_data_platform()
    resolution = service.resolve_entity(
        normalized,
        identifier_type="display_symbol",
    )
    if resolution.get("status") == "resolved":
        exchange = str((resolution.get("entity") or {}).get("exchange") or "")
        for segment, contract in ARCHIVE_MARKETS.items():
            if contract["exchange"] == exchange:
                return segment, "entity_registry"
    if normalized.endswith(".TW"):
        return "sii", "display_symbol_suffix_fallback"
    if normalized.endswith(".TWO"):
        return "otc", "display_symbol_suffix_fallback"
    raise IncomeStatementHistoryError(
        "market segment cannot be resolved; pass market_segment explicitly"
    )


_FIELD_LABELS = {
    "revenue": (
        "營業收入合計",
        "營業收入",
        "收益合計",
        "收入合計",
        "收益",
        "收入",
    ),
    "gross_profit": (
        "營業毛利（毛損）淨額",
        "營業毛利（毛損）",
        "營業毛利",
    ),
    "operating_income": (
        "營業利益（損失）",
        "營業利益（損失）淨額",
        "營業利益",
    ),
    "net_income": (
        "母公司業主（淨利∕損）",
        "淨利（損）歸屬於母公司業主",
        "淨利（淨損）歸屬於母公司業主",
        "本期稅後淨利（淨損）",
        "本期淨利（淨損）",
        "本期淨利",
    ),
    "eps": (
        "基本每股盈餘",
        "基本每股盈餘（元）",
    ),
}


def _find_metric_row(
    rows: list[list[str]],
    labels: tuple[str, ...],
) -> tuple[list[str] | None, str | None]:
    normalized_labels = tuple(_clean_cell(label) for label in labels)
    for label in normalized_labels:
        for row in rows:
            if not row or _clean_cell(row[0]) != label:
                continue
            if any(_number(value) is not None for value in row[1:]):
                return row, label
    return None, None


def _reported_and_quarter_value(
    row: list[str] | None,
    *,
    quarter: int,
) -> tuple[float | None, float | None]:
    if row is None:
        return None, None
    reported_index = 5 if quarter in {2, 3} and len(row) > 5 else 1
    reported = _number(row[reported_index] if len(row) > reported_index else None)
    if quarter == 4:
        current_quarter = None
    else:
        current_quarter = _number(row[1] if len(row) > 1 else None)
    return reported, current_quarter


def _company_name(caption: str | None) -> str | None:
    text = _clean_cell(caption)
    match = re.search(r"本資料由(.+?)公司提供", text)
    return _clean_cell(match.group(1)) if match else None


def parse_archive_html(
    body: bytes,
    *,
    period: str,
    market_segment: str,
    symbol: str,
    source_url: str,
    request_parameters: dict[str, str],
    acquired_at: str,
) -> IncomeStatementItem:
    year, quarter = _period_parts(period)
    if market_segment not in ARCHIVE_MARKETS:
        raise IncomeStatementHistoryError("unknown MOPS archive market segment")
    text = body.decode("utf-8", errors="replace")
    parser = _MopsIncomeStatementHTMLParser()
    parser.feed(text)
    if not parser.rows or "綜合損益表" not in text:
        raise IncomeStatementSourceError(
            f"MOPS archive did not contain an income statement: {source_url}"
        )

    reported: dict[str, float | None] = {}
    current_quarter: dict[str, float | None] = {}
    raw_labels: dict[str, str | None] = {}
    for field, labels in _FIELD_LABELS.items():
        row, label = _find_metric_row(parser.rows, labels)
        reported[field], current_quarter[field] = _reported_and_quarter_value(
            row,
            quarter=quarter,
        )
        raw_labels[field] = label

    if not any(value is not None for value in reported.values()):
        raise IncomeStatementSourceError(
            f"MOPS archive contained no supported income statement metrics: {source_url}"
        )
    normalized = normalize_symbol(symbol)
    code = normalized.split(".", 1)[0]
    suffix = str(ARCHIVE_MARKETS[market_segment]["symbol_suffix"])
    canonical_symbol = f"{code}{suffix}"
    net_income_label = raw_labels["net_income"]
    return IncomeStatementItem(
        period=validate_period(period),
        fiscal_year=year,
        quarter=quarter,
        symbol=canonical_symbol,
        name=_company_name(parser.company_caption),
        statement_scope=parser.statement_scope or "綜合損益表",
        revenue=reported["revenue"],
        gross_profit=reported["gross_profit"],
        operating_income=reported["operating_income"],
        net_income=reported["net_income"],
        net_income_basis=(
            "attributable_to_parent"
            if net_income_label
            and ("母公司業主" in net_income_label or "歸屬於母公司" in net_income_label)
            else "total_after_tax"
            if net_income_label
            else None
        ),
        eps=reported["eps"],
        current_quarter_revenue=current_quarter["revenue"],
        current_quarter_gross_profit=current_quarter["gross_profit"],
        current_quarter_operating_income=current_quarter["operating_income"],
        current_quarter_net_income=current_quarter["net_income"],
        current_quarter_eps=current_quarter["eps"],
        current_quarter_status=(
            "official_disclosed"
            if quarter in {1, 2, 3}
            else "not_disclosed_in_annual_summary"
        ),
        source=(
            "MOPS historical individual-company income statement "
            f"({ARCHIVE_MARKETS[market_segment]['label']})"
        ),
        source_id=INCOME_STATEMENT_SOURCE_ID,
        source_url=source_url,
        source_market=market_segment,
        source_request_parameters=dict(request_parameters),
        raw_field_labels=raw_labels,
        acquired_at=acquired_at,
        published_at=None,
        available_at=acquired_at,
    )


def fetch_archive_period(
    period: str,
    *,
    market_segment: str,
    symbol: str,
    timeout: float | None = None,
) -> IncomeStatementArchivePage:
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
    dataset = get_source_registry().dataset("mops_income_statement_archive")
    attempts = max(1, dataset.failure_strategy.max_attempts)
    request_timeout = float(timeout or dataset.failure_strategy.timeout_seconds)
    backoffs = dataset.failure_strategy.backoff_seconds
    request_endpoints = archive_request_endpoints()
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(
                request_endpoints[min(attempt, len(request_endpoints) - 1)],
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
            return IncomeStatementArchivePage(
                period=period,
                market_segment=market_segment,
                source_url=source_url,
                request_parameters=request_parameters,
                acquired_at=acquired_at,
                content_type=content_type,
                body=body,
                item=item,
            )
        except (
            HTTPError,
            URLError,
            TimeoutError,
            IncomeStatementSourceError,
        ) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            if backoffs:
                import time

                time.sleep(float(backoffs[min(attempt, len(backoffs) - 1)]))
    raise IncomeStatementSourceError(
        f"MOPS income statement archive failed for {period}: {last_error}"
    ) from last_error


def _entity_id_for(
    symbol: str,
    *,
    market_segment: str,
    platform: MarketDataPlatform,
) -> str:
    resolution = platform.resolve_entity(
        symbol,
        identifier_type="display_symbol",
    )
    if resolution.get("status") == "resolved":
        return str(resolution["entity"]["entity_id"])
    return stable_entity_id(
        market="taiwan",
        exchange=str(ARCHIVE_MARKETS[market_segment]["exchange"]),
        source_code=normalize_symbol(symbol).split(".", 1)[0],
    )


def _storage_entity_ids(
    symbol: str,
    *,
    market_segment: str,
    platform: MarketDataPlatform,
) -> list[str]:
    normalized = normalize_symbol(symbol)
    canonical = _entity_id_for(
        normalized,
        market_segment=market_segment,
        platform=platform,
    )
    fallback = stable_entity_id(
        market="taiwan",
        exchange=str(ARCHIVE_MARKETS[market_segment]["exchange"]),
        source_code=normalized.split(".", 1)[0],
    )
    return list(dict.fromkeys((canonical, fallback)))


def _field_provenance(
    record: dict[str, Any],
    temporal: Any,
    raw_payload_id: str,
) -> dict[str, dict[str, Any]]:
    return {
        pointer: {
            "source_id": INCOME_STATEMENT_SOURCE_ID,
            "temporal": temporal,
            "updated_at": temporal.acquired_at,
            "raw_payload_id": raw_payload_id,
            "raw_json_pointer": f"/0{pointer}" if pointer != "/" else "/0",
            "quality_status": "valid",
            "quality_flags": [],
            "transformation_id": INCOME_STATEMENT_TRANSFORMATION,
            "input_fields": [pointer],
        }
        for pointer in payload_leaf_pointers(record)
    }


def _persist_archive_page(
    page: IncomeStatementArchivePage,
    *,
    platform: MarketDataPlatform,
) -> list[Any]:
    record = page.item.model_dump(mode="json")
    return platform.ingest_records(
        source_id=INCOME_STATEMENT_SOURCE_ID,
        dataset=INCOME_STATEMENT_DATASET,
        records=[record],
        entity_id_for=lambda row: _entity_id_for(
            str(row["symbol"]),
            market_segment=page.market_segment,
            platform=platform,
        ),
        observation_key_for=lambda row: str(row["period"]),
        time_basis="fiscal_period",
        fiscal_period_for=lambda row: _fiscal_month(str(row["period"])),
        period_start_for=lambda row: _period_start(str(row["period"])),
        period_end_for=lambda row: _period_end(str(row["period"])),
        observed_at_for=lambda row: _period_end(str(row["period"])),
        published_at_for=lambda row: row.get("published_at"),
        available_at_for=lambda row: str(
            row.get("available_at") or page.acquired_at
        ),
        effective_at_for=lambda row: _period_end(str(row["period"])),
        request_url=page.source_url,
        raw_response=page.body,
        raw_content_type=page.content_type,
        raw_content_encoding="utf-8",
        raw_parser_id=INCOME_STATEMENT_ARCHIVE_PARSER,
        transformation_id=INCOME_STATEMENT_TRANSFORMATION,
        acquired_at=page.acquired_at,
        field_provenance_for=_field_provenance,
    )


def query_income_statement_history(
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
        raise IncomeStatementHistoryError("symbol is required")
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
            dataset=INCOME_STATEMENT_DATASET,
            entity_id=storage_entity_id,
            knowledge_at=knowledge_at,
            effective_at=effective_at,
            limit=10000,
        )
    ]
    requested_set = set(requested_periods)
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("source_id") or "") != INCOME_STATEMENT_SOURCE_ID:
            continue
        record = dict(row.get("record") or {})
        if record.get("statement_kind") not in {None, "income_statement"}:
            continue
        if (
            str(record.get("statement_semantics") or "")
            != "year_to_date_cumulative"
        ):
            continue
        period = str(record.get("period") or row.get("observation_key") or "")
        if period not in requested_set:
            continue
        existing = selected.get(period)
        if existing is None or str(row.get("acquired_at") or "") > str(
            existing.get("acquired_at") or ""
        ):
            selected[period] = {**row, "record": record}
    ordered_periods = sorted(selected, key=_period_index, reverse=True)
    page_limit = max(1, min(int(limit), MAX_HISTORY_QUARTERS))
    items = [
        IncomeStatementItem.model_validate(
            selected[period]["record"]
        ).model_dump(mode="json")
        for period in ordered_periods[:page_limit]
    ]
    stored_periods = sorted(selected, key=_period_index)
    missing_periods = [
        period for period in requested_periods if period not in selected
    ]
    entity_id = _entity_id_for(
        normalized,
        market_segment=market,
        platform=service,
    )
    return {
        "schema_version": INCOME_STATEMENT_HISTORY_SCHEMA_VERSION,
        "symbol": normalized,
        "entity_id": entity_id,
        "storage_entity_ids": storage_entity_ids,
        "market_segment": market,
        "market_resolution_basis": resolution_basis,
        "start_period": requested_periods[0],
        "end_period": requested_periods[-1],
        "unit": "thousand_twd",
        "eps_unit": "twd_per_share",
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
            "covered_years": (
                len(
                    {
                        _period_parts(period)[0]
                        for period in stored_periods
                    }
                )
            ),
        },
        "source_ids": (
            [INCOME_STATEMENT_SOURCE_ID] if selected else []
        ),
        "point_in_time": {
            "knowledge_at": knowledge_at,
            "effective_at": effective_at,
            "mode": "explicit_cutoff" if knowledge_at or effective_at else "current",
        },
        "truthfulness": {
            "reported_values": (
                "Primary revenue, gross profit, operating income, net income "
                "and EPS are official year-to-date cumulative values."
            ),
            "single_quarter_values": (
                "Q1-Q3 single-quarter values are official disclosed columns; "
                "Q4 single-quarter values remain null because the annual "
                "summary does not disclose them."
            ),
            "archive_publication_time": (
                "The historical statement page does not provide the original "
                "filing timestamp; published_at remains null and available_at "
                "is conservatively the actual acquisition time."
            ),
            "missing_values": (
                "Metrics that are not meaningful or not disclosed for an "
                "industry remain null."
            ),
        },
    }


def backfill_income_statement_history(
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
        raise IncomeStatementHistoryError("symbol is required")
    end = end_period or latest_conservatively_available_period()
    requested_periods = archive_periods(start_period, end)
    market, resolution_basis = resolve_market_segment(
        normalized,
        platform=service,
        requested=market_segment,
    )
    partition_keys = {
        period: f"{market}:{period}:{normalized}"
        for period in requested_periods
    }
    checkpoints = service.warehouse.get_checkpoints(
        source_id=INCOME_STATEMENT_SOURCE_ID,
        dataset=INCOME_STATEMENT_DATASET,
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
                    source_id=INCOME_STATEMENT_SOURCE_ID,
                    dataset=INCOME_STATEMENT_DATASET,
                    partition_key=partition_key,
                    status="failed",
                    error={"type": type(exc).__name__, "message": str(exc)},
                    metadata={"symbol": normalized, "market_segment": market},
                )
                continue
            fetched_periods.append(period)
            revision_count += len(envelopes)
            service.warehouse.save_checkpoint(
                source_id=INCOME_STATEMENT_SOURCE_ID,
                dataset=INCOME_STATEMENT_DATASET,
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
    history = query_income_statement_history(
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
