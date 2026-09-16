from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import asyncio
from threading import Event

import pytest

from stock_ai.data_platform import product_catalog as catalog
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.data_platform.warehouse import content_hash

ACQUIRED = "2026-09-12T12:00:00+00:00"


def html_catalog(sections=None, *, updated="2026/09/12", market="上市", emerging=False):
    if sections is None:
        code, name, isin = {"上市": ("2330", "台積電", "TW0002330008"),
                            "上櫃": ("8299", "離線上櫃", "TW0008299009"),
                            "興櫃": ("7001", "離線興櫃", "TW0007001000")}[market]
        sections = [("股票", [(code, name, isin, "ESVUFR")])]
    text = f"<table><h2>最近更新日期:{updated}</h2></table><table>"
    text += "<tr>" + "".join(f"<td>{value}</td>" for value in
        ["有價證券代號及名稱", "國際證券辨識號碼(ISIN Code)", "上市日", "市場別", "產業別", "CFICode", "備註"]) + "</tr>"
    for section, rows in sections:
        if not emerging:
            text += f"<tr><td colspan=7>{escape(section)}</td></tr>"
        for code, name, isin, cfi in rows:
            cells = [f"{code}　{name}", isin, "1994/09/05", market, "", cfi, ""]
            text += "<tr>" + "".join(f"<td>{escape(value)}</td>" for value in cells) + "</tr>"
    return (text + "</table>").encode("cp950")


def captured(raw, dataset_id, **changes):
    url = catalog.source_endpoint(dataset_id)
    return {"raw": raw, "source_url": url, "effective_url": url, "http_status": 200,
            "content_type": "text/html;charset=MS950", "requested_at": ACQUIRED,
            "acquired_at": ACQUIRED, **changes}


def test_official_sections_classify_share_classes_and_structured_products():
    raw = html_catalog([
        ("股票", [("2330", "台積電", "TW0002330008", "ESVUFR")]),
        ("ETF", [("00400A", "主動基金", "TW00000400A3", "CEOJEU")]),
        ("ETN", [("020000", "富邦特選蘋果N", "TW0000200005", "CMXXXU")]),
        ("臺灣存託憑證(TDR)", [("9103", "美德醫療-DR", "TW0009103002", "EDSDDR")]),
        ("特別股", [("2887Z1", "特別股", "TW0002887Z13", "EPNCAR"),
                      ("2897B", "可轉特別股", "TW0002897B01", "EFNRAR")]),
    ])
    result = catalog.parse_product_catalogue(raw, dataset_id="twse_isin_listed", acquired_at=ACQUIRED)
    by_symbol = {r["symbol"]: r for r in result["receipts"]}
    assert result["classification_status_counts"] == {"verified": 6}
    assert [by_symbol[s]["product_type"] for s in ("2330.TW", "00400A.TW", "020000.TW", "9103.TW", "2887Z1.TW")] == [
        "ordinary_stock", "etf", "etn", "depositary_receipt", "preferred_stock"]
    for receipt in result["receipts"]:
        assert receipt["raw_sha256"] == sha256(raw).hexdigest()
        assert receipt["row_sha256"] == content_hash(receipt["source_row"])
        assert receipt["acquired_at"] == ACQUIRED
        assert receipt["source_updated_on"] == "2026-09-12"


def test_heading_and_cfi_disagreement_is_retained_as_conflict():
    raw = html_catalog([("股票", [("2330", "看起來像股票", "TW0002330008", "CMXXXU")])])
    receipt = catalog.parse_product_catalogue(raw, dataset_id="twse_isin_listed", acquired_at=ACQUIRED)["receipts"][0]
    assert receipt["status"] == "conflict"
    assert receipt["reasons"] == ["official_section_cfi_conflict"]


