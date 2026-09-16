"""Explicit, conservative board-trade proxy for bounded paper experiments.

This is not an odd-lot exchange quote or a claim to queue priority. Strict
exchange matching remains the default broker behavior. The Host must opt a
paper account into this model; model receipts are never execution evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from open_stock_ai.data.execution_quote import execution_eligibility, parse_exchange_timestamp
from .taiwan_market_rules import tick_size


def _hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class OddLotBoardProxyModel:
    participation_rate: float = 0.01
    max_shares_per_auction: int = 25
    adverse_impact_bps: float = 20.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.participation_rate) or not 0 < self.participation_rate <= 0.01:
            raise ValueError("odd_lot_proxy_participation_must_be_positive_and_at_most_one_percent")
        if isinstance(self.max_shares_per_auction, bool) or not isinstance(self.max_shares_per_auction, int) or not 1 <= self.max_shares_per_auction <= 25:
            raise ValueError("odd_lot_proxy_auction_cap_must_be_1_to_25_shares")
        if not math.isfinite(self.adverse_impact_bps) or not 20 <= self.adverse_impact_bps <= 1000:
            raise ValueError("odd_lot_proxy_adverse_impact_must_be_20_to_1000_bps")

    def evaluate(self, *, ticket: dict[str, Any], market: dict[str, Any], rules: dict[str, Any],
                 now: datetime, slippage_bps: float) -> dict[str, Any]:
        blockers = []
        envelope = market.get("source_envelope") or {}
        trade = market.get("source_trade") or {}
        envelope = envelope if isinstance(envelope, dict) else {}
        trade = trade if isinstance(trade, dict) else {}
        quote = execution_eligibility(envelope, horizon="swing", now=now)
        if not quote["execution_eligible"] or not quote["current_last_trade"]:
            blockers.extend(quote["blockers"] or ["fresh_board_trade_required"])
        if not (rules.get("enforced") and rules.get("order_valid")
                and rules.get("session", {}).get("name") == "odd_lot_call_auction"
                and rules.get("entry_allowed") and ticket.get("side") in {"buy", "sell"}):
            blockers.append("odd_lot_proxy_requires_valid_regular_session_limit_order")
        if market.get("paper_training_fill_override") is True:
            blockers.append("odd_lot_proxy_cannot_use_latest_mark_training_override")
        source_at = parse_exchange_timestamp(trade.get("exchange_timestamp"), envelope.get("provider_id"))
        envelope_at = parse_exchange_timestamp(envelope.get("exchange_timestamp"), envelope.get("provider_id"))
        market_at = parse_exchange_timestamp(market.get("source_timestamp"), envelope.get("provider_id"))
        price, size = _number(trade.get("price")), _number(trade.get("size_shares"))
        bound = bool(trade.get("provider_id") == envelope.get("provider_id")
                     and trade.get("symbol") == ticket.get("symbol") == market.get("symbol")
                     and trade.get("venue") in {"TWSE", "TPEx"}
                     and trade.get("venue") == market.get("exchange")
                     and trade.get("channel") == "regular_lot" and str(trade.get("trade_id") or "").strip()
                     and source_at is not None and source_at == envelope_at == market_at
                     and price is not None and price > 0 and price == _number(market.get("price")))
        if not bound:
            blockers.append("board_trade_identity_or_price_not_bound")
        if size is None or size <= 0 or not size.is_integer():
            blockers.append("observed_board_trade_size_required")
        capacity = min(self.max_shares_per_auction, math.floor(size * self.participation_rate)) if size and size > 0 else 0
        if capacity <= 0:
            blockers.append("board_trade_participation_below_one_share")
        extra = [_number(market.get(field, 0)) for field in ("market_impact_bps", "latency_ms", "latency_impact_bps_per_100ms")]
        if any(value is None or value < 0 for value in extra):
            blockers.append("odd_lot_proxy_additional_impact_invalid")
            extra = [0.0, 0.0, 0.0]
        impact = self.adverse_impact_bps + max(0, slippage_bps) + extra[0] + extra[1] * extra[2] / 100
        simulated_price = None
        if price is not None and price > 0:
            raw = Decimal(str(price)) * (Decimal(1) + Decimal(str(impact / 10000)) * (1 if ticket.get("side") == "buy" else -1))
            if raw > 0:
                unit = tick_size(raw)
                simulated_price = float((raw / unit).to_integral_value(rounding=ROUND_CEILING if ticket.get("side") == "buy" else ROUND_FLOOR) * unit)
            if simulated_price is None or simulated_price <= 0:
                blockers.append("odd_lot_proxy_simulated_price_invalid")
        # Identity excludes received_at: fetching the same trade again cannot
        # manufacture another liquidity budget, even across restarts/orders.
        identity = {key: trade.get(key) for key in ("provider_id", "symbol", "venue", "channel", "trade_id")}
        identity["exchange_timestamp"] = source_at.isoformat() if source_at else None
        body = {
            "schema_version": "open_stock_ai.odd_lot_board_proxy.v1", "is_simulated": True,
            "execution_evidence_eligible": False, "model": "bounded_board_trade_odd_lot_proxy",
            "assumption": "Simulated odd-lot auction using a fresh board trade; no observed odd-lot fill or queue priority.",
            "source_identity": identity, "quote_token": _hash(identity),
            "auction_slot": int(source_at.timestamp()) // 5 if source_at else None,
            "observed_board_trade_size_shares": size, "observed_board_trade_price": price,
            "participation_rate": self.participation_rate, "max_shares_per_auction": self.max_shares_per_auction,
            "quantity_cap": capacity, "adverse_impact_bps": self.adverse_impact_bps,
            "configured_slippage_bps": slippage_bps, "total_adverse_bps": impact, "simulated_fill_price": simulated_price,
            "eligible": not blockers, "blockers": list(dict.fromkeys(blockers)),
            "official_session_reference": "https://twse-regulation.twse.com.tw/ENG/TW/law/DOC01_print.aspx?FLCODE=FL007115&FLNO=8",
        }
        return {**body, "receipt_sha256": _hash(body)}

    def allocate(self, store: Any, *, account_id: str, order_id: str, fill_sequence: int,
                 requested: float, receipt: dict[str, Any]) -> float:
        """Durably consume a shared budget before filling, failing conservatively.

        A crash/rejected OMS fill can consume an opportunity without a fill; it
        cannot create liquidity on retry. Different accounts represent separate
        paper scenarios, while all orders in one account share the same budget.
        """
        if not receipt.get("eligible") or requested <= 0:
            return 0.0
        identity = receipt["source_identity"]
        with store._connect() as conn:
            conn.execute("""create table if not exists paper_odd_lot_proxy_allocations (
                account_id text not null, quote_token text not null, order_id text not null,
                fill_sequence integer not null, symbol text not null, venue text not null,
                channel text not null, auction_slot integer not null, allocated_quantity real not null,
                receipt_json text not null, primary key(account_id,quote_token,order_id,fill_sequence)
            )""")
            conn.execute("create index if not exists idx_paper_odd_lot_proxy_slot on paper_odd_lot_proxy_allocations(account_id,symbol,venue,channel,auction_slot)")
            conn.commit()
            conn.execute("begin immediate")
            existing = conn.execute("select 1 from paper_odd_lot_proxy_allocations where account_id=? and quote_token=? and order_id=? and fill_sequence=?",
                                    (account_id, receipt["quote_token"], order_id, fill_sequence)).fetchone()
            if existing:
                return 0.0
            used_trade = float(conn.execute("select coalesce(sum(allocated_quantity),0) from paper_odd_lot_proxy_allocations where account_id=? and quote_token=?",
                                           (account_id, receipt["quote_token"])).fetchone()[0])
            first = conn.execute("select receipt_json from paper_odd_lot_proxy_allocations where account_id=? and quote_token=? order by rowid limit 1",
                                 (account_id, receipt["quote_token"])).fetchone()
            first_receipt = json.loads(first[0]) if first else receipt
            if any(first_receipt[key] != receipt[key] for key in ("observed_board_trade_size_shares", "observed_board_trade_price")):
                raise ValueError("odd_lot_proxy_trade_identity_conflicts_with_persisted_source")
            used_slot = float(conn.execute("select coalesce(sum(allocated_quantity),0) from paper_odd_lot_proxy_allocations where account_id=? and symbol=? and venue=? and channel=? and auction_slot=?",
                                          (account_id, identity["symbol"], identity["venue"], identity["channel"], receipt["auction_slot"])).fetchone()[0])
            available = max(0, math.floor(min(requested, min(receipt["quantity_cap"], first_receipt["quantity_cap"]) - used_trade,
                                             self.max_shares_per_auction - used_slot)))
            conn.execute("insert into paper_odd_lot_proxy_allocations values (?,?,?,?,?,?,?,?,?,?)",
                         (account_id, receipt["quote_token"], order_id, fill_sequence, identity["symbol"], identity["venue"],
                          identity["channel"], receipt["auction_slot"], available, json.dumps(receipt, sort_keys=True, allow_nan=False)))
            return float(available)
