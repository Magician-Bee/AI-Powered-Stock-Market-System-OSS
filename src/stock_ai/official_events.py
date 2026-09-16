from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import io
from pathlib import Path
import sqlite3
from typing import Any

from open_stock_ai.storage.migrations import ManagedSQLiteConnection

from .models import EventItem
from .data_platform.source_registry import source_endpoint
from .realtime_data import normalize_symbol


SCHEMA_VERSION = "stock_ai.official_events.v1"
IMPORT_SCHEMA_VERSION = "stock_ai.official_events_import.v1"
DEFAULT_DB_PATH = Path("output/official_events.sqlite")
DEFAULT_MOPS_SOURCE_URL = source_endpoint("mops_company_events")

MOPS_COMPANY_EVENTS_SOURCE = {
    "key": "mops_company_events",
    "name": "MOPS company material events",
    "source_tier": 2,
    "official_public_data": True,
    "required_for": ["company_events", "news_center", "material_event_alerts"],
    "update_window": "pre_market_or_after_hours",
    "live_trading_source": False,
    "endpoint": "/api/official/mops/company-events",
    "import_endpoint": "/api/official/mops/company-events/import",
    "csv_import_endpoint": "/api/official/mops/company-events/import-csv",
    "guardrail": "MOPS material events are official public disclosures, not realtime trading quotes.",
}


EVENT_TYPE_ALIASES = {
    "material": "material_event",
    "materialevent": "material_event",
    "重大訊息": "material_event",
    "重大公告": "material_event",
    "法說會": "earnings_call",
    "股東會": "shareholder_meeting",
    "除權息": "ex_dividend",
    "現金股利": "cash_dividend",
    "股票股利": "stock_dividend",
    "減資": "capital_reduction",
    "增資": "capital_increase",
    "私募": "private_placement",
    "併購": "merger",
    "庫藏股": "treasury_stock",
    "注意股票": "attention_stock",
    "處置股票": "disposition_stock",
    "恢復交易": "resume_trading",
    "暫停交易": "halt_trading",
}