def test_duplicate_rows_are_never_last_win():
    raw = html_catalog([("股票", [("2330", "甲", "TW0002330008", "ESVUFR"),
                                  ("2330", "乙", "TW0002330008", "ESVUFR")])])
    rows = catalog.parse_product_catalogue(raw, dataset_id="twse_isin_listed", acquired_at=ACQUIRED)["receipts"]
    assert len(rows) == 2
    assert all(r["status"] == "conflict" and "duplicate_official_product_identity" in r["reasons"] for r in rows)


def test_new_section_stays_unknown_and_visible():
    raw = html_catalog([("股票", [("2330", "甲", "TW0002330008", "ESVUFR")]),
                        ("未審核新類型", [("2345", "乙", "TW0002345006", "ESVUFR")])])
    rows = catalog.parse_product_catalogue(raw, dataset_id="twse_isin_listed", acquired_at=ACQUIRED)["receipts"]
    assert rows[1]["status"] == "unknown"
    assert rows[1]["product_type"] == "unknown"


@pytest.mark.parametrize("dataset,market,venue,segment,section", [
    ("tpex_isin_otc", "上櫃", "TPEx", "ordinary", "股票"),
    ("tpex_isin_emerging", "興櫃", "TPEx-ESB", "emerging", "興櫃"),
    ("twse_isin_listed", "上市臺灣創新板", "TWSE", "innovation", "創新板"),
])
def test_venue_and_market_segment_are_separate(dataset, market, venue, segment, section):
    raw = html_catalog([(section, [("1234", "樣本", "TW0001234000", "ESVUFR")])],
                       market=market, emerging=dataset == "tpex_isin_emerging")
    r = catalog.parse_product_catalogue(raw, dataset_id=dataset, acquired_at=ACQUIRED)["receipts"][0]
    assert (r["venue"], r["market_segment"], r["status"]) == (venue, segment, "verified")


@pytest.mark.parametrize("mutate,reason", [
    (lambda raw: b"", "empty_or_oversize"),
    (lambda raw: raw[:-8], "incomplete_table"),
    (lambda raw: raw.replace(b"CFICode", b"NewField"), "header_unrecognized"),
    (lambda raw: raw.replace(b"2026/09/12", b"2026/09/13"), "future_source_date"),
    (lambda raw: raw.replace(b"2026/09/12", b"unknown"), "source_date_missing"),
    (lambda raw: raw.replace(b"</td></tr></table>", b"</tr></table>"), "unclosed_cell"),
    (lambda raw: raw + b"\xff", ""),
    (lambda raw: raw + b"<table><tr>", "additional_table_unreviewed"),
])
def test_corrupt_or_incomplete_catalogues_cannot_be_complete_snapshots(mutate, reason):
    with pytest.raises((ValueError, UnicodeDecodeError), match=reason or None):
        catalog.parse_product_catalogue(mutate(html_catalog()), dataset_id="twse_isin_listed", acquired_at=ACQUIRED)


def test_source_identity_and_timezone_required():
    with pytest.raises(ValueError, match="url_mismatch"):
        catalog.parse_product_catalogue(html_catalog(), dataset_id="twse_isin_listed", acquired_at=ACQUIRED,
            source_url="https://example.invalid/catalogue")
    with pytest.raises(ValueError, match="timezone_required"):
        catalog.parse_product_catalogue(html_catalog(), dataset_id="twse_isin_listed", acquired_at="2026-09-12T12:00:00")


def test_registered_catalogues_never_attest_historical_availability():
    registry = catalog.get_source_registry()
    for dataset_id in catalog.CATALOGUES:
        dataset = registry.dataset(dataset_id)
        assert dataset.source_id == "twse_isin"
        assert dataset.failure_strategy.max_attempts == 1
        assert dataset.failure_strategy.on_exhausted == "fail_closed"
        assert registry.identify_dataset_url(catalog.source_endpoint(dataset_id)).dataset_id == dataset_id


