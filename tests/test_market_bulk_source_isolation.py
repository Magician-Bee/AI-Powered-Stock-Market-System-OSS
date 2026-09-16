"""Isolated bulk-source failures retain successful rows and honest scan evidence."""
import asyncio
from copy import deepcopy
from http.client import IncompleteRead
import socket

import pytest

from stock_ai.market_intelligence import feature_store
from stock_ai.market_intelligence.broad_scanner import BroadScanner
from stock_ai.market_intelligence.service import MarketIntelligenceService
from stock_ai.market_intelligence.snapshot_store import SnapshotStore
from stock_ai.models import SecurityMasterItem
from product_classification_fixtures import product_fixture


@pytest.fixture
def sources(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("bulk isolation tests must not open real transports")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    master = []
    for symbol, venue, product in (
        ("2330.TW", "TWSE", "ordinary_stock"),
        ("020000.TW", "TWSE", "etn"),
        ("8069.TWO", "TPEx", "ordinary_stock"),
    ):
        identity = product_fixture(symbol, product)
        master.append(SecurityMasterItem(
            symbol=symbol, name="offline fixture", market="taiwan", exchange=venue,
            listing_type="listed" if venue == "TWSE" else "otc", source="offline fixture",
            entity_id=identity["entity_id"], product_classification=identity["product_classification"],
        ))
    def list_master(**kwargs):
        assert kwargs == {"market": "taiwan", "limit": 100000, "include_lifecycle": True}
        return master
    monkeypatch.setattr(feature_store, "list_securities_master", list_master)
    def optional(**kwargs):
        assert kwargs["allow_network"] is False
        return []
    monkeypatch.setattr(feature_store, "list_monthly_revenues", optional)
    monkeypatch.setattr(feature_store, "list_institutional_flows", optional)
    values = {
        "TWSE": [{"Code": code, "Date": "2026-09-11", "ClosingPrice": "100", "OpeningPrice": "98",
                  "HighestPrice": "101", "LowestPrice": "97", "Change": "2", "TradeVolume": "1000000",
                  "TradeValue": "100000000", "Transaction": "1000"} for code in ("2330", "020000")],
        "TPEx": [{"SecuritiesCompanyCode": "8069", "Date": "2026-09-11", "Close": "115", "Open": "112",
                  "High": "116", "Low": "111", "Change": "3", "TradingShares": "1000000",
                  "TransactionAmount": "115000000", "TransactionNumber": "1000"}],
    }
    calls = []
    def load(venue):
        calls.append(venue)
        value = values[venue]
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value) if isinstance(value, list) else value
    monkeypatch.setattr(feature_store, "twse_quotes", lambda: load("TWSE"))
    monkeypatch.setattr(feature_store, "tpex_quotes", lambda: load("TPEx"))
    return master, values, calls


def workspace(tmp_path):
    return MarketIntelligenceService(
        store=SnapshotStore(tmp_path / "isolated-market.sqlite"),
        scanner=BroadScanner(feature_loader=feature_store.load_all_taiwan_features,
                             feature_enricher=lambda rows: rows, position_loader=lambda: {}),
    )


