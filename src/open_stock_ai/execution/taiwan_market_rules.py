from __future__ import annotations

"""Fail-closed Taiwan exchange rules for the local paper-broker path.

This is deliberately a *matching eligibility* contract, not a claim that the
paper broker has an exchange order book.  A normal quote is enough for regular
board-lot continuous matching.  Odd-lot and auction executions require the
matching receipt from their respective auction, otherwise the order remains
open instead of borrowing a regular-board-lot price.
"""

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from stock_ai.market_calendar import TAIPEI_TZ, default_taiwan_market_calendar


SCHEMA_VERSION = "open_stock_ai.taiwan_paper_market_rules.v1"
BOARD_LOT_SIZE = 1_000
OFFICIAL_RULE_SOURCES = (
    "https://www.twse.com.tw/en/products/system/trading.html",
    "https://twse-regulation.twse.com.tw/EN/law/DAT0201.aspx?FLCODE=FL007115",
    "https://www.tpex.org.tw/en-us/mainboard/trading/rules/system.html",
)


def _decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid exchange-rule number: {value!r}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("exchange-rule numbers must be finite and positive")
    return parsed


def tick_size(price: Any) -> Decimal:
    """Return the TWSE/TPEx equity price tick for a positive TWD price."""

    value = _decimal(price)
    if value is None:
        raise ValueError("tick price is required")
    if value < Decimal("10"):
        return Decimal("0.01")
    if value < Decimal("50"):
        return Decimal("0.05")
    if value < Decimal("100"):
        return Decimal("0.1")
    if value < Decimal("500"):
        return Decimal("0.5")
    if value < Decimal("1000"):
        return Decimal("1")
    return Decimal("5")


def is_valid_tick(price: Any) -> bool:
    value = _decimal(price)
    if value is None:
        return True
    unit = tick_size(value)
    return value % unit == 0


