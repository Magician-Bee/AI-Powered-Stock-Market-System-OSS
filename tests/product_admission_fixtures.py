"""Explicit offline Host fixtures, never official product or market evidence."""
from uuid import NAMESPACE_URL, uuid5

from open_stock_ai.execution.product_admission import assess_new_entry_product
from open_stock_ai.execution.trading_plan import content_hash
from product_classification_fixtures import product_fixture


def product_snapshot(symbol, *, now, product_type=None):
    product_type = product_type or ("etf" if symbol == "0050.TW" else "ordinary_stock")
    feature = product_fixture(symbol, product_type, now=now)
    return {"entity_id": "ENT-" + uuid5(NAMESPACE_URL, "offline-product:" + symbol).hex,
            "lifecycle_status": "active", "classification": feature["product_classification"]}


def product_resolver(*, symbol, market, now):
    return product_snapshot(symbol, now=now)


def product_feature(symbol, *, now, product_type=None):
    snapshot = product_snapshot(symbol, now=now, product_type=product_type)
    return {"symbol": symbol, "exchange": snapshot["classification"]["venue"],
            "entity_id": snapshot["entity_id"], "lifecycle_status": snapshot["lifecycle_status"],
            "product_classification": snapshot["classification"]}


def admitted_product(symbol, *, now):
    return assess_new_entry_product(symbol=symbol, market="TW", now=now,
                                    product_snapshot=product_snapshot(symbol, now=now))
