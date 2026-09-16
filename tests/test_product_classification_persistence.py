from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json

import pytest

from stock_ai.data_platform.contracts import EntityRecord
from stock_ai.data_platform.service import MarketDataPlatform, stable_entity_id
from stock_ai.data_platform.product_catalog import parse_product_catalogue
from stock_ai.data_platform.warehouse import content_hash


NOW = datetime(2026, 9, 12, 4, tzinfo=timezone.utc)
ACQUIRED = NOW.isoformat()
RAW_FIXTURES = {}


@pytest.fixture
def platform(tmp_path):
    return MarketDataPlatform(database_path=tmp_path / "classification.sqlite")


def add_listing(platform, symbol="1234.TW", venue="TWSE", *, seed=None, status="active", metadata=None):
    entity_id = stable_entity_id(market="taiwan", exchange=venue, source_code=seed or symbol)
    entity = EntityRecord(
        entity_id=entity_id, entity_type="stock", canonical_name="同一發行人",
        market="taiwan", exchange=venue, lifecycle_status=status,
        metadata={"display_symbol": symbol, "custom": {"retain": True}, **(metadata or {})},
    )
    platform.warehouse.upsert_entity(entity, identifiers=[{
        "source_id": "tpex_openapi" if seed or venue == "TPEx" else "twse_openapi",
        "identifier_type": "display_symbol", "identifier_value": symbol,
        "valid_from": "2026-01-01T00:00:00+00:00", "metadata": {"venue": venue},
    }])
    return entity


def receipt(symbol="1234.TW", venue="TWSE", product_type="ordinary_stock", **changes):
    dataset = {"TWSE": "twse_isin_listed", "TPEx": "tpex_isin_otc", "TPEx-ESB": "tpex_isin_emerging"}[venue]
    section, cfi = {
        "ordinary_stock": ("股票", "ESVUFR"), "preferred_stock": ("特別股", "EPNCAR"),
        "etf": ("ETF", "CEOJEU"),
    }[product_type]
    if venue == "TPEx-ESB" and product_type == "ordinary_stock":
        section = "興櫃"
    acquired = changes.get("acquired_at", ACQUIRED)
    source_day = acquired[:10].replace("-", "/")
    market = {"TWSE": "上市", "TPEx": "上櫃", "TPEx-ESB": "興櫃"}[venue]
    header = ["有價證券代號及名稱", "國際證券辨識號碼(ISIN Code)", "上市日", "市場別", "產業別", "CFICode", "備註"]
    def row(cells):
        return "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in cells) + "</tr>"
    text = f"<html><body><h2>最近更新日期:{source_day}</h2><table>" + row(header)
    if product_type != "ordinary_stock":
        text += row(["股票"]) + row(["9999　離線普通股", "TW0009999000", "2026/01/01", market, "", "ESVUFR", ""])
    text += row([section]) + row([symbol.split(".")[0] + "　離線fixture", "TW0001234000", "2026/01/01", market, "fixture", cfi, ""])
    raw = (text + "</table></body></html>").encode("cp950")
    parsed = parse_product_catalogue(raw, dataset_id=dataset, acquired_at=acquired)
    value = next(item for item in parsed["receipts"] if item["symbol"] == symbol)
    payload = {"dataset_id": dataset, "wire_sha256": sha256(raw).hexdigest(), "acquired_at": acquired}
    raw_id = "RAW-" + sha256(f"twse_isin:{content_hash(payload)}".encode()).hexdigest()[:32]
    value["raw_payload_id"] = raw_id
    RAW_FIXTURES[raw_id] = (raw, value["source_url"], payload)
    return {**value, **changes}


def sync(platform, receipts, **kwargs):
    for value in receipts:
        fixture = RAW_FIXTURES.get(value.get("raw_payload_id"))
        if fixture is None:
            continue
        raw, url, payload = fixture
        raw_id = platform.warehouse.record_raw_payload(
            source_id="twse_isin", payload=payload, raw_body=raw, request_url=url,
            requested_at=payload["acquired_at"], received_at=payload["acquired_at"],
            content_type="text/html", content_encoding="cp950", parser_id="stock_ai.product_classification.v1",
            metadata={"dataset": "security_master", "source_dataset": payload["dataset_id"],
                      "effective_url": url, "capture_truncated": False},
        )
        assert raw_id == value["raw_payload_id"]
    return platform.sync_product_classifications(receipts, **kwargs)


def resolve(platform, symbol="1234.TW", market="TWSE"):
    return platform.resolve_product_classification(symbol=symbol, market=market, now=NOW)


def metadata(platform, entity_id):
    return platform.warehouse.entity_profile(entity_id)["metadata"]


