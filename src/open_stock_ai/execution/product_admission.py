"""Host product identity admission, separate from research and position reduction.

The resolver owns provenance and exact current identifier resolution. Hashes
here detect corruption; a caller-supplied dictionary is not source authority.
"""
from __future__ import annotations

from datetime import date, datetime
import inspect
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from .trading_plan import content_hash, utc_time


PRODUCT_ADMISSION_POLICY = "ordinary_stock_only_7_calendar_days_v1"
_SOURCES = {
    "TWSE": ("twse_isin_listed", "2", ".TW"),
    "TPEx": ("tpex_isin_otc", "4", ".TWO"),
    "TPEx-ESB": ("tpex_isin_emerging", "5", ".TWO"),
}
_PRODUCTS = {"ordinary_stock", "etf", "etn", "depositary_receipt", "preferred_stock",
             "warrant", "bond", "other", "unknown"}
from open_stock_ai.product_identity import OFFICIAL_PRODUCT_SECTIONS


def _source_row_reasons(receipt: dict) -> list[str]:
    row = receipt.get("source_row")
    if not isinstance(row, dict):
        return ["product_classification_row_missing"]
    cells, section = row.get("cells"), row.get("section")
    if (not isinstance(cells, list) or len(cells) != 7 or not all(isinstance(cell, str) for cell in cells)
            or not isinstance(section, str)):
        return ["product_classification_row_shape_invalid"]
    reasons = []
    parts = cells[0].split(maxsplit=1)
    code = str(receipt.get("symbol") or "").split(".")[0]
    venue = receipt.get("venue") if isinstance(receipt.get("venue"), str) else None
    market = {"TWSE": "上市", "TPEx": "上櫃", "TPEx-ESB": "興櫃"}.get(venue)
    if section == "創新板" and venue == "TWSE":
        market = "上市臺灣創新板"
    if (len(parts) != 2 or parts[0] != code or cells[1] != receipt.get("isin")
            or cells[5] != receipt.get("cfi_code") or cells[3] != market):
        reasons.append("product_classification_row_identity_mismatch")
    expected = OFFICIAL_PRODUCT_SECTIONS.get(section)
    if (not expected or receipt.get("product_type") != expected[0]
            or not cells[5].startswith(expected[1]) or receipt.get("market_segment") != expected[2]):
        reasons.append("product_classification_row_type_mismatch")
    return reasons


def resolve_product_snapshot(resolver, *, symbol: str, market: str, now: datetime) -> dict[str, Any]:
    """Read the injected local resolver; outages deny entry, cancellation propagates."""
    if resolver is None:
        return {"classification": {"status": "unknown", "reasons": ["product_resolver_unavailable"]}}
    try:
        result = resolver(symbol=symbol, market=market, now=now)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ValueError("product_resolver_must_be_local_synchronous")
        if not isinstance(result, dict):
            raise ValueError("product_resolver_snapshot_required")
        return result
    except Exception:
        return {"classification": {"status": "unknown", "reasons": ["product_resolver_unavailable"]}}


def assess_new_entry_product(*, symbol: str, market: str, product_snapshot: dict | None,
                             now: datetime, expected_entity_id: str | None = None) -> dict[str, Any]:
    """Assess an already Host-resolved receipt without fetching or mutating it."""
    reasons: list[str] = []
    try:
        snapshot = json.loads(json.dumps(product_snapshot or {}, ensure_ascii=False, allow_nan=False))
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot")
    except (TypeError, ValueError):
        snapshot = {}
        reasons.append("product_classification_invalid_json")
    receipt = snapshot.get("classification")
    receipt = receipt if isinstance(receipt, dict) else {}
    entity = snapshot.get("entity_id")
    venue = receipt.get("venue") if isinstance(receipt.get("venue"), str) else None
    product = receipt.get("product_type") if isinstance(receipt.get("product_type"), str) else None
    segment = receipt.get("market_segment") if isinstance(receipt.get("market_segment"), str) else None
    if not isinstance(entity, str) or not re.fullmatch(r"ENT-[0-9a-f]{32}", entity):
        reasons.append("product_entity_identity_unverified")
    if expected_entity_id is not None and (not expected_entity_id or entity != expected_entity_id):
        reasons.append("product_entity_identity_mismatch")
    if (receipt.get("schema_version") != "stock_ai.product_classification.v1"
            or receipt.get("status") != "verified" or product not in _PRODUCTS
            or segment not in {"ordinary", "innovation", "emerging", "other"}):
        reasons.append("product_classification_unverified")
    source = _SOURCES.get(venue)
    if (not source or receipt.get("symbol") != symbol
            or not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{4}[A-Z0-9]{0,2}\.TW(?:O)?", symbol)
            or source and not symbol.endswith(source[2])):
        reasons.append("product_symbol_venue_mismatch")
    accepted_market = {"TW", "TAIWAN", str(venue).upper()}
    if str(market).upper() not in accepted_market:
        reasons.append("product_market_mismatch")
    if (not source or receipt.get("source_id") != "twse_isin"
            or receipt.get("source_dataset") != source[0]
            or receipt.get("source_url") != f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={source[1]}"):
        reasons.append("product_classification_source_mismatch")
    if any(not isinstance(receipt.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[key])
           for key in ("raw_sha256", "row_sha256")):
        reasons.append("product_classification_hash_missing")
    reasons.extend(_source_row_reasons(receipt))
    if "source_row" in receipt and receipt.get("row_sha256") != content_hash(receipt["source_row"]):
        reasons.append("product_classification_row_hash_mismatch")
    if (not isinstance(receipt.get("isin"), str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", receipt["isin"])
            or not isinstance(receipt.get("cfi_code"), str) or not re.fullmatch(r"[A-Z]{6}", receipt["cfi_code"])):
        reasons.append("product_classification_identifiers_missing")
    try:
        instant, acquired = utc_time(now), utc_time(receipt.get("acquired_at"))
        local_day = instant.astimezone(ZoneInfo("Asia/Taipei")).date()
        if not isinstance(receipt.get("source_updated_on"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", receipt["source_updated_on"]):
            raise ValueError("source date format")
        source_day = date.fromisoformat(receipt["source_updated_on"])
        acquisition_day = acquired.astimezone(ZoneInfo("Asia/Taipei")).date()
        acquired_age = (local_day - acquisition_day).days
        if source_day > acquisition_day:
            reasons.append("product_classification_source_after_acquisition")
        if acquired > instant or source_day > local_day:
            reasons.append("product_classification_from_future")
        if acquired_age > 7 or (local_day - source_day).days > 7:
            reasons.append("product_classification_stale")
    except (TypeError, ValueError, KeyError):
        reasons.append("product_classification_time_invalid")
    verified = not reasons
    if receipt.get("product_type") != "ordinary_stock":
        reasons.append("product_type_not_supported")
    if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{4,6}\.TW(?:O)?", symbol):
        reasons.append("product_symbol_not_supported")
    if venue not in {"TWSE", "TPEx"}:
        reasons.append("product_venue_not_supported")
    if receipt.get("market_segment") != "ordinary":
        reasons.append("product_segment_not_supported")
    if snapshot.get("lifecycle_status") != "active":
        reasons.append("product_lifecycle_not_active")
    result = {"policy_version": PRODUCT_ADMISSION_POLICY, "allowed": not reasons,
              "classification_verified": verified, "reasons": list(dict.fromkeys(reasons)),
              "entity_id": entity, "lifecycle_status": snapshot.get("lifecycle_status"),
              "classification": receipt}
    result["receipt_sha256"] = content_hash(result)
    return result
