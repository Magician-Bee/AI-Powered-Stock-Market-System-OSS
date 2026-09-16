from __future__ import annotations

import csv
from datetime import datetime
import io
from pathlib import Path
import sqlite3
from typing import Any

import httpx

from open_stock_ai.agent_runtime.transport_guard import default_external_transport_guard
from open_stock_ai.storage.migrations import ManagedSQLiteConnection

from .data_platform.source_registry import source_endpoint
from .models import (
    TaifexFuturesInstitutionalRecord,
    TaifexPutCallRatioRecord,
    TDCCHoldingDistributionRecord,
)


SCHEMA_VERSION = "stock_ai.official_derivatives.v1"
IMPORT_SCHEMA_VERSION = "stock_ai.official_derivatives_import.v1"
DEFAULT_DB_PATH = Path("output/official_derivatives.sqlite")

DEFAULT_TDCC_SOURCE_TEXT = "TDCC official weekly holding distribution"
DEFAULT_TAIFEX_FUTURES_SOURCE_TEXT = "TAIFEX official futures institutional open interest"
DEFAULT_TAIFEX_PUT_CALL_SOURCE_TEXT = "TAIFEX official put/call ratio"
TAIFEX_FUTURES_INSTITUTIONAL_OPENAPI = source_endpoint(
    "taifex_futures_institutional"
)
_TRANSPORT_GUARD = default_external_transport_guard()

TDCC_SOURCE = {
    "key": "tdcc_holding_distribution",
    "name": "TDCC 集保股權分散表",
    "source_tier": 2,
    "official_public_data": True,
    "required_for": ["chip_data", "major_holder_ratio", "small_shareholder_count", "concentration_score"],
    "update_window": "weekly",
    "live_trading_source": False,
    "endpoint": "/api/official/tdcc/holding-distribution",
    "import_endpoint": "/api/official/tdcc/holding-distribution/import",
    "csv_import_endpoint": "/api/official/tdcc/holding-distribution/import-csv",
    "guardrail": "TDCC 週資料只能作為籌碼與集中度分析，不可當成盤中即時交易資料。",
}

TAIFEX_SOURCE = {
    "key": "taifex_derivatives",
    "name": "TAIFEX 期貨與選擇權官方公開資料",
    "source_tier": 2,
    "official_public_data": True,
    "required_for": ["futures_indices", "institutional_futures", "open_interest", "put_call_ratio"],
    "update_window": "pre_market_or_post_close",
    "live_trading_source": False,
    "endpoint": "/api/official/taifex/derivatives-summary",
    "import_endpoint": "/api/official/taifex/derivatives-summary/import",
    "csv_import_endpoint": "/api/official/taifex/derivatives-summary/import-csv",
    "guardrail": "TAIFEX 公開盤後資料可用於期權籌碼與風險背景；盤中即時期貨/選擇權交易仍需正式授權行情或券商 API。",
}


