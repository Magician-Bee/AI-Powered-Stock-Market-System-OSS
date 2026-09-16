from pathlib import Path

import stock_ai.peer_comparison as subject


class FakePlatform:
    def __init__(self):
        self.items = [
            {
                "entity_id": "target",
                "symbol": "2330.TW",
                "name": "台積電",
                "exchange": "TWSE",
                "industry": "24",
                "entity_type": "stock",
                "trading_status": "active",
            },
            {
                "entity_id": "peer-a",
                "symbol": "2303.TW",
                "name": "聯電",
                "exchange": "TWSE",
                "industry": "24",
                "entity_type": "stock",
                "trading_status": "active",
            },
            {
                "entity_id": "peer-b",
                "symbol": "5347.TWO",
                "name": "世界",
                "exchange": "TPEx",
                "industry": "24",
                "entity_type": "stock",
                "trading_status": "active",
            },
            {
                "entity_id": "wrong-industry",
                "symbol": "2603.TW",
                "name": "長榮",
                "exchange": "TWSE",
                "industry": "15",
                "entity_type": "stock",
                "trading_status": "active",
            },
        ]

    def resolve_entity(self, symbol):
        item = next(row for row in self.items if row["symbol"] == symbol)
        return {
            "entity": {
                **item,
                "canonical_name": item["name"],
                "metadata": {"short_name": item["name"]},
            }
        }

    def securities(self, **_kwargs):
        return list(self.items)


def _valuation(symbol):
    values = {
        "2330.TW": (24.0, 8.0, 1.0),
        "2303.TW": (12.0, 1.5, 5.0),
        "5347.TWO": (18.0, 3.0, 3.0),
    }
    pe, pb, dividend = values[symbol]
    return {
        "date": "2026-07-29",
        "pe": pe,
        "pb": pb,
        "dividend_yield_percent": dividend,
        "source_id": "official_exchange",
        "source_url": f"https://example.test/{symbol}",
    }


def _ratios(symbol, **_kwargs):
    values = {
        "2330.TW": (58.0, 48.0, 42.0, 30.0, 20.0),
        "2303.TW": (32.0, 20.0, 18.0, 16.0, 30.0),
        "5347.TWO": (40.0, 28.0, 25.0, 22.0, 25.0),
    }
    gross, operating, net, roe, debt = values[symbol]
    return {
        "items": [{
            "period": "2026-Q1",
            "gross_margin_percent": gross,
            "operating_margin_percent": operating,
            "net_margin_percent": net,
            "roe_percent": roe,
            "debt_ratio_percent": debt,
            "source_comparison": {"periods_match": True},
        }]
    }


def test_peer_comparison_requires_exact_official_industry_and_explicit_selection():
    payload = subject.query_peer_comparison(
        "2330.TW",
        peers=["2303.TW", "2603.TW", "5347.TWO"],
        period="2026-Q1",
        platform=FakePlatform(),
        valuation_fetcher=_valuation,
        ratio_fetcher=_ratios,
    )
    assert payload["schema_version"] == subject.PEER_COMPARISON_SCHEMA_VERSION
    assert payload["status"] == "complete"
    assert payload["industry"] == "24"
    assert payload["selection"]["accepted_peers"] == ["2303.TW", "5347.TWO"]
    assert payload["selection"]["rejected_peers"] == [
        {"symbol": "2603.TW", "reason": "official_industry_mismatch"}
    ]
    assert [item["symbol"] for item in payload["companies"]] == [
        "2330.TW",
        "2303.TW",
        "5347.TWO",
    ]
    pe = next(item for item in payload["benchmarks"] if item["field"] == "pe")
    assert pe["target_value"] == 24
    assert pe["peer_median"] == 18
    assert pe["target_percentile"] == 83.33
    assert "better" in pe["interpretation"]


def test_peer_universe_discloses_deterministic_candidates_but_does_not_auto_select():
    payload = subject.query_peer_comparison(
        "2330.TW",
        peers=[],
        period="2026-Q1",
        platform=FakePlatform(),
        valuation_fetcher=_valuation,
        ratio_fetcher=_ratios,
    )
    assert payload["status"] == "selection_required"
    assert payload["companies"] == []
    assert payload["selection"]["accepted_peers"] == []
    assert payload["selection"]["candidate_count"] == 2
    assert [item["symbol"] for item in payload["selection"]["candidate_preview"]] == [
        "2303.TW",
        "5347.TWO",
    ]
    assert payload["selection"]["no_hidden_peer_selection"] is True


def test_peer_universe_uses_an_official_snapshot_while_a_new_warehouse_is_empty(monkeypatch):
    class EmptyPlatform:
        def securities(self, **_kwargs):
            return []

    companies = [
        {"公司代號": "2330", "公司簡稱": "台積電", "公司名稱": "台積電", "產業別": "24"},
        {"公司代號": "2303", "公司簡稱": "聯電", "公司名稱": "聯電", "產業別": "24"},
    ]
    monkeypatch.setattr(subject, "get_market_data_platform", lambda: EmptyPlatform())
    monkeypatch.setattr(subject, "twse_companies", lambda: companies)
    monkeypatch.setattr(subject, "twse_quotes", lambda: [{"Code": "2330"}, {"Code": "2303"}])

    payload = subject.peer_universe("2330.TW")

    assert payload["target"]["symbol"] == "2330.TW"
    assert [item["symbol"] for item in payload["candidates"]] == ["2303.TW"]
    assert payload["selection_contract"]["source"].startswith("TWSE / TPEx official")


def test_peer_comparison_ui_exposes_selection_basis_and_comparison_table():
    root = Path(subject.__file__).resolve().parent
    markup = (root / "ui/static/index.html").read_text(encoding="utf-8")
    script = (root / "ui/static/js/features/dashboard.js").read_text(
        encoding="utf-8"
    )
    assert 'id="peerComparisonPeers"' in markup
    assert 'id="loadPeerComparisonBtn"' in markup
    assert "官方證券主檔相同產業" in markup
    assert "renderPeerComparison" in script
    assert "/fundamentals/valuation/peers" in script