def immutable_rows(platform):
    with platform.warehouse._connect() as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"select * from {table}")]
            for table in ("entity_identifiers", "entity_identity_merges", "entity_lifecycle_events", "data_revisions")
        }


def test_metadata_overlay_keeps_identity_lifecycle_and_existing_evidence(platform):
    entity = add_listing(platform)
    before = platform.warehouse.entity_profile(entity.entity_id)
    evidence = immutable_rows(platform)
    value = receipt(product_type="preferred_stock")
    result = sync(platform, [value], acquired_at=ACQUIRED)
    after = platform.warehouse.entity_profile(entity.entity_id)
    assert result["updated_count"] == 1
    assert after["metadata"]["product_classifications"] == {"TWSE:1234.TW": value}
    assert after["metadata"]["custom"] == {"retain": True}
    assert {key: val for key, val in before.items() if key != "metadata"} == {
        key: val for key, val in after.items() if key != "metadata"
    }
    assert immutable_rows(platform) == evidence
    assert resolve(platform) == {
        "entity_id": entity.entity_id, "lifecycle_status": "active", "classification": value,
    }
    public = platform.securities()[0]
    assert public["entity_type"] == "stock"
    assert public["product_classification"] == {**value, "entity_id": entity.entity_id, "lifecycle_status": "active"}


def test_share_classes_and_venues_keep_separate_exact_receipts(platform):
    ordinary = add_listing(platform)
    preferred = add_listing(platform, "12345.TW")
    otc = add_listing(platform, "1234.TWO", "TPEx")
    receipts = [receipt(), receipt("12345.TW", product_type="preferred_stock"), receipt("1234.TWO", "TPEx", "etf")]
    sync(platform, receipts, acquired_at=ACQUIRED)
    assert len({ordinary.entity_id, preferred.entity_id, otc.entity_id}) == 3
    assert resolve(platform)["classification"]["product_type"] == "ordinary_stock"
    assert resolve(platform, "12345.TW")["classification"]["product_type"] == "preferred_stock"
    assert resolve(platform, "1234.TWO", "TPEx")["classification"]["product_type"] == "etf"
    assert resolve(platform, "1234.TWO", "TWSE")["classification"]["status"] == "unknown"


def test_plural_metadata_retains_multiple_legacy_listings_without_merging(platform):
    entity = add_listing(platform)
    platform.warehouse.upsert_entity(entity, identifiers=[{
        "source_id": "twse_openapi", "identifier_type": "display_symbol",
        "identifier_value": "12345.TW", "valid_from": "2026-01-01T00:00:00+00:00",
    }])
    sync(platform, [receipt(), receipt("12345.TW", product_type="preferred_stock")], acquired_at=ACQUIRED)
    values = metadata(platform, entity.entity_id)["product_classifications"]
    assert set(values) == {"TWSE:1234.TW", "TWSE:12345.TW"}
    assert resolve(platform, "12345.TW")["classification"]["product_type"] == "preferred_stock"


def test_ambiguous_identity_is_never_selected_or_reidentified(platform):
    first = add_listing(platform)
    second = add_listing(platform, seed="other-issuer")
    result = sync(platform, [receipt()], acquired_at=ACQUIRED)
    assert result["ambiguous_bindings"] == ["TWSE:1234.TW"]
    assert result["updated_count"] == 0
    assert "product_classifications" not in metadata(platform, first.entity_id)
    assert "product_classifications" not in metadata(platform, second.entity_id)
    assert resolve(platform)["classification"]["status"] == "conflict"
    assert resolve(platform)["entity_id"] is None


def test_tpex_and_emerging_require_exact_venue(platform):
    add_listing(platform, "1234.TWO", "TPEx")
    add_listing(platform, "1234.TWO", "TPEx-ESB")
    sync(platform, [receipt("1234.TWO", "TPEx"), receipt("1234.TWO", "TPEx-ESB")], acquired_at=ACQUIRED)
    assert resolve(platform, "1234.TWO", "taiwan")["classification"]["status"] == "conflict"
    assert resolve(platform, "1234.TWO", "TPEx")["classification"]["venue"] == "TPEx"
    assert resolve(platform, "1234.TWO", "TPEx-ESB")["classification"]["venue"] == "TPEx-ESB"


def test_stale_lifecycle_inventory_and_generic_upsert_keep_newer_catalog(platform):
    old_entity = add_listing(platform)
    value = receipt()
    sync(platform, [value], acquired_at=ACQUIRED)
    platform.warehouse.write_security_lifecycle_batch(
        entities=[old_entity], identifiers=[], revisions=[], events=[], raw_payload_ids={},
        acquired_at=ACQUIRED, code_version="offline-fixture",
    )
    assert resolve(platform)["classification"] == value
    platform.warehouse.upsert_entity(old_entity)
    assert resolve(platform)["classification"] == value


