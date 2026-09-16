"""Cooperative cancellation with isolated bytes, files and worker threads."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Event
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from open_stock_ai.research import official_candle_loader as loader
from stock_ai import autonomous_trading_service as wiring
from test_autonomous_campaign import NOW, setup


def official_bytes(url):
    query = parse_qs(urlparse(url).query)
    month = query["date"][0][:6]
    code = query["stockNo"][0]
    return json.dumps({"stat": "OK", "date": month + "01", "title": f"{code} fixture",
                      "data": [[f"{int(month[:4]) - 1911}/{month[4:]}/04", "1000", "100000",
                                "100", "101", "99", "100"]]}).encode()


def load_range(tmp_path, fetch, *, stop_event=None):
    return loader.load_official_candles("2330.TW", start="2021-01-01", end="2021-03-31",
                                       cache_dir=tmp_path, fetch_bytes=fetch, stop_event=stop_event)


def test_cancelled_before_start_does_not_fetch_or_create_cache(tmp_path):
    stop = Event()
    stop.set()
    calls = []
    with pytest.raises(asyncio.CancelledError, match="official_history_cancelled"):
        load_range(tmp_path / "not-created", lambda *args: calls.append(args), stop_event=stop)
    assert calls == [] and not (tmp_path / "not-created").exists()


def test_stop_signal_does_not_accept_an_arbitrary_callback(tmp_path):
    # The Host passes a concrete threading.Event; there is no callback whose
    # exception could be swallowed as an ordinary monthly source failure.
    calls = []
    with pytest.raises(TypeError, match="stop_event_must_be_threading_event"):
        load_range(tmp_path, lambda *args: calls.append(args), stop_event=lambda: False)
    assert calls == []


@pytest.mark.parametrize("rejected", [False, True])
def test_cancel_after_received_month_keeps_its_raw_evidence_but_returns_no_partial_success(tmp_path, rejected):
    stop = Event()
    calls, received = [], []

    def fetch(url, timeout):
        calls.append(url)
        raw = b"not-json-fixture" if rejected else official_bytes(url)
        received.append(raw)
        stop.set()
        return raw

    with pytest.raises(asyncio.CancelledError, match="official_history_cancelled"):
        load_range(tmp_path, fetch, stop_event=stop)
    assert len(calls) == 1
    assert parse_qs(urlparse(calls[0]).query)["date"] == ["20210301"]
    if rejected:
        path = tmp_path / "rejected-responses" / (hashlib.sha256(received[0]).hexdigest() + ".bin")
        assert path.read_bytes() == received[0]
        assert not list(tmp_path.glob("2330.TW-*.json"))
    else:
        cached = json.loads((tmp_path / "2330.TW-202103.json").read_text())
        assert cached["raw_json"].encode() == received[0]
        assert cached["raw_sha256"] == hashlib.sha256(received[0]).hexdigest()
        assert len(list(tmp_path.glob("2330.TW-*.json"))) == 1
        # Standalone calls without a stop signal retain their old cache path.
        result = loader.load_official_candles("2330.TW", start="2021-03-01", end="2021-03-31",
            cache_dir=tmp_path, fetch_bytes=lambda *args: pytest.fail("accepted cache should be reused"))
        assert result["coverage_complete"] and result["source_request_receipts"][0]["cache_hit"]


def test_cancel_after_transport_error_does_not_attempt_next_month(tmp_path):
    stop, calls = Event(), []

    def fetch(url, timeout):
        calls.append(url)
        stop.set()
        raise OSError("offline source unavailable")

    with pytest.raises(asyncio.CancelledError):
        load_range(tmp_path, fetch, stop_event=stop)
    assert len(calls) == 1


def test_cancellation_during_final_receipt_hash_cannot_return_a_completed_range(tmp_path, monkeypatch):
    stop = Event()
    original_hash = loader.content_hash

    def hash_and_cancel(value):
        result = original_hash(value)
        stop.set()
        return result

    monkeypatch.setattr(loader, "content_hash", hash_and_cancel)
    with pytest.raises(asyncio.CancelledError):
        load_range(tmp_path, lambda url, timeout: official_bytes(url), stop_event=stop)
    # Completed monthly evidence remains retained even though the range's
    # final summary must not report success after the cancellation signal.
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_cancel_between_native_redirect_and_legacy_retry_never_dispatches_retry(tmp_path, monkeypatch):
    stop, calls = Event(), []
    monkeypatch.setattr(loader, "_NEXT_NETWORK_REQUEST_AT", 0)

    def open_fixture(request, *, timeout):
        calls.append((request.full_url, timeout))
        stop.set()
        raise HTTPError(request.full_url, 308, "offline redirect", {}, None)

    monkeypatch.setattr(loader, "urlopen", open_fixture)
    with pytest.raises(asyncio.CancelledError):
        loader.load_official_candles("2330.TW", start="2021-01-01", end="2021-03-31",
                                     cache_dir=tmp_path, stop_event=stop, timeout_seconds=8)
    assert len(calls) == 1 and "/rwd/" in calls[0][0] and calls[0][1] == 8


def test_cancellation_wakes_rate_limit_wait_without_consuming_or_lowering_admission(monkeypatch):
    stop, waiting = Event(), Event()
    # Keep this test's worker waiting until the test signals cancellation;
    # no request is made and the production 0.25-second interval is unchanged.
    admission = loader.clock.monotonic() + 5
    monkeypatch.setattr(loader, "_NEXT_NETWORK_REQUEST_AT", admission)
    original_wait = stop.wait

    def wait(timeout):
        waiting.set()
        return original_wait(timeout)

    monkeypatch.setattr(stop, "wait", wait)
    monkeypatch.setattr(loader, "urlopen", lambda *args, **kwargs: pytest.fail("cancelled admission must not open a URL"))
    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(loader._fetch_once, "https://www.twse.com.tw/fixture", 8, stop_event=stop)
        try:
            assert waiting.wait(1)
            stop.set()
            with pytest.raises(asyncio.CancelledError):
                worker.result(timeout=1)
        finally:
            stop.set()
    assert loader._NEXT_NETWORK_REQUEST_AT == admission
    assert loader._NETWORK_REQUEST_INTERVAL_SECONDS == .25


def test_cached_months_also_propagate_cancellation_without_overwriting_cache(tmp_path, monkeypatch):
    original = load_range(tmp_path, lambda url, timeout: official_bytes(url))
    assert original["coverage_complete"]
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    stop = Event()
    parse = loader.parse_official_month
    observed = []

    def cancel_after_parse(payload, **kwargs):
        observed.append(kwargs["month"])
        result = parse(payload, **kwargs)
        stop.set()
        return result

    monkeypatch.setattr(loader, "parse_official_month", cancel_after_parse)
    with pytest.raises(asyncio.CancelledError):
        load_range(tmp_path, lambda *args: pytest.fail("cached cancellation must not fetch"), stop_event=stop)
    assert observed == ["202103"]
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == before


def test_cancelled_campaign_history_signals_worker_and_never_starts_next_symbol(tmp_path, monkeypatch):
    real_load = loader.load_official_candles
    release, finished = Event(), Event()
    calls, signals, settings = [], [], []

    async def scenario():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fetch(url, timeout):
            calls.append(url)
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2), "offline request fixture was not released"
            return official_bytes(url)

        def tracked_load(symbol, **kwargs):
            signals.append(kwargs["stop_event"])
            settings.append((symbol, kwargs["max_months"], kwargs["timeout_seconds"]))
            kwargs["cache_dir"] = tmp_path / "raw-cache"
            try:
                return real_load(symbol, **kwargs, fetch_bytes=fetch)
            finally:
                finished.set()

        monkeypatch.setattr(wiring, "load_official_candles", tracked_load)
        service, _ = setup(tmp_path)
        service.history_loader = wiring._history
        task = asyncio.create_task(service.research(now=NOW, deep_limit=2))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert signals[0].is_set()
            release.set()
            assert await asyncio.to_thread(finished.wait, 1)
            assert len(calls) == 1 and len(settings) == 1
            assert settings[0][1:] == (50, 8)
            assert len(list((tmp_path / "raw-cache").glob("*.json"))) == 1
            assert service.status()["latest_cycle_id"] is None
            assert service.status()["last_research_at"] is None
            assert service.broker.submissions == []
            with service.plans.store._connect() as conn:
                assert conn.execute("select count(*) from autonomous_research_cycles").fetchone()[0] == 0
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