def test_loader_retains_rejected_raw_and_other_venues_continue(monkeypatch, tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market.sqlite")
    platform.sync_security_master_payloads(twse_companies=[{"公司代號": "2330", "公司簡稱": "台積電"}],
        twse_quotes=[{"Code": "2330", "Name": "台積電"}], acquired_at=ACQUIRED)
    before = platform.securities()[0]
    bad = b"<html>upstream error</html>"
    def fetch(dataset):
        market = catalog.CATALOGUES[dataset][1]
        raw = bad if dataset == "twse_isin_listed" else html_catalog(market=market, emerging=market == "興櫃")
        return captured(raw, dataset)
    monkeypatch.setattr(catalog, "fetch_product_catalogue", fetch)
    result = catalog.OfficialProductClassificationLoader(platform).run(force=True, as_of=ACQUIRED)
    assert [r["status"] for r in result] == ["failed", "succeeded", "succeeded"]
    assert result[0]["classification"]["raw_payload_id"].startswith("RAW-")
    assert result[0]["classification"]["raw_sha256"] == sha256(bad).hexdigest()
    after = platform.securities()[0]
    assert after["entity_id"] == before["entity_id"]
    assert after["trading_status"] == before["trading_status"] == "active"
    assert after["product_classification"]["status"] == "unknown"
    with platform.warehouse._connect() as conn:
        row = conn.execute("select body_blob,wire_hash from raw_data_objects where source_id='twse_isin' and request_url like '%strMode=2'").fetchone()
        assert bytes(row["body_blob"]) == bad
        assert row["wire_hash"] == sha256(bad).hexdigest()
    checkpoint = platform.warehouse.get_checkpoint(source_id="twse_isin", dataset="security_master", partition_key="twse_isin_listed")
    assert checkpoint["status"] == "failed"


def test_loader_exact_raw_to_existing_identity_and_cached_read(monkeypatch, tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market.sqlite")
    platform.sync_security_master_payloads(twse_companies=[{"公司代號": "2330", "公司簡稱": "台積電"}],
        twse_quotes=[{"Code": "2330", "Name": "台積電"}], acquired_at=ACQUIRED)
    original = platform.securities()[0]["entity_id"]
    calls = []
    def fetch(dataset):
        calls.append(dataset)
        market = catalog.CATALOGUES[dataset][1]
        return captured(html_catalog(market=market, emerging=market == "興櫃"), dataset)
    monkeypatch.setattr(catalog, "fetch_product_catalogue", fetch)
    first = catalog.OfficialProductClassificationLoader(platform).run(as_of=ACQUIRED)
    second = catalog.OfficialProductClassificationLoader(platform).run(as_of=ACQUIRED)
    assert len(calls) == 3
    assert all(r["status"] == "succeeded" for r in first)
    assert all(r["status"] == "skipped_fresh" for r in second)
    item = platform.resolve_product_classification(symbol="2330.TW", market="TW", now=datetime(2026, 9, 12, 13, tzinfo=timezone.utc))
    assert item["entity_id"] == original
    assert item["classification"]["status"] == "verified"
    assert item["classification"]["raw_payload_id"].startswith("RAW-")


def test_cancelled_refresh_finishes_current_partition_without_starting_another(monkeypatch, tmp_path):
    platform = MarketDataPlatform(database_path=tmp_path / "market.sqlite")
    stop = Event()
    calls = []
    def fetch(dataset):
        calls.append(dataset)
        stop.set()
        return captured(html_catalog(), dataset)
    monkeypatch.setattr(catalog, "fetch_product_catalogue", fetch)
    with pytest.raises(asyncio.CancelledError):
        catalog.OfficialProductClassificationLoader(platform).run(as_of=ACQUIRED, stop_event=stop)
    assert calls == ["twse_isin_listed"]
    checkpoint = platform.warehouse.get_checkpoint(source_id="twse_isin", dataset="security_master", partition_key="twse_isin_listed")
    assert checkpoint["status"] == "succeeded"
    assert platform.warehouse.get_checkpoint(source_id="twse_isin", dataset="security_master", partition_key="tpex_isin_otc") is None