def test_real_lifecycle_refresh_keeps_classification_and_immutable_revision_bytes(platform):
    companies = [{"公司代號": "1234", "公司簡稱": "甲", "公司名稱": "甲股份有限公司", "上市日期": "1150102"}]
    first = platform.sync_security_master_payloads(
        twse_companies=companies, twse_quotes=[{"Code": "1234", "Name": "甲", "ClosingPrice": "100"}],
        acquired_at=ACQUIRED,
    )
    assert first["revision_count"] > 0
    evidence = immutable_rows(platform)
    entity_id = platform.securities()[0]["entity_id"]
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    assert immutable_rows(platform) == evidence
    second = platform.sync_security_master_payloads(
        twse_companies=companies, twse_quotes=[{"Code": "1234", "Name": "甲", "ClosingPrice": "101"}],
        acquired_at="2026-09-12T04:01:00+00:00",
    )
    assert second["revision_count"] == 0
    assert resolve(platform)["entity_id"] == entity_id
    assert resolve(platform)["classification"] == receipt()
    assert immutable_rows(platform)["data_revisions"] == evidence["data_revisions"]


def test_complete_catalog_absence_invalidates_only_same_source_and_preserves_lifecycle(platform):
    missing = add_listing(platform)
    add_listing(platform, "5678.TW")
    otc = add_listing(platform, "1234.TWO", "TPEx")
    old = receipt()
    sync(platform, [old, receipt("5678.TW"), receipt("1234.TWO", "TPEx")], acquired_at=ACQUIRED)
    # A subset is not a complete catalog.
    sync(platform, [receipt("5678.TW")], acquired_at=ACQUIRED)
    assert resolve(platform)["classification"] == old
    result = sync(platform,
        [receipt("5678.TW")], acquired_at=ACQUIRED, complete_source_datasets=("twse_isin_listed",),
    )
    tombstone = resolve(platform)["classification"]
    assert result["invalidated_count"] == 1
    assert tombstone["status"] == "unknown"
    assert tombstone["reasons"] == ["absent_from_complete_catalog"]
    assert tombstone["previous_receipt"] == old
    assert tombstone["absence_evidence"][0]["raw_sha256"] == receipt("5678.TW")["raw_sha256"]
    assert platform.warehouse.entity_profile(missing.entity_id)["lifecycle_status"] == "active"
    assert resolve(platform, "1234.TWO", "TPEx")["entity_id"] == otc.entity_id
    assert resolve(platform, "1234.TWO", "TPEx")["classification"]["status"] == "verified"


def test_empty_or_failed_catalog_cannot_erase_market_and_invalid_batch_is_atomic(platform):
    entity = add_listing(platform)
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    before = metadata(platform, entity.entity_id)
    sync(platform, [], acquired_at=ACQUIRED)
    with pytest.raises(ValueError, match="nonempty"):
        sync(platform, [], acquired_at=ACQUIRED, complete_source_datasets=("twse_isin_listed",))
    with pytest.raises(ValueError, match="invalid product"):
        sync(platform, [receipt(product_type="etf"), receipt(venue="TWSE", source_dataset="tpex_isin_otc")], acquired_at=ACQUIRED)
    assert metadata(platform, entity.entity_id) == before


def test_conflicting_duplicate_rows_fail_closed(platform):
    add_listing(platform)
    result = sync(platform, [receipt(), receipt(product_type="preferred_stock")], acquired_at=ACQUIRED)
    value = resolve(platform)["classification"]
    assert result["updated_count"] == 1
    assert value["status"] == "conflict"
    assert value["product_type"] == "unknown"
    assert len(value["conflicting_receipts"]) == 2
    sync(platform, [receipt(product_type="preferred_stock"), receipt()], acquired_at=ACQUIRED)
    assert resolve(platform)["classification"] == value


def test_late_older_refresh_cannot_replace_or_remove_newer_classification(platform):
    add_listing(platform)
    add_listing(platform, "5678.TW")
    value = receipt(product_type="preferred_stock")
    sync(platform, [value], acquired_at=ACQUIRED)
    older = "2026-09-11T00:00:00+00:00"
    result = sync(platform, [receipt(acquired_at=older)], acquired_at=older)
    assert result["stale_ignored_count"] == 1
    assert resolve(platform)["classification"] == value
    result = sync(platform,
        [receipt("5678.TW", acquired_at=older)], acquired_at=older,
        complete_source_datasets=("twse_isin_listed",),
    )
    assert result["stale_ignored_count"] == 1
    assert resolve(platform)["classification"] == value


