"""Concurrent home readers share source work without weakening admission."""
from concurrent.futures import Future, ThreadPoolExecutor
import json
from threading import Condition, Event

import pytest

from open_stock_ai.agent_runtime.transport_guard import ExternalTransportAdmissionError, ExternalTransportGuard
# Match application initialization: the data platform exports this source loader.
import stock_ai.data_platform
from stock_ai import taiwan_official as official


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(official, "_CACHE", {})
    monkeypatch.setattr(official, "_CACHE_LOADED_AT", {})
    monkeypatch.setattr(official, "_CACHE_IN_FLIGHT", {})


def followers(monkeypatch, count):
    """Deterministically observe joined callers, without timing sleeps."""
    joined = 0
    condition = Condition()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            nonlocal joined
            with condition:
                joined += 1
                condition.notify_all()
            return super().result(timeout)

    monkeypatch.setattr(official, "Future", ObservedFuture)

    def wait():
        with condition:
            assert condition.wait_for(lambda: joined >= count, timeout=3), "Callers did not coalesce"
    return wait


def test_concurrent_company_loads_share_one_real_guard_admission(monkeypatch):
    guard = ExternalTransportGuard()  # Unchanged default: one request per source scope.
    monkeypatch.setattr(official, "default_external_transport_guard", lambda: guard)
    wait_followers = followers(monkeypatch, 5)
    entered, release = Event(), Event()
    calls = []
    payload = [{"公司代號": "2330", "公司名稱": "Official fixture"}]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    def fetch(request, **kwargs):
        calls.append(request.full_url)
        entered.set()
        assert release.wait(3)
        return Response()

    monkeypatch.setattr(official, "urlopen", fetch)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = [pool.submit(official.twse_companies)]
        try:
            assert entered.wait(3)
            results.extend(pool.submit(official.twse_companies) for _ in range(5))
            wait_followers()
            # A genuinely separate source request remains subject to the guard.
            with pytest.raises(ExternalTransportAdmissionError, match="maximum_concurrency_reached"):
                official._get_json(official.TWSE_COMPANIES)
        finally:
            release.set()
        assert all(result.result(timeout=3) == payload for result in results)
    assert calls == [official.TWSE_COMPANIES]
    assert official.twse_companies() == payload
    assert len(calls) == 1
    assert official.official_cache_status()["loaded_at"].get("twse_companies")


class LoaderInterrupted(BaseException):
    pass


@pytest.mark.parametrize("error", [TimeoutError("source timed out"), LoaderInterrupted("cancelled")])
def test_shared_errors_release_every_waiter_and_allow_later_retry(monkeypatch, error):
    wait_followers = followers(monkeypatch, 3)
    entered, release = Event(), Event()
    calls = []

    def load():
        calls.append("load")
        entered.set()
        assert release.wait(3)
        raise error

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [pool.submit(official._cached, ("failed",), 30, load)]
        try:
            assert entered.wait(3)
            results.extend(pool.submit(official._cached, ("failed",), 30, load) for _ in range(3))
            wait_followers()
        finally:
            release.set()
        for result in results:
            with pytest.raises(type(error)) as caught:
                result.result(timeout=3)
            assert caught.value is error
    assert calls == ["load"]
    assert official.official_cache_status() == {"loaded_at": {}, "entry_count": 0}
    assert official._cached(("failed",), 30, lambda: ["recovered"]) == ["recovered"]


def test_uncached_empty_result_is_shared_but_next_call_reloads(monkeypatch):
    wait_followers = followers(monkeypatch, 2)
    entered, release = Event(), Event()
    calls = []

    def empty():
        calls.append("load")
        entered.set()
        assert release.wait(3)
        return []

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = [pool.submit(official._cached, ("empty",), 30, empty, cache_empty=False)]
        try:
            assert entered.wait(3)
            results.extend(pool.submit(official._cached, ("empty",), 30, empty, cache_empty=False) for _ in range(2))
            wait_followers()
        finally:
            release.set()
        assert all(result.result(timeout=3) == [] for result in results)
    assert calls == ["load"]
    assert official.official_cache_status()["entry_count"] == 0
    assert official._cached(("empty",), 30, lambda: ["fresh"], cache_empty=False) == ["fresh"]


def test_different_keys_can_load_while_another_key_is_pending():
    entered, release = Event(), Event()

    def slow():
        entered.set()
        assert release.wait(3)
        return "first"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(official._cached, ("first",), 30, slow)
        try:
            assert entered.wait(3)
            second = pool.submit(official._cached, ("second",), 30, lambda: "second")
            assert second.result(timeout=2) == "second"
        finally:
            release.set()
        assert first.result(timeout=3) == "first"


def test_clearing_during_load_preserves_coalescing_without_repopulating_cache(monkeypatch):
    wait_followers = followers(monkeypatch, 1)
    entered, release = Event(), Event()

    def slow():
        entered.set()
        assert release.wait(3)
        return "before-clear"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(official._cached, ("cleared",), 30, slow)
        try:
            assert entered.wait(3)
            official.clear_official_caches()
            second = pool.submit(official._cached, ("cleared",), 30, lambda: pytest.fail("duplicate active transport"))
            wait_followers()
        finally:
            release.set()
        assert first.result(timeout=3) == second.result(timeout=3) == "before-clear"
    assert official.official_cache_status() == {"loaded_at": {}, "entry_count": 0}
    assert official._cached(("cleared",), 30, lambda: "after-clear") == "after-clear"


def test_expired_entry_refreshes_instead_of_returning_old_data():
    official._CACHE[("expired",)] = (official.time.monotonic() - 1, "old")
    assert official._cached(("expired",), 30, lambda: "new") == "new"
