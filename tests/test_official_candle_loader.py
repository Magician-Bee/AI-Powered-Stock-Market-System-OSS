from __future__ import annotations

import json
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from urllib.error import HTTPError

from open_stock_ai.research.official_candle_loader import load_official_candles, parse_official_month
from open_stock_ai.research import official_candle_loader


def _twse(code="2330", month="202101"):
    return {"stat":"OK","date":month+"01","title":f"110年01月 {code} 台積電 各日成交資訊",
            "data":[[f"{int(month[:4])-1911}/{month[4:]}/04","1,000","100,000","100","101","99","100"]]}


def test_twse_response_must_match_requested_code_and_month():
    rows,identity=parse_official_month(_twse(),symbol="2330.TW",month="202101")
    assert rows[0]["volume"]==1000 and rows[0]["close"]==100
    assert "2330" in identity["response_title"]
    with pytest.raises(ValueError,match="symbol_mismatch"):
        parse_official_month(_twse("2317"),symbol="2330.TW",month="202101")
    with pytest.raises(ValueError,match="month_mismatch"):
        parse_official_month(_twse(month="202102"),symbol="2330.TW",month="202101")


def test_tpex_normalizes_lots_and_thousands_only_with_verified_unit_contract():
    payload={"stat":"ok","date":"20210101","code":"3105","name":"穩懋",
             "tables":[{"fields":["日 期","成交張數","成交仟元","開盤","最高","最低","收盤"],
                        "data":[["110/01/04","2","200","100","101","99","100"]]}]}
    rows,_=parse_official_month(payload,symbol="3105.TWO",month="202101")
    assert rows[0]["volume"]==2000 and rows[0]["turnover"]==200000
    payload["tables"][0]["fields"]=["日 期","成交仟股","成交仟元","開盤","最高","最低","收盤"]
    rows,_=parse_official_month(payload,symbol="3105.TWO",month="202101")
    assert rows[0]["volume"]==2000 and rows[0]["turnover"]==200000
    payload["tables"][0]["fields"]=["日 期","成交股數","成交金額(元)","開盤","最高","最低","收盤"]
    rows,_=parse_official_month(payload,symbol="3105.TWO",month="202101")
    assert rows[0]["volume"]==2 and rows[0]["turnover"]==200
    payload["tables"][0]["fields"][1]="成交量"
    with pytest.raises(ValueError,match="volume_units_unverified"):
        parse_official_month(payload,symbol="3105.TWO",month="202101")


def test_source_label_cannot_turn_us_symbol_into_taiwan_official_history():
    with pytest.raises(ValueError,match="identity_required"):
        load_official_candles("AAPL.TW",start="2021-01-01",end="2021-01-31")


def test_closed_month_cache_is_request_and_hash_bound_and_never_falls_back(tmp_path):
    calls=[]
    def fetch(url,timeout):
        calls.append(url)
        assert urlparse(url).hostname=="www.twse.com.tw"
        return json.dumps(_twse()).encode()
    first=load_official_candles("2330.TW",start="2021-01-01",end="2021-01-31",cache_dir=tmp_path,fetch_bytes=fetch)
    second=load_official_candles("2330.TW",start="2021-01-01",end="2021-01-31",cache_dir=tmp_path,fetch_bytes=fetch)
    assert len(calls)==1 and first["rows"]==second["rows"]
    assert second["source_request_receipts"][0]["cache_hit"] is True
    assert first["data_evidence"]["instrument_identity_verified"] is True
    assert first["data_evidence"]["corporate_actions_verified"] is False
    cached=tmp_path/"2330.TW-202101.json"
    payload=json.loads(cached.read_text());payload["raw_sha256"]="0"*64;cached.write_text(json.dumps(payload))
    rejected=load_official_candles("2330.TW",start="2021-01-01",end="2021-01-31",cache_dir=tmp_path,fetch_bytes=fetch)
    assert rejected["rows"]==[] and not rejected["coverage_complete"]
    assert "hash_mismatch" in rejected["source_request_receipts"][0]["reason"]
    assert len(calls) == 1  # A damaged cache is not silently replaced by another response.


def test_bounded_source_failures_stop_requests_and_preserve_missing_months():
    calls=[]
    def fetch(url,timeout):
        calls.append(url)
        raise OSError("source_unavailable")
    result=load_official_candles("2330.TW",start="2021-01-01",end="2021-06-30",fetch_bytes=fetch)
    assert len(calls)==3
    assert len(result["rejected_months"])==6
    assert result["data_evidence"]["source_provenance_verified"] is False
    with pytest.raises(ValueError,match="budget_exceeded"):
        load_official_candles("2330.TW",start="2021-01-01",end="2026-06-30",max_months=12,fetch_bytes=fetch)


