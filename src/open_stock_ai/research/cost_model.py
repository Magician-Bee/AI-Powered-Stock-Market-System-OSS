from __future__ import annotations

"""Versioned, source-attributed Taiwan secondary-market cost schedules.

This module deliberately separates the statutory seller tax from the fee a
broker charges its customer.  A regulatory reference rate is useful for
research sensitivity analysis, but it is not evidence of an account's actual
commission schedule; callers can therefore keep the result non-certifiable
until a broker-specific rate is supplied.
"""

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import lru_cache
from math import sqrt
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

from open_stock_ai.research.cost_schedule_receipts import (
    DurableBrokerCostScheduleReceipt,
    ReviewedBrokerCostScheduleReceipt,
)


class UnsupportedCostSchedule(ValueError):
    """Raised when the requested venue/product/date has no verified rule."""


_VENUES = {"TWSE", "TPEX"}
_PRODUCTS = {"stock", "etf", "bond_etf"}
_LOT_TYPES = {"board_lot", "odd_lot"}
_ROOT = Path(__file__).resolve().parents[3]
_RULES_PATH = _ROOT / "config" / "taiwan_market_cost_rules.yaml"
_RULES_SCHEMA = "open_stock_ai.taiwan_market_cost_rules.v1"
_IMPACT_RULES_PATH = _ROOT / "config" / "taiwan_market_impact_rules.yaml"
_IMPACT_RULES_SCHEMA = "open_stock_ai.taiwan_market_impact_rules.v1"
_OFFICIAL_SOURCE_HOSTS = {
    "law-out.mof.gov.tw",
    "twse-regulation.twse.com.tw",
    "www.twse.com.tw",
}
_SHA256_HEX = set("0123456789abcdef")


