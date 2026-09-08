from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from statistics import pstdev
from typing import Any
from uuid import uuid4

import httpx

from open_stock_ai.agent_runtime import default_external_transport_guard
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.research.cost_model import PointInTimeMarketImpactModel

from .daily_history import query_daily_history
from .data_platform.source_registry import source_endpoint
from .realtime_data import normalize_symbol
from .realtime_quotes import fetch_twse_mis_quote


class LiquidityAssessmentError(ValueError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _roc_date(value: Any) -> str:
    text = "".join(character for character in str(value or "") if character.isdigit())
    if len(text) != 7:
        raise LiquidityAssessmentError("official share record has an invalid ROC date")
    return date(int(text[:3]) + 1911, int(text[3:5]), int(text[5:7])).isoformat()


def _positive_int(value: Any, *, field: str) -> int:
    try:
        number = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise LiquidityAssessmentError(f"{field} must be a positive integer") from exc
    if number <= 0:
        raise LiquidityAssessmentError(f"{field} must be a positive integer")
    return number


class OfficialShareLedger:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def import_record(
        self,
        *,
        symbol: str,
        issued_common_shares: Any,
        effective_date: str,
        source_id: str,
        source_url: str,
        source_payload: dict[str, Any],
        acquired_at: str | None = None,
    ) -> dict[str, Any]:
        normalized = normalize_symbol(symbol)
        shares = _positive_int(issued_common_shares, field="issued_common_shares")
        if not source_payload:
            raise LiquidityAssessmentError("source_payload must preserve the official record")
        payload_hash = sha256(_canonical_json(source_payload).encode()).hexdigest()
        created_at = _now()
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                """
                select * from official_share_revisions
                 where symbol=? and payload_hash=?
                """,
                (normalized, payload_hash),
            ).fetchone()
            if existing is not None:
                return self._serialize(existing)
            previous = conn.execute(
                """
                select * from official_share_revisions
                 where symbol=?
                 order by revision desc
                 limit 1
                """,
                (normalized,),
            ).fetchone()
            revision = int(previous["revision"]) + 1 if previous else 1
            revision_id = str(uuid4())
            conn.execute(
                """
                insert into official_share_revisions (
                    revision_id, symbol, revision, effective_date,
                    issued_common_shares, source_id, source_url, acquired_at,
                    payload_hash, supersedes_revision_id, source_payload_json,
                    created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    normalized,
                    revision,
                    effective_date,
                    shares,
                    source_id,
                    source_url,
                    acquired_at or created_at,
                    payload_hash,
                    previous["revision_id"] if previous else None,
                    _canonical_json(source_payload),
                    created_at,
                ),
            )
            conn.commit()
            row = conn.execute(
                "select * from official_share_revisions where revision_id=?",
                (revision_id,),
            ).fetchone()
        return self._serialize(row)

    def latest(self, symbol: str) -> dict[str, Any] | None:
        normalized = normalize_symbol(symbol)
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select * from official_share_revisions
                 where symbol=?
                 order by effective_date desc, revision desc
                 limit 1
                """,
                (normalized,),
            ).fetchone()
        return self._serialize(row) if row else None

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "revision_id": row["revision_id"],
            "symbol": row["symbol"],
            "revision": int(row["revision"]),
            "effective_date": row["effective_date"],
            "issued_common_shares": int(row["issued_common_shares"]),
            "source_id": row["source_id"],
            "source_url": row["source_url"],
            "acquired_at": row["acquired_at"],
            "payload_hash": row["payload_hash"],
            "supersedes_revision_id": row["supersedes_revision_id"],
            "source_payload": json.loads(row["source_payload_json"]),
        }


