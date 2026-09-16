"""Isolated retained-byte fixtures; never production market evidence."""
from datetime import datetime, timedelta
from hashlib import sha256
from html import escape
import json
import sqlite3

import pytest

from stock_ai.data_platform import product_catalog as catalog
from stock_ai.data_platform.catalogue_identity import retained_catalogue_issue_bindings
from stock_ai.data_platform.service import MarketDataPlatform
from stock_ai.data_platform.warehouse import content_hash


ACQUIRED = "2026-09-12T12:00:00+00:00"


def retain_catalogue(platform, *, dataset_id="twse_isin_listed", rows=None,
                     acquired_at=ACQUIRED, source_updated_on="2026/09/12", raw=None):
    """Retain a complete synthetic catalogue and its successful native checkpoint.

    Rows use code/name/isin/listed_on and optional section/cfi. No entities,
    company observations or quotes are created. Returns parsed v1 receipts.
    """
    venue, market, _ = catalog.CATALOGUES[dataset_id]
    if raw is None:
        values = list(rows or [])
        if not any(row.get("cfi", "RWCCCC").startswith("ES") for row in values):
            code, isin = {"TWSE": ("2330", "TW0002330008"), "TPEx": ("8299", "TW0008299009"),
                          "TPEx-ESB": ("7001", "TW0007001000")}[venue]
            values.insert(0, {"code": code, "name": "離線股票", "isin": isin,
                              "listed_on": "2004/12/06" if venue == "TPEx" else "1994/09/05",
                              "section": "股票", "cfi": "ESVUFR"})
        text = f"<table><h2>最近更新日期:{source_updated_on}</h2></table><table>"
        text += "<tr>" + "".join(f"<td>{v}</td>" for v in [
            "有價證券代號及名稱", "國際證券辨識號碼(ISIN Code)", "上市日", "市場別", "產業別", "CFICode", "備註"]) + "</tr>"
        for row in values:
            section = row.get("section", "上市認購(售)權證" if venue == "TWSE" else "上櫃認購(售)權證")
            if dataset_id != "tpex_isin_emerging":
                text += f"<tr><td colspan=7>{escape(section)}</td></tr>"
            cells = [row["code"] + "　" + row["name"], row["isin"], row["listed_on"], market,
                     "", row.get("cfi", "RWCCCC"), ""]
            text += "<tr>" + "".join(f"<td>{escape(v)}</td>" for v in cells) + "</tr>"
        raw = (text + "</table>").encode("cp950")
    wire_hash = sha256(raw).hexdigest()
    url = catalog.source_endpoint(dataset_id)
    raw_id = platform.warehouse.record_raw_payload(
        source_id="twse_isin", payload={"dataset_id": dataset_id, "wire_sha256": wire_hash, "acquired_at": acquired_at},
        raw_body=raw, request_url=url, requested_at=acquired_at, received_at=acquired_at,
        http_status=200, content_type="text/html;charset=MS950", content_encoding="cp950",
        parser_id="stock_ai.product_classification.v1", metadata={"dataset": "security_master",
            "source_dataset": dataset_id, "effective_url": url, "capture_truncated": False},
    )
    parsed = catalog.parse_product_catalogue(raw, dataset_id=dataset_id, acquired_at=acquired_at)
    for receipt in parsed["receipts"]:
        receipt["raw_payload_id"] = raw_id
    platform.warehouse.save_checkpoint(source_id="twse_isin", dataset="security_master", partition_key=dataset_id,
        status="succeeded", cursor_value=wire_hash,
        metadata={**{key: value for key, value in parsed.items() if key != "receipts"}, "raw_payload_id": raw_id})
    return parsed["receipts"]


@pytest.fixture
def platform(tmp_path):
    return MarketDataPlatform(database_path=tmp_path / "market.sqlite")


def warrant(code="700001", **changes):
    return {"code": code, "name": "離線發行甲", "isin": "TW0700000100", "listed_on": "2026/09/12", **changes}


def test_exact_retained_row_date_name_and_proof_without_identity_or_writes(platform, monkeypatch):
    original = retain_catalogue(platform, rows=[warrant()])
    before = json.loads(json.dumps(original))
    statements = []
    real_connect = sqlite3.connect
    def connect(*args, **kwargs):
        assert "mode=ro" in str(args[0])
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    result = retained_catalogue_issue_bindings(platform.warehouse, as_of=ACQUIRED)
    issue = result["bindings"][("TWSE", "700001")]
    assert issue["source_name"] == "離線發行甲"
    assert issue["isin"] == "TW0700000100"
    assert issue["listed_on"] == "2026-09-12"
    assert issue["listed_at"] == "2026-09-11T16:00:00+00:00"
    assert issue["eligible_as_of"] is True
    assert issue["listing_status"] == "listed_date_reached"
    assert issue["historical_pit_eligible"] is False
    assert issue["product_classification"] == original[1]
    assert issue["receipt_sha256"] == content_hash({k: v for k, v in issue.items() if k != "receipt_sha256"})
    assert original == before
    assert all("listed_at" not in r and "listed_on" not in r for r in original)
    assert statements and all(s.lstrip().split()[0].casefold() in {"select", "pragma", "begin", "rollback"} for s in statements)