def _canonical_depth_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a retained PIT L1/L5 snapshot before hashing or consuming it."""

    if not isinstance(snapshot, Mapping):
        raise ValueError("depth_snapshot_invalid")
    snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    captured_at = str(snapshot.get("captured_at") or "").strip()
    levels = snapshot.get("levels")
    if not snapshot_id or not captured_at or not isinstance(levels, list) or not levels:
        raise ValueError("depth_snapshot_metadata_or_levels_missing")
    canonical_levels: list[dict[str, float]] = []
    for index, level in enumerate(levels):
        if not isinstance(level, Mapping):
            raise ValueError(f"depth_snapshot_level_invalid:{index}")
        try:
            price = float(level["price"])
            size = float(level.get("size", level.get("volume")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"depth_snapshot_level_invalid:{index}") from exc
        if price <= 0 or size <= 0:
            raise ValueError(f"depth_snapshot_level_nonpositive:{index}")
        canonical_levels.append({"price": price, "size": size})
    return {
        "snapshot_id": snapshot_id,
        "captured_at": captured_at,
        "levels": canonical_levels,
    }


def hash_depth_snapshot(snapshot: Mapping[str, Any]) -> str:
    """Return the stable hash for the canonical retained depth payload."""

    canonical = _canonical_depth_snapshot(snapshot)
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _depth_execution(
    *,
    snapshot: Mapping[str, Any],
    side: str,
    reference_price: float,
    quantity: int,
) -> tuple[str, str, int, float, float]:
    canonical = _canonical_depth_snapshot(snapshot)
    expected_hash = hash_depth_snapshot(canonical)
    supplied_hash = str(snapshot.get("snapshot_sha256") or "").lower().strip()
    if supplied_hash != expected_hash:
        raise ValueError("depth_snapshot_hash_mismatch")
    levels = sorted(
        canonical["levels"],
        key=lambda item: item["price"],
        reverse=side == "sell",
    )
    remaining = float(quantity)
    notional = 0.0
    for level in levels:
        consumed = min(remaining, level["size"])
        notional += consumed * level["price"]
        remaining -= consumed
        if remaining <= 0:
            break
    if remaining > 0:
        raise ValueError("depth_snapshot_insufficient_quantity")
    vwap = notional / float(quantity)
    impact_bps = abs(vwap - reference_price) / reference_price * 10_000.0
    return (
        str(canonical["snapshot_id"]),
        expected_hash,
        len(levels),
        vwap,
        impact_bps,
    )


@dataclass(frozen=True)
class TaiwanTaxRule:
    rule_id: str
    venues: frozenset[str]
    product_types: frozenset[str]
    lot_types: frozenset[str]
    side: str
    valid_from: date
    valid_to: date | None
    tax_bps: float
    source_ids: tuple[str, ...]
    day_trade_offset: bool | None = None
    bond_etf_tax_exemption_eligible: bool | None = None

    def matches(
        self,
        *,
        venue: str,
        product_type: str,
        lot_type: str,
        side: str,
        trade_date: date,
        is_day_trade_offset: bool,
        bond_etf_tax_exemption_eligible: bool | None,
    ) -> bool:
        if not (
            venue in self.venues
            and product_type in self.product_types
            and lot_type in self.lot_types
            and side == self.side
            and self.valid_from <= trade_date
            and (self.valid_to is None or trade_date <= self.valid_to)
        ):
            return False
        if self.day_trade_offset is not None and self.day_trade_offset is not is_day_trade_offset:
            return False
        return (
            self.bond_etf_tax_exemption_eligible is None
            or self.bond_etf_tax_exemption_eligible is bond_etf_tax_exemption_eligible
        )

    @property
    def specificity(self) -> int:
        return int(self.day_trade_offset is not None) + int(
            self.bond_etf_tax_exemption_eligible is not None
        )


class TaiwanMarketCostRulebook:
    """Validated local snapshot of reviewed official Taiwan cost rules."""

    def __init__(self, path: str | Path = _RULES_PATH) -> None:
        self.path = Path(path).expanduser().resolve()
        raw = self.path.read_bytes()
        payload = yaml.safe_load(raw) or {}
        if payload.get("schema_version") != _RULES_SCHEMA:
            raise ValueError(
                f"invalid Taiwan market cost rule schema:{payload.get('schema_version')}"
            )
        self.schema_version = str(payload["schema_version"])
        self.snapshot_id = str(payload.get("snapshot_id") or "")
        self.snapshot_sha256 = hashlib.sha256(raw).hexdigest()
        if not self.snapshot_id:
            raise ValueError("Taiwan market cost rules require snapshot_id")

        source_items = payload.get("sources") or []
        sources = {str(item.get("source_id")): dict(item) for item in source_items}
        if not sources or len(sources) != len(source_items):
            raise ValueError("Taiwan market cost rule sources must be present and unique")
        for source_id, source in sources.items():
            host = (urlparse(str(source.get("url") or "")).hostname or "").lower()
            if host not in _OFFICIAL_SOURCE_HOSTS:
                raise ValueError(
                    f"Taiwan market cost source is not approved:{source_id}:{host}"
                )
            content_sha256 = str(source.get("content_sha256") or "").lower().strip()
            if len(content_sha256) != 64 or any(char not in _SHA256_HEX for char in content_sha256):
                raise ValueError(
                    f"Taiwan market cost source requires captured content sha256:{source_id}"
                )
            try:
                datetime.fromisoformat(str(source.get("retrieved_at") or ""))
            except ValueError as exc:
                raise ValueError(
                    f"Taiwan market cost source requires retrieved_at:{source_id}"
                ) from exc
        self.sources = sources

        commission = dict(payload.get("commission_reference") or {})
        self.commission_reference_rule_id = str(commission.get("rule_id") or "")
        self.standard_commission_bps = _nonnegative(
            commission.get("reference_bps"), "commission_reference_bps"
        )
        if not self.commission_reference_rule_id or self.standard_commission_bps is None:
            raise ValueError("Taiwan market cost rules require a commission reference")
        self._validate_source_ids(tuple(commission.get("source_ids") or ()))

        rules: list[TaiwanTaxRule] = []
        for item in payload.get("tax_rules") or []:
            source_ids = tuple(str(value) for value in item.get("source_ids") or ())
            self._validate_source_ids(source_ids)
            rule = TaiwanTaxRule(
                rule_id=str(item.get("rule_id") or ""),
                venues=frozenset(str(value).upper() for value in item.get("venues") or ()),
                product_types=frozenset(
                    str(value).lower() for value in item.get("product_types") or ()
                ),
                lot_types=frozenset(
                    str(value).lower() for value in item.get("lot_types") or ()
                ),
                side=str(item.get("side") or "").lower(),
                valid_from=_as_date(item.get("valid_from")),
                valid_to=_as_date(item["valid_to"]) if item.get("valid_to") else None,
                tax_bps=float(_nonnegative(item.get("tax_bps"), "tax_bps") or 0.0),
                source_ids=source_ids,
                day_trade_offset=(
                    bool(item["day_trade_offset"])
                    if "day_trade_offset" in item
                    else None
                ),
                bond_etf_tax_exemption_eligible=(
                    bool(item["bond_etf_tax_exemption_eligible"])
                    if "bond_etf_tax_exemption_eligible" in item
                    else None
                ),
            )
            if (
                not rule.rule_id
                or not rule.venues
                or not rule.product_types
                or not rule.lot_types
                or rule.side not in {"buy", "sell"}
            ):
                raise ValueError(f"invalid Taiwan tax rule:{rule.rule_id or 'missing'}")
            rules.append(rule)
        if not rules or len({item.rule_id for item in rules}) != len(rules):
            raise ValueError("Taiwan tax rules must be present and unique")
        self.tax_rules = tuple(rules)

    def _validate_source_ids(self, source_ids: tuple[str, ...]) -> None:
        missing = sorted(set(source_ids) - set(self.sources))
        if not source_ids or missing:
            raise ValueError(f"Taiwan market cost rule source references invalid:{missing}")

    def tax_rule(
        self,
        *,
        venue: str,
        product_type: str,
        lot_type: str,
        side: str,
        trade_date: date,
        is_day_trade_offset: bool,
        bond_etf_tax_exemption_eligible: bool | None,
    ) -> TaiwanTaxRule:
        if (
            side == "sell"
            and product_type == "bond_etf"
            and bond_etf_tax_exemption_eligible is None
        ):
            raise UnsupportedCostSchedule("bond_etf_tax_exemption_eligibility_missing")
        matches = [
            rule
            for rule in self.tax_rules
            if rule.matches(
                venue=venue,
                product_type=product_type,
                lot_type=lot_type,
                side=side,
                trade_date=trade_date,
                is_day_trade_offset=is_day_trade_offset,
                bond_etf_tax_exemption_eligible=bond_etf_tax_exemption_eligible,
            )
        ]
        if not matches:
            raise UnsupportedCostSchedule(
                "tax_rule_unverified:"
                f"{venue}:{product_type}:{lot_type}:{side}:{trade_date.isoformat()}"
            )
        matches.sort(
            key=lambda item: (item.specificity, item.valid_from, item.rule_id),
            reverse=True,
        )
        winner = matches[0]
        if (
            len(matches) > 1
            and matches[1].specificity == winner.specificity
            and matches[1].valid_from == winner.valid_from
        ):
            raise UnsupportedCostSchedule(
                f"tax_rule_ambiguous:{winner.rule_id}:{matches[1].rule_id}"
            )
        return winner

    def source_urls(self, source_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(str(self.sources[source_id]["url"]) for source_id in source_ids)

    def source_receipts(self, source_ids: tuple[str, ...]) -> tuple[dict[str, str], ...]:
        """Return the immutable official-source capture metadata for a rule.

        The content hash is intentionally recorded separately from the local
        YAML snapshot hash.  A changed official page therefore cannot be
        silently treated as the same reviewed source capture.
        """
        self._validate_source_ids(source_ids)
        return tuple(
            {
                "source_id": source_id,
                "authority": str(self.sources[source_id].get("authority") or ""),
                "url": str(self.sources[source_id]["url"]),
                "document": str(self.sources[source_id].get("document") or ""),
                "checked_at": str(self.sources[source_id].get("checked_at") or ""),
                "retrieved_at": str(self.sources[source_id]["retrieved_at"]),
                "content_sha256": str(self.sources[source_id]["content_sha256"]).lower(),
            }
            for source_id in sorted(set(source_ids))
        )

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "snapshot_sha256": self.snapshot_sha256,
            "commission_reference_rule_id": self.commission_reference_rule_id,
            "source_ids": sorted(self.sources),
            "official_source_receipts": list(self.source_receipts(tuple(sorted(self.sources)))),
            "tax_rule_ids": sorted(item.rule_id for item in self.tax_rules),
        }


@lru_cache(maxsize=4)
def load_taiwan_market_cost_rulebook(
    path: str | Path = _RULES_PATH,
) -> TaiwanMarketCostRulebook:
    return TaiwanMarketCostRulebook(path)


@dataclass(frozen=True)
class TaiwanMarketImpactRule:
    """Effective-dated coefficients for a single venue/product/side scope."""

    rule_id: str
    venues: frozenset[str]
    product_types: frozenset[str]
    sides: frozenset[str]
    valid_from: date
    valid_to: date | None
    base_slippage_bps: float
    impact_coefficient_bps: float
    volatility_coefficient: float
    calibration_status: str
    execution_evidence_eligible: bool

    def matches(
        self,
        *,
        venue: str,
        product_type: str,
        side: str,
        trade_date: date,
    ) -> bool:
        return (
            venue in self.venues
            and product_type in self.product_types
            and side in self.sides
            and self.valid_from <= trade_date
            and (self.valid_to is None or trade_date <= self.valid_to)
        )

    @property
    def specificity(self) -> int:
        return -len(self.venues) - len(self.product_types) - len(self.sides)


class TaiwanMarketImpactRulebook:
    """Immutable impact-parameter snapshot with a verifiable calibration artifact.

    A rule may deliberately be a research baseline.  In that case it remains
    useful for sensitivity analysis but is never accepted as execution proof.
    """

    def __init__(self, path: str | Path = _IMPACT_RULES_PATH) -> None:
        self.path = Path(path).expanduser().resolve()
        raw = self.path.read_bytes()
        payload = yaml.safe_load(raw) or {}
        if payload.get("schema_version") != _IMPACT_RULES_SCHEMA:
            raise ValueError(
                f"invalid Taiwan market impact rule schema:{payload.get('schema_version')}"
            )
        self.schema_version = str(payload["schema_version"])
        self.snapshot_id = str(payload.get("snapshot_id") or "")
        self.snapshot_sha256 = hashlib.sha256(raw).hexdigest()
        if not self.snapshot_id:
            raise ValueError("Taiwan market impact rules require snapshot_id")

        artifact_value = str(payload.get("calibration_artifact") or "").strip()
        expected_artifact_sha = str(payload.get("calibration_artifact_sha256") or "").lower()
        artifact = (_ROOT / artifact_value).resolve()
        try:
            artifact.relative_to(_ROOT)
        except ValueError as exc:
            raise ValueError("impact calibration artifact must be inside project root") from exc
        if not artifact_value or not artifact.is_file() or len(expected_artifact_sha) != 64:
            raise ValueError("market impact rules require a calibration artifact and sha256")
        actual_artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual_artifact_sha != expected_artifact_sha:
            raise ValueError("market impact calibration artifact hash mismatch")
        self.calibration_artifact = artifact_value
        self.calibration_artifact_sha256 = actual_artifact_sha

        rules: list[TaiwanMarketImpactRule] = []
        for item in payload.get("rules") or []:
            status = str(item.get("calibration_status") or "").strip()
            evidence_eligible = item.get("execution_evidence_eligible") is True
            if status not in {"research_baseline", "empirically_calibrated"}:
                raise ValueError(f"invalid market impact calibration status:{status or 'missing'}")
            if evidence_eligible and status != "empirically_calibrated":
                raise ValueError("research baseline impact rule cannot certify execution evidence")
            rule = TaiwanMarketImpactRule(
                rule_id=str(item.get("rule_id") or ""),
                venues=frozenset(str(value).upper() for value in item.get("venues") or ()),
                product_types=frozenset(
                    str(value).lower() for value in item.get("product_types") or ()
                ),
                sides=frozenset(str(value).lower() for value in item.get("sides") or ()),
                valid_from=_as_date(item.get("valid_from")),
                valid_to=_as_date(item["valid_to"]) if item.get("valid_to") else None,
                base_slippage_bps=float(
                    _nonnegative(item.get("base_slippage_bps"), "base_slippage_bps") or 0.0
                ),
                impact_coefficient_bps=float(
                    _nonnegative(item.get("impact_coefficient_bps"), "impact_coefficient_bps") or 0.0
                ),
                volatility_coefficient=float(
                    _nonnegative(item.get("volatility_coefficient"), "volatility_coefficient") or 0.0
                ),
                calibration_status=status,
                execution_evidence_eligible=evidence_eligible,
            )
            if (
                not rule.rule_id
                or not rule.venues <= _VENUES
                or not rule.product_types <= _PRODUCTS
                or not rule.sides <= {"buy", "sell"}
            ):
                raise ValueError(f"invalid Taiwan market impact rule:{rule.rule_id or 'missing'}")
            rules.append(rule)
        if not rules or len({item.rule_id for item in rules}) != len(rules):
            raise ValueError("Taiwan market impact rules must be present and unique")
        self.rules = tuple(rules)

    def rule(
        self,
        *,
        venue: str,
        product_type: str,
        side: str,
        trade_at: date | datetime | str,
    ) -> TaiwanMarketImpactRule:
        normalized_venue = str(venue or "").upper().strip()
        normalized_product = str(product_type or "").lower().strip()
        normalized_side = str(side or "").lower().strip()
        if normalized_venue not in _VENUES:
            raise UnsupportedCostSchedule(f"impact_unsupported_venue:{normalized_venue or 'missing'}")
        if normalized_product not in _PRODUCTS:
            raise UnsupportedCostSchedule(
                f"impact_unsupported_product_type:{normalized_product or 'missing'}"
            )
        if normalized_side not in {"buy", "sell"}:
            raise UnsupportedCostSchedule(f"impact_unsupported_side:{normalized_side or 'missing'}")
        trade_date = _as_date(trade_at)
        matches = [
            item
            for item in self.rules
            if item.matches(
                venue=normalized_venue,
                product_type=normalized_product,
                side=normalized_side,
                trade_date=trade_date,
            )
        ]
        if not matches:
            raise UnsupportedCostSchedule(
                "impact_rule_unverified:"
                f"{normalized_venue}:{normalized_product}:{normalized_side}:{trade_date.isoformat()}"
            )
        matches.sort(
            key=lambda item: (item.specificity, item.valid_from, item.rule_id), reverse=True
        )
        winner = matches[0]
        if (
            len(matches) > 1
            and matches[1].specificity == winner.specificity
            and matches[1].valid_from == winner.valid_from
        ):
            raise UnsupportedCostSchedule(
                f"impact_rule_ambiguous:{winner.rule_id}:{matches[1].rule_id}"
            )
        return winner

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "snapshot_sha256": self.snapshot_sha256,
            "calibration_artifact": self.calibration_artifact,
            "calibration_artifact_sha256": self.calibration_artifact_sha256,
            "rule_ids": sorted(item.rule_id for item in self.rules),
        }


@lru_cache(maxsize=4)
def load_taiwan_market_impact_rulebook(
    path: str | Path = _IMPACT_RULES_PATH,
) -> TaiwanMarketImpactRulebook:
    return TaiwanMarketImpactRulebook(path)


@dataclass(frozen=True)
class MarketImpactQuote:
    """A fill-price impact receipt built from actual liquidity inputs.

    It intentionally has no synthetic fallback for bid/ask spread, trailing
    ADV or realized volatility.  Those inputs determine whether a replay can
    support execution evidence; replacing an absent liquidity observation with
    zero would make thin names look cheaper than they are.
    """

    side: str
    reference_price: float
    quantity: int
    bid_ask_spread_bps: float
    realized_volatility_bps: float
    adv_volume_shares: float
    bar_volume_shares: float
    base_slippage_bps: float
    impact_coefficient_bps: float
    volatility_coefficient: float
    impact_rule_id: str | None = None
    impact_schedule_version: str | None = None
    impact_schedule_sha256: str | None = None
    calibration_status: str = "unscheduled"
    calibration_artifact_sha256: str | None = None
    calibration_execution_evidence_eligible: bool = False
    depth_data_status: str = "unavailable"
    depth_snapshot_id: str | None = None
    depth_snapshot_sha256: str | None = None
    depth_levels: int = 0
    depth_vwap_price: float | None = None
    depth_impact_bps: float | None = None

    @property
    def adv_participation_rate(self) -> float:
        return self.quantity / self.adv_volume_shares

    @property
    def bar_participation_rate(self) -> float:
        return self.quantity / self.bar_volume_shares

    @property
    def volatility_cost_bps(self) -> float:
        return self.realized_volatility_bps * self.volatility_coefficient

    @property
    def impact_bps(self) -> float:
        # Square-root impact is conservative for large orders while preserving
        # monotonicity for order size and inverse liquidity.  The larger of
        # PIT ADV participation and actual-bar participation prevents a high
        # ADV name from masking an oversized order in a thin execution bar.
        liquidity_pressure = max(self.adv_participation_rate, self.bar_participation_rate)
        volatility_multiplier = 1.0 + self.realized_volatility_bps / 1_000.0
        return self.impact_coefficient_bps * sqrt(liquidity_pressure) * volatility_multiplier

    @property
    def total_slippage_bps(self) -> float:
        return (
            self.base_slippage_bps
            + self.bid_ask_spread_bps / 2.0
            + self.volatility_cost_bps
            + self.impact_bps
        )

    @property
    def execution_evidence_eligible(self) -> bool:
        return self.calibration_execution_evidence_eligible and all(
            value > 0
            for value in (
                self.reference_price,
                self.quantity,
                self.adv_volume_shares,
                self.bar_volume_shares,
            )
        )

    @property
    def depth_simulation_eligible(self) -> bool:
        """Only a retained PIT book may support an order-book simulation."""

        return (
            self.depth_data_status == "available"
            and bool(self.depth_snapshot_id)
            and isinstance(self.depth_snapshot_sha256, str)
            and len(self.depth_snapshot_sha256) == 64
            and self.depth_levels >= 1
            and self.depth_vwap_price is not None
            and self.depth_vwap_price > 0
        )

    def fill_price(self) -> float:
        if self.depth_simulation_eligible:
            return float(self.depth_vwap_price)
        multiplier = 1.0 + self.total_slippage_bps / 10_000.0
        return self.reference_price * multiplier if self.side == "buy" else self.reference_price / multiplier

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.market_impact_quote.v1",
            "side": self.side,
            "reference_price": self.reference_price,
            "quantity": self.quantity,
            "bid_ask_spread_bps": self.bid_ask_spread_bps,
            "realized_volatility_bps": self.realized_volatility_bps,
            "adv_volume_shares": self.adv_volume_shares,
            "bar_volume_shares": self.bar_volume_shares,
            "adv_participation_rate": self.adv_participation_rate,
            "bar_participation_rate": self.bar_participation_rate,
            "base_slippage_bps": self.base_slippage_bps,
            "volatility_cost_bps": self.volatility_cost_bps,
            "impact_bps": self.impact_bps,
            "total_slippage_bps": self.total_slippage_bps,
            "impact_rule_id": self.impact_rule_id,
            "impact_schedule_version": self.impact_schedule_version,
            "impact_schedule_sha256": self.impact_schedule_sha256,
            "calibration_status": self.calibration_status,
            "calibration_artifact_sha256": self.calibration_artifact_sha256,
            "calibration_execution_evidence_eligible": self.calibration_execution_evidence_eligible,
            "simulation_mode": (
                "order_book_depth" if self.depth_simulation_eligible else "spread_adv_proxy"
            ),
            "depth_data_status": self.depth_data_status,
            "depth_snapshot_id": self.depth_snapshot_id,
            "depth_snapshot_sha256": self.depth_snapshot_sha256,
            "depth_levels": self.depth_levels,
            "depth_vwap_price": self.depth_vwap_price,
            "depth_impact_bps": self.depth_impact_bps,
            "depth_simulation_eligible": self.depth_simulation_eligible,
            "execution_evidence_eligible": self.execution_evidence_eligible,
        }


@dataclass(frozen=True)
class PointInTimeMarketImpactModel:
    """Quote conservative simulated fills using known-at-decision liquidity."""

    impact_coefficient_bps: float = 15.0
    volatility_coefficient: float = 0.05
    base_slippage_bps: float = 0.0

    def quote(
        self,
        *,
        side: str,
        reference_price: float,
        quantity: int,
        bid_ask_spread_bps: float | None,
        realized_volatility_bps: float | None,
        adv_volume_shares: float | None,
        bar_volume_shares: float | None,
        depth_snapshot: Mapping[str, Any] | None = None,
    ) -> MarketImpactQuote:
        normalized_side = str(side or "").lower().strip()
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("market_impact_side_must_be_buy_or_sell")
        inputs = {
            "reference_price": reference_price,
            "quantity": quantity,
            "bid_ask_spread_bps": bid_ask_spread_bps,
            "realized_volatility_bps": realized_volatility_bps,
            "adv_volume_shares": adv_volume_shares,
            "bar_volume_shares": bar_volume_shares,
        }
        for name, value in inputs.items():
            if value is None or float(value) < 0 or (name not in {"bid_ask_spread_bps", "realized_volatility_bps"} and float(value) == 0):
                raise ValueError(f"market_impact_input_invalid:{name}")
        depth_values: tuple[str | None, str | None, int, float | None, float | None]
        if depth_snapshot is None:
            depth_values = (None, None, 0, None, None)
        else:
            depth_values = _depth_execution(
                snapshot=depth_snapshot,
                side=normalized_side,
                reference_price=float(reference_price),
                quantity=int(quantity),
            )
        return MarketImpactQuote(
            side=normalized_side,
            reference_price=float(reference_price),
            quantity=int(quantity),
            bid_ask_spread_bps=float(bid_ask_spread_bps),
            realized_volatility_bps=float(realized_volatility_bps),
            adv_volume_shares=float(adv_volume_shares),
            bar_volume_shares=float(bar_volume_shares),
            base_slippage_bps=max(0.0, float(self.base_slippage_bps)),
            impact_coefficient_bps=max(0.0, float(self.impact_coefficient_bps)),
            volatility_coefficient=max(0.0, float(self.volatility_coefficient)),
            depth_data_status="available" if depth_snapshot is not None else "unavailable",
            depth_snapshot_id=depth_values[0],
            depth_snapshot_sha256=depth_values[1],
            depth_levels=depth_values[2],
            depth_vwap_price=depth_values[3],
            depth_impact_bps=depth_values[4],
        )


class TaiwanSecondaryMarketImpactSchedule:
    """Select a versioned market-impact rule instead of a global coefficient."""

    def __init__(self, rulebook: TaiwanMarketImpactRulebook | None = None) -> None:
        self.rulebook = rulebook or load_taiwan_market_impact_rulebook()

    def rule(
        self,
        *,
        venue: str,
        product_type: str,
        side: str,
        trade_at: date | datetime | str,
    ) -> TaiwanMarketImpactRule:
        return self.rulebook.rule(
            venue=venue,
            product_type=product_type,
            side=side,
            trade_at=trade_at,
        )

    def quote(
        self,
        *,
        venue: str,
        product_type: str,
        side: str,
        trade_at: date | datetime | str,
        reference_price: float,
        quantity: int,
        bid_ask_spread_bps: float | None,
        realized_volatility_bps: float | None,
        adv_volume_shares: float | None,
        bar_volume_shares: float | None,
        depth_snapshot: Mapping[str, Any] | None = None,
    ) -> MarketImpactQuote:
        rule = self.rule(
            venue=venue,
            product_type=product_type,
            side=side,
            trade_at=trade_at,
        )
        quote = PointInTimeMarketImpactModel(
            impact_coefficient_bps=rule.impact_coefficient_bps,
            volatility_coefficient=rule.volatility_coefficient,
            base_slippage_bps=rule.base_slippage_bps,
        ).quote(
            side=side,
            reference_price=reference_price,
            quantity=quantity,
            bid_ask_spread_bps=bid_ask_spread_bps,
            realized_volatility_bps=realized_volatility_bps,
            adv_volume_shares=adv_volume_shares,
            bar_volume_shares=bar_volume_shares,
            depth_snapshot=depth_snapshot,
        )
        return replace(
            quote,
            impact_rule_id=rule.rule_id,
            impact_schedule_version=self.rulebook.snapshot_id,
            impact_schedule_sha256=self.rulebook.snapshot_sha256,
            calibration_status=rule.calibration_status,
            calibration_artifact_sha256=self.rulebook.calibration_artifact_sha256,
            calibration_execution_evidence_eligible=rule.execution_evidence_eligible,
        )


@dataclass(frozen=True)
class TaiwanCostQuote:
    venue: str
    product_type: str
    lot_type: str
    side: str
    trade_date: date
    commission_bps: float
    commission_minimum_twd: float
    exchange_fee_bps: float
    sell_tax_bps: float
    broker_schedule_verified: bool
    exchange_schedule_verified: bool
    tax_schedule_verified: bool
    broker_fee_schedule_id: str | None
    exchange_fee_schedule_id: str | None
    broker_schedule_receipt_id: str | None
    broker_schedule_receipt_sha256: str | None
    broker_schedule_persistence_sha256: str | None
    tax_rule_id: str
    rule_snapshot_id: str
    rule_snapshot_sha256: str
    rule_source_ids: tuple[str, ...]
    sources: tuple[str, ...]
    reviewed_account_schedule: bool = False
    review_receipt_sha256: str | None = None
    review_document_receipt_ids: tuple[str, ...] = ()

    @property
    def execution_evidence_eligible(self) -> bool:
        return (
            # A durable schedule alone only proves that this installation has
            # retained some caller-provided numbers.  It is not account-owner
            # evidence until the aggregate binds both reviewed broker and
            # exchange documents.  Keep the legacy durable receipt useful for
            # sensitivity analysis, but never let it promote a replay.
            self.reviewed_account_schedule
            and isinstance(self.review_receipt_sha256, str)
            and len(self.review_receipt_sha256) == 64
            and len(self.review_document_receipt_ids) == 2
            and self.broker_schedule_verified
            and self.exchange_schedule_verified
            and self.tax_schedule_verified
        )

    def amounts(self, notional: float) -> dict[str, float]:
        gross = max(0.0, float(notional))
        commission = _money(max(
            self.commission_minimum_twd if gross else 0.0,
            gross * self.commission_bps / 10_000.0,
        ))
        exchange_fee = _money(gross * self.exchange_fee_bps / 10_000.0)
        tax = _money(gross * self.sell_tax_bps / 10_000.0) if self.side == "sell" else 0.0
        return {
            "commission": commission,
            "exchange_fee": exchange_fee,
            "sell_tax": tax,
            "fees": _money(commission + exchange_fee + tax),
        }

    def receipt(self, notional: float) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.taiwan_cost_quote.v1",
            "schedule_version": self.rule_snapshot_id,
            "schedule_sha256": self.rule_snapshot_sha256,
            "venue": self.venue,
            "product_type": self.product_type,
            "lot_type": self.lot_type,
            "side": self.side,
            "trade_date": self.trade_date.isoformat(),
            "commission_bps": self.commission_bps,
            "commission_minimum_twd": self.commission_minimum_twd,
            "exchange_fee_bps": self.exchange_fee_bps,
            "sell_tax_bps": self.sell_tax_bps,
            "broker_schedule_verified": self.broker_schedule_verified,
            "exchange_schedule_verified": self.exchange_schedule_verified,
            "tax_schedule_verified": self.tax_schedule_verified,
            "broker_fee_schedule_id": self.broker_fee_schedule_id,
            "exchange_fee_schedule_id": self.exchange_fee_schedule_id,
            "broker_schedule_receipt_id": self.broker_schedule_receipt_id,
            "broker_schedule_receipt_sha256": self.broker_schedule_receipt_sha256,
            "broker_schedule_persistence_sha256": self.broker_schedule_persistence_sha256,
            "tax_rule_id": self.tax_rule_id,
            "rule_source_ids": list(self.rule_source_ids),
            "execution_evidence_eligible": self.execution_evidence_eligible,
            "sources": list(self.sources),
            "reviewed_account_schedule": self.reviewed_account_schedule,
            "review_receipt_sha256": self.review_receipt_sha256,
            "review_document_receipt_ids": list(self.review_document_receipt_ids),
            "amounts": self.amounts(notional),
        }


class TaiwanSecondaryMarketCostSchedule:
    """Officially sourced tax rules plus an explicit broker-fee boundary."""

    def __init__(self, rulebook: TaiwanMarketCostRulebook | None = None) -> None:
        self.rulebook = rulebook or load_taiwan_market_cost_rulebook()

    @property
    def standard_commission_bps(self) -> float:
        return float(self.rulebook.standard_commission_bps)

    def quote(
        self,
        *,
        venue: str,
        product_type: str,
        lot_type: str,
        side: str,
        trade_at: date | datetime | str,
        is_day_trade_offset: bool = False,
        broker_id: str | None = None,
        account_alias: str | None = None,
        broker_commission_bps: float | None = None,
        broker_minimum_commission_twd: float | None = None,
        broker_fee_schedule_id: str | None = None,
        exchange_fee_bps: float | None = None,
        exchange_fee_schedule_id: str | None = None,
        broker_schedule_receipt: DurableBrokerCostScheduleReceipt | None = None,
        reviewed_broker_schedule_receipt: ReviewedBrokerCostScheduleReceipt | None = None,
        bond_etf_tax_exemption_eligible: bool | None = None,
    ) -> TaiwanCostQuote:
        normalized_venue = str(venue or "").upper().strip()
        normalized_product = str(product_type or "").lower().strip()
        normalized_lot_type = str(lot_type or "").lower().strip()
        normalized_side = str(side or "").lower().strip()
        trade_date = _as_date(trade_at)
        if normalized_venue not in _VENUES:
            raise UnsupportedCostSchedule(f"unsupported_venue:{normalized_venue or 'missing'}")
        if normalized_product not in _PRODUCTS:
            raise UnsupportedCostSchedule(f"unsupported_product_type:{normalized_product or 'missing'}")
        if normalized_lot_type not in _LOT_TYPES:
            raise UnsupportedCostSchedule(f"unsupported_lot_type:{normalized_lot_type or 'missing'}")
        if normalized_side not in {"buy", "sell"}:
            raise UnsupportedCostSchedule(f"unsupported_side:{normalized_side or 'missing'}")

        tax_rule = self.rulebook.tax_rule(
            venue=normalized_venue,
            product_type=normalized_product,
            lot_type=normalized_lot_type,
            side=normalized_side,
            trade_date=trade_date,
            is_day_trade_offset=is_day_trade_offset,
            bond_etf_tax_exemption_eligible=bond_etf_tax_exemption_eligible,
        )
        supplied_commission = _nonnegative(broker_commission_bps, "broker_commission_bps")
        supplied_minimum = _nonnegative(broker_minimum_commission_twd, "broker_minimum_commission_twd")
        supplied_exchange_fee = _nonnegative(exchange_fee_bps, "exchange_fee_bps")
        normalized_broker_schedule_id = str(broker_fee_schedule_id or "").strip() or None
        normalized_exchange_schedule_id = str(exchange_fee_schedule_id or "").strip() or None
        receipt_id: str | None = None
        receipt_sha256: str | None = None
        persistence_sha256: str | None = None
        broker_verified = False
        exchange_verified = False
        reviewed_account_schedule = False
        review_receipt_sha256: str | None = None
        review_document_receipt_ids: tuple[str, ...] = ()
        if reviewed_broker_schedule_receipt is not None:
            if not isinstance(reviewed_broker_schedule_receipt, ReviewedBrokerCostScheduleReceipt):
                raise UnsupportedCostSchedule("reviewed_broker_cost_schedule_receipt_invalid")
            reviewed_broker_schedule_receipt.verify()
            reviewed_schedule = reviewed_broker_schedule_receipt.schedule
            if broker_schedule_receipt is not None and broker_schedule_receipt != reviewed_schedule:
                raise UnsupportedCostSchedule("reviewed_broker_cost_schedule_receipt_mismatch")
            broker_schedule_receipt = reviewed_schedule
            reviewed_account_schedule = True
            review_receipt_sha256 = reviewed_broker_schedule_receipt.review_receipt_sha256
            review_document_receipt_ids = tuple(
                item.receipt.receipt_id for item in reviewed_broker_schedule_receipt.documents
            )
        if broker_schedule_receipt is not None:
            if not isinstance(broker_schedule_receipt, DurableBrokerCostScheduleReceipt):
                raise UnsupportedCostSchedule("broker_cost_schedule_receipt_not_durable")
            broker_schedule_receipt.verify()
            account_receipt = broker_schedule_receipt.receipt
            normalized_broker_id = str(broker_id or "").strip()
            normalized_account_alias = str(account_alias or "").strip()
            if not normalized_broker_id or not normalized_account_alias:
                raise UnsupportedCostSchedule("broker_cost_schedule_receipt_account_scope_missing")
            if not account_receipt.matches(
                broker_id=normalized_broker_id,
                account_alias=normalized_account_alias,
                venue=normalized_venue,
                product_type=normalized_product,
                lot_type=normalized_lot_type,
                side=normalized_side,
                trade_date=trade_date,
            ):
                raise UnsupportedCostSchedule("broker_cost_schedule_receipt_scope_unverified")
            receipt_id = account_receipt.receipt_id
            receipt_sha256 = account_receipt.receipt_sha256
            persistence_sha256 = broker_schedule_receipt.persistence_sha256
            _require_equal_or_missing(
                supplied_commission,
                account_receipt.broker_commission_bps,
                "broker_commission_bps",
            )
            _require_equal_or_missing(
                supplied_minimum,
                account_receipt.broker_minimum_commission_twd,
                "broker_minimum_commission_twd",
            )
            _require_equal_or_missing(
                supplied_exchange_fee,
                account_receipt.exchange_fee_bps,
                "exchange_fee_bps",
            )
            _require_text_equal_or_missing(
                normalized_broker_schedule_id,
                account_receipt.broker_fee_schedule_id,
                "broker_fee_schedule_id",
            )
            _require_text_equal_or_missing(
                normalized_exchange_schedule_id,
                account_receipt.exchange_fee_schedule_id,
                "exchange_fee_schedule_id",
            )
            supplied_commission = account_receipt.broker_commission_bps
            supplied_minimum = account_receipt.broker_minimum_commission_twd
            supplied_exchange_fee = account_receipt.exchange_fee_bps
            normalized_broker_schedule_id = account_receipt.broker_fee_schedule_id
            normalized_exchange_schedule_id = account_receipt.exchange_fee_schedule_id
            broker_verified = True
            exchange_verified = True
        # Raw numbers can still drive a transparent sensitivity replay, but
        # they are never account evidence merely because they carry an ID.
        commission_bps = supplied_commission if supplied_commission is not None else self.standard_commission_bps
        minimum = supplied_minimum if supplied_minimum is not None else 0.0
        return TaiwanCostQuote(
            venue=normalized_venue,
            product_type=normalized_product,
            lot_type=normalized_lot_type,
            side=normalized_side,
            trade_date=trade_date,
            commission_bps=float(commission_bps),
            commission_minimum_twd=float(minimum),
            # Exchange fees are not separately charged to a customer under the
            # generic broker commission contract.  A broker-specific pass-through
            # fee must be supplied explicitly instead of being invented here.
            exchange_fee_bps=float(supplied_exchange_fee or 0.0),
            sell_tax_bps=float(tax_rule.tax_bps),
            broker_schedule_verified=broker_verified,
            exchange_schedule_verified=exchange_verified,
            tax_schedule_verified=True,
            broker_fee_schedule_id=normalized_broker_schedule_id,
            exchange_fee_schedule_id=normalized_exchange_schedule_id,
            broker_schedule_receipt_id=receipt_id,
            broker_schedule_receipt_sha256=receipt_sha256,
            broker_schedule_persistence_sha256=persistence_sha256,
            tax_rule_id=tax_rule.rule_id,
            rule_snapshot_id=self.rulebook.snapshot_id,
            rule_snapshot_sha256=self.rulebook.snapshot_sha256,
            rule_source_ids=tax_rule.source_ids,
            sources=self.rulebook.source_urls(tax_rule.source_ids),
            reviewed_account_schedule=reviewed_account_schedule,
            review_receipt_sha256=review_receipt_sha256,
            review_document_receipt_ids=review_document_receipt_ids,
        )


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(ZoneInfo("Asia/Taipei")).date()


def _nonnegative(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    number = float(value)
    if number < 0:
        raise UnsupportedCostSchedule(f"invalid_{name}")
    return number


def _money(value: float) -> float:
    return round(float(value), 2)


def _require_equal_or_missing(actual: float | None, expected: float, field: str) -> None:
    if actual is not None and float(actual) != float(expected):
        raise UnsupportedCostSchedule(f"broker_cost_schedule_receipt_mismatch:{field}")


def _require_text_equal_or_missing(actual: str | None, expected: str, field: str) -> None:
    if actual is not None and actual != expected:
        raise UnsupportedCostSchedule(f"broker_cost_schedule_receipt_mismatch:{field}")
