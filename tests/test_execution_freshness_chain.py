from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from open_stock_ai.data.market_data_hub import MarketDataHub
from open_stock_ai.types import StockRequest
from stock_ai import paper_training_api, services
from stock_ai.models import Entity, MarketSummary, PricePoint


def _summary(*, old=True, state="closed"):
    now = datetime.now(timezone.utc)
    traded_at = now - timedelta(hours=12) if old else now - timedelta(seconds=2)
    return MarketSummary(
        entity=Entity(entity_id="fixture", symbol="2330.TW", name="台積電", entity_type="stock", market="taiwan", exchange="TWSE", currency="TWD"),
        latest_price=PricePoint(date=traded_at.isoformat(), open=100, high=100, low=100, close=100, volume=1000),
        change_percent=0, trend="fixture", events=[], linked_factors=[],
        provider_id="twse_mis", connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
        quote_kind="last_trade", authorized=False, realtime=True, delayed=False, official_close=False,
        max_age_seconds=15, data_source="TWSE MIS fixture", data_timestamp=now.isoformat(), trading_state=state,
    )


def test_verified_price_uses_exchange_time_and_keeps_stale_data_for_research(monkeypatch):
    summary = _summary()
    monkeypatch.setattr(paper_training_api, "get_execution_price_summary", lambda _symbol: summary)
    monkeypatch.setattr(paper_training_api, "_record_runtime_slo", lambda *args, **kwargs: {})
    price = paper_training_api._verified_price("2330.TW", require_execution_quote=False)
    assert price["source_timestamp"] == summary.latest_price.date
    assert price["source_envelope"]["received_at"] == summary.data_timestamp
    assert price["is_realtime"] is False
    assert price["price"] == 100
    assert price["execution_eligibility"]["execution_eligible"] is False
    with pytest.raises(HTTPException) as exc:
        paper_training_api._verified_price("2330.TW")
    assert exc.value.status_code == 422
    assert "quote_expired" in exc.value.detail["blockers"]