def fetch_taifex_foreign_futures_open_interest(
    contract: str = "臺股期貨",
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Read the latest foreign institutional futures position from TAIFEX.

    TAIFEX publishes this dataset after daily clearing. The returned trade
    date is authoritative; it may be the previous trading day before the
    current day's post-close figures are released.
    """

    selected_contract = str(contract or "臺股期貨").strip()
    response = _TRANSPORT_GUARD.call_sync(
        "source:taifex:futures-institutional",
        lambda: httpx.get(
            TAIFEX_FUTURES_INSTITUTIONAL_OPENAPI,
            headers={"Accept": "application/json", "User-Agent": "Open-Stock-AI/1.0"},
            timeout=max(1.0, min(float(timeout_seconds), 30.0)),
            follow_redirects=True,
        ),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("TAIFEX OpenAPI returned a non-list payload")

    related_contracts = {"臺股期貨", "小型臺指期貨", "微型臺指期貨"}
    parsed: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("Item") or "").strip() not in {"外資", "外資及陸資"}:
            continue
        name = str(item.get("ContractCode") or "").strip()
        if name not in related_contracts:
            continue
        raw_date = str(item.get("Date") or "").strip()
        trade_date = (
            f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            if len(raw_date) == 8 and raw_date.isdigit()
            else raw_date
        )
        foreign_long = int(_clean_numeric(item.get("OpenInterest(Long)"), integer=True) or 0)
        foreign_short = int(_clean_numeric(item.get("OpenInterest(Short)"), integer=True) or 0)
        reported_net = _clean_numeric(item.get("OpenInterest(Net)"), integer=True)
        net = int(reported_net if reported_net is not None else foreign_long - foreign_short)
        parsed.append(
            {
                "trade_date": trade_date,
                "contract": name,
                "investor": str(item.get("Item") or "外資及陸資"),
                "foreign_long": foreign_long,
                "foreign_short": foreign_short,
                "foreign_net": net,
                "net_direction": "net_short" if net < 0 else ("net_long" if net > 0 else "flat"),
                "net_short_contracts": max(-net, 0),
                "net_long_contracts": max(net, 0),
            }
        )

    selected = next((item for item in parsed if item["contract"] == selected_contract), None)
    if selected is None:
        raise RuntimeError(f"TAIFEX OpenAPI has no foreign position row for {selected_contract}")
    return {
        "schema_version": "stock_ai.taifex_foreign_open_interest.v1",
        "source": {
            "name": "臺灣期貨交易所 OpenAPI",
            "url": TAIFEX_FUTURES_INSTITUTIONAL_OPENAPI,
            "official": True,
            "update_window": "post_close_after_daily_clearing",
        },
        "requested_contract": selected_contract,
        "latest_trade_date": selected["trade_date"],
        "position": selected,
        "related_contracts": parsed,
        "interpretation": (
            f"{selected_contract}外資未平倉淨空 {selected['net_short_contracts']:,} 口"
            if selected["foreign_net"] < 0
            else f"{selected_contract}外資未平倉淨多 {selected['net_long_contracts']:,} 口"
        ),
        "publication_note": "期交所資料以最新已公布交易日為準；當日數字通常於盤後結算完成後更新。",
        "aggregation_warning": "臺股期貨、小型臺指期貨與微型臺指期貨契約乘數不同，本結果不將三者口數直接相加。",
        "generated_at": _generated_at(),
    }


def _generated_at() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _db_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else DEFAULT_DB_PATH


def _connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = _db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, factory=ManagedSQLiteConnection)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists tdcc_holding_distribution (
            report_date text not null,
            symbol text not null,
            name text,
            total_holders integer,
            total_shares integer,
            major_holder_1000_lot_ratio real,
            holder_400_lot_ratio real,
            small_shareholder_count integer,
            concentration_score real,
            source text not null,
            imported_at text not null,
            primary key (report_date, symbol)
        );
        create table if not exists taifex_futures_institutional (
            trade_date text not null,
            contract text not null,
            product_name text not null,
            foreign_long integer,
            foreign_short integer,
            investment_trust_long integer,
            investment_trust_short integer,
            dealer_long integer,
            dealer_short integer,
            open_interest integer,
            source text not null,
            imported_at text not null,
            primary key (trade_date, contract)
        );
        create table if not exists taifex_put_call_ratio (
            trade_date text not null primary key,
            put_volume integer,
            call_volume integer,
            put_call_ratio real,
            put_open_interest integer,
            call_open_interest integer,
            open_interest_put_call_ratio real,
            source text not null,
            imported_at text not null
        );
        """
    )
    conn.commit()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"select count(*) from {table}").fetchone()[0])


def _latest(conn: sqlite3.Connection, table: str, date_column: str) -> str | None:
    row = conn.execute(f"select max({date_column}) from {table}").fetchone()
    return row[0] if row and row[0] else None


def _normalize_symbol(symbol: str | None) -> str | None:
    if not symbol:
        return None
    value = symbol.strip().upper()
    if not value:
        return None
    if "." not in value and value[:1].isdigit():
        return f"{value}.TW"
    return value


def _clean_header(value: str | None) -> str:
    return str(value or "").strip().replace("\ufeff", "").lower().replace(" ", "").replace("_", "")


def _clean_numeric(value: Any, *, integer: bool = False) -> int | float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--", "—", "N/A", "NA", "null", "None"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = (
        text.strip("()")
        .replace(",", "")
        .replace("%", "")
        .replace("％", "")
        .replace("張", "")
        .replace("股", "")
        .replace("口", "")
        .strip()
    )
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if negative:
        number = -number
    return int(round(number)) if integer else number


