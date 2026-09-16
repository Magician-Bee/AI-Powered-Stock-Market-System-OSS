from __future__ import annotations

from datetime import datetime, timezone

import pytest

from open_stock_ai.data.execution_quote import execution_eligibility, quote_envelope


NOW = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)


def test_intraday_requires_authorized_realtime_last_trade():
    public_mis = quote_envelope(
        provider_id="twse_mis",
        connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
        quote_kind="last_trade",
        exchange_timestamp="2026-07-16T08:59:58+08:00",
        received_at="2026-07-16T00:59:59+00:00",
        max_age_seconds=15,
        authorized=False,
        realtime=True,
        delayed=False,
        official_close=False,
    )

    intraday = execution_eligibility(public_mis, horizon="intraday", now=NOW)
    swing = execution_eligibility(public_mis, horizon="swing", now=NOW)

    assert intraday["execution_eligible"] is False
    assert "intraday_requires_authorized_realtime" in intraday["blockers"]
    assert swing["execution_eligible"] is True


def test_official_close_is_swing_eligible_but_never_intraday_eligible():
    close = quote_envelope(
        provider_id="twse_openapi",
        connector_id="stock_ai.taiwan_official.official_summary_payload",
        quote_kind="official_close",
        exchange_timestamp="2026-07-15",
        received_at="2026-07-16T00:30:00+00:00",
        max_age_seconds=172800,
        authorized=True,
        realtime=False,
        delayed=False,
        official_close=True,
    )

    assert execution_eligibility(close, horizon="swing", now=NOW)["execution_eligible"] is True
    assert execution_eligibility(close, horizon="intraday", now=NOW)["execution_eligible"] is False


def test_tampered_or_expired_source_envelope_is_blocked():
    envelope = quote_envelope(
        provider_id="twse_mis",
        connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
        quote_kind="last_trade",
        exchange_timestamp="2026-07-16T08:00:00+08:00",
        received_at="2026-07-16T00:00:00+00:00",
        max_age_seconds=15,
        authorized=True,
        realtime=True,
        delayed=False,
        official_close=False,
    )
    envelope["connector_id"] = "forged.connector"

    result = execution_eligibility(envelope, horizon="intraday", now=NOW)

    assert result["execution_eligible"] is False
    assert "connector_identity_mismatch" in result["blockers"]
    assert "source_envelope_signature_mismatch" in result["blockers"]
    assert "quote_expired" in result["blockers"]


def _trade(**overrides):
    values = dict(
        provider_id="twse_mis", connector_id="stock_ai.realtime_quotes.fetch_twse_mis_quote",
        quote_kind="last_trade", exchange_timestamp="2026-07-16T08:59:58+08:00",
        received_at=NOW.isoformat(), max_age_seconds=15,
        authorized=True, realtime=True, delayed=False, official_close=False, trading_state="trading",
    )
    return quote_envelope(**{**values, **overrides})


@pytest.mark.parametrize("horizon", ["intraday", "swing", "weekly", "monthly", "after_market"])
def test_receiving_an_old_trade_again_never_renews_execution_freshness(horizon):
    envelope = _trade(exchange_timestamp="2026-07-15T21:00:00+08:00")
    result = execution_eligibility(envelope, horizon=horizon, now=NOW)
    assert result["execution_eligible"] is False
    assert result["research_eligible"] is True
    assert result["age_seconds"] == 12 * 3600
    assert result["freshness_basis"] == "exchange_timestamp"
    assert "quote_expired" in result["blockers"]


@pytest.mark.parametrize("timestamp, blocker", [
    (None, "quote_timestamp_invalid"),
    ("invalid", "quote_timestamp_invalid"),
    ("2026-07-16", "last_trade_requires_exchange_time"),
    ("2026-07-16T10:00:00+08:00", "quote_timestamp_in_future"),
])
def test_unusable_exchange_time_cannot_fall_back_to_recent_reception(timestamp, blocker):
    result = execution_eligibility(_trade(exchange_timestamp=timestamp), horizon="swing", now=NOW)
    assert result["execution_eligible"] is False
    assert result["research_eligible"] is True
    assert blocker in result["blockers"]


def test_compact_taiwan_exchange_clock_is_interpreted_in_taipei_not_utc():
    result = execution_eligibility(_trade(exchange_timestamp="20260716 08:59:58"), horizon="intraday", now=NOW)
    assert result["execution_eligible"] is True
    assert result["age_seconds"] == 2
    assert result["exchange_timestamp"] == "2026-07-16T08:59:58+08:00"


@pytest.mark.parametrize("state", ["closed", "halted", "pre_open", "trial", "delayed_open"])
def test_nontrading_state_cannot_be_overridden_by_realtime_boolean(state):
    result = execution_eligibility(_trade(trading_state=state), horizon="swing", now=NOW)
    assert result["execution_eligible"] is False
    assert result["research_eligible"] is True
    assert "last_trade_market_not_open" in result["blockers"]


def test_old_official_close_keeps_its_close_date_and_remains_research_eligible():
    close = _trade(
        provider_id="twse_openapi", connector_id="stock_ai.taiwan_official.official_summary_payload",
        quote_kind="official_close", exchange_timestamp="2026-07-01", max_age_seconds=345600,
        official_close=True, realtime=False, trading_state="closed",
    )
    result = execution_eligibility(close, horizon="after_market", now=NOW)
    assert result["execution_eligible"] is False
    assert "quote_expired" in result["blockers"]
    assert result["research_eligible"] is True


def test_research_horizon_and_yahoo_never_become_executable_from_flags():
    assert execution_eligibility(_trade(), horizon="research", now=NOW)["execution_eligible"] is False
    yahoo = _trade(provider_id="yahoo", connector_id="stock_ai.yahoo_data.fetch_yahoo_summary")
    result = execution_eligibility(yahoo, horizon="swing", now=NOW)
    assert result["execution_eligible"] is False
    assert "provider_quote_kind_not_execution_capable" in result["blockers"]
    assert result["research_eligible"] is True