@pytest.mark.parametrize("state,stale,expected", [
    ("closed", False, False), ("trading", True, False), ("trading", False, True),
])
def test_market_summary_never_labels_closed_or_stale_mis_trade_realtime(monkeypatch, state, stale, expected):
    async def quote(_symbol):
        return {"data": {
            "symbol": "2330", "exchange": "tse", "name": "台積電", "last_price": 100,
            "date": "20260911", "time": "13:30:00", "received_at": "2026-09-11T14:00:00+08:00",
            "trading_status": state, "is_stale": stale, "user_delay_ms": 5000,
            "exchange_timestamp": "2026-09-11T13:30:00.001+08:00", "market_session": "regular_lot",
            "sequence": 101, "last_trade_size_shares": 3000,
        }}

    monkeypatch.setattr(services, "fetch_twse_mis_quote", quote)
    monkeypatch.setattr(services, "_debug_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(services, "_linked_factors_for", lambda *_: [])
    monkeypatch.setattr(services, "_observe_independent_same_day_quote", lambda *args, **kwargs: {})
    monkeypatch.setattr(services, "_attach_market_summary_quality_receipt", lambda summary, **kwargs: summary)
    summary = services.get_market_summary("2330.TW", include_events=False)
    assert summary is not None
    assert summary.realtime is expected
    assert summary.quote_kind == "last_trade"
    assert summary.latest_price.close == 100
    assert summary.trading_state == state
    assert datetime.fromisoformat(summary.latest_price.date) == datetime.fromisoformat("2026-09-11T13:30:00.001+08:00")
    assert summary.source_trade == {
        "provider_id": "twse_mis", "symbol": "2330.TW", "venue": "TWSE", "channel": "regular_lot",
        "exchange_timestamp": "2026-09-11T13:30:00.001+08:00", "trade_id": "101", "price": 100, "size_shares": 3000,
    }
    monkeypatch.setattr(paper_training_api, "get_execution_price_summary", lambda _symbol: summary)
    monkeypatch.setattr(paper_training_api, "_record_runtime_slo", lambda *args, **kwargs: {})
    forwarded = paper_training_api._verified_price("2330.TW", require_execution_quote=False)
    assert forwarded["source_trade"] == summary.source_trade
    assert "odd_lot_auction_matched" not in forwarded


def test_closed_last_trade_selects_official_close_for_paper_horizon(monkeypatch):
    closed = _summary().model_copy(update={"realtime": False})
    official = closed.model_copy(update={"quote_kind": "official_close", "official_close": True})
    monkeypatch.setattr(services, "get_market_detail_summary", lambda _symbol: closed)
    monkeypatch.setattr(services, "official_summary_payload", lambda _symbol: {"latest": closed.latest_price})
    monkeypatch.setattr(services, "_observe_independent_same_day_quote", lambda *args, **kwargs: {})
    monkeypatch.setattr(services, "_summary_from_real_payload", lambda payload, **kwargs: official)
    assert services.get_execution_price_summary("2330.TW") is official


@pytest.mark.parametrize("horizon,old,state", [
    ("swing", True, "closed"), ("research", False, "trading"),
])
def test_market_hub_preserves_analysis_without_promoting_received_at_to_trade_time(monkeypatch, horizon, old, state):
    summary = _summary(old=old, state=state)
    bundle = {
        "schema_version": "stock_ai.research_data_bundle.v1", "symbol": "2330.TW", "market": "TW",
        "price": 100, "price_source": "TWSE MIS fixture", "price_timestamp": summary.data_timestamp,
        "price_is_fallback": False, "ohlcv": [], "news": [], "financials": {}, "chips": {}, "announcements": [],
        "raw": {"summary": summary.model_dump(mode="json")},
    }
    monkeypatch.setattr("open_stock_ai.data.market_data_hub.UnifiedResearchDataGateway", lambda: SimpleNamespace(load=lambda **kwargs: bundle))
    monkeypatch.setattr(MarketDataHub, "_point_in_time_dataset", staticmethod(lambda **kwargs: {}))
    monkeypatch.setattr("stock_ai.source_policy.evaluate_source_policy", lambda payload: {"overall_allowed": True, "price_policy": {"allowed": True, "blockers": []}})
    contract = MarketDataHub().load(StockRequest(symbol="2330.TW", market="TW", horizon=horizon)).raw["data_contract"]
    assert contract["exchange_timestamp"] == summary.latest_price.date
    assert contract["source_envelope"]["trading_state"] == state
    assert contract["analysis_ready"] is True
    assert contract["agent_research_allowed"] is True
    assert contract["execution_eligible"] is False
    assert contract["decision_ready"] is False


@pytest.mark.parametrize("eligible", [False, True])
def test_valuation_update_does_not_turn_ineligible_quote_into_order_fill(monkeypatch, eligible):
    price = {
        "symbol": "2330.TW", "market": "TW", "price": 100, "price_source": "fixture",
        "source_timestamp": "2026-09-10T13:30:00+08:00", "is_realtime": False, "is_fallback": True,
        "source_kind": "delayed_last_trade", "source_envelope": {"exchange_timestamp": "2026-09-10T13:30:00+08:00"},
        "execution_eligibility": {"execution_eligible": eligible, "blockers": [] if eligible else ["quote_expired"]},
    }
    mark = Mock(return_value=price)
    tick = Mock(return_value={"results": [{"status": "filled"}]})
    monkeypatch.setattr(paper_training_api, "_lab", lambda: SimpleNamespace(account_summary=lambda: {"positions": [{"symbol": "2330.TW"}]}, apply_market_mark=mark))
    monkeypatch.setattr(paper_training_api, "_broker", lambda: SimpleNamespace(open_symbols=lambda: ["2330.TW"], process_market_tick=tick))
    monkeypatch.setattr(paper_training_api, "_verified_price", lambda *args, **kwargs: price)
    monkeypatch.setattr(paper_training_api, "paper_training_account", lambda **kwargs: {})
    result = paper_training_api.paper_training_mark_to_market()
    assert result["updated_count"] == 1
    mark.assert_called_once()
    assert tick.call_count == int(eligible)
    assert result["processed_order_count"] == int(eligible)
    assert bool(result["execution_skips"]) is not eligible