def _read_delimited_rows(text: str, *, delimiter: str | None = None) -> list[dict[str, str]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    sample = raw[:4096]
    selected_delimiter = delimiter
    if not selected_delimiter:
        try:
            selected_delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            selected_delimiter = "\t" if "\t" in sample else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=selected_delimiter)
    return [dict(row) for row in reader if any(str(value or "").strip() for value in row.values())]


def _normalize_delimited_row(row: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        mapped = aliases.get(_clean_header(key))
        if mapped:
            normalized[mapped] = value
    return normalized


TDCC_CSV_ALIASES = {
    "資料日期": "report_date",
    "日期": "report_date",
    "週別": "report_date",
    "reportdate": "report_date",
    "date": "report_date",
    "股票代號": "symbol",
    "證券代號": "symbol",
    "代號": "symbol",
    "symbol": "symbol",
    "code": "symbol",
    "股票名稱": "name",
    "證券名稱": "name",
    "名稱": "name",
    "name": "name",
    "總股東人數": "total_holders",
    "股東人數": "total_holders",
    "totalholders": "total_holders",
    "總股數": "total_shares",
    "集保股數": "total_shares",
    "totalshares": "total_shares",
    "千張大戶比例": "major_holder_1000_lot_ratio",
    "千張以上比例": "major_holder_1000_lot_ratio",
    "1000張以上比例": "major_holder_1000_lot_ratio",
    "majorholder1000lotratio": "major_holder_1000_lot_ratio",
    "400張以上比例": "holder_400_lot_ratio",
    "四百張以上比例": "holder_400_lot_ratio",
    "holder400lotratio": "holder_400_lot_ratio",
    "小股東人數": "small_shareholder_count",
    "smallshareholdercount": "small_shareholder_count",
    "集中度分數": "concentration_score",
    "concentrationscore": "concentration_score",
    "來源": "source",
    "source": "source",
}

TAIFEX_FUTURES_CSV_ALIASES = {
    "交易日期": "trade_date",
    "日期": "trade_date",
    "tradedate": "trade_date",
    "date": "trade_date",
    "契約": "contract",
    "契約代號": "contract",
    "商品代號": "contract",
    "contract": "contract",
    "商品": "product_name",
    "商品名稱": "product_name",
    "productname": "product_name",
    "外資多單": "foreign_long",
    "外資及陸資多單": "foreign_long",
    "foreignlong": "foreign_long",
    "外資空單": "foreign_short",
    "外資及陸資空單": "foreign_short",
    "foreignshort": "foreign_short",
    "投信多單": "investment_trust_long",
    "investmenttrustlong": "investment_trust_long",
    "投信空單": "investment_trust_short",
    "investmenttrustshort": "investment_trust_short",
    "自營商多單": "dealer_long",
    "dealerlong": "dealer_long",
    "自營商空單": "dealer_short",
    "dealershort": "dealer_short",
    "未平倉": "open_interest",
    "未平倉口數": "open_interest",
    "openinterest": "open_interest",
    "來源": "source",
    "source": "source",
}

TAIFEX_PUT_CALL_CSV_ALIASES = {
    "交易日期": "trade_date",
    "日期": "trade_date",
    "tradedate": "trade_date",
    "date": "trade_date",
    "賣權成交量": "put_volume",
    "putvolume": "put_volume",
    "買權成交量": "call_volume",
    "callvolume": "call_volume",
    "putcallratio": "put_call_ratio",
    "putcallratio成交量": "put_call_ratio",
    "賣買權成交量比率": "put_call_ratio",
    "賣權未平倉": "put_open_interest",
    "putopeninterest": "put_open_interest",
    "買權未平倉": "call_open_interest",
    "callopeninterest": "call_open_interest",
    "未平倉putcallratio": "open_interest_put_call_ratio",
    "未平倉賣買權比率": "open_interest_put_call_ratio",
    "openinterestputcallratio": "open_interest_put_call_ratio",
    "來源": "source",
    "source": "source",
}


def _coerce_tdcc_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {**row}
    normalized["symbol"] = _normalize_symbol(str(normalized.get("symbol") or "")) or ""
    normalized.setdefault("source", DEFAULT_TDCC_SOURCE_TEXT)
    for key in ["total_holders", "total_shares", "small_shareholder_count"]:
        normalized[key] = _clean_numeric(normalized.get(key), integer=True)
    for key in ["major_holder_1000_lot_ratio", "holder_400_lot_ratio", "concentration_score"]:
        normalized[key] = _clean_numeric(normalized.get(key), integer=False)
    return normalized


def _coerce_taifex_futures_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {**row}
    normalized.setdefault("source", DEFAULT_TAIFEX_FUTURES_SOURCE_TEXT)
    for key in [
        "foreign_long",
        "foreign_short",
        "investment_trust_long",
        "investment_trust_short",
        "dealer_long",
        "dealer_short",
        "open_interest",
    ]:
        normalized[key] = _clean_numeric(normalized.get(key), integer=True)
    return normalized


def _coerce_taifex_put_call_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = {**row}
    normalized.setdefault("source", DEFAULT_TAIFEX_PUT_CALL_SOURCE_TEXT)
    for key in ["put_volume", "call_volume", "put_open_interest", "call_open_interest"]:
        normalized[key] = _clean_numeric(normalized.get(key), integer=True)
    for key in ["put_call_ratio", "open_interest_put_call_ratio"]:
        normalized[key] = _clean_numeric(normalized.get(key), integer=False)
    return normalized


def _has_values(row: dict[str, Any], required: list[str]) -> bool:
    return all(str(row.get(key) or "").strip() for key in required)


def _tdcc_records_from_payload(payload: dict[str, Any]) -> list[TDCCHoldingDistributionRecord]:
    raw_items = payload.get("items")
    if raw_items is None and any(key in payload for key in TDCCHoldingDistributionRecord.model_fields):
        raw_items = [payload]
    if not isinstance(raw_items, list):
        raw_items = []
    records = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        normalized = _coerce_tdcc_row(item)
        if _has_values(normalized, ["report_date", "symbol", "source"]):
            records.append(TDCCHoldingDistributionRecord(**normalized))
    return records


def _taifex_futures_records_from_payload(payload: dict[str, Any]) -> list[TaifexFuturesInstitutionalRecord]:
    raw_items = payload.get("futures_institutional") or payload.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = []
    records = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        normalized = _coerce_taifex_futures_row(item)
        if _has_values(normalized, ["trade_date", "contract", "product_name", "source"]):
            records.append(TaifexFuturesInstitutionalRecord(**normalized))
    return records


def _taifex_put_call_records_from_payload(payload: dict[str, Any]) -> list[TaifexPutCallRatioRecord]:
    raw_items = payload.get("put_call_ratio") or []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []
    records = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        normalized = _coerce_taifex_put_call_row(item)
        if _has_values(normalized, ["trade_date", "source"]):
            records.append(TaifexPutCallRatioRecord(**normalized))
    return records


def import_tdcc_holding_distribution_csv(payload: dict[str, Any], *, db_path: str | Path | None = None) -> dict[str, Any]:
    text = str(payload.get("text") or payload.get("csv_text") or payload.get("tsv_text") or "")
    delimiter = payload.get("delimiter")
    rows = []
    for row in _read_delimited_rows(text, delimiter=delimiter if isinstance(delimiter, str) else None):
        normalized = _coerce_tdcc_row(_normalize_delimited_row(row, TDCC_CSV_ALIASES))
        if _has_values(normalized, ["report_date", "symbol", "source"]):
            rows.append(normalized)
    result = import_tdcc_holding_distribution({"items": rows}, db_path=db_path)
    return {
        **result,
        "method": "tdcc_holding_distribution_csv_import",
        "input_format": "csv_or_tsv",
        "parsed_row_count": len(rows),
    }


def import_taifex_derivatives_csv(payload: dict[str, Any], *, db_path: str | Path | None = None) -> dict[str, Any]:
    delimiter = payload.get("delimiter")
    delimiter_text = delimiter if isinstance(delimiter, str) else None
    futures_text = str(payload.get("futures_institutional_text") or payload.get("futures_institutional_csv") or "")
    put_call_text = str(payload.get("put_call_ratio_text") or payload.get("put_call_ratio_csv") or "")
    combined_text = str(payload.get("text") or payload.get("csv_text") or payload.get("tsv_text") or "")

    futures_rows = []
    for row in _read_delimited_rows(futures_text, delimiter=delimiter_text):
        normalized = _coerce_taifex_futures_row(_normalize_delimited_row(row, TAIFEX_FUTURES_CSV_ALIASES))
        if _has_values(normalized, ["trade_date", "contract", "product_name", "source"]):
            futures_rows.append(normalized)
    put_call_rows = []
    for row in _read_delimited_rows(put_call_text, delimiter=delimiter_text):
        normalized = _coerce_taifex_put_call_row(_normalize_delimited_row(row, TAIFEX_PUT_CALL_CSV_ALIASES))
        if _has_values(normalized, ["trade_date", "source"]):
            put_call_rows.append(normalized)
    if combined_text:
        for row in _read_delimited_rows(combined_text, delimiter=delimiter_text):
            normalized_futures = _normalize_delimited_row(row, TAIFEX_FUTURES_CSV_ALIASES)
            normalized_put_call = _normalize_delimited_row(row, TAIFEX_PUT_CALL_CSV_ALIASES)
            dataset = _clean_header(str(row.get("dataset") or row.get("資料集") or row.get("類型") or ""))
            if dataset in {"putcall", "putcallratio", "選擇權", "賣買權"} or "put_call_ratio" in normalized_put_call:
                coerced = _coerce_taifex_put_call_row(normalized_put_call)
                if _has_values(coerced, ["trade_date", "source"]):
                    put_call_rows.append(coerced)
            elif normalized_futures:
                coerced = _coerce_taifex_futures_row(normalized_futures)
                if _has_values(coerced, ["trade_date", "contract", "product_name", "source"]):
                    futures_rows.append(coerced)

    result = import_taifex_derivatives(
        {"futures_institutional": futures_rows, "put_call_ratio": put_call_rows},
        db_path=db_path,
    )
    return {
        **result,
        "method": "taifex_derivatives_csv_import",
        "input_format": "csv_or_tsv",
        "parsed_futures_row_count": len(futures_rows),
        "parsed_put_call_row_count": len(put_call_rows),
    }


def import_tdcc_holding_distribution(payload: dict[str, Any], *, db_path: str | Path | None = None) -> dict[str, Any]:
    records = _tdcc_records_from_payload(payload or {})
    imported_at = _generated_at()
    with _connect(db_path) as conn:
        for record in records:
            conn.execute(
                """
                insert into tdcc_holding_distribution (
                    report_date, symbol, name, total_holders, total_shares,
                    major_holder_1000_lot_ratio, holder_400_lot_ratio,
                    small_shareholder_count, concentration_score, source, imported_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(report_date, symbol) do update set
                    name=excluded.name,
                    total_holders=excluded.total_holders,
                    total_shares=excluded.total_shares,
                    major_holder_1000_lot_ratio=excluded.major_holder_1000_lot_ratio,
                    holder_400_lot_ratio=excluded.holder_400_lot_ratio,
                    small_shareholder_count=excluded.small_shareholder_count,
                    concentration_score=excluded.concentration_score,
                    source=excluded.source,
                    imported_at=excluded.imported_at
                """,
                (
                    record.report_date,
                    record.symbol,
                    record.name,
                    record.total_holders,
                    record.total_shares,
                    record.major_holder_1000_lot_ratio,
                    record.holder_400_lot_ratio,
                    record.small_shareholder_count,
                    record.concentration_score,
                    record.source,
                    imported_at,
                ),
            )
        conn.commit()
        total = _count(conn, "tdcc_holding_distribution")
        latest = _latest(conn, "tdcc_holding_distribution", "report_date")
    return {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "method": "tdcc_holding_distribution_import",
        "dry_run": False,
        "mutated": True,
        "imported_count": len(records),
        "stored_count": total,
        "latest_report_date": latest,
        "db_path": str(_db_path(db_path)),
        "source_tier": 2,
        "guardrail": TDCC_SOURCE["guardrail"],
    }


def import_taifex_derivatives(payload: dict[str, Any], *, db_path: str | Path | None = None) -> dict[str, Any]:
    futures_records = _taifex_futures_records_from_payload(payload or {})
    put_call_records = _taifex_put_call_records_from_payload(payload or {})
    imported_at = _generated_at()
    with _connect(db_path) as conn:
        for record in futures_records:
            conn.execute(
                """
                insert into taifex_futures_institutional (
                    trade_date, contract, product_name, foreign_long, foreign_short,
                    investment_trust_long, investment_trust_short, dealer_long,
                    dealer_short, open_interest, source, imported_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(trade_date, contract) do update set
                    product_name=excluded.product_name,
                    foreign_long=excluded.foreign_long,
                    foreign_short=excluded.foreign_short,
                    investment_trust_long=excluded.investment_trust_long,
                    investment_trust_short=excluded.investment_trust_short,
                    dealer_long=excluded.dealer_long,
                    dealer_short=excluded.dealer_short,
                    open_interest=excluded.open_interest,
                    source=excluded.source,
                    imported_at=excluded.imported_at
                """,
                (
                    record.trade_date,
                    record.contract,
                    record.product_name,
                    record.foreign_long,
                    record.foreign_short,
                    record.investment_trust_long,
                    record.investment_trust_short,
                    record.dealer_long,
                    record.dealer_short,
                    record.open_interest,
                    record.source,
                    imported_at,
                ),
            )
        for record in put_call_records:
            conn.execute(
                """
                insert into taifex_put_call_ratio (
                    trade_date, put_volume, call_volume, put_call_ratio,
                    put_open_interest, call_open_interest,
                    open_interest_put_call_ratio, source, imported_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(trade_date) do update set
                    put_volume=excluded.put_volume,
                    call_volume=excluded.call_volume,
                    put_call_ratio=excluded.put_call_ratio,
                    put_open_interest=excluded.put_open_interest,
                    call_open_interest=excluded.call_open_interest,
                    open_interest_put_call_ratio=excluded.open_interest_put_call_ratio,
                    source=excluded.source,
                    imported_at=excluded.imported_at
                """,
                (
                    record.trade_date,
                    record.put_volume,
                    record.call_volume,
                    record.put_call_ratio,
                    record.put_open_interest,
                    record.call_open_interest,
                    record.open_interest_put_call_ratio,
                    record.source,
                    imported_at,
                ),
            )
        conn.commit()
        futures_total = _count(conn, "taifex_futures_institutional")
        put_call_total = _count(conn, "taifex_put_call_ratio")
        latest_futures = _latest(conn, "taifex_futures_institutional", "trade_date")
        latest_put_call = _latest(conn, "taifex_put_call_ratio", "trade_date")
    return {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "method": "taifex_derivatives_import",
        "dry_run": False,
        "mutated": True,
        "imported_futures_count": len(futures_records),
        "imported_put_call_count": len(put_call_records),
        "stored_futures_count": futures_total,
        "stored_put_call_count": put_call_total,
        "latest_futures_trade_date": latest_futures,
        "latest_put_call_trade_date": latest_put_call,
        "db_path": str(_db_path(db_path)),
        "source_tier": 2,
        "guardrail": TAIFEX_SOURCE["guardrail"],
    }


def tdcc_holding_distribution_contract(symbol: str | None = None, *, db_path: str | Path | None = None) -> dict[str, Any]:
    normalized_symbol = _normalize_symbol(symbol)
    sample = TDCCHoldingDistributionRecord(
        report_date="YYYY-MM-DD",
        symbol=normalized_symbol or "<explicit-symbol-required>",
        name="範例標的",
        total_holders=None,
        total_shares=None,
        major_holder_1000_lot_ratio=None,
        holder_400_lot_ratio=None,
        small_shareholder_count=None,
        concentration_score=None,
        source="TDCC official weekly holding distribution",
    )
    rows: list[dict[str, Any]] = []
    total = 0
    latest = None
    if _db_path(db_path).exists():
        with _connect(db_path) as conn:
            where = "where symbol = ?" if normalized_symbol else ""
            params = (normalized_symbol,) if normalized_symbol else ()
            rows = _rows(
                conn,
                f"""
                select report_date, symbol, name, total_holders, total_shares,
                       major_holder_1000_lot_ratio, holder_400_lot_ratio,
                       small_shareholder_count, concentration_score, source
                from tdcc_holding_distribution
                {where}
                order by report_date desc, symbol asc
                limit 100
                """,
                params,
            )
            total = _count(conn, "tdcc_holding_distribution")
            latest = _latest(conn, "tdcc_holding_distribution", "report_date")
    connected = bool(rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "method": "tdcc_holding_distribution_contract",
        "source": TDCC_SOURCE,
        "symbol": normalized_symbol,
        "status": "cache_available" if connected else "external_source_required",
        "connected": connected,
        "dry_run": True,
        "count": len(rows),
        "stored_count": total,
        "latest_report_date": latest,
        "items": rows,
        "sample_record": sample.model_dump(),
        "required_fields": list(TDCCHoldingDistributionRecord.model_fields),
        "blocking_reason": None if connected else "TDCC 官方集保股權分散資料尚未匯入本地快取；目前只提供資料契約與來源邊界。",
        "db_path": str(_db_path(db_path)),
        "generated_at": _generated_at(),
    }


def taifex_derivatives_summary_contract(*, db_path: str | Path | None = None) -> dict[str, Any]:
    futures_sample = TaifexFuturesInstitutionalRecord(
        trade_date="YYYY-MM-DD",
        contract="TX",
        product_name="臺股期貨",
        source="TAIFEX official futures institutional open interest",
    )
    put_call_sample = TaifexPutCallRatioRecord(
        trade_date="YYYY-MM-DD",
        source="TAIFEX official put/call ratio",
    )
    futures_rows: list[dict[str, Any]] = []
    put_call_rows: list[dict[str, Any]] = []
    futures_total = 0
    put_call_total = 0
    latest_futures = None
    latest_put_call = None
    if _db_path(db_path).exists():
        with _connect(db_path) as conn:
            futures_rows = _rows(
                conn,
                """
                select trade_date, contract, product_name, foreign_long, foreign_short,
                       investment_trust_long, investment_trust_short, dealer_long,
                       dealer_short, open_interest, source
                from taifex_futures_institutional
                order by trade_date desc, contract asc
                limit 100
                """,
            )
            put_call_rows = _rows(
                conn,
                """
                select trade_date, put_volume, call_volume, put_call_ratio,
                       put_open_interest, call_open_interest,
                       open_interest_put_call_ratio, source
                from taifex_put_call_ratio
                order by trade_date desc
                limit 100
                """,
            )
            futures_total = _count(conn, "taifex_futures_institutional")
            put_call_total = _count(conn, "taifex_put_call_ratio")
            latest_futures = _latest(conn, "taifex_futures_institutional", "trade_date")
            latest_put_call = _latest(conn, "taifex_put_call_ratio", "trade_date")
    connected = bool(futures_rows or put_call_rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "method": "taifex_derivatives_summary_contract",
        "source": TAIFEX_SOURCE,
        "status": "cache_available" if connected else "external_source_required",
        "connected": connected,
        "dry_run": True,
        "futures_institutional": {
            "count": len(futures_rows),
            "stored_count": futures_total,
            "latest_trade_date": latest_futures,
            "items": futures_rows,
            "sample_record": futures_sample.model_dump(),
            "required_fields": list(TaifexFuturesInstitutionalRecord.model_fields),
        },
        "put_call_ratio": {
            "count": len(put_call_rows),
            "stored_count": put_call_total,
            "latest_trade_date": latest_put_call,
            "items": put_call_rows,
            "sample_record": put_call_sample.model_dump(),
            "required_fields": list(TaifexPutCallRatioRecord.model_fields),
        },
        "blocking_reason": None if connected else "TAIFEX 官方期貨三大法人、未平倉與 Put/Call Ratio 尚未匯入本地快取；目前只提供資料契約與來源邊界。",
        "db_path": str(_db_path(db_path)),
        "generated_at": _generated_at(),
    }


def official_derivatives_status(*, db_path: str | Path | None = None) -> dict[str, Any]:
    tdcc = tdcc_holding_distribution_contract(db_path=db_path)
    taifex = taifex_derivatives_summary_contract(db_path=db_path)
    connected_count = sum(1 for item in [tdcc, taifex] if item.get("connected") is True)
    return {
        "schema_version": SCHEMA_VERSION,
        "method": "official_derivatives_status",
        "source_tier": 2,
        "dry_run": True,
        "live_trading_source": False,
        "connected_source_count": connected_count,
        "required_source_count": 2,
        "sources": [TDCC_SOURCE, TAIFEX_SOURCE],
        "contracts": {
            "tdcc_holding_distribution": tdcc,
            "taifex_derivatives_summary": taifex,
        },
        "db_path": str(_db_path(db_path)),
        "guardrails": [
            TDCC_SOURCE["guardrail"],
            TAIFEX_SOURCE["guardrail"],
            "未接正式券商 API 或授權即時行情時，不啟用正式期貨/選擇權交易或秒級策略。",
        ],
        "generated_at": _generated_at(),
    }