def test_future_listing_retained_without_claiming_active_and_opens_at_taipei_midnight(platform):
    retain_catalogue(platform, rows=[warrant(listed_on="2026/09/13")], acquired_at="2026-09-12T15:00:00+00:00")
    before = retained_catalogue_issue_bindings(platform.warehouse, as_of="2026-09-12T15:59:59+00:00")["bindings"][("TWSE", "700001")]
    after = retained_catalogue_issue_bindings(platform.warehouse, as_of="2026-09-12T16:00:00+00:00")["bindings"][("TWSE", "700001")]
    assert before["listing_status"] == "pre_listing" and before["eligible_as_of"] is False
    assert after["listing_status"] == "listed_date_reached" and after["eligible_as_of"] is True
    assert "active" not in (before["listing_status"], after["listing_status"])


@pytest.mark.parametrize("listed", ["", "115/09/12", "2026/9/12", "2026/02/30", "2026/09/12 injected", "2026-09-12"])
def test_unvalidated_v1_listing_cell_cannot_become_issue_identity(platform, listed):
    rows = retain_catalogue(platform, rows=[warrant(listed_on=listed)])
    assert rows[1]["status"] == "verified"  # v1 classification contract is unchanged.
    result = retained_catalogue_issue_bindings(platform.warehouse, as_of=ACQUIRED)
    assert ("TWSE", "700001") not in result["bindings"]
    assert result["partitions"][0]["rejection_counts"] == {"catalogue_listing_date_missing_or_invalid": 1}


def test_duplicate_isin_across_codes_rejected_instead_of_merging(platform):
    retain_catalogue(platform, rows=[warrant(), warrant("700002")])
    result = retained_catalogue_issue_bindings(platform.warehouse, as_of=ACQUIRED)
    assert all(key[1] not in {"700001", "700002"} for key in result["bindings"])
    assert result["partitions"][0]["rejection_counts"] == {"catalogue_duplicate_isin": 2}


@pytest.mark.parametrize("offset,allowed,reason", [
    (-1, False, "catalogue_acquired_after_as_of"),
    (21600, True, None),
    (21601, False, "catalogue_acquisition_stale"),
])
def test_registered_six_hour_acquisition_boundary(platform, offset, allowed, reason):
    retain_catalogue(platform, rows=[warrant()])
    instant = (datetime.fromisoformat(ACQUIRED) + timedelta(seconds=offset)).isoformat()
    result = retained_catalogue_issue_bindings(platform.warehouse, as_of=instant)
    assert (("TWSE", "700001") in result["bindings"]) is allowed
    if reason:
        assert result["partitions"][0]["reasons"] == [reason]


def test_fresh_capture_cannot_make_two_day_old_source_current(platform):
    retain_catalogue(platform, rows=[warrant()], acquired_at="2026-09-13T16:00:00+00:00")
    result = retained_catalogue_issue_bindings(platform.warehouse, as_of="2026-09-13T16:00:00+00:00")
    assert result["bindings"] == {}
    assert result["partitions"][0]["reasons"] == ["catalogue_source_date_stale_or_future"]


def test_skipped_fresh_loader_still_resolves_retained_checkpoint_without_fetch(platform, monkeypatch):
    def fetch(dataset):
        # Reuse the raw bytes created by the fixture, through exact local reads.
        receipts = retain_catalogue(platform, dataset_id=dataset)
        raw = platform.warehouse.raw_payload(receipts[0]["raw_payload_id"], include_body=True)
        url = catalog.source_endpoint(dataset)
        return {"raw": raw["body"], "source_url": url, "effective_url": url, "http_status": 200,
                "content_type": "text/html", "requested_at": ACQUIRED, "acquired_at": ACQUIRED}
    monkeypatch.setattr(catalog, "fetch_product_catalogue", fetch)
    loader = catalog.OfficialProductClassificationLoader(platform)
    assert all(row["status"] == "succeeded" for row in loader.run(as_of=ACQUIRED))
    monkeypatch.setattr(catalog, "fetch_product_catalogue", lambda *_: pytest.fail("cached local read must not fetch"))
    again = loader.run(as_of=ACQUIRED)
    assert all(row["status"] == "skipped_fresh" and row["classification"] is None for row in again)
    result = retained_catalogue_issue_bindings(platform.warehouse, as_of=ACQUIRED)
    assert result["status"] == "verified"
    assert {key[0] for key in result["bindings"]} == {"TWSE", "TPEx", "TPEx-ESB"}


@pytest.mark.parametrize("tamper", ["bytes", "source", "checkpoint_hash", "checkpoint_failed"])
def test_warm_retained_proof_rejects_raw_or_checkpoint_tamper(platform, tamper):
    retain_catalogue(platform, rows=[warrant()])
    assert ("TWSE", "700001") in retained_catalogue_issue_bindings(platform.warehouse, as_of=ACQUIRED)["bindings"]
    with platform.warehouse._connect() as conn:
        if tamper in {"bytes", "source"}:
            # Simulate storage corruption in this disposable fixture only;
            # ordinary application writes are separately blocked by the trigger.
            conn.execute("drop trigger trg_raw_data_objects_immutable_update")
        if tamper == "bytes":
            body = conn.execute("select raw_object_id,body_blob from raw_data_objects").fetchone()
            conn.execute("update raw_data_objects set body_blob=? where raw_object_id=?",
                         (bytes(body["body_blob"]).replace(b"1994", b"1995"), body["raw_object_id"]))
        elif tamper == "source":
            conn.execute("update raw_data_objects set source_id='twse_openapi'")
        elif tamper == "checkpoint_hash":
            conn.execute("update data_ingestion_checkpoints set cursor_value='wrong'")
        else:
            conn.execute("update data_ingestion_checkpoints set status='failed'")
    result = retained_catalogue_issue_bindings(platform.warehouse, as_of=ACQUIRED)
    assert result["bindings"] == {}
    assert result["partitions"][0]["status"] == "unavailable"
    assert result["partitions"][0]["reasons"]
