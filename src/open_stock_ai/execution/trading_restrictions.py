from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid4, uuid5

from open_stock_ai.storage.sqlite_store import SQLiteStore


TYPE_ALIASES = {
    "attention": "attention_stock",
    "attention_stock": "attention_stock",
    "notice": "attention_stock",
    "disposition": "disposition_stock",
    "disposition_stock": "disposition_stock",
    "punish": "disposition_stock",
    "halt": "halt_trading",
    "halt_trading": "halt_trading",
    "suspend": "halt_trading",
    "resume": "resume_trading",
    "resume_trading": "resume_trading",
    "price_limit": "price_limit",
    "daily_price_limit": "price_limit",
}
SUPPORTED_TYPES = frozenset(TYPE_ALIASES.values())
OFFICIAL_HOSTS = frozenset(
    {
        "twse.com.tw",
        "www.twse.com.tw",
        "openapi.twse.com.tw",
        "mops.twse.com.tw",
        "tpex.org.tw",
        "www.tpex.org.tw",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid trading-restriction number: {value!r}") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("trading-restriction prices must be finite and positive")
    return result


def _timestamp(value: Any, *, field: str, end_of_day: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    try:
        if len(text) == 10:
            parsed = datetime.combine(
                date.fromisoformat(text),
                time.max if end_of_day else time.min,
                tzinfo=timezone.utc,
            )
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date or datetime") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TradingRestrictionLedger:
    """Immutable official trading restrictions and deterministic execution gates."""

    store: SQLiteStore

    def import_restriction(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize(payload)
        payload_hash = sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()
        now = _now()
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            existing = conn.execute(
                """
                select * from trading_restriction_revisions
                 where restriction_id=? and payload_hash=?
                """,
                (normalized["restriction_id"], payload_hash),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return {"created": False, "idempotent": True, "item": self._serialize(existing)}
            previous = conn.execute(
                """
                select * from trading_restriction_revisions
                 where restriction_id=? order by revision desc limit 1
                """,
                (normalized["restriction_id"],),
            ).fetchone()
            revision = int(previous["revision"]) + 1 if previous else 1
            revision_id = f"TRREV-{uuid4().hex}"
            conn.execute(
                """
                insert into trading_restriction_revisions (
                    revision_id, restriction_id, revision, symbol, market,
                    restriction_type, status, effective_from, effective_until,
                    official_verified, source_id, source_url, acquired_at,
                    payload_hash, supersedes_revision_id, terms_json,
                    source_payload_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    normalized["restriction_id"],
                    revision,
                    normalized["symbol"],
                    normalized["market"],
                    normalized["restriction_type"],
                    normalized["status"],
                    normalized["effective_from"],
                    normalized["effective_until"],
                    1 if normalized["official_verified"] else 0,
                    normalized["source_id"],
                    normalized["source_url"],
                    normalized["acquired_at"],
                    payload_hash,
                    previous["revision_id"] if previous else None,
                    _canonical_json(normalized["terms"]),
                    _canonical_json(normalized["source_payload"]),
                    now,
                ),
            )
            row = conn.execute(
                "select * from trading_restriction_revisions where revision_id=?",
                (revision_id,),
            ).fetchone()
            conn.commit()
            return {"created": True, "idempotent": False, "item": self._serialize(row)}

    def import_restrictions(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        if not payloads:
            raise ValueError("at least one trading restriction is required")
        results = [self.import_restriction(payload) for payload in payloads]
        return {
            "schema_version": "open_stock_ai.trading_restriction_import.v1",
            "count": len(results),
            "created_count": sum(1 for result in results if result["created"]),
            "idempotent_count": sum(1 for result in results if result["idempotent"]),
            "items": [result["item"] for result in results],
            "storage": "immutable_revision_ledger",
        }

    def list_restrictions(
        self,
        *,
        symbol: str | None = None,
        as_of: str | None = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses = [
            """
            not exists (
                select 1 from trading_restriction_revisions newer
                 where newer.restriction_id=trading_restriction_revisions.restriction_id
                   and newer.revision>trading_restriction_revisions.revision
            )
            """
        ]
        params: list[Any] = []
        normalized_symbol = str(symbol or "").strip().upper()
        if normalized_symbol:
            clauses.append("symbol=?")
            params.append(normalized_symbol)
        cutoff = _timestamp(as_of or _now(), field="as_of")
        if active_only:
            clauses.extend(
                [
                    "official_verified=1",
                    "status in ('active', 'confirmed')",
                    "effective_from<=?",
                    "(effective_until is null or effective_until>=?)",
                ]
            )
            params.extend([cutoff, cutoff])
        params.append(max(1, min(int(limit), 1000)))
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select * from trading_restriction_revisions where "
                + " and ".join(clauses)
                + " order by effective_from desc, restriction_id limit ?",
                params,
            ).fetchall()
        return {
            "schema_version": "open_stock_ai.trading_restrictions.v1",
            "symbol": normalized_symbol or None,
            "as_of": cutoff,
            "active_only": active_only,
            "count": len(rows),
            "items": [self._serialize(row) for row in rows],
            "source_policy": "official_twse_mops_tpex_source_required_for_execution_gates",
        }

    def evaluate(
        self,
        *,
        ticket: dict[str, Any],
        market: dict[str, Any] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        market = market or {}
        symbol = str(ticket.get("symbol") or "").strip().upper()
        cutoff = as_of or market.get("restriction_as_of") or _now()
        listing = self.list_restrictions(symbol=symbol, as_of=str(cutoff), active_only=True, limit=1000)
        active = list(listing["items"])
        warnings: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []

        trading_state = str(market.get("trading_state") or "").strip().lower()
        if trading_state in {"halted", "suspended", "closed_temporarily"}:
            blockers.append({"code": "trading_halted", "message": "Verified market state is halted."})

        timeline = sorted(
            (
                item
                for item in self.list_restrictions(symbol=symbol, as_of=str(cutoff), limit=1000)["items"]
                if item["official_verified"] and item["effective_from"] <= listing["as_of"]
                and (item["effective_until"] is None or item["effective_until"] >= listing["as_of"])
                and item["status"] in {"active", "confirmed"}
                and item["restriction_type"] in {"halt_trading", "resume_trading"}
            ),
            key=lambda item: (item["effective_from"], item["revision"]),
        )
        if timeline and timeline[-1]["restriction_type"] == "halt_trading":
            blockers.append(
                {
                    "code": "trading_halted",
                    "message": "Official halt is active until an official resume event.",
                    "restriction_id": timeline[-1]["restriction_id"],
                }
            )

        disposition = [item for item in active if item["restriction_type"] == "disposition_stock"]
        attention = [item for item in active if item["restriction_type"] == "attention_stock"]
        if attention:
            warnings.append(
                {
                    "code": "attention_stock",
                    "message": "Official attention-stock notice is active; trading remains allowed.",
                    "restriction_ids": [item["restriction_id"] for item in attention],
                }
            )
        order_type = str(ticket.get("order_type") or "market").lower()
        if disposition:
            warnings.append(
                {
                    "code": "disposition_stock",
                    "message": "Disposition-stock matching or settlement controls are active.",
                    "restriction_ids": [item["restriction_id"] for item in disposition],
                }
            )
            if order_type not in {"limit", "stop_limit"}:
                blockers.append(
                    {
                        "code": "disposition_requires_limit_order",
                        "message": "Paper simulation requires an explicit limit price for disposition stocks.",
                    }
                )

        bounds = self._price_bounds(active, market)
        for field in ("limit_price", "stop_price"):
            value = _number(ticket.get(field))
            if value is None:
                continue
            if bounds["limit_down"] is not None and value < bounds["limit_down"] - 1e-9:
                blockers.append({"code": f"{field}_below_daily_limit", "field": field, **bounds})
            if bounds["limit_up"] is not None and value > bounds["limit_up"] + 1e-9:
                blockers.append({"code": f"{field}_above_daily_limit", "field": field, **bounds})
        price = _number(market.get("price"))
        if price is not None:
            if bounds["limit_down"] is not None and price < bounds["limit_down"] - 1e-9:
                blockers.append({"code": "market_price_below_daily_limit", **bounds})
            if bounds["limit_up"] is not None and price > bounds["limit_up"] + 1e-9:
                blockers.append({"code": "market_price_above_daily_limit", **bounds})
            side = str(ticket.get("side") or "").lower()
            liquidity_confirmed = (
                market.get("liquidity_confirmed") is True
                or (
                    side == "buy"
                    and market.get("buy_liquidity_confirmed") is True
                )
                or (
                    side == "sell"
                    and market.get("sell_liquidity_confirmed") is True
                )
            )
            if order_type in {"market", "stop"} and not liquidity_confirmed:
                if side == "buy" and bounds["limit_up"] is not None and price >= bounds["limit_up"] - 1e-9:
                    blockers.append({"code": "limit_up_liquidity_unverified", **bounds})
                if side == "sell" and bounds["limit_down"] is not None and price <= bounds["limit_down"] + 1e-9:
                    blockers.append({"code": "limit_down_liquidity_unverified", **bounds})

        return {
            "schema_version": "open_stock_ai.trading_restriction_evaluation.v1",
            "symbol": symbol,
            "as_of": listing["as_of"],
            "allowed": not blockers,
            "reason": blockers[0]["code"] if blockers else None,
            "blockers": blockers,
            "warnings": warnings,
            "active_restrictions": active,
            "price_limits": bounds,
            "policy": {
                "attention_stock": "warn_only",
                "disposition_stock": "explicit_limit_order_required",
                "halt_trading": "block_new_orders_and_fills_until_official_resume",
                "price_limit": "reject_out_of_range_prices_and_unverified_limit-locked_market_fills",
            },
        }

    def _normalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").strip().upper()
        restriction_type = TYPE_ALIASES.get(str(payload.get("restriction_type") or payload.get("type") or "").lower())
        if not symbol:
            raise ValueError("symbol is required")
        if restriction_type not in SUPPORTED_TYPES:
            raise ValueError("unsupported trading restriction type")
        source_url = str(payload.get("source_url") or "").strip()
        source_host = (urlparse(source_url).hostname or "").lower()
        official_verified = bool(payload.get("official_verified"))
        if official_verified and source_host not in OFFICIAL_HOSTS:
            raise ValueError("official_verified requires a TWSE, MOPS, or TPEx source URL")
        source_payload = payload.get("source_payload")
        if not isinstance(source_payload, dict) or not source_payload:
            raise ValueError("source_payload must preserve the non-empty official source record")
        effective_from = _timestamp(payload.get("effective_from") or payload.get("effective_date"), field="effective_from")
        effective_until_raw = payload.get("effective_until") or payload.get("end_date")
        effective_until = (
            _timestamp(effective_until_raw, field="effective_until", end_of_day=True)
            if effective_until_raw
            else None
        )
        if effective_until and effective_until < effective_from:
            raise ValueError("effective_until must not precede effective_from")
        terms = dict(payload.get("terms") or {})
        for key in ("limit_up", "limit_down", "auction_interval_minutes", "full_cash_delivery"):
            if key in payload and key not in terms:
                terms[key] = payload[key]
        limit_up = _number(terms.get("limit_up"))
        limit_down = _number(terms.get("limit_down"))
        if limit_up is not None and limit_down is not None and limit_down >= limit_up:
            raise ValueError("limit_down must be below limit_up")
        terms["limit_up"] = limit_up
        terms["limit_down"] = limit_down
        identity = "|".join(
            [
                source_url,
                str(payload.get("source_record_id") or source_payload.get("id") or ""),
                symbol,
                restriction_type,
                effective_from,
            ]
        )
        restriction_id = str(payload.get("restriction_id") or uuid5(NAMESPACE_URL, identity))
        return {
            "restriction_id": restriction_id,
            "symbol": symbol,
            "market": str(payload.get("market") or "TW").strip().upper(),
            "restriction_type": restriction_type,
            "status": str(payload.get("status") or "active").strip().lower(),
            "effective_from": effective_from,
            "effective_until": effective_until,
            "official_verified": official_verified,
            "source_id": str(payload.get("source_id") or source_host or "unknown").strip(),
            "source_url": source_url,
            "acquired_at": _timestamp(payload.get("acquired_at") or _now(), field="acquired_at"),
            "terms": terms,
            "source_payload": source_payload,
        }

    @staticmethod
    def _price_bounds(active: list[dict[str, Any]], market: dict[str, Any]) -> dict[str, float | None]:
        limit_up = _number(market.get("limit_up"))
        limit_down = _number(market.get("limit_down"))
        for item in active:
            if item["restriction_type"] != "price_limit":
                continue
            limit_up = limit_up or _number(item["terms"].get("limit_up"))
            limit_down = limit_down or _number(item["terms"].get("limit_down"))
        return {"limit_up": limit_up, "limit_down": limit_down}

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "revision_id": row["revision_id"],
            "restriction_id": row["restriction_id"],
            "revision": int(row["revision"]),
            "symbol": row["symbol"],
            "market": row["market"],
            "restriction_type": row["restriction_type"],
            "status": row["status"],
            "effective_from": row["effective_from"],
            "effective_until": row["effective_until"],
            "official_verified": bool(row["official_verified"]),
            "source_id": row["source_id"],
            "source_url": row["source_url"],
            "acquired_at": row["acquired_at"],
            "payload_hash": row["payload_hash"],
            "supersedes_revision_id": row["supersedes_revision_id"],
            "terms": json.loads(row["terms_json"]),
            "source_payload": json.loads(row["source_payload_json"]),
            "created_at": row["created_at"],
        }