CSV_ALIASES = {
    "eventid": "event_id",
    "id": "event_id",
    "事件編號": "event_id",
    "資料序號": "event_id",
    "公司代號": "symbol",
    "股票代號": "symbol",
    "代號": "symbol",
    "symbol": "symbol",
    "code": "symbol",
    "公司名稱": "name",
    "股票名稱": "name",
    "名稱": "name",
    "name": "name",
    "發言日期": "event_date",
    "日期": "event_date",
    "公告日期": "event_date",
    "eventdate": "event_date",
    "date": "event_date",
    "發言時間": "event_time",
    "時間": "event_time",
    "公告時間": "event_time",
    "time": "event_time",
    "事件類型": "event_type",
    "類型": "event_type",
    "eventtype": "event_type",
    "subject": "title",
    "主旨": "title",
    "標題": "title",
    "title": "title",
    "說明": "summary",
    "內容": "summary",
    "summary": "summary",
    "remark": "summary",
    "詳細資料": "summary",
    "網址": "source_url",
    "url": "source_url",
    "sourceurl": "source_url",
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
        create table if not exists mops_company_events (
            event_id text primary key,
            symbol text not null,
            name text,
            event_time text not null,
            event_type text not null,
            title text not null,
            summary text not null,
            source_url text not null,
            official_verified integer not null,
            imported_at text not null
        );
        create index if not exists idx_mops_company_events_symbol_time
            on mops_company_events(symbol, event_time);
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


def _clean_header(value: str | None) -> str:
    return str(value or "").strip().replace("\ufeff", "").lower().replace(" ", "").replace("_", "")


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\u3000", " ").strip()


def _normalize_event_symbol(value: Any) -> str | None:
    text = _clean_text(value).upper()
    if not text:
        return None
    if "." not in text and text[:1].isdigit():
        return f"{text}.TW"
    return normalize_symbol(text)


def _symbol_keys(symbol: str | None) -> set[str]:
    if not symbol:
        return set()
    normalized = _normalize_event_symbol(symbol)
    raw = _clean_text(symbol).upper()
    keys = {raw}
    if normalized:
        keys.add(normalized.upper())
        keys.add(normalized.upper().replace(".TW", "").replace(".TWO", ""))
    return {key for key in keys if key}


def _normalize_event_type(value: Any) -> str:
    text = _clean_text(value)
    compact = text.lower().replace(" ", "").replace("_", "")
    if compact in EVENT_TYPE_ALIASES:
        return EVENT_TYPE_ALIASES[compact]
    if text in EVENT_TYPE_ALIASES:
        return EVENT_TYPE_ALIASES[text]
    return "material_event"


def _normalize_event_time(row: dict[str, Any]) -> str:
    raw = _clean_text(row.get("event_time"))
    date = _clean_text(row.get("event_date"))
    if raw and "T" in raw:
        return raw
    if date and raw:
        return f"{date}T{raw}"
    if date:
        return date
    return raw or _generated_at()


def _event_id(row: dict[str, Any]) -> str:
    explicit = _clean_text(row.get("event_id"))
    if explicit:
        return explicit
    seed = "|".join(
        [
            _clean_text(row.get("symbol")),
            _normalize_event_time(row),
            _clean_text(row.get("title")),
            _clean_text(row.get("summary")),
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"mops-{digest}"


def _normalize_event_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    row: dict[str, Any] = {}
    for key, value in raw.items():
        mapped = CSV_ALIASES.get(_clean_header(key), key)
        row[mapped] = value
    symbol = _normalize_event_symbol(row.get("symbol"))
    title = _clean_text(row.get("title"))
    if not title:
        title = _clean_text(row.get("summary"))[:80]
    if not symbol or not title:
        return None
    summary = _clean_text(row.get("summary")) or title
    return {
        "event_id": _event_id({**row, "symbol": symbol, "title": title, "summary": summary}),
        "symbol": symbol,
        "name": _clean_text(row.get("name")) or None,
        "event_time": _normalize_event_time(row),
        "event_type": _normalize_event_type(row.get("event_type")),
        "title": title,
        "summary": summary[:1000],
        "source_url": _clean_text(row.get("source_url")) or DEFAULT_MOPS_SOURCE_URL,
        "official_verified": 1,
    }


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
    return [dict(row) for row in reader if any(_clean_text(value) for value in row.values())]


def import_mops_company_events(payload: dict[str, Any], *, db_path: str | Path | None = None) -> dict[str, Any]:
    raw_items = payload.get("items") or payload.get("events") or payload.get("rows") or []
    if not isinstance(raw_items, list):
        raw_items = []
    normalized = [_normalize_event_row(item) for item in raw_items if isinstance(item, dict)]
    rows = [item for item in normalized if item]
    imported_at = _generated_at()
    with _connect(db_path) as conn:
        for row in rows:
            conn.execute(
                """
                insert into mops_company_events (
                    event_id, symbol, name, event_time, event_type, title, summary,
                    source_url, official_verified, imported_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(event_id) do update set
                    symbol=excluded.symbol,
                    name=excluded.name,
                    event_time=excluded.event_time,
                    event_type=excluded.event_type,
                    title=excluded.title,
                    summary=excluded.summary,
                    source_url=excluded.source_url,
                    official_verified=excluded.official_verified,
                    imported_at=excluded.imported_at
                """,
                (
                    row["event_id"],
                    row["symbol"],
                    row["name"],
                    row["event_time"],
                    row["event_type"],
                    row["title"],
                    row["summary"],
                    row["source_url"],
                    row["official_verified"],
                    imported_at,
                ),
            )
        conn.commit()
        total_count = _count(conn, "mops_company_events")
    return {
        "schema_version": IMPORT_SCHEMA_VERSION,
        "method": "mops_company_events_json_import",
        "source": MOPS_COMPANY_EVENTS_SOURCE,
        "parsed_count": len(raw_items),
        "imported_count": len(rows),
        "total_count": total_count,
        "db_path": str(_db_path(db_path)),
        "imported_at": imported_at,
        "dry_run": False,
        "live_trading_source": False,
    }


def import_mops_company_events_csv(payload: dict[str, Any], *, db_path: str | Path | None = None) -> dict[str, Any]:
    text = (
        payload.get("text")
        or payload.get("csv_text")
        or payload.get("mops_events_text")
        or payload.get("company_events_text")
        or ""
    )
    rows = _read_delimited_rows(str(text), delimiter=payload.get("delimiter"))
    result = import_mops_company_events({"items": rows}, db_path=db_path)
    result["method"] = "mops_company_events_csv_import"
    result["parsed_row_count"] = len(rows)
    return result


def list_mops_company_events(
    *,
    symbol: str | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[EventItem]:
    with _connect(db_path) as conn:
        params: list[Any] = []
        where = ""
        keys = _symbol_keys(symbol)
        if keys:
            where = f"where upper(symbol) in ({','.join('?' for _ in keys)})"
            params.extend(sorted(keys))
        params.append(max(1, min(limit, 500)))
        rows = _rows(
            conn,
            f"""
            select * from mops_company_events
            {where}
            order by event_time desc, imported_at desc
            limit ?
            """,
            tuple(params),
        )
    return [_event_item_from_row(row) for row in rows]


def _event_item_from_row(row: dict[str, Any]) -> EventItem:
    return EventItem(
        event_id=str(row["event_id"]),
        event_time=str(row["event_time"]),
        related_symbols=[str(row["symbol"])],
        event_type=str(row["event_type"] or "material_event"),
        title=str(row["title"]),
        summary=str(row["summary"]),
        sentiment="neutral",
        estimated_impact_direction="mixed",
        confidence=0.92,
        source_url=str(row["source_url"] or DEFAULT_MOPS_SOURCE_URL),
    )


def official_events_status(*, symbol: str | None = None, db_path: str | Path | None = None) -> dict[str, Any]:
    with _connect(db_path) as conn:
        total_count = _count(conn, "mops_company_events")
        latest_event_time = _latest(conn, "mops_company_events", "event_time")
        items = [item.model_dump() for item in list_mops_company_events(symbol=symbol, limit=20, db_path=db_path)]
    connected = total_count > 0
    return {
        "schema_version": SCHEMA_VERSION,
        "source_tier": 2,
        "dry_run": True,
        "live_trading_source": False,
        "required_source_count": 1,
        "connected_source_count": 1 if connected else 0,
        "sources": [MOPS_COMPANY_EVENTS_SOURCE],
        "mops_company_events": {
            "available": connected,
            "count": total_count,
            "latest_event_time": latest_event_time,
            "items": items,
            "blocking_reason": None
            if connected
            else "MOPS company events have not been imported into the local official cache.",
        },
        "db_path": str(_db_path(db_path)),
        "generated_at": _generated_at(),
    }
