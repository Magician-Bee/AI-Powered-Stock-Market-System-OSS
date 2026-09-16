"""Explicit product-receipt shapes for isolated tests, never market proof."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from open_stock_ai.execution.trading_plan import content_hash


def product_fixture(symbol="2330.TW", product_type="ordinary_stock", *, venue=None, segment="ordinary", now=None):
    instant = now or datetime.now(timezone.utc) - timedelta(seconds=1)
    venue = venue or ("TPEx" if symbol.endswith(".TWO") else "TWSE")
    dataset, mode = {"TWSE": ("twse_isin_listed", "2"), "TPEx": ("tpex_isin_otc", "4"),
                     "TPEx-ESB": ("tpex_isin_emerging", "5")}[venue]
    entity = "ENT-" + content_hash({"fixture_symbol": symbol})[:32]
    section, cfi = {
        "ordinary_stock": ({"ordinary": "股票", "innovation": "創新板", "emerging": "興櫃"}.get(segment, "unrecognized_fixture_section"), "ESVUFR"),
        "etf": ("ETF", "CEOGEU"), "etn": ("ETN", "CMVUFR"),
        "depositary_receipt": ("臺灣存託憑證(TDR)", "EDVUFR"),
        "preferred_stock": ("特別股", "EPVUFR"),
        "warrant": ("上櫃認購(售)權證" if venue == "TPEx" else "上市認購(售)權證", "RWCCCC"),
        "other": ("受益證券-不動產投資信託", "CBVUFR"),
    }.get(product_type, ("unrecognized_fixture_section", "XXXXXX"))
    segment = segment if product_type == "ordinary_stock" else "other"
    market_label = {"TWSE": "上市", "TPEx": "上櫃", "TPEx-ESB": "興櫃"}[venue]
    if segment == "innovation" and venue == "TWSE":
        market_label = "上市臺灣創新板"
    source_row = {"cells": [symbol.split(".")[0] + " 離線fixture", "TW0000000000", "2020/01/01",
                             market_label, "fixture", cfi, ""], "section": section}
    return {"entity_id": entity, "lifecycle_status": "active", "product_classification": {
        "schema_version": "stock_ai.product_classification.v1", "status": "verified",
        "product_type": product_type, "market_segment": segment, "symbol": symbol, "venue": venue,
        "isin": "TW0000000000", "cfi_code": cfi, "source_id": "twse_isin", "source_dataset": dataset,
        "source_url": f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}",
        "acquired_at": instant.isoformat(), "source_updated_on": instant.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat(),
        "raw_sha256": content_hash({"fixture_raw": symbol}), "row_sha256": content_hash(source_row),
        "source_row": source_row, "fixture_only_not_market_or_ev_proof": True}}
