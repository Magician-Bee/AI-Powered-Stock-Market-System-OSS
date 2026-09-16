"""Account-scoped, pre-evaluation multiplicity and repeat-look contracts."""
import asyncio
from dataclasses import replace

from open_stock_ai.execution.trading_plan import content_hash
from product_admission_fixtures import product_feature
from test_autonomous_campaign import NOW, setup


def test_full_declared_universe_registered_before_first_history_and_not_only_deep_subset(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW", "2317.TW", "2454.TW"))
    loader = service.history_loader
    observed_registry_sizes = []

    async def history(symbol, now):
        with service.plans.store._connect() as conn:
            observed_registry_sizes.append(conn.execute("select count(*) from autonomous_research_family_members").fetchone()[0])
        return await loader(symbol, now)

    service.history_loader = history
    cycle = asyncio.run(service.research(now=NOW, deep_limit=1))
    family = cycle["research_family"]
    assert observed_registry_sizes == [6]
    assert family["current_universe_symbol_count"] == 3
    assert family["evaluation_family_size"] == 6 and cycle["deep_selected_count"] == 1
    assert family["positive_ev_promotion_allowed"] is False
    for candidate in cycle["results"][0]["candidates"]:
        receipt = service._evidence(candidate["qualification_id"], "qualification")
        assert receipt["evaluation_manifest"]["evaluation_family_size"] == 6
        assert receipt["evaluation_manifest"]["evaluation_context"] == family
        assert "exploratory_campaign_requires_independent_locked_confirmation" in receipt["reasons"]
        assert receipt["research_paper_candidate_eligible"] is True
        assert receipt["positive_ev_qualified"] is False
        assert receipt["receipt_sha256"] == content_hash({k:v for k,v in receipt.items() if k != "receipt_sha256"})


def test_repeated_cycle_is_not_new_independent_sample_and_shrinking_universe_keeps_prior_members(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW", "2317.TW"))
    async def scenario():
        first = await service.research(now=NOW, deep_limit=1)
        second = await service.research(now=NOW, deep_limit=1)
        assert second["research_family"]["evaluation_family_size"] == 4
        assert second["research_family"]["registry_sha256"] == first["research_family"]["registry_sha256"]
        assert second["research_family"]["prior_registered_cycle_count"] == 1
        assert second["research_family"]["evaluation_id"] != first["research_family"]["evaluation_id"]
        async def narrow():
            feature = {**product_feature("2454.TW", now=NOW), "close": 106,
                       "data_as_of": NOW.date().isoformat(), "trade_value": 1}
            return {"features": [feature], "all_features": [dict(feature)]}
        service.scanner = narrow
        third = await service.research(now=NOW, deep_limit=1)
        assert third["research_family"]["current_symbol_strategy_pair_count"] == 2
        assert third["research_family"]["evaluation_family_size"] == 6
        assert third["research_family"]["prior_registered_cycle_count"] == 2
    asyncio.run(scenario())


def test_strategy_version_change_adds_family_members_and_failed_history_never_shrinks_family(tmp_path, monkeypatch):
    import open_stock_ai.execution.autonomous_campaign as module
    service, _ = setup(tmp_path, symbols=("2330.TW", "2317.TW"))
    asyncio.run(service.research(now=NOW, deep_limit=1))
    originals = module.frozen_candidates()
    monkeypatch.setattr(module, "frozen_candidates", lambda: [replace(originals[0], stop_atr=2.5), originals[1]])
    async def failed(symbol, now):
        raise ValueError("fixture_source_unavailable")
    service.history_loader = failed
    cycle = asyncio.run(service.research(now=NOW, deep_limit=1))
    assert cycle["deep_success_count"] == 0
    assert cycle["research_family"]["evaluation_family_size"] == 6
    assert cycle["research_family"]["current_symbol_strategy_pair_count"] == 4
    assert cycle["errors"]


def test_registry_is_account_scoped_and_duplicate_or_noncanonical_symbols_do_not_forge_family(tmp_path):
    service, _ = setup(tmp_path, symbols=("2330.TW", "2330.TW", "AAPL"))
    first = asyncio.run(service.research(now=NOW, deep_limit=1))
    assert first["research_family"]["current_universe_symbols"] == ["2330.TW"]
    assert first["research_family"]["evaluation_family_size"] == 2
    from test_autonomous_trading_plans import BrokerFixture
    broker = BrokerFixture()
    broker.account_id = "separate-family-account"
    other, _ = setup(tmp_path, shared=service.plans.store, broker=broker, symbols=("2317.TW",))
    second = asyncio.run(other.research(now=NOW, deep_limit=1))
    assert second["research_family"]["evaluation_family_size"] == 2
    assert second["research_family"]["prior_registered_cycle_count"] == 0
