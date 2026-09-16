"""Opt-in board-trade paper scenario, with durable source-volume allocation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
from typing import Any
from zoneinfo import ZoneInfo

from open_stock_ai.data.execution_quote import execution_eligibility, parse_exchange_timestamp
from .taiwan_market_rules import tick_size


MODEL = "bounded_board_trade_paper"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value):
    try:
        number = float(value)
        return number if not isinstance(value, bool) and math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class BoardLotTradeModel:
    participation_rate: float = 0.01
    max_shares_per_trade: int = 1000
    adverse_impact_bps: float = 20.0

    def __post_init__(self):
        if not math.isfinite(self.participation_rate) or not 0 < self.participation_rate <= 0.01:
            raise ValueError("board_paper_participation_must_be_at_most_one_percent")
        if (isinstance(self.max_shares_per_trade, bool) or not isinstance(self.max_shares_per_trade, int)
                or not 1000 <= self.max_shares_per_trade <= 10000 or self.max_shares_per_trade % 1000):
            raise ValueError("board_paper_trade_cap_must_be_1_to_10_board_lots")
        if not math.isfinite(self.adverse_impact_bps) or not 20 <= self.adverse_impact_bps <= 1000:
            raise ValueError("board_paper_adverse_impact_must_be_20_to_1000_bps")

    def evaluate(self, *, ticket: dict, market: dict, rules: dict, now: datetime, slippage_bps: float) -> dict:
        envelope, trade = market.get("source_envelope") or {}, market.get("source_trade") or {}
        envelope = envelope if isinstance(envelope, dict) else {}
        trade = trade if isinstance(trade, dict) else {}
        gate = execution_eligibility(envelope, horizon="swing", now=now)
        blockers = list(gate["blockers"])
        if not gate["current_last_trade"]:
            blockers.append("board_paper_fresh_current_trade_required")
        if not (rules.get("enforced") and rules.get("order_valid") and rules.get("matching_allowed")
                and rules.get("session", {}).get("name") == "continuous"
                and time(9) <= now.astimezone(ZoneInfo("Asia/Taipei")).time().replace(tzinfo=None) < time(13, 25)
                and ticket.get("lot_type") == "board_lot" and ticket.get("side") in {"buy", "sell"}
                and ticket.get("order_type") == "limit" and ticket.get("time_in_force") == "rod"):
            blockers.append("board_paper_requires_valid_continuous_session_limit_order")
        if market.get("paper_training_fill_override") is True:
            blockers.append("board_paper_cannot_use_latest_mark_override")
        source_at = parse_exchange_timestamp(trade.get("exchange_timestamp"), envelope.get("provider_id"))
        envelope_at = parse_exchange_timestamp(envelope.get("exchange_timestamp"), envelope.get("provider_id"))
        market_at = parse_exchange_timestamp(market.get("source_timestamp"), envelope.get("provider_id"))
        price, size = _number(trade.get("price")), _number(trade.get("size_shares"))
        if not (trade.get("provider_id") == envelope.get("provider_id") in {"twse_mis", "licensed_realtime"}
                and trade.get("symbol") == ticket.get("symbol") == market.get("symbol")
                and trade.get("venue") in {"TWSE", "TPEx"} and trade.get("venue") == market.get("exchange")
                and trade.get("channel") == "regular_lot" and str(trade.get("trade_id") or "").strip()
                and source_at is not None and source_at == envelope_at == market_at
                and price is not None and price > 0 and price == _number(market.get("price"))):
            blockers.append("board_paper_source_trade_identity_or_price_not_bound")
        if size is None or size <= 0 or not size.is_integer():
            blockers.append("board_paper_observed_trade_size_required")
        capacity = min(self.max_shares_per_trade, math.floor(size*self.participation_rate/1000)*1000) if size and size > 0 else 0
        if capacity < 1000:
            blockers.append("board_paper_participation_below_one_board_lot")
        extra = [_number(market.get(field, 0)) for field in ("market_impact_bps", "latency_ms", "latency_impact_bps_per_100ms")]
        if any(value is None or value < 0 for value in extra):
            blockers.append("board_paper_additional_impact_invalid")
            extra = [0.0, 0.0, 0.0]
        impact = self.adverse_impact_bps+max(0, slippage_bps)+extra[0]+extra[1]*extra[2]/100
        simulated = None
        if price is not None and price > 0:
            raw = Decimal(str(price))*(Decimal(1)+Decimal(str(impact/10000))*(1 if ticket.get("side") == "buy" else -1))
            if raw > 0:
                unit = tick_size(raw)
                simulated = float((raw/unit).to_integral_value(rounding=ROUND_CEILING if ticket.get("side") == "buy" else ROUND_FLOOR)*unit)
        if simulated is None or simulated <= 0:
            blockers.append("board_paper_simulated_price_invalid")
        identity = {key: trade.get(key) for key in ("provider_id", "symbol", "venue", "channel", "trade_id")}
        identity["exchange_timestamp"] = source_at.isoformat() if source_at else None
        body = {"schema_version": "open_stock_ai.board_lot_trade_simulation.v1", "model": MODEL,
                "is_simulated": True, "execution_evidence_eligible": False,
                "assumption": "Simulated board-lot execution with adverse price and a share of an observed board trade; no exchange fill or queue priority.",
                "source_identity": identity, "quote_token": _hash(identity),
                "observed_trade_size_shares": size, "observed_trade_price": price,
                "participation_rate": self.participation_rate, "quantity_cap": capacity,
                "max_shares_per_trade": self.max_shares_per_trade, "simulated_fill_price": simulated,
                "total_adverse_bps": impact, "eligible": not blockers, "blockers": list(dict.fromkeys(blockers))}
        return {**body, "receipt_sha256": _hash(body)}

    def allocate(self, store: Any, *, account_id: str, order_id: str, fill_sequence: int,
                 requested: float, receipt: dict) -> float:
        """Reserve before filling; failure may forfeit capacity, never reuse it."""
        if not receipt.get("eligible") or requested < 1000:
            return 0.0
        with store._connect() as conn:
            conn.execute("""create table if not exists paper_board_trade_allocations (
                account_id text not null, quote_token text not null, order_id text not null,
                fill_sequence integer not null, quantity real not null, receipt_json text not null,
                primary key(account_id,quote_token,order_id,fill_sequence))""")
            conn.commit()
            conn.execute("begin immediate")
            if conn.execute("select 1 from paper_board_trade_allocations where account_id=? and quote_token=? and order_id=? and fill_sequence=?",
                            (account_id, receipt["quote_token"], order_id, fill_sequence)).fetchone():
                return 0.0
            first = conn.execute("select receipt_json from paper_board_trade_allocations where account_id=? and quote_token=? order by rowid limit 1",
                                 (account_id, receipt["quote_token"])).fetchone()
            original = json.loads(first[0]) if first else receipt
            if any(original[key] != receipt[key] for key in ("observed_trade_size_shares", "observed_trade_price")):
                raise ValueError("board_paper_trade_identity_conflicts_with_persisted_source")
            used = float(conn.execute("select coalesce(sum(quantity),0) from paper_board_trade_allocations where account_id=? and quote_token=?",
                                      (account_id, receipt["quote_token"])).fetchone()[0])
            quantity = max(0, math.floor(min(requested, min(original["quantity_cap"], receipt["quantity_cap"])-used)/1000)*1000)
            conn.execute("insert into paper_board_trade_allocations values (?,?,?,?,?,?)",
                         (account_id, receipt["quote_token"], order_id, fill_sequence, quantity, json.dumps(receipt, allow_nan=False)))
            return float(quantity)