@dataclass(frozen=True)
class TaiwanPaperMarketRules:
    """Validate one Taiwan paper order against tradable units and phases."""

    board_lot_size: int = BOARD_LOT_SIZE

    def evaluate(self, *, ticket: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
        symbol = str(ticket.get("symbol") or "").strip().upper()
        venue = str(ticket.get("market") or market.get("market") or "TW").strip().upper()
        enforced = market.get("exchange_rules_enforced") is True
        if not self._is_taiwan(venue, symbol):
            return self._non_taiwan_result(symbol=symbol, venue=venue)

        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        quantity = _decimal(ticket.get("requested_quantity") or ticket.get("quantity_shares"))
        lot_type = str(ticket.get("lot_type") or "board_lot").strip().lower()
        order_type = str(ticket.get("order_type") or "market").strip().lower()
        time_in_force = str(ticket.get("time_in_force") or "rod").strip().lower()
        session = str(ticket.get("session") or "regular").strip().lower()

        if quantity is None:
            blockers.append({"code": "exchange_quantity_missing"})
        elif quantity != quantity.to_integral_value():
            blockers.append({"code": "share_quantity_must_be_integral", "quantity": float(quantity)})
        elif lot_type == "board_lot" and quantity % self.board_lot_size != 0:
            blockers.append(
                {
                    "code": "board_lot_quantity_must_be_multiple_of_1000",
                    "quantity": float(quantity),
                    "board_lot_size": self.board_lot_size,
                }
            )
        elif lot_type == "odd_lot" and not (Decimal("1") <= quantity < self.board_lot_size):
            blockers.append(
                {
                    "code": "odd_lot_quantity_must_be_1_to_999_shares",
                    "quantity": float(quantity),
                    "board_lot_size": self.board_lot_size,
                }
            )
        elif lot_type not in {"board_lot", "odd_lot"}:
            blockers.append({"code": "unsupported_taiwan_lot_type", "lot_type": lot_type})

        if lot_type == "odd_lot" and (order_type != "limit" or time_in_force != "rod"):
            blockers.append(
                {
                    "code": "odd_lot_requires_limit_order_and_rod",
                    "order_type": order_type,
                    "time_in_force": time_in_force,
                }
            )
        if session == "after_hours" and lot_type == "board_lot" and (
            order_type != "limit" or time_in_force != "rod"
        ):
            blockers.append(
                {
                    "code": "after_hours_fixed_price_requires_limit_order_and_rod",
                    "order_type": order_type,
                    "time_in_force": time_in_force,
                }
            )
        if session not in {"regular", "after_hours"}:
            blockers.append({"code": "unsupported_taiwan_session", "session": session})

        for field in ("limit_price", "stop_price"):
            value = ticket.get(field)
            if value in {None, ""}:
                continue
            if not is_valid_tick(value):
                blockers.append(
                    {
                        "code": f"{field}_is_off_tick",
                        "field": field,
                        "value": float(_decimal(value) or 0),
                        "tick_size": str(tick_size(value)),
                    }
                )
        market_price = market.get("price")
        if market_price not in {None, ""} and not is_valid_tick(market_price):
            blockers.append(
                {
                    "code": "market_price_is_off_tick",
                    "value": float(_decimal(market_price) or 0),
                    "tick_size": str(tick_size(market_price)),
                }
            )

        price_limits = self._price_limits(market)
        if price_limits["enforced"]:
            for field in ("limit_price", "stop_price", "price"):
                value = ticket.get(field) if field != "price" else market.get("price")
                decimal = _decimal(value)
                if decimal is None:
                    continue
                if decimal < price_limits["limit_down"] or decimal > price_limits["limit_up"]:
                    blockers.append(
                        {
                            "code": f"{field}_outside_daily_price_limit",
                            "field": field,
                            "value": float(decimal),
                            "limit_down": float(price_limits["limit_down"]),
                            "limit_up": float(price_limits["limit_up"]),
                        }
                    )
        elif not price_limits["exempt"]:
            warnings.append({"code": "daily_price_limits_not_supplied_by_quote"})
            if enforced:
                blockers.append({"code": "daily_price_limits_required_for_exchange_simulation"})

        if market.get("paper_training_fill_override") is True:
            # A local training fill is deliberately not an exchange order. It
            # retains all arithmetic, price-tick and daily-limit validation,
            # but does not pretend a 100-share practice fill is a board-lot or
            # odd-lot exchange submission. The receipt labels this boundary.
            exchange_submission_codes = {
                "board_lot_quantity_must_be_multiple_of_1000",
                "odd_lot_quantity_must_be_1_to_999_shares",
                "unsupported_taiwan_lot_type",
                "odd_lot_requires_limit_order_and_rod",
                "after_hours_fixed_price_requires_limit_order_and_rod",
                "unsupported_taiwan_session",
                # Official close payloads are a valid, signed source for a
                # local training mark but do not always contain the next
                # session's price-limit bands.  Those bands are mandatory for
                # an exchange simulation, not for this explicitly labelled
                # non-exchange fill.  When bands *are* supplied, the checks
                # above remain in force.
                "daily_price_limits_required_for_exchange_simulation",
            }
            blockers = [item for item in blockers if item.get("code") not in exchange_submission_codes]
        phase = (
            {
                "entry_allowed": True,
                "matching_allowed": True,
                "reason": "local_paper_training_latest_verified_mark",
                "mode": "local_paper_training",
            }
            if market.get("paper_training_fill_override") is True
            else self._phase(ticket=ticket, market=market, blockers=blockers)
        )
        order_valid = not blockers
        return {
            "schema_version": SCHEMA_VERSION,
            "venue": venue,
            "symbol": symbol,
            "enforced": enforced,
            "order_valid": order_valid,
            "entry_allowed": phase["entry_allowed"],
            "matching_allowed": phase["matching_allowed"],
            "allowed": order_valid and phase["entry_allowed"],
            "reason": blockers[0]["code"] if blockers else (None if phase["entry_allowed"] else phase["reason"]),
            "blockers": blockers,
            "warnings": warnings,
            "tick_size": str(tick_size(market_price)) if market_price not in {None, ""} else None,
            "price_limits": self._serialize_price_limits(price_limits),
            "session": phase,
            "official_rule_sources": list(OFFICIAL_RULE_SOURCES),
            "matching_boundary": (
                "local paper-training fill at the latest verified mark; not an exchange queue or live order"
                if market.get("paper_training_fill_override") is True
                else "paper broker uses an explicit quote/auction receipt and does not claim exchange queue priority"
            ),
        }

    def session_expired(
        self,
        *,
        ticket: dict[str, Any],
        submitted_market: dict[str, Any],
        observed_market: dict[str, Any],
    ) -> bool:
        """Whether a same-day ROD order has passed its exchange session."""

        if str(ticket.get("time_in_force") or "rod").lower() != "rod":
            return False
        submitted_at = self._market_timestamp(submitted_market)
        observed_at = self._market_timestamp(observed_market)
        if submitted_at is None or observed_at is None:
            return False
        submitted_local = submitted_at.astimezone(TAIPEI_TZ)
        observed_local = observed_at.astimezone(TAIPEI_TZ)
        if observed_local.date() > submitted_local.date():
            return True
        if observed_local.date() < submitted_local.date():
            return False
        session = str(ticket.get("session") or "regular").lower()
        cutoff = time(13, 30) if session == "regular" else time(14, 30)
        return observed_local.time().replace(tzinfo=None) > cutoff

    def _phase(self, *, ticket: dict[str, Any], market: dict[str, Any], blockers: list[dict[str, Any]]) -> dict[str, Any]:
        observed_at = self._market_timestamp(market)
        if observed_at is None:
            blockers.append({"code": "exchange_timestamp_required"})
            return {
                "name": "unknown",
                "entry_allowed": False,
                "matching_allowed": False,
                "reason": "exchange_timestamp_required",
            }
        local = observed_at.astimezone(TAIPEI_TZ)
        day = default_taiwan_market_calendar().day_status(local.date())
        if not day["trading_day"]:
            return {
                "name": "closed",
                "entry_allowed": False,
                "matching_allowed": False,
                "reason": "market_closed",
                "calendar_reason": day["reason"],
                "as_of": local.isoformat(),
            }
        clock = local.time().replace(tzinfo=None)
        lot_type = str(ticket.get("lot_type") or "board_lot").lower()
        session = str(ticket.get("session") or "regular").lower()
        if session == "regular" and lot_type == "board_lot":
            if time(8, 30) <= clock < time(9, 0):
                return self._phase_result("pre_open", True, False, "awaiting_opening_auction", local)
            if time(9, 0) <= clock < time(13, 25):
                return self._phase_result("continuous", True, True, None, local)
            if time(13, 25) <= clock < time(13, 30):
                matched = market.get("auction_matched") is True
                return self._phase_result("closing_auction", True, matched, "awaiting_closing_auction", local)
            return self._phase_result("closed", False, False, "regular_session_closed", local)
        if session == "regular" and lot_type == "odd_lot":
            if time(9, 0) <= clock < time(9, 10):
                return self._phase_result("odd_lot_opening_auction", True, False, "awaiting_odd_lot_opening_auction", local)
            if time(9, 10) <= clock < time(13, 30):
                matched = market.get("odd_lot_auction_matched") is True
                return self._phase_result("odd_lot_call_auction", True, matched, "awaiting_odd_lot_auction_receipt", local)
            return self._phase_result("closed", False, False, "odd_lot_session_closed", local)
        if session == "after_hours" and lot_type == "board_lot":
            if time(14, 0) <= clock < time(14, 30):
                return self._phase_result("after_hours_fixed_price", True, False, "awaiting_after_hours_fixed_price_match", local)
            matched = (
                clock == time(14, 30)
                and market.get("after_hours_fixed_price_matched") is True
                and self._matches_closing_price(market)
            )
            return self._phase_result("after_hours_fixed_price", False, matched, "after_hours_session_closed", local)
        if session == "after_hours" and lot_type == "odd_lot":
            if time(13, 40) <= clock < time(14, 30):
                return self._phase_result("after_hours_odd_lot_auction", True, False, "awaiting_after_hours_odd_lot_auction", local)
            matched = clock == time(14, 30) and market.get("odd_lot_auction_matched") is True
            return self._phase_result("after_hours_odd_lot_auction", False, matched, "after_hours_odd_lot_session_closed", local)
        return self._phase_result("unknown", False, False, "unsupported_taiwan_session", local)

    @staticmethod
    def _phase_result(name: str, entry_allowed: bool, matching_allowed: bool, reason: str | None, local: datetime) -> dict[str, Any]:
        return {
            "name": name,
            "entry_allowed": entry_allowed,
            "matching_allowed": matching_allowed,
            "reason": reason,
            "as_of": local.isoformat(),
            "timezone": "Asia/Taipei",
        }

    @staticmethod
    def _price_limits(market: dict[str, Any]) -> dict[str, Any]:
        exempt = market.get("price_limit_exempt") is True
        limit_up = _decimal(market.get("limit_up"))
        limit_down = _decimal(market.get("limit_down"))
        if (limit_up is None) != (limit_down is None):
            raise ValueError("daily_price_limits_must_include_both_bounds")
        if limit_up is not None and limit_down is not None and limit_down >= limit_up:
            raise ValueError("daily_price_limit_bounds_are_invalid")
        return {"enforced": limit_up is not None, "exempt": exempt, "limit_up": limit_up, "limit_down": limit_down}

    @staticmethod
    def _serialize_price_limits(limits: dict[str, Any]) -> dict[str, Any]:
        return {
            "enforced": bool(limits["enforced"]),
            "exempt": bool(limits["exempt"]),
            "limit_up": float(limits["limit_up"]) if limits["limit_up"] is not None else None,
            "limit_down": float(limits["limit_down"]) if limits["limit_down"] is not None else None,
        }

    @staticmethod
    def _market_timestamp(market: dict[str, Any]) -> datetime | None:
        value = market.get("source_timestamp")
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("exchange_timestamp_must_be_iso8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("exchange_timestamp_timezone_required")
        return parsed

    @staticmethod
    def _matches_closing_price(market: dict[str, Any]) -> bool:
        price = _decimal(market.get("price"))
        close = _decimal(market.get("closing_price"))
        return price is not None and close is not None and price == close

    @staticmethod
    def _is_taiwan(venue: str, symbol: str) -> bool:
        return venue in {"TW", "TWSE", "TPEX"} or symbol.endswith(".TW") or symbol.endswith(".TWO")

    @staticmethod
    def _non_taiwan_result(*, symbol: str, venue: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "venue": venue,
            "symbol": symbol,
            "enforced": False,
            "order_valid": True,
            "entry_allowed": True,
            "matching_allowed": True,
            "allowed": True,
            "reason": None,
            "blockers": [],
            "warnings": [{"code": "taiwan_exchange_rules_not_applicable"}],
            "tick_size": None,
            "price_limits": {"enforced": False, "exempt": False, "limit_up": None, "limit_down": None},
            "session": {"name": "not_applicable", "entry_allowed": True, "matching_allowed": True, "reason": None},
            "official_rule_sources": list(OFFICIAL_RULE_SOURCES),
            "matching_boundary": "taiwan exchange rules do not apply to this venue",
        }