@pytest.mark.parametrize("status", ["pre_listing", "suspended", "delisted", "expired", "unknown"])
def test_only_active_listing_resolves_verified(platform, status):
    entity = add_listing(platform, status=status)
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    value = resolve(platform)
    assert value["entity_id"] == entity.entity_id
    assert value["classification"]["status"] == "unknown"
    assert value["classification"]["reasons"] == ["product_listing_not_active"]


def test_expired_identifier_does_not_resolve(platform):
    add_listing(platform)
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    with platform.warehouse._connect() as conn:
        conn.execute("update entity_identifiers set valid_to='2026-09-11T00:00:00+00:00'")
    assert resolve(platform)["classification"]["status"] == "unknown"


def test_tampered_receipt_binding_is_not_projected(platform):
    entity = add_listing(platform)
    value = {"product_classifications": {"TWSE:1234.TW": receipt("9999.TW")}}
    with platform.warehouse._connect() as conn:
        conn.execute("update market_entities set metadata_json=? where entity_id=?", (json.dumps(value), entity.entity_id))
    assert resolve(platform)["classification"]["reasons"] == ["product_receipt_binding_mismatch"]
    assert platform.securities()[0]["product_classification"]["status"] == "unknown"


def test_provisional_flag_requires_official_lifecycle_observation(platform):
    entity = add_listing(platform, metadata={"provisional": True})
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    assert resolve(platform)["classification"]["status"] == "unknown"
    platform.warehouse.upsert_entity(entity.model_copy(update={"metadata": {
        **entity.metadata, "source_datasets": ["twse_companies_quotes"],
    }}))
    assert resolve(platform)["classification"]["status"] == "verified"


@pytest.mark.parametrize("symbol,market,now", [
    ("1234.tw", "TWSE", NOW), ("1234.TW", "unknown", NOW),
    ("1234.TW", "TWSE", datetime(2026, 9, 12)),
])
def test_resolver_input_is_fail_closed_and_read_only(platform, symbol, market, now):
    add_listing(platform)
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    with platform.warehouse._connect() as conn:
        before = list(conn.iterdump())
    assert platform.resolve_product_classification(symbol=symbol, market=market, now=now)["classification"]["status"] == "unknown"
    with platform.warehouse._connect() as conn:
        assert list(conn.iterdump()) == before


def test_valid_resolver_and_master_projection_work_with_query_only_connections(platform, monkeypatch):
    add_listing(platform)
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    connect = platform.warehouse._connect

    def readonly():
        conn = connect()
        conn.execute("pragma query_only=on")
        return conn

    monkeypatch.setattr(platform.warehouse, "_connect", readonly)
    assert resolve(platform)["classification"]["status"] == "verified"
    assert platform.securities()[0]["product_classification"]["status"] == "verified"


@pytest.mark.parametrize("field,value", [
    ("source_row", None), ("cfi_code", "CMXXXU"), ("isin", "TW0009999000"),
    ("source_updated_on", "2026-09-11"), ("acquired_at", "2026-09-12T03:00:00+00:00"),
    ("raw_sha256", "c" * 64), ("row_sha256", "c" * 64),
    ("raw_payload_id", "RAW-" + "0" * 32),
])
def test_production_resolver_rejects_receipt_not_derived_from_exact_raw(platform, field, value):
    entity = add_listing(platform)
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    assert resolve(platform)["classification"]["status"] == "verified"
    altered = metadata(platform, entity.entity_id)
    altered["product_classifications"]["TWSE:1234.TW"][field] = value
    with platform.warehouse._connect() as conn:
        conn.execute("update market_entities set metadata_json=? where entity_id=?", (json.dumps(altered), entity.entity_id))
    assert resolve(platform)["classification"]["status"] == "unknown"


def test_forged_ordinary_label_cannot_override_retained_preferred_share_row(platform):
    entity = add_listing(platform)
    sync(platform, [receipt(product_type="preferred_stock")], acquired_at=ACQUIRED)
    altered = metadata(platform, entity.entity_id)
    altered["product_classifications"]["TWSE:1234.TW"].update(
        product_type="ordinary_stock", market_segment="ordinary", cfi_code="ESVUFR",
    )
    with platform.warehouse._connect() as conn:
        conn.execute("update market_entities set metadata_json=? where entity_id=?", (json.dumps(altered), entity.entity_id))
    assert resolve(platform)["classification"]["status"] == "unknown"


