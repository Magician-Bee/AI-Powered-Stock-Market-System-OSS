from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from open_stock_ai.agent_runtime.completion_contract import decision_ready_candidates_from_result
from open_stock_ai.agent_workspace import _recommendation_bucket
from open_stock_ai.data.market_data_hub import MarketDataHub
from open_stock_ai.types import StockRequest


def test_official_close_allows_advisory_analysis_but_not_execution(monkeypatch) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    bundle = {
        "schema_version": "stock_ai.research_data_bundle.v1",
        "symbol": "2887.TW",
        "market": "TW",
        "price": 42.5,
        "price_source": "TWSE official close",
        "price_timestamp": timestamp,
        "price_is_fallback": True,
        "ohlcv": [],
        "news": [],
        "financials": {},
        "chips": {},
        "announcements": [],
        "raw": {
            "summary": {
                "provider_id": "twse_openapi",
                "connector_id": "stock_ai.taiwan_official.official_summary_payload",
                "quote_kind": "official_close",
                "authorized": True,
                "realtime": False,
                "delayed": False,
                "official_close": True,
                "max_age_seconds": 345600,
                "data_timestamp": timestamp,
            }
        },
    }

    class Gateway:
        def __init__(self) -> None:
            self.platform = SimpleNamespace(
                resolve_entity=lambda *_args, **_kwargs: {"status": "unresolved"}
            )

        def load(self, **_kwargs):
            return bundle

    monkeypatch.setattr("open_stock_ai.data.market_data_hub.UnifiedResearchDataGateway", Gateway)
    snapshot = MarketDataHub().load(StockRequest(symbol="2887.TW", market="TW", horizon="swing"))
    contract = snapshot.raw["data_contract"]

    assert contract["analysis_ready"] is True
    assert contract["analysis_mode"] == "official_close_advisory"
    assert contract["decision_ready"] is False
    assert contract["execution_eligible"] is False
    assert "fallback_price_not_execution_eligible" in contract["blockers"]


def test_advisory_only_data_is_watch_not_a_decision_candidate() -> None:
    payload = {"signal": {"action": "buy", "rule_score": 0.9}}
    contract = {"decision_ready": False, "analysis_ready": True}
    workspace = {
        "schema_version": "open_stock_ai.agent_workspace.v1",
        "symbol": "2887.TW",
        "recommendation_bucket": _recommendation_bucket(payload, contract),
        "analysis_only": True,
        "execution_permission": "blocked",
        "data_status": {"decision_ready": False, "analysis_ready": True},
    }

    assert workspace["recommendation_bucket"] == "watch"
    assert decision_ready_candidates_from_result(workspace) == []
