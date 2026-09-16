"""Offline control-flow regressions; no persistent account or model is opened."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from stock_ai import autonomous_trading_api as api
from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider
from test_autonomy_instrument_scope import context


@pytest.mark.parametrize("use_candidate_plans", [False, True])
def test_disable_bypasses_cycle_validation_and_plan_creation(monkeypatch, use_candidate_plans):
    calls = []
    state = {"enabled": True, "review_enabled": True}
    def forbidden(*args, **kwargs):
        pytest.fail("Disabling must not read a missing/stale cycle, create plans or start a model")
    def configure(*, enabled):
        calls.append("campaign_control")
        state["enabled"] = enabled
    def configure_review(*, enabled):
        calls.append("review_control")
        state["review_enabled"] = enabled
    async def manage():
        calls.append("manage_existing_protection")
        assert state == {"enabled": False, "review_enabled": False}
        return {"enabled": False, "results": [], "errors": []}
    service = SimpleNamespace(configure=configure, cycle=forbidden, create_plans=forbidden, manage=manage)
    review = SimpleNamespace(configure=configure_review, status=lambda: {"enabled": state["review_enabled"]}, request=forbidden)
    monkeypatch.setattr(api, "get_autonomous_campaign", lambda: service)
    monkeypatch.setattr(api, "get_autonomous_model_review", lambda actual: review if actual is service else forbidden())
    result = asyncio.run(api.autonomy_activate(api.ActivateCycleRequest(
        cycle_id="missing-or-expired", enabled=False, use_candidate_plans=use_candidate_plans)))
    assert calls == ["campaign_control", "review_control", "manage_existing_protection"]
    assert result["activation_skipped"] == "campaign_disabled" and result["plans"] == []
    assert not result["model_review"]["enabled"] and not result["management"]["enabled"]


def test_review_failure_cannot_prevent_trading_control_from_stopping(monkeypatch):
    state = {"enabled": True}
    service = SimpleNamespace(configure=lambda *, enabled: state.update(enabled=enabled))
    monkeypatch.setattr(api, "get_autonomous_campaign", lambda: service)
    def failed_review(actual):
        assert actual is service
        raise RuntimeError("offline review control unavailable")
    monkeypatch.setattr(api, "get_autonomous_model_review", failed_review)
    with pytest.raises(RuntimeError, match="review control unavailable"):
        asyncio.run(api.autonomy_activate(api.ActivateCycleRequest(cycle_id="old", enabled=False)))
    assert state["enabled"] is False


@pytest.mark.parametrize("paused", ["campaign", "review"])
@pytest.mark.parametrize("use_candidate_plans", [False, True])
def test_paused_background_activation_rejects_before_any_plan_or_control_mutation(monkeypatch, paused, use_candidate_plans):
    def forbidden(*args, **kwargs):
        pytest.fail("A paused background review must reject before mutation")
    service = SimpleNamespace(cycle=lambda cycle: {"cycle_id": cycle, "created_at": datetime.now(timezone.utc).isoformat()},
        status=lambda: {"enabled": paused != "campaign", "plans": []}, create_plans=forbidden,
        assert_activation_authorized=lambda *, authorized_at: None,
        configure=forbidden, manage=forbidden, _retain=forbidden)
    review = SimpleNamespace(runtime=SimpleNamespace(store=SimpleNamespace(get_run=lambda run: {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "request": {"metadata": {"autonomous_model_review": {"cycle_id": "c"}}}})),
        status=lambda: {"enabled": paused != "review"}, register_current_review=forbidden, configure=forbidden)
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_campaign", lambda: service)
    monkeypatch.setattr("stock_ai.autonomous_trading_service.get_autonomous_model_review", lambda _service: review)
    with pytest.raises(PermissionError, match="paused_since_review_started"):
        asyncio.run(AutonomousTradingToolProvider().execute("autonomy.activate",
            {"cycle_id": "c", "use_candidate_plans": use_candidate_plans}, context(())))


def test_research_api_defaults_to_twenty_stock_deep_rotation():
    assert api.ResearchCycleRequest().deep_limit == 20
