from types import SimpleNamespace

from stock_ai import phase1_data
from stock_ai import services
from stock_ai.models import SecurityMasterItem


def _lifecycle():
    return {
        "by_entity_type": {"stock": 2, "warrant": 3, "security": 1},
        "by_status": {"active": 2, "unknown": 3, "pre_listing": 1},
        "by_exchange": {"TWSE": 3, "TPEx": 2, "TPEx-ESB": 1},
    }


def test_explicit_refresh_enters_isolated_loader_without_eager_source_fetch(monkeypatch):
    warehouse = SimpleNamespace(path="/tmp/market.sqlite", lifecycle_summary=_lifecycle)
    platform = SimpleNamespace(warehouse=warehouse)
    calls = []
    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: platform)
    monkeypatch.setattr(phase1_data, "clear_official_caches", lambda: calls.append("clear"))
    monkeypatch.setattr(phase1_data._cached_margin_rows, "cache_clear", lambda: calls.append("margin"))
    monkeypatch.setattr(
        phase1_data.OfficialSecurityMasterLoader,
        "run",
        lambda self, **kwargs: calls.append(kwargs) or {"status": "partial", "partitions": []},
    )
    for name in ("twse_companies", "twse_quotes", "tpex_companies", "tpex_quotes"):
        monkeypatch.setattr(phase1_data, name, lambda: (_ for _ in ()).throw(
            AssertionError("refresh path must not fetch before the isolated loader")))

    result = phase1_data.securities_master_status(refresh=True)

    assert calls == ["clear", "margin", {"force": True}]
    assert result["count"] == 6
    assert result["by_exchange"] == {"TWSE": 3, "TPEx": 2, "TPEx-ESB": 1}
    assert result["active"] == 2 and result["pending_quote"] == 1
    assert result["data_platform"]["status"] == "partial"
    assert result["data_platform"]["warehouse"]["tables"] == {"entities": 6}


def test_loader_failure_reports_retained_master_without_false_success(monkeypatch):
    warehouse = SimpleNamespace(path="/tmp/market.sqlite", lifecycle_summary=_lifecycle)
    platform = SimpleNamespace(warehouse=warehouse)
    monkeypatch.setattr(phase1_data, "get_market_data_platform", lambda: platform)
    monkeypatch.setattr(phase1_data, "clear_official_caches", lambda: None)
    monkeypatch.setattr(phase1_data._cached_margin_rows, "cache_clear", lambda: None)
    monkeypatch.setattr(
        phase1_data.OfficialSecurityMasterLoader,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("source failed")),
    )

    result = phase1_data.securities_master_status(refresh=True)

    assert result["count"] == 6 and result["active"] == 2
    assert result["data_platform"]["status"] == "failed"
    assert result["data_platform"]["sync"] == {"status": "failed"}
    assert result["data_platform"]["error"]["type"] == "RuntimeError"


def test_entity_search_discovers_persisted_identity_without_quote_source(monkeypatch):
    stored = SecurityMasterItem(
        entity_id="ENT-" + "d" * 32,
        symbol="00838B.TWO",
        name="離線債券 ETF",
        market="taiwan",
        exchange="TPEx",
        listing_type="etf",
        entity_type="etf",
        lifecycle_status="unknown",
        product_classification={"product_type": "etf"},
        source="Unified Market Warehouse security_master",
    )
    monkeypatch.setattr(services, "list_securities_master", lambda **kwargs: [stored])
    monkeypatch.setattr(services, "search_taiwan_official", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("persisted match must not fetch a quote source")))

    result = services.search_entities("00838B")

    assert len(result) == 1
    assert result[0].entity_id == stored.entity_id
    assert result[0].symbol == "00838B.TWO"
    assert result[0].entity_type == "etf" and result[0].is_active is False
    assert result[0].lifecycle_status == "unknown"