def test_self_redirect_retries_only_the_verified_same_exchange_legacy_endpoint(monkeypatch):
    calls=[]
    def fetch_once(url,timeout):
        calls.append(url)
        if "/rwd/" in url:
            raise HTTPError(url,308,"self redirect",{},None)
        assert url.startswith("https://www.twse.com.tw/exchangeReport/STOCK_DAY?")
        return json.dumps(_twse()).encode()
    monkeypatch.setattr(official_candle_loader,"_fetch_once",fetch_once)
    result=load_official_candles("2330.TW",start="2021-01-01",end="2021-01-31")
    assert result["coverage_complete"] and len(calls)==2
    assert "/exchangeReport/" in result["source_request_receipts"][0]["effective_url"]


def test_nonofficial_response_domain_cannot_become_official_provenance():
    result=load_official_candles("2330.TW",start="2021-01-01",end="2021-01-31",
                                fetch_bytes=lambda url,timeout:(json.dumps(_twse()).encode(),"https://example.com/data"))
    assert not result["coverage_complete"] and result["rows"]==[]


@pytest.mark.parametrize("tamper", [False, True])
def test_network_stop_still_validates_cache_without_reopening_network(tmp_path, tamper):
    def good(url, timeout):
        month = parse_qs(urlparse(url).query)["date"][0][:6]
        return json.dumps(_twse(month=month)).encode()
    for month in ("202101", "202103"):
        load_official_candles("2330.TW", start=f"{month[:4]}-{month[4:]}-01", end=f"{month[:4]}-{month[4:]}-28",
                              cache_dir=tmp_path, fetch_bytes=good)
    if tamper:
        path = tmp_path / "2330.TW-202103.json"
        payload = json.loads(path.read_text())
        payload["raw_sha256"] = "0" * 64
        path.write_text(json.dumps(payload))
    calls = []
    def failed(url, timeout):
        calls.append(parse_qs(urlparse(url).query)["date"][0][:6])
        raise OSError("fixture source unavailable")
    result = load_official_candles("2330.TW", start="2021-01-01", end="2021-06-30", cache_dir=tmp_path, fetch_bytes=failed)
    receipts = {row["month"]: row for row in result["source_request_receipts"]}
    assert calls == ["202106", "202105", "202104"]
    assert receipts["202101"]["status"] == "verified"
    assert receipts["202103"]["cache_hit"] is True
    assert "stopped_after_three_source_failures" in receipts["202102"]["reason"]
    assert result["coverage_complete"] is False
    assert len(result["rows"]) == (1 if tamper else 2)
    assert result["data_evidence"]["source_provenance_verified"] is (not tamper)
    if tamper:
        assert "hash_mismatch" in receipts["202103"]["reason"]
        assert receipts["202103"]["failure_kind"] == "verification_failure"


def test_intervening_cache_hits_do_not_reset_network_failure_budget(tmp_path):
    for month in ("202102", "202104", "202106"):
        load_official_candles("2330.TW", start=f"{month[:4]}-{month[4:]}-01", end=f"{month[:4]}-{month[4:]}-28",
                              cache_dir=tmp_path, fetch_bytes=lambda url, timeout, month=month: json.dumps(_twse(month=month)).encode())
    calls = []
    def failed(url, timeout):
        calls.append(parse_qs(urlparse(url).query)["date"][0][:6])
        raise OSError("fixture source unavailable")
    result = load_official_candles("2330.TW", start="2021-01-01", end="2021-07-31", cache_dir=tmp_path, fetch_bytes=failed)
    assert calls == ["202107", "202105", "202103"]
    assert len(result["rows"]) == 3
    assert result["source_request_receipts"][0]["reason"].endswith("monthly_requests_stopped_after_three_source_failures")


