from __future__ import annotations

from datetime import datetime, timezone

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