def fetch_official_share_revision(
    store: SQLiteStore,
    symbol: str,
    *,
    refresh: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    normalized = normalize_symbol(symbol)
    ledger = OfficialShareLedger(store)
    if not refresh:
        return ledger.latest(normalized), None
    code = normalized.split(".", 1)[0]
    is_tpex = normalized.endswith(".TWO")
    dataset = "tpex_companies" if is_tpex else "twse_companies"
    source_id = "tpex_openapi" if is_tpex else "twse_openapi"
    url = source_endpoint(dataset)
    try:
        response = default_external_transport_guard().call_sync(
            f"source:{source_id}:{dataset}:share_revision",
            lambda: httpx.get(
                url, timeout=20, headers={"User-Agent": "StockAI/0.1"}
            ),
        )
        response.raise_for_status()
        rows = response.json()
        code_field = "SecuritiesCompanyCode" if is_tpex else "公司代號"
        share_field = "IssueShares" if is_tpex else "已發行普通股數或TDR原股發行股數"
        date_field = "Date" if is_tpex else "出表日期"
        row = next(
            item
            for item in rows
            if str(item.get(code_field) or "").strip() == code
        )
        return (
            ledger.import_record(
                symbol=normalized,
                issued_common_shares=row.get(share_field),
                effective_date=_roc_date(row.get(date_field)),
                source_id=source_id,
                source_url=url,
                source_payload=row,
            ),
            None,
        )
    except Exception as exc:
        cached = ledger.latest(normalized)
        return (
            cached,
            {
                "code": "official_share_refresh_failed",
                "message": str(exc),
                "served_cached_revision": cached is not None,
            },
        )


def build_liquidity_assessment(
    *,
    symbol: str,
    history_points: list[dict[str, Any]],
    history_source_ids: list[str],
    quote: dict[str, Any] | None,
    share_revision: dict[str, Any] | None,
    window_sessions: int = 20,
    order_quantity_shares: int | None = None,
    assessed_at: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if not 5 <= int(window_sessions) <= 120:
        raise LiquidityAssessmentError("window_sessions must be between 5 and 120")
    quantity = (
        _positive_int(order_quantity_shares, field="order_quantity_shares")
        if order_quantity_shares is not None
        else None
    )
    usable = [
        point
        for point in history_points
        if float(point.get("volume") or 0) > 0
    ][-int(window_sessions) :]
    volumes = [float(point["volume"]) for point in usable]
    turnovers = [
        float(point["turnover"])
        for point in usable
        if point.get("turnover") is not None and float(point["turnover"]) >= 0
    ]
    average_volume = sum(volumes) / len(volumes) if volumes else None
    average_turnover = sum(turnovers) / len(turnovers) if turnovers else None
    latest = usable[-1] if usable else None

    bids = list((quote or {}).get("bids") or [])
    asks = list((quote or {}).get("asks") or [])
    best_bid = float(bids[0]["price"]) if bids and bids[0].get("price") is not None else None
    best_ask = float(asks[0]["price"]) if asks and asks[0].get("price") is not None else None
    midpoint = (
        (best_bid + best_ask) / 2
        if best_bid is not None and best_ask is not None and best_ask >= best_bid
        else None
    )
    spread = best_ask - best_bid if midpoint is not None else None
    spread_bps = spread / midpoint * 10_000 if midpoint else None

    issued_shares = (
        int(share_revision["issued_common_shares"]) if share_revision else None
    )
    current_volume = (quote or {}).get("total_volume_shares")
    turnover_volume = (
        float(current_volume)
        if current_volume is not None
        else float(latest["volume"]) if latest else None
    )
    turnover_rate = (
        turnover_volume / issued_shares * 100
        if turnover_volume is not None and issued_shares
        else None
    )
    turnover_basis = (
        "realtime_cumulative_volume"
        if current_volume is not None
        else "latest_official_daily_volume" if latest else "unavailable"
    )

    participation = (
        quantity / average_volume
        if quantity is not None and average_volume
        else None
    )
    closing_prices = [float(point["close"]) for point in usable if float(point.get("close") or 0) > 0]
    close_returns_bps = [
        (closing_prices[index] / closing_prices[index - 1] - 1.0) * 10_000.0
        for index in range(1, len(closing_prices))
    ]
    realized_volatility_bps = pstdev(close_returns_bps) if len(close_returns_bps) >= 2 else None
    impact_quote = None
    if (
        midpoint is not None
        and quantity is not None
        and average_volume is not None
        and turnover_volume is not None
        and turnover_volume > 0
        and spread_bps is not None
        and realized_volatility_bps is not None
    ):
        impact_quote = PointInTimeMarketImpactModel().quote(
            side="buy",
            reference_price=midpoint,
            quantity=quantity,
            bid_ask_spread_bps=spread_bps,
            realized_volatility_bps=realized_volatility_bps,
            adv_volume_shares=average_volume,
            bar_volume_shares=turnover_volume,
        )
    slippage_bps = impact_quote.total_slippage_bps if impact_quote is not None else None
    blockers: list[str] = []
    if average_volume is None:
        blockers.append("average_volume_unavailable")
    if average_turnover is None:
        blockers.append("average_turnover_unavailable")
    if spread_bps is None:
        blockers.append("live_bid_ask_spread_unavailable")
    if quantity is None:
        blockers.append("order_quantity_required_for_slippage")
    if realized_volatility_bps is None:
        blockers.append("realized_volatility_unavailable")
    if blockers:
        status = "insufficient_data"
    elif (
        spread_bps <= 10
        and participation <= 0.01
        and average_turnover >= 50_000_000
    ):
        status = "highly_tradeable"
    elif (
        spread_bps <= 30
        and participation <= 0.05
        and average_turnover >= 10_000_000
    ):
        status = "tradeable"
    else:
        status = "constrained"

    payload = {
        "schema_version": "stock_ai.liquidity_assessment.v1",
        "symbol": normalized,
        "assessed_at": assessed_at or _now(),
        "window_sessions": int(window_sessions),
        "history_start": usable[0].get("date") if usable else None,
        "history_end": usable[-1].get("date") if usable else None,
        "metrics": {
            "latest_turnover_twd": (
                float(latest["turnover"])
                if latest and latest.get("turnover") is not None
                else None
            ),
            "average_daily_turnover_twd": average_turnover,
            "average_daily_volume_shares": average_volume,
            "turnover_rate_percent": turnover_rate,
            "turnover_rate_basis": turnover_basis,
            "issued_common_shares": issued_shares,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_ask_spread": spread,
            "bid_ask_spread_bps": spread_bps,
            "realized_volatility_bps": realized_volatility_bps,
        },
        "order_assessment": {
            "order_quantity_shares": quantity,
            "average_volume_participation_percent": (
                participation * 100 if participation is not None else None
            ),
            "estimated_slippage_bps": slippage_bps,
            "estimated_slippage_model": (
                "pit_adv_bar_participation_sqrt_impact_plus_half_spread_and_realized_volatility"
                if slippage_bps is not None
                else None
            ),
            "market_impact_receipt": impact_quote.receipt() if impact_quote is not None else None,
            "estimate_is_not_a_fill_guarantee": True,
        },
        "tradability": {
            "status": status,
            "complete": not blockers,
            "blockers": blockers,
            "policy": {
                "highly_tradeable": "spread<=10bps, order<=1% ADV, average turnover>=TWD50m",
                "tradeable": "spread<=30bps, order<=5% ADV, average turnover>=TWD10m",
                "constrained": "complete inputs but one or more tradeable thresholds fail",
                "insufficient_data": "one or more required inputs are unavailable",
            },
        },
        "sources": {
            "history_source_ids": sorted(set(history_source_ids)),
            "history_price_basis": "unadjusted",
            "quote_source_id": (quote or {}).get("provider"),
            "quote_received_at": (quote or {}).get("received_at"),
            "share_revision": share_revision,
        },
        "warnings": list(warnings or []),
    }
    return payload


def assess_symbol_liquidity(
    store: SQLiteStore,
    symbol: str,
    *,
    window_sessions: int = 20,
    order_quantity_shares: int | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    end_date = date.today()
    start_date = end_date - timedelta(days=max(45, int(window_sessions) * 3))
    warnings: list[dict[str, Any]] = []
    history = query_daily_history(
        normalized,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        limit=5000,
        refresh=refresh,
        allow_fallback=False,
        price_basis="unadjusted",
    )
    quote = None
    try:
        quote = asyncio.run(fetch_twse_mis_quote(normalized))["data"]
    except Exception as exc:
        warnings.append({"code": "live_quote_unavailable", "message": str(exc)})
    share_revision, share_warning = fetch_official_share_revision(
        store,
        normalized,
        refresh=refresh,
    )
    if share_warning:
        warnings.append(share_warning)
    assessment = build_liquidity_assessment(
        symbol=normalized,
        history_points=[point.model_dump() for point in history["points"]],
        history_source_ids=list(history.get("source_ids") or []),
        quote=quote,
        share_revision=share_revision,
        window_sessions=window_sessions,
        order_quantity_shares=order_quantity_shares,
        warnings=warnings,
    )
    _persist_assessment(store, assessment)
    return assessment


def _persist_assessment(store: SQLiteStore, payload: dict[str, Any]) -> None:
    assessment_id = str(uuid4())
    input_hash = sha256(_canonical_json(payload).encode()).hexdigest()
    sources = payload["sources"]
    order = payload["order_assessment"]
    with store._connect() as conn:
        conn.execute(
            """
            insert into liquidity_assessments (
                assessment_id, symbol, assessed_at, history_start, history_end,
                window_sessions, order_quantity_shares, tradability_status,
                estimated_slippage_bps, history_source_ids_json,
                quote_source_id, share_revision_id, input_hash,
                assessment_json, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assessment_id,
                payload["symbol"],
                payload["assessed_at"],
                payload["history_start"],
                payload["history_end"],
                payload["window_sessions"],
                order["order_quantity_shares"],
                payload["tradability"]["status"],
                order["estimated_slippage_bps"],
                _canonical_json(sources["history_source_ids"]),
                sources["quote_source_id"],
                (sources.get("share_revision") or {}).get("revision_id"),
                input_hash,
                _canonical_json(payload),
                _now(),
            ),
        )
        conn.commit()
    payload["assessment_id"] = assessment_id
    payload["input_hash"] = input_hash