@pytest.mark.parametrize("table,column,value", [
    ("raw_data_payloads", "request_url", "https://example.invalid/"),
    ("raw_data_payloads", "http_status", 503),
    ("raw_data_payloads", "received_at", "2026-09-12T03:00:00+00:00"),
    ("raw_data_payloads", "metadata_json", '{"effective_url":"https://example.invalid/"}'),
    ("raw_payload_objects", "parser_id", "different-parser"),
    ("raw_data_objects", "wire_hash", "c" * 64),
    ("raw_data_objects", "byte_length", 1),
])
def test_raw_capture_metadata_binding_is_rechecked_after_cache_warms(platform, table, column, value):
    add_listing(platform)
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    assert resolve(platform)["classification"]["status"] == "verified"
    with platform.warehouse._connect() as conn:
        # Simulate on-disk damage only in this disposable fixture database.
        conn.execute(f"drop trigger if exists trg_{table}_immutable_update")
        conn.execute(f"update {table} set {column}=?", (value,))
    assert resolve(platform)["classification"]["status"] == "unknown"


def test_raw_body_corruption_is_detected_after_warm_cache(platform):
    add_listing(platform)
    sync(platform, [receipt()], acquired_at=ACQUIRED)
    assert resolve(platform)["classification"]["status"] == "verified"
    with platform.warehouse._connect() as conn:
        conn.execute("drop trigger trg_raw_data_objects_immutable_update")
        row = conn.execute("select raw_object_id,body_blob from raw_data_objects where source_id='twse_isin'").fetchone()
        body = bytes(row["body_blob"])
        conn.execute("update raw_data_objects set body_blob=? where raw_object_id=?", (b"!" + body[1:], row["raw_object_id"]))
    assert resolve(platform)["classification"]["status"] == "unknown"


def test_catalogue_parse_is_reused_across_reads_and_unrelated_writes(platform, monkeypatch):
    from stock_ai.data_platform import product_catalog
    add_listing(platform)
    value = receipt()
    sync(platform, [value], acquired_at=ACQUIRED)
    parser = product_catalog.parse_product_catalogue
    calls = []

    def parse(*args, **kwargs):
        calls.append(kwargs["dataset_id"])
        return parser(*args, **kwargs)

    monkeypatch.setattr(product_catalog, "parse_product_catalogue", parse)
    assert resolve(platform)["classification"]["status"] == "verified"
    assert resolve(platform)["classification"]["status"] == "verified"
    assert platform.securities()[0]["product_classification"]["status"] == "verified"
    add_listing(platform, "5678.TW")
    assert resolve(platform)["classification"]["status"] == "verified"
    assert calls == ["twse_isin_listed"]
    assert platform.warehouse._product_proof_connection.execute("pragma query_only").fetchone()[0] == 1


def test_raw_parser_cache_is_bounded_to_three_catalogues(platform):
    for index in range(4):
        symbol = f"{1234 + index}.TW"
        add_listing(platform, symbol)
        sync(platform, [receipt(symbol)], acquired_at=ACQUIRED)
        assert resolve(platform, symbol)["classification"]["status"] == "verified"
    assert len(platform.warehouse._product_proof_cache) == 3


def test_master_batch_reads_and_parses_shared_raw_only_once(platform, monkeypatch):
    from stock_ai.data_platform import product_catalog
    add_listing(platform)
    add_listing(platform, "9999.TW")
    preferred = receipt(product_type="preferred_stock")
    raw, url, _ = RAW_FIXTURES[preferred["raw_payload_id"]]
    ordinary = next(value for value in parse_product_catalogue(
        raw, dataset_id="twse_isin_listed", acquired_at=ACQUIRED, source_url=url,
    )["receipts"] if value["symbol"] == "9999.TW")
    ordinary["raw_payload_id"] = preferred["raw_payload_id"]
    sync(platform, [preferred, ordinary], acquired_at=ACQUIRED)
    parser = product_catalog.parse_product_catalogue
    calls = []

    def parse(*args, **kwargs):
        calls.append(1)
        return parser(*args, **kwargs)

    monkeypatch.setattr(product_catalog, "parse_product_catalogue", parse)
    assert all(row["product_classification"]["status"] == "verified" for row in platform.securities())
    assert calls == [1]
    traced = []
    platform.warehouse._product_proof_connection.set_trace_callback(traced.append)
    assert len(platform.securities()) == 2
    assert not any("select body_blob" in query for query in traced)
    add_listing(platform, "5678.TW")
    traced.clear()
    assert len(platform.securities()) == 3
    assert sum("select body_blob" in query for query in traced) == 1
    assert calls == [1]