def test_parser_rejection_retains_original_bytes_for_replay_without_success_cache(tmp_path):
    raw = b"\xef\xbb\xbf" + json.dumps(_twse(code="2317"), ensure_ascii=False).encode()
    calls = []
    def fetch(url, timeout):
        calls.append(url)
        return raw, url.replace("/rwd/zh/afterTrading/", "/exchangeReport/")
    result = load_official_candles("2330.TW", start="2021-01-01", end="2021-01-31", cache_dir=tmp_path, fetch_bytes=fetch)
    receipt = result["source_request_receipts"][0]
    assert receipt["status"] == "rejected" and receipt["failure_kind"] == "verification_failure"
    assert receipt["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["acquired_at"] and "/exchangeReport/" in receipt["effective_url"]
    path = Path(receipt["rejected_raw_path"])
    assert path.read_bytes() == raw
    assert path.name == receipt["raw_sha256"] + ".bin"
    assert receipt["raw_retention_status"] == "retained"
    assert not (tmp_path / "2330.TW-202101.json").exists()
    with pytest.raises(ValueError, match="symbol_mismatch"):
        parse_official_month(json.loads(path.read_bytes()), symbol="2330.TW", month="202101")
    before = path.stat().st_mtime_ns
    repeated = load_official_candles("2330.TW", start="2021-01-01", end="2021-01-31", cache_dir=tmp_path, fetch_bytes=fetch)
    assert repeated["source_request_receipts"][0]["rejected_raw_path"] == str(path)
    assert path.stat().st_mtime_ns == before
    assert list(path.parent.iterdir()) == [path]
    assert len(calls) == 2  # A rejected blob is never reused as an accepted monthly cache.


@pytest.mark.parametrize("raw", [b"not JSON", b"\xff\xfeinvalid utf8"])
def test_unparseable_response_still_has_replayable_raw_hash(tmp_path, raw):
    result = load_official_candles("2330.TW", start="2021-01-01", end="2021-01-31", cache_dir=tmp_path,
                                   fetch_bytes=lambda url, timeout: raw)
    receipt = result["source_request_receipts"][0]
    assert result["rows"] == [] and receipt["status"] == "rejected"
    assert receipt["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert Path(receipt["rejected_raw_path"]).read_bytes() == raw


def test_rejected_raw_retention_failure_does_not_hide_original_parser_error(tmp_path, monkeypatch):
    raw = json.dumps(_twse(code="2317")).encode()
    def denied(*args):
        raise OSError("fixture evidence storage unavailable")
    monkeypatch.setattr(official_candle_loader.os, "link", denied)
    result = load_official_candles("2330.TW", start="2021-01-01", end="2021-01-31", cache_dir=tmp_path,
                                   fetch_bytes=lambda url, timeout: raw)
    receipt = result["source_request_receipts"][0]
    assert receipt["reason"] == "ValueError:official_response_symbol_mismatch"
    assert receipt["failure_kind"] == "verification_failure"
    assert receipt["raw_retention_status"] == "failed"
    assert "storage unavailable" in receipt["raw_retention_error"]
    assert receipt["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["acquired_at"] and receipt["effective_url"]
    assert "rejected_raw_path" not in receipt
    assert list((tmp_path / "rejected-responses").iterdir()) == []


def test_existing_rejected_blob_corruption_is_reported_without_overwriting(tmp_path):
    raw = json.dumps(_twse(code="2317")).encode()
    directory = tmp_path / "rejected-responses"
    directory.mkdir()
    path = directory / (hashlib.sha256(raw).hexdigest() + ".bin")
    path.write_bytes(b"damaged")
    result = load_official_candles("2330.TW", start="2021-01-01", end="2021-01-31", cache_dir=tmp_path,
                                   fetch_bytes=lambda url, timeout: raw)
    receipt = result["source_request_receipts"][0]
    assert receipt["raw_retention_status"] == "failed"
    assert "content_hash_mismatch" in receipt["raw_retention_error"]
    assert path.read_bytes() == b"damaged" and "rejected_raw_path" not in receipt


def test_cache_write_failure_preserves_verified_received_bars(tmp_path):
    cache = tmp_path / "cache-is-file"
    cache.write_text("existing file")
    raw = json.dumps(_twse()).encode()
    result = load_official_candles("2330.TW", start="2021-01-01", end="2021-01-31", cache_dir=cache,
                                   fetch_bytes=lambda url, timeout: raw)
    receipt = result["source_request_receipts"][0]
    assert len(result["rows"]) == 1 and result["data_evidence"]["source_provenance_verified"] is True
    assert receipt["status"] == "verified" and receipt["cache_write_status"] == "failed"
    assert receipt["cache_write_error"] and "raw_cache_path" not in receipt
    assert receipt["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert cache.read_text() == "existing file"


def test_bom_bytes_remain_hash_bound_through_accepted_cache(tmp_path):
    raw = b"\xef\xbb\xbf" + json.dumps(_twse(), ensure_ascii=False).encode()
    first = load_official_candles("2330.TW", start="2021-01-01", end="2021-01-31", cache_dir=tmp_path,
                                  fetch_bytes=lambda url, timeout: raw)
    def unexpected_fetch(url, timeout):
        pytest.fail("verified closed-month cache should avoid network")
    second = load_official_candles("2330.TW", start="2021-01-01", end="2021-01-31", cache_dir=tmp_path, fetch_bytes=unexpected_fetch)
    assert first["rows"] == second["rows"]
    for result in (first, second):
        assert result["source_request_receipts"][0]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
        assert result["data_evidence"]["source_provenance_verified"] is True
