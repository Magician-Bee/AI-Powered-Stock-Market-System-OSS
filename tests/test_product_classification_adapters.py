"""Offline adapter regressions; receipts here are fixtures, not market evidence."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from open_stock_ai.agent_runtime import AgentRunContext
from stock_ai import phase1_data
from stock_ai.agent_tools import StockAgentToolRegistry
from stock_ai.market_intelligence import api, feature_store
from stock_ai.market_intelligence.broad_scanner import BroadScanner
from stock_ai.market_intelligence.deep_analysis import select_deep_analysis_symbols
from stock_ai.market_intelligence.product_projection import product_assessment, product_counts
from stock_ai.market_intelligence.snapshot_store import SnapshotStore
from stock_ai.models import SecurityMasterItem
from product_classification_fixtures import product_fixture
from test_market_intelligence_workspace import _feature, snapshot


def _forbidden(*args, **kwargs):
    raise AssertionError("local read must not fetch or write")


@pytest.fixture
def local_universe_master(monkeypatch):
    """Exercise the real local adapter while making implicit refresh fatal."""
    rows = []
    for symbol, product_type in (("2330.TW", "ordinary_stock"), ("020000.TW", "etn")):
        identity = product_fixture(symbol, product_type)
        rows.append({"symbol": symbol, "name": f"fixture {symbol}", "market": "taiwan",
                     "exchange": "TWSE", "listing_type": "listed", "source": "local fixture",
                     "entity_id": identity["entity_id"], "trading_status": "active",
                     "product_classification": identity["product_classification"]})
    calls = []
    def securities(**kwargs):
        calls.append(kwargs)
        assert kwargs == {"query": "", "market": "all", "limit": 5000}
        return deepcopy(rows)
    platform = SimpleNamespace(securities=securities, sync_security_master=_forbidden)
    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: platform)
    for name in ("twse_companies", "tpex_companies", "twse_quotes", "tpex_quotes", "_cached_margin_rows", "clear_official_caches"):
        monkeypatch.setattr(phase1_data, name, _forbidden)
    monkeypatch.setattr(phase1_data.OfficialSecurityMasterLoader, "run", _forbidden)
    return platform, rows, calls


def test_explicit_universe_uses_local_master_and_preserves_product_identity(local_universe_master):
    from open_stock_ai.types import UniverseSnapshot

    _platform, stored, calls = local_universe_master
    universe = UniverseSnapshot(source="explicit_symbols", symbols=("020000.TW", "2330.TW", "9999.TW"))
    rows = phase1_data.securities_for_universe(universe, limit=2)
    assert [row.symbol for row in rows] == ["020000.TW", "2330.TW"]
    assert len(calls) == 1
    for row, original in zip(rows, reversed(stored)):
        assert row.entity_id == original["entity_id"]
        assert row.trading_status == original["trading_status"]
        assert row.product_classification == original["product_classification"]


@pytest.mark.parametrize("failed", [False, True])
def test_explicit_universe_missing_local_master_stays_unknown(local_universe_master, failed):
    from open_stock_ai.types import UniverseSnapshot

    platform, _stored, _calls = local_universe_master
    def unavailable(**kwargs):
        if failed:
            raise OSError("isolated local master unavailable")
        return []
    platform.securities = unavailable
    universe = UniverseSnapshot(source="explicit_symbols", symbols=("020000.TW", "8069.TWO"))
    rows = phase1_data.securities_for_universe(universe)
    assert [row.symbol for row in rows] == list(universe.symbols)
    assert [row.exchange for row in rows] == ["TWSE", "TPEx"]
    for row in rows:
        assert row.entity_id is None
        assert row.trading_status == row.listing_type == "unknown"
        assert row.product_classification["status"] == "unknown"
        assert row.product_classification["product_type"] == "unknown"
        assert "security metadata unavailable" in row.source


def test_empty_universe_does_not_read_master(local_universe_master):
    from open_stock_ai.types import UniverseSnapshot

    platform, _stored, _calls = local_universe_master
    platform.securities = _forbidden
    assert phase1_data.securities_for_universe(UniverseSnapshot.empty()) == []


def test_notification_preview_aggregators_do_not_refresh_security_master(monkeypatch, local_universe_master):
    import socket
    from stock_ai import mvp_features, services

    _platform, _stored, calls = local_universe_master
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    # Quotes/news/financial inputs remain separate operations. Only the
    # metadata lookup is under test; all other inputs are explicit fixtures.
    for module in (phase1_data, mvp_features):
        for name in ("list_institutional_flows", "list_margin_trading", "list_monthly_revenues"):
            monkeypatch.setattr(module, name, lambda **kwargs: [])
    monkeypatch.setattr(phase1_data, "latest_official_quote", lambda *_: (None, None, 0, "fixture unavailable", "TWSE"))
    monkeypatch.setattr(services, "get_market_events_brief", lambda *args, **kwargs: [])
    monkeypatch.setattr(mvp_features, "get_market_events_brief", lambda *args, **kwargs: [])
    monkeypatch.setattr(mvp_features, "get_news_center", lambda **kwargs: {"items": []})
    async def no_quotes(symbols):
        assert symbols == ["020000.TW"]
        return {}
    monkeypatch.setattr(mvp_features, "_fetch_watchlist_quotes", no_quotes)

    result = mvp_features.get_notification_previews(symbol="020000.TW")
    assert len(calls) == 2  # Both the daily report and watchlist use local metadata.
    assert result["count"] == 1
    assert result["items"][0]["related_symbols"] == ["020000.TW"]


@pytest.mark.parametrize("unavailable", [False, True])
def test_empty_or_failed_local_master_read_never_refreshes(monkeypatch, unavailable):
    def rows(**kwargs):
        assert kwargs == {"query": "no-match", "market": "taiwan", "limit": 7}
        if unavailable:
            raise OSError("fixture unavailable")
        return []

    def lifecycle():
        if unavailable:
            raise OSError("fixture unavailable")
        return {"by_entity_type": {}, "by_status": {}, "by_exchange": {}}

    platform = SimpleNamespace(securities=rows, sync_security_master=_forbidden,
                               warehouse=SimpleNamespace(path="fixture.sqlite", lifecycle_summary=lifecycle))
    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: platform)
    for name in ("twse_companies", "tpex_companies", "twse_quotes", "tpex_quotes", "_cached_margin_rows", "clear_official_caches"):
        monkeypatch.setattr(phase1_data, name, _forbidden)
    monkeypatch.setattr(phase1_data.OfficialSecurityMasterLoader, "run", _forbidden)
    assert phase1_data.list_securities_master(q="no-match", market="taiwan", limit=7, include_lifecycle=True) == []
    status = phase1_data.securities_master_status(refresh=False)
    assert status["count"] == 0
    assert status["data_platform"]["status"] == ("unavailable" if unavailable else "empty")
    assert status["data_platform"]["sync"]["status"] == "not_requested"
    assert status["refresh_policy"]["classification_refresh"] == "explicit_only"


def test_explicit_refresh_preserves_partial_source_failure(monkeypatch):
    result = {"status": "partial", "failures": [{"source_dataset": "tpex_isin_otc", "error": "TimeoutError"}]}
    platform = SimpleNamespace(warehouse=SimpleNamespace(lifecycle_summary=lambda: {}), status=lambda: {"warehouse": {}})
    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: platform)
    for name in ("twse_companies", "tpex_companies", "twse_quotes", "tpex_quotes"):
        monkeypatch.setattr(phase1_data, name, lambda: [])
    monkeypatch.setattr(phase1_data, "clear_official_caches", lambda: None)
    monkeypatch.setattr(phase1_data, "_cached_margin_rows", SimpleNamespace(cache_clear=lambda: None))
    calls = []
    def run(_self, *, force):
        calls.append(force)
        return deepcopy(result)
    monkeypatch.setattr(phase1_data.OfficialSecurityMasterLoader, "run", run)
    status = phase1_data.securities_master_status(refresh=True)
    assert calls == [True]
    assert status["data_platform"]["status"] == "partial"
    assert status["data_platform"]["sync"] == result


@pytest.mark.parametrize("with_receipt", [False, True])
def test_master_features_and_saved_snapshot_preserve_product_identity(monkeypatch, tmp_path, with_receipt):
    identity = product_fixture("2330.TW")
    row = {"symbol": "2330.TW", "name": "fixture", "market": "taiwan", "exchange": "TWSE",
           "listing_type": "listed", "source": "fixture", "entity_type": "stock",
           "entity_id": identity["entity_id"], "trading_status": "active"}
    if with_receipt:
        row["product_classification"] = identity["product_classification"]
    platform = SimpleNamespace(securities=lambda **kwargs: [row])
    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: platform)
    master = phase1_data.list_securities_master(include_lifecycle=True)
    assert isinstance(master[0], SecurityMasterItem)
    def local_master(**kwargs):
        assert kwargs["include_lifecycle"] is True
        assert kwargs["limit"] == 100000
        return master
    monkeypatch.setattr(feature_store, "list_securities_master", local_master)
    monkeypatch.setattr(feature_store, "twse_quotes", lambda: [{"Code": "2330", "Date": "2026-09-11", "ClosingPrice": "100", "TradeVolume": "10000"}])
    monkeypatch.setattr(feature_store, "tpex_quotes", lambda: [])
    def local_optional(**kwargs):
        assert kwargs["allow_network"] is False
        return []
    monkeypatch.setattr(feature_store, "list_monthly_revenues", local_optional)
    monkeypatch.setattr(feature_store, "list_institutional_flows", local_optional)
    monkeypatch.setattr(feature_store, "attach_current_source_inputs", lambda rows, **kwargs: rows)
    features = feature_store.load_all_taiwan_features()
    assert features[0]["lifecycle_status"] == "active"
    scanned = BroadScanner(feature_loader=lambda: features, feature_enricher=lambda rows: rows, position_loader=lambda: {}).scan()
    current = snapshot()
    current.candidate_details = scanned["candidate_details"]
    current.rankings = scanned["rankings"]
    current.portfolio_actions = scanned["portfolio_actions"]
    current.universe.resolved_count = len(features)
    current.universe.valid_data_count = len(features)
    store = SnapshotStore(tmp_path / "adapter.sqlite")
    store.save_snapshot(current)
    restored = store.snapshot(current.snapshot_id)
    assert restored is not None
    candidate = restored.candidate_details["2330.TW"]
    expected = identity["product_classification"] if with_receipt else master[0].product_classification
    assert candidate.product_classification == expected
    assert candidate.lifecycle_status == "active"
    projected = api._bootstrap_snapshot(restored)
    assert projected["candidate_details"]["2330.TW"]["product_classification"] == expected
    assert projected["universe"]["investable_count"] == int(with_receipt)
    assert product_assessment(candidate)["classification_verified"] is with_receipt


def test_all_product_types_remain_researchable_and_entry_counts_use_receipts():
    kinds = ["ordinary_stock", "etf", "etn", "depositary_receipt", "preferred_stock", "warrant", "bond", "other", "unknown"]
    features = []
    for index, kind in enumerate(kinds):
        row = _feature(f"{2000 + index}.TW")
        row.update(product_fixture(row["symbol"], kind))
        # Legacy flags intentionally disagree with product receipts.
        row.update(is_etf=False, is_warrant=False, is_special_security=False)
        if kind == "unknown":
            row["product_classification"]["status"] = "unknown"
        features.append(row)
    result = BroadScanner(feature_loader=lambda: features, feature_enricher=lambda rows: rows, position_loader=lambda: {}).scan()
    assert len(result["features"]) == len(kinds)
    assert set(result["candidate_details"]) == {row["symbol"] for row in features}
    counts = result["universe_breakdown"]
    assert counts["investable_count"] == counts["ordinary_stock_count"] == 1
    # Bond has no reviewed official section/CFI mapping yet. A fixture label
    # cannot certify that product; its research row remains visible as unknown.
    assert counts["product_type_counts"] == {**dict.fromkeys(kinds, 1), "bond": 0, "unknown": 2}
    assert counts["product_classification_status_counts"] == {"verified": 7, "unknown": 2, "conflict": 0}
    assert counts["product_classification_reason_counts"]["product_type_not_supported"] == 8
    assert set(select_deep_analysis_symbols(result)) == set(result["candidate_details"])
    assert len(select_deep_analysis_symbols({"rankings": {"insufficient_data": [str(i) for i in range(40)]}}, limit=999)) == 20


def test_stale_legacy_flags_cannot_override_verified_ordinary_receipt():
    row = _feature("2330.TW")
    def scan(value):
        return BroadScanner(feature_loader=lambda: [value], feature_enricher=lambda rows: rows, position_loader=lambda: {}).scan()
    baseline = scan(row)
    changed = deepcopy(row)
    changed.update(is_warrant=True, is_etf=True, is_special_security=True, is_managed_stock=True)
    result = scan(changed)
    assert result["universe_breakdown"] == baseline["universe_breakdown"]
    assert result["rankings"] == baseline["rankings"]
    assert result["candidate_details"][row["symbol"]].host_risk_status == "passed"


@pytest.mark.parametrize("segment,venue,symbol", [("innovation", "TWSE", "2000.TW"), ("emerging", "TPEx-ESB", "2000.TWO")])
def test_verified_ordinary_type_is_distinct_from_entry_eligible_segment(segment, venue, symbol):
    row = {"symbol": symbol, **product_fixture(symbol, venue=venue, segment=segment)}
    counts = product_counts([row])
    assert counts["ordinary_stock_count"] == 1
    assert counts["product_classification_status_counts"]["verified"] == 1
    assert counts["investable_count"] == 0
    assert "product_segment_not_supported" in product_assessment(row)["reasons"]


@pytest.mark.parametrize("invalid", ["stale", "conflict", "wrong_hash", "wrong_symbol"])
def test_unverified_receipts_never_inherit_legacy_stock_eligibility(invalid):
    now = datetime.now(timezone.utc)
    row = _feature("2330.TW")
    if invalid == "stale":
        row.update(product_fixture("2330.TW", now=now - timedelta(days=8)))
    elif invalid == "conflict":
        row["product_classification"]["status"] = "conflict"
    elif invalid == "wrong_hash":
        row["product_classification"]["row_sha256"] = "0" * 64
    else:
        row["product_classification"]["symbol"] = "2000.TW"
    before = deepcopy(row)
    counts = product_counts([row], now=now)
    assert counts["ordinary_stock_count"] == counts["investable_count"] == 0
    assert counts["product_classification_status_counts"]["conflict" if invalid == "conflict" else "unknown"] == 1
    assert row == before


def test_legacy_saved_snapshot_zero_counts_stay_zero_and_research_navigation_stays_visible(tmp_path, monkeypatch):
    legacy = snapshot().model_dump(mode="json")
    for item in legacy["candidate_details"].values():
        for field in ("product_classification", "product_entry_assessment", "lifecycle_status"):
            item.pop(field, None)
        item.update(entity_id=None, category="actionable_now", market_category="BUY_NOW", host_risk_status="passed")
    legacy["universe"].update(ordinary_stock_count=0, investable_count=0, resolved_count=2)
    legacy["rankings"] = {key: list(legacy["candidate_details"]) if key == "actionable_now" else []
                          for key in legacy["rankings"]}
    current = type(snapshot()).model_validate(legacy)
    before = current.model_dump(mode="json")
    store = SnapshotStore(tmp_path / "legacy.sqlite")
    store.save_snapshot(current)
    restored = store.snapshot(current.snapshot_id)
    payload = api._bootstrap_snapshot(restored)
    assert payload["universe"]["investable_count"] == payload["universe"]["ordinary_stock_count"] == 0
    assert payload["universe"]["resolved_count"] == 2
    assert payload["classification_counts"]["BUY_NOW"] == 0
    assert payload["ranking_counts"]["insufficient_data"] == 2
    assert len(api._market_rankings(restored)["volume"]) == 2
    assert api._market_decision_lists(restored)["buy_now"] == []
    assert len(api._saved_navigation_items(restored, [{"symbol": "1111.TW"}])) == 1
    monkeypatch.setattr(api, "_service", lambda: SimpleNamespace(latest_snapshot=lambda: restored, snapshot=lambda key: restored))
    assert api.latest_intelligence_snapshot(ensure=False)["classification_counts"]["BUY_NOW"] == 0
    assert api.intelligence_snapshot(restored.snapshot_id)["universe"]["investable_count"] == 0
    assert api.instrument_intelligence("1111.TW")["detail"]["market_category"] == "INSUFFICIENT_DATA"
    assert api.instrument_evidence("1111.TW")["host_risk_status"] == "blocked"
    assert current.model_dump(mode="json") == before
    assert restored.model_dump(mode="json") == before


@pytest.mark.parametrize("kind", ["etf", "depositary_receipt", "unknown"])
@pytest.mark.parametrize("change,expected", [(-4, "reduce"), (-8, "exit")])
def test_unsupported_held_products_keep_reduce_and_exit_visible(kind, change, expected):
    row = _feature("2330.TW", change=change)
    row.update(product_fixture(row["symbol"], kind))
    if kind == "unknown":
        row["product_classification"] = {}
    scan = BroadScanner(feature_loader=lambda: [row], feature_enricher=lambda rows: rows,
                        position_loader=lambda: {row["symbol"]: {"quantity": 1000, "average_cost": 100, "weight_percent": 5}}).scan()
    assert scan["portfolio_actions"][expected] == [row["symbol"]]
    current = snapshot()
    current.candidate_details = scan["candidate_details"]
    current.rankings = scan["rankings"]
    current.portfolio_actions = scan["portfolio_actions"]
    assert api._portfolio_views(current)[expected][0]["symbol"] == row["symbol"]
    assert api._market_decision_lists(current)["sell_reduce"][0]["portfolio_action"] == expected
    assert api._saved_navigation_items(current, [{"symbol": row["symbol"]}])[0]["symbol"] == row["symbol"]


@pytest.mark.parametrize("refresh", [False, True])
def test_agent_search_only_refreshes_explicitly_and_then_reads_local(monkeypatch, refresh):
    calls = []
    def status(**kwargs):
        calls.append(("status", kwargs))
        return {"data_platform": {"status": "empty"}}
    def local(**kwargs):
        calls.append(("read", kwargs))
        assert kwargs["include_lifecycle"] is True
        assert kwargs["limit"] == 7
        return []
    monkeypatch.setattr("stock_ai.agent_tools.securities_master_status", status)
    monkeypatch.setattr("stock_ai.agent_tools.list_securities_master", local)
    asyncio.run(StockAgentToolRegistry().execute("market.search_taiwan_securities",
                {"query": "no-match", "limit": 7, "refresh": refresh},
                AgentRunContext(run_id="product-adapter-fixture", autonomy="advisory", symbols=())))
    assert calls[0] == ("status", {"refresh": refresh})
    assert [entry[0] for entry in calls] == ["status", "read"]