@pytest.mark.parametrize("failed_venue", ["TWSE", "TPEx"])
def test_one_bulk_failure_preserves_other_venue_and_roundtrips_failure_evidence(sources, tmp_path, failed_venue):
    master, values, calls = sources
    values[failed_venue] = IncompleteRead(b"isolated partial body", 2048)
    service = workspace(tmp_path)
    created = service.build_snapshot()
    restored = service.snapshot(created.snapshot_id)
    assert calls == ["TWSE", "TPEx"]
    assert restored.status == "data_degraded"
    assert restored.data_as_of == "2026-09-11"
    assert set(restored.candidate_details) == {item.symbol for item in master}
    for item in master:
        candidate = restored.candidate_details[item.symbol]
        assert candidate.entity_id == item.entity_id
        assert candidate.product_classification == item.product_classification
        quality = candidate.data_quality
        if item.exchange != failed_venue:
            assert candidate.latest_price == (100 if item.exchange == "TWSE" else 115)
            assert quality.data_as_of == "2026-09-11"
            assert "primary_source_error" not in quality.source_observation
            assert candidate.evidence[0].observed_at == "2026-09-11"
            continue
        assert candidate.latest_price is None and candidate.volume == 0
        assert quality.data_as_of is None and quality.status == "insufficient"
        assert quality.fallback is False
        assert set(quality.missing_fields) >= {"latest_price", "volume", "data_as_of"}
        assert quality.source_observation["status"] == "not_observed"
        assert quality.source_observation["reason"] == "independent_bulk_quote_observation_not_connected"
        error = quality.source_observation["primary_source_error"]
        assert error["status"] == "failed" and error["venue"] == failed_venue
        assert error["type"] == "IncompleteRead" and "2048" in error["message"]
        assert error["source"] == quality.source
        assert candidate.market_category == (
            "INSUFFICIENT_DATA" if item.product_classification["product_type"] == "ordinary_stock" else "AVOID_NOW"
        )
        assert item.symbol not in restored.rankings["actionable_now"]
        assert len(candidate.evidence) == 1
        evidence = candidate.evidence[0]
        assert evidence.quality == "invalid" and evidence.observed_at is None
        assert "IncompleteRead" in evidence.statement and "未取得本次行情" in evidence.statement
        assert evidence.source == error["source"] and evidence.fallback is False
        receipt = candidate.data_quality_receipt
        assert receipt.certification_status == "blocked"
        assert receipt.source_disagreement_status == "not_observed"
        assert receipt.data_quality.source_observation["primary_source_error"] == error


def test_both_bulk_sources_succeed_once_without_changing_quote_dates(sources):
    _, _, calls = sources
    rows = feature_store.load_all_taiwan_features()
    assert calls == ["TWSE", "TPEx"]
    assert len(rows) == 3
    assert all(row["close"] is not None and row["data_as_of"] == "2026-09-11" for row in rows)
    assert all("primary_source_error" not in row["data_quality"]["source_observation"] for row in rows)


def test_failed_bulk_iteration_does_not_publish_rows_from_truncated_payload(sources):
    _, values, calls = sources
    first = values["TWSE"][0]
    def truncated():
        yield first
        raise IncompleteRead(b"isolated", 512)
    values["TWSE"] = truncated()
    rows = {row["symbol"]: row for row in feature_store.load_all_taiwan_features()}
    assert calls == ["TWSE", "TPEx"]
    assert rows["2330.TW"]["close"] is None and rows["2330.TW"]["data_as_of"] is None
    assert rows["8069.TWO"]["close"] == 115


def test_both_bulk_failures_raise_and_existing_service_preserves_prior_snapshot(sources, tmp_path):
    _, values, calls = sources
    service = workspace(tmp_path)
    previous = service.build_snapshot()
    values.update(TWSE=IncompleteRead(b"isolated", 123), TPEx=TimeoutError("isolated TPEx outage"))
    calls.clear()
    scan = service.create_scan()
    with pytest.raises(RuntimeError, match="official_bulk_quotes_unavailable") as failure:
        service.run_scan(scan["scan_id"])
    assert calls == ["TWSE", "TPEx"]
    assert "TWSE_ALL_QUOTES: IncompleteRead" in str(failure.value)
    assert "TPEX_DAILY_QUOTES: TimeoutError" in str(failure.value)
    assert service.latest_snapshot().model_dump(mode="json") == previous.model_dump(mode="json")
    assert service.store.scan(scan["scan_id"])["status"] == "failed"


@pytest.mark.parametrize("venue", ["TWSE", "TPEx"])
def test_bulk_cancellation_propagates_and_starts_no_later_source(sources, venue):
    _, values, calls = sources
    values[venue] = asyncio.CancelledError("isolated cancellation")
    with pytest.raises(asyncio.CancelledError, match="isolated cancellation"):
        feature_store.load_all_taiwan_features()
    assert calls == (["TWSE"] if venue == "TWSE" else ["TWSE", "TPEx"])


def test_source_failure_message_is_bounded_in_quality_and_candidate_evidence(sources, tmp_path):
    _, values, _ = sources
    values["TPEx"] = ValueError("isolated-message-" + "x" * 5000)
    restored = workspace(tmp_path).build_snapshot().candidate_details["8069.TWO"]
    assert len(restored.data_quality.source_observation["primary_source_error"]["message"]) == 512
    assert len(restored.evidence[0].statement) < 650
