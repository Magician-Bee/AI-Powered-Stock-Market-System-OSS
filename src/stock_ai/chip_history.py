from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from open_stock_ai.config.settings import load_settings

from .phase1_data import list_institutional_flows, list_margin_trading
from .config import get_settings
from .data_platform.chip_pit_coverage import ChipPITCoverageReceiptStore, chip_pit_coverage
from .realtime_data import normalize_symbol
from .short_daytrade_history import ShortDaytradeStore
from .tdcc_holding_history import TDCCHoldingHistoryStore


CHIP_HISTORY_SCHEMA_VERSION = "stock_ai.chip_history.v1"
OFFICIAL_REFRESH_BUDGET_SECONDS = 12.0


def _runtime_data_path() -> Path:
    path = Path(load_settings().sqlite_path).expanduser()
    if not path.is_absolute():
        path = get_settings().project_root / path
    return path


@lru_cache(maxsize=1)
def _default_chip_coverage_store() -> ChipPITCoverageReceiptStore:
    return ChipPITCoverageReceiptStore(_runtime_data_path())


def _dict(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return dict(item.model_dump(mode="json"))
    return dict(item)


def _deduplicate(items: list[dict[str, Any]], *, date_field: str = "trade_date") -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        key = (str(item.get(date_field) or ""), str(item.get("symbol") or ""))
        selected[key] = item
    return sorted(selected.values(), key=lambda item: str(item.get(date_field) or ""))


def _stored_borrowed_short_items(symbol: str, *, limit: int) -> list[dict[str, Any]]:
    return ShortDaytradeStore(_runtime_data_path()).borrowed_short_items(symbol, limit=limit)


def _stored_tdcc_items(symbol: str, *, weeks: int) -> list[dict[str, Any]]:
    return TDCCHoldingHistoryStore(_runtime_data_path()).query(symbol, weeks=weeks)


def _streak(items: list[dict[str, Any]], field: str) -> dict[str, Any]:
    if not items:
        return {"direction": "none", "days": 0}
    latest = float(items[-1].get(field) or 0)
    direction = "buy" if latest > 0 else "sell" if latest < 0 else "flat"
    days = 0
    for item in reversed(items):
        value = float(item.get(field) or 0)
        current = "buy" if value > 0 else "sell" if value < 0 else "flat"
        if current != direction:
            break
        days += 1
    return {"direction": direction, "days": days}


def build_chip_history(
    *,
    symbol: str,
    institutional_items: list[dict[str, Any]],
    margin_items: list[dict[str, Any]],
    borrowed_short_items: list[dict[str, Any]] | None = None,
    tdcc_items: list[dict[str, Any]] | None = None,
    refresh: dict[str, Any] | None = None,
    coverage_store: ChipPITCoverageReceiptStore | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    institutional = _deduplicate(
        [item for item in institutional_items if normalize_symbol(str(item.get("symbol") or "")) == normalized]
    )
    margin = _deduplicate(
        [item for item in margin_items if normalize_symbol(str(item.get("symbol") or "")) == normalized]
    )
    borrowed_short = _deduplicate(
        [
            item
            for item in (borrowed_short_items or [])
            if normalize_symbol(str(item.get("symbol") or "")) == normalized
        ]
    )
    tdcc = _deduplicate(
        [
            item
            for item in (tdcc_items or [])
            if normalize_symbol(str(item.get("symbol") or "")) == normalized
        ],
        date_field="report_date",
    )
    institutional_series = []
    running = {"foreign_net": 0, "trust_net": 0, "dealer_net": 0, "total_institutional_net": 0}
    for item in institutional:
        for field in running:
            running[field] += int(item.get(field) or 0)
        institutional_series.append({
            **item,
            "cumulative": dict(running),
        })
    margin_series = []
    for item in margin:
        margin_balance = int(item.get("margin_balance") or 0)
        short_balance = int(item.get("short_balance") or 0)
        margin_limit = int(item.get("margin_limit") or 0)
        short_limit = int(item.get("short_limit") or 0)
        margin_series.append({
            **item,
            "margin_change": margin_balance - int(item.get("margin_previous_balance") or 0),
            "short_change": short_balance - int(item.get("short_previous_balance") or 0),
            "margin_utilization_percent": round(margin_balance / margin_limit * 100, 4) if margin_limit else None,
            "short_utilization_percent": round(short_balance / short_limit * 100, 4) if short_limit else None,
            "short_to_margin_percent": round(short_balance / margin_balance * 100, 4) if margin_balance else None,
        })
    return {
        "schema_version": CHIP_HISTORY_SCHEMA_VERSION,
        "symbol": normalized,
        "status": "complete"
        if institutional_series and margin_series and borrowed_short and tdcc
        else "partial",
        "institutional": {
            "items": institutional_series,
            "count": len(institutional_series),
            "foreign_streak": _streak(institutional, "foreign_net"),
            "trust_streak": _streak(institutional, "trust_net"),
            "dealer_streak": _streak(institutional, "dealer_net"),
            "total_streak": _streak(institutional, "total_institutional_net"),
            "coverage": {
                "first_date": institutional[0]["trade_date"] if institutional else None,
                "last_date": institutional[-1]["trade_date"] if institutional else None,
            },
        },
        "margin": {
            "items": margin_series,
            "count": len(margin_series),
            "coverage": {
                "first_date": margin[0]["trade_date"] if margin else None,
                "last_date": margin[-1]["trade_date"] if margin else None,
            },
        },
        "companion_streams": {
            "borrowed_short": {
                "count": len(borrowed_short),
                "first_date": borrowed_short[0].get("trade_date") if borrowed_short else None,
                "last_date": borrowed_short[-1].get("trade_date") if borrowed_short else None,
                "kind": "published_borrow_activity_not_executable_locate",
            },
            "tdcc_holding_distribution": {
                "count": len(tdcc),
                "first_date": tdcc[0].get("report_date") if tdcc else None,
                "last_date": tdcc[-1].get("report_date") if tdcc else None,
            },
        },
        "refresh": refresh,
        "pit_coverage": chip_pit_coverage(
            institutional=institutional,
            margin=margin,
            borrowed_short=borrowed_short,
            tdcc=tdcc,
            receipt_store=coverage_store,
        ),
        "truthfulness": {
            "institutional_investors_separate": ["foreign", "investment_trust", "dealer"],
            "margin_and_short_separate": True,
            "history_uses_stored_official_observations": True,
            "calendar_gaps_not_filled_with_zero": True,
        },
    }


def query_chip_history(
    symbol: str,
    *,
    days: int = 20,
    refresh: bool = False,
    refresh_budget_seconds: float = OFFICIAL_REFRESH_BUDGET_SECONDS,
    flow_loader: Callable[..., list[Any]] = list_institutional_flows,
    margin_loader: Callable[..., list[Any]] = list_margin_trading,
    borrowed_short_loader: Callable[..., list[dict[str, Any]]] = _stored_borrowed_short_items,
    tdcc_loader: Callable[..., list[dict[str, Any]]] = _stored_tdcc_items,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    day_count = max(1, min(int(days), 60))
    flow_records: list[dict[str, Any]] = []
    margin_records: list[dict[str, Any]] = []
    refresh_info = None
    if refresh:
        dates = [date.today() - timedelta(days=offset) for offset in range(day_count)]
        failed: list[str] = []
        pending_dates: list[str] = []
        executor = ThreadPoolExecutor(max_workers=6)
        try:
            futures = {
                executor.submit(
                    flow_loader,
                    symbol=normalized,
                    date=target.isoformat(),
                    limit=day_count * 2,
                ): target
                for target in dates
            }
            margin_future = executor.submit(margin_loader, symbol=normalized, limit=day_count * 2)
            completed, pending = wait(
                [*futures, margin_future],
                timeout=max(0.0, float(refresh_budget_seconds)),
            )
            for future in completed:
                if future is margin_future:
                    try:
                        margin_records.extend(_dict(item) for item in future.result())
                    except Exception:
                        failed.append("margin_trading")
                    continue
                target = futures[future]
                try:
                    flow_records.extend(_dict(item) for item in future.result())
                except Exception:
                    failed.append(target.isoformat())
            for future in pending:
                if future is margin_future:
                    pending_dates.append("margin_trading")
                else:
                    pending_dates.append(futures[future].isoformat())
                future.cancel()
        finally:
            # URL requests cannot be forcibly interrupted, but the user-facing
            # response must not wait for a transient official-source outage.
            executor.shutdown(wait=False, cancel_futures=True)
        refresh_info = {
            "requested_calendar_days": day_count,
            "failed_dates": sorted(failed),
            "pending_dates": sorted(pending_dates),
            "budget_seconds": max(0.0, float(refresh_budget_seconds)),
            "zero_fill_used": False,
        }
    if not flow_records and not refresh:
        flow_records = [
            _dict(item)
            for item in flow_loader(symbol=normalized, limit=day_count * 2)
        ]
    if not margin_records and not refresh:
        margin_records = [
            _dict(item) for item in margin_loader(symbol=normalized, limit=day_count * 2)
        ]
    try:
        borrowed_short_items = borrowed_short_loader(symbol=normalized, limit=day_count * 2)
    except Exception:
        # Missing local companion evidence remains visible in the receipt;
        # it must not make the useful institutional/margin view fail open.
        borrowed_short_items = []
    try:
        tdcc_items = tdcc_loader(
            symbol=normalized,
            weeks=min(104, max(1, (day_count + 4) // 5)),
        )
    except Exception:
        tdcc_items = []
    return build_chip_history(
        symbol=normalized,
        institutional_items=flow_records,
        margin_items=margin_records,
        borrowed_short_items=borrowed_short_items,
        tdcc_items=tdcc_items,
        refresh=refresh_info,
        coverage_store=_default_chip_coverage_store(),
    )
