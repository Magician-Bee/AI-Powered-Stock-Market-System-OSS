from __future__ import annotations

from stock_ai.market_intelligence.candidate_ranker import rank_candidates
from stock_ai.market_intelligence.broad_scanner import BroadScanner
from stock_ai.market_intelligence.scanner_factor_enrichment import attach_current_source_inputs, enrich_features


class _Platform:
    def __init__(self, rows_by_domain: dict[str, list[dict]]) -> None:
        self.rows_by_domain = rows_by_domain

    def standard_query(self, domain: str, **_kwargs):
        return self.rows_by_domain.get(domain, [])


def _row(domain: str, record: dict, *, source_id: str = "twse_official_web") -> dict:
    return {
        "domain": domain,
        "entity_id": "ENT-2330",
        "source_id": source_id,
        "revision_id": f"REV-{domain}",
        "available_at": "2026-08-20T07:00:00+00:00",
        "effective_at": "2026-08-20T00:00:00+00:00",
        "record": record,
        "availability_contract_snapshot": {
            "contract": {"production_contract_covered": True},
            "decision": {
                "available_at": "2026-08-20T07:00:00+00:00",
                "historical_pit_eligible": True,
            },
        },
    }


def _feature() -> dict:
    return {
        "symbol": "2330.TW", "entity_id": "ENT-2330", "name": "台積電", "exchange": "TWSE",
        "industry": "半導體", "is_etf": False, "is_warrant": False, "is_managed_stock": False,
        "is_special_security": False, "close": 1000.0, "high": 1010.0, "low": 990.0,
        "change_percent": 2.0, "volume": 1_000_000, "trade_value": 1_000_000_000,
        "range_position": 0.9, "data_quality": {"status": "ready", "score": 1, "source": "fixture"}, "evidence": [],
    }


def test_scanner_enrichment_uses_persisted_available_warehouse_factors():
    platform = _Platform({
        "financials": [_row("financials", {"revenue_yoy": 20}), _row("financials", {"pe": 10})],
        "flows": [_row("flows", {"foreign_net": 1200})],
        "events": [_row("events", {"sentiment": "positive"})],
    })

    enriched = enrich_features([_feature()], platform=platform, as_of="2026-08-20T08:00:00+00:00")[0]
    factors = enriched["scanner_factor_inputs"]

    assert factors["fundamental"]["value"] == 66.67
    assert factors["valuation"]["value"] == 85.71
    assert factors["chip"]["value"] == 75.0
    assert factors["event"]["value"] == 75.0
    assert all(item["historical_pit_eligible"] is True for item in factors.values())

    detail = rank_candidates([enriched], market_regime="neutral")[0]["2330.TW"]
    assert detail.factor_scores["fundamental_score"]["status"] == "ready"
    assert detail.factor_scores["valuation_score"]["revision_id"] == "REV-financials"
    assert detail.score > 0


def test_scanner_enrichment_rejects_future_contract_decisions():
    future = _row("financials", {"revenue_yoy": 30})
    future["available_at"] = "2026-08-21T07:00:00+00:00"
    future["availability_contract_snapshot"]["decision"]["available_at"] = future["available_at"]
    enriched = enrich_features([_feature()], platform=_Platform({"financials": [future]}), as_of="2026-08-20T08:00:00+00:00")[0]

    assert enriched["scanner_factor_inputs"]["fundamental"]["status"] == "unavailable"
    assert enriched["scanner_factor_inputs"]["fundamental"]["reason"] == "no_available_warehouse_factor"


def test_current_official_bulk_inputs_are_visible_but_not_promoted_to_pit():
    current = attach_current_source_inputs(
        [_feature()],
        revenues=[{"symbol": "2330.TW", "yoy_change_percent": 20, "report_date": "2026-08-20", "source": "TWSE OpenAPI revenue"}],
        institutional_flows=[{"symbol": "2330.TW", "foreign_net": 12_000, "trade_date": "2026-08-20", "source": "TWSE official T86"}],
    )[0]

    fundamental = current["scanner_factor_inputs"]["fundamental"]
    chip = current["scanner_factor_inputs"]["chip"]
    assert fundamental["value"] == 66.67
    assert chip["value"] == 75.0
    assert fundamental["status"] == chip["status"] == "partial"
    assert fundamental["historical_pit_eligible"] is False
    assert chip["reason"] == "current_source_export_not_historical_pit_certified"


def test_broad_scanner_passes_every_feature_through_the_factor_enricher():
    scanner = BroadScanner(
        feature_loader=lambda: [_feature()],
        feature_enricher=lambda items: [
            {**item, "scanner_factor_inputs": {"fundamental": {"value": 80, "status": "ready", "source": "fixture"}}}
            for item in items
        ],
        position_loader=lambda: {},
    )

    snapshot = scanner.scan()

    candidate = snapshot["candidate_details"]["2330.TW"]
    assert candidate.factor_scores["fundamental_score"]["value"] == 80.0
    assert candidate.factor_scores["fundamental_score"]["source"] == "fixture"
