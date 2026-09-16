"""Offline admission/receipt tests, with no SDK, network, server, or orders."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from open_stock_ai.agent_runtime.contracts import AgentRunContext
from open_stock_ai.agent_runtime.providers.codex import CodexProvider
from open_stock_ai.agent_runtime.providers.model_metadata import host_review_provider_selection, restore_provider_model_metadata
from open_stock_ai.execution.trading_plan import content_hash
from open_stock_ai.storage.sqlite_store import SQLiteStore
from stock_ai.agent_run_store import AgentRunStore
from stock_ai.autonomous_model_review import AutonomousModelReview

NOW = datetime(2026, 9, 11, 7, tzinfo=timezone.utc)
ACTUAL = {"provider": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "medium", "resolution_source": "sdk_thread_start"}


class CampaignFixture:
    def __init__(self, store, account="test-paper"):
        self.plans = SimpleNamespace(store=store)
        self.broker = SimpleNamespace(account_id=account, mode="paper")
        self.enabled = True
        self.cycles = {}

    def status(self):
        return {"enabled": self.enabled}

    def assert_activation_authorized(self, *, authorized_at):
        from open_stock_ai.execution.trading_plan import utc_time
        utc_time(authorized_at)

    def add(self, *, now=NOW, marker="a"):
        cycle = {"schema_version": "open_stock_ai.autonomous_research_cycle.v1", "account_id": self.broker.account_id,
                 "created_at": now.isoformat(), "results": [{"symbol": "2330.TW", "history_id": "AE-history", "last_bar": now.isoformat()}],
                 "universe_count": 1800, "ordinary_stock_count": 1600, "usable_bulk_count": 1590,
                 "deep_selected_count": 1, "deep_success_count": 1, "bulk_evidence_id": marker, "errors": []}
        key = "AC-" + content_hash(cycle)
        cycle["cycle_id"] = key
        self.cycles[key] = cycle
        return key

    def cycle(self, cycle_id):
        result = deepcopy(self.cycles[cycle_id])
        if "AC-" + content_hash({k: v for k, v in result.items() if k != "cycle_id"}) != cycle_id:
            raise ValueError("retained_campaign_cycle_corrupted")
        return result


class RuntimeFixture:
    def __init__(self, path):
        self.store = AgentRunStore(path)
        self.calls = []
        self.mode = "complete"
        self.selected = {"selected_model": "gpt-5.6-sol", "selected_reasoning_effort": "medium"}
        self.driver = SimpleNamespace(describe=lambda: dict(self.selected))
        self._service_provider = lambda: SimpleNamespace(drivers={"codex": self.driver})
        self.entered, self.release = None, None

    async def create_run(self, **request):
        self.calls.append(deepcopy(request))
        if self.mode == "uncertain_before":
            raise TimeoutError("no acceptance receipt")
        request = {**request, "metadata": request.pop("run_metadata")}
        run = self.store.create_run(f"AR-review-{len(self.calls)}", request)
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        if self.mode == "uncertain_after":
            raise TimeoutError("persisted before reply was lost")
        if self.mode == "cancelled_after":
            raise asyncio.CancelledError()
        if self.mode == "complete":
            self.store.complete_run(run["run_id"], {"status": "completed", "provider_model_metadata": dict(ACTUAL)})
        return self.store.get_run(run["run_id"])


def setup(tmp_path, **kwargs):
    campaign = CampaignFixture(SQLiteStore(tmp_path / "campaign.sqlite"))
    runtime = RuntimeFixture(tmp_path / "runs.sqlite")
    review = AutonomousModelReview(campaign=campaign, runtime=runtime, **kwargs)
    return campaign, runtime, review


def test_default_disabled_and_paused_campaign_do_not_spend(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    cycle = campaign.add()
    assert asyncio.run(review.request(cycle_id=cycle, now=NOW))["status"] == "disabled"
    review.configure(enabled=True)
    campaign.enabled = False
    assert asyncio.run(review.request(cycle_id=cycle, now=NOW))["status"] == "disabled"
    assert runtime.calls == []
    assert review.status(now=NOW)["used_today"] == 0


def _forward_template_probe(campaign, runtime):
    """Capture the real policy hash with explicit source/config fixtures."""
    from open_stock_ai.research.agent_forward_validation import freeze_agent_policy

    def prepare(**kwargs):
        return freeze_agent_policy(model_receipt=kwargs["model_receipt"], prompt_template=kwargs["prompt_template"],
            decision_policy={"scope": "offline_template_contract_fixture"}, tool_manifest=kwargs["tool_manifest"],
            source_hashes={kind: {"fixture": "a"*64} for kind in ("tools", "execution", "config")},
            required_evidence=["price_history", "cost_model"])
    campaign.forward_monitor = SimpleNamespace(prepare=prepare)
    runtime._service_provider = lambda: SimpleNamespace(
        tools=SimpleNamespace(manifest=lambda: [{"name": "autonomy.evidence"}]))


def _api_review_request(objective, *, intent=None):
    from stock_ai.agent_api import AgentRunRequest, _run_context, _run_metadata
    request = AgentRunRequest(objective=objective, context_scope="market", symbols=["2317.TW"],
                              intent=intent or {})
    normalized, symbols, scope = _run_context(request)
    return {"objective": normalized, "symbols": symbols, "metadata": _run_metadata(request, scope)}


@pytest.mark.parametrize("route", ["direct", "market_scope", "market_decision"])
def test_standard_forward_policy_stable_across_cycles_after_real_api_normalization(tmp_path, route):
    campaign, runtime, review = setup(tmp_path)
    _forward_template_probe(campaign, runtime)
    policies = []
    objectives = []
    for index in range(2):
        cycle_id = campaign.add(now=NOW+timedelta(days=index), marker=f"day-{index}")
        standard = review._objective(campaign.cycle(cycle_id))
        request = {"objective": standard, "symbols": [], "metadata": {}} if route == "direct" else _api_review_request(
            standard, intent={"category": "market_decision", "scope": "market"} if route == "market_decision" else {})
        objectives.append(request["objective"])
        policy = review.prepare_forward_review(cycle_id=cycle_id, run={"run_id": f"AR-{index}", "request": request},
                                               model_receipt=ACTUAL)
        assert cycle_id not in policy["prompt_template"]
        assert '"cycle_id"' not in policy["prompt_template"]
        assert "使用 autonomy.propose_plan" in policy["prompt_template"]
        policies.append(policy)
    assert objectives[0] != objectives[1]
    assert policies[0]["policy_version"] == policies[1]["policy_version"]
    if route != "direct":
        assert "Discover and compare securities using broad market evidence" in policies[0]["prompt_template"]
    assert policies[0]["prompt_template"].startswith("[MODEL_TASK_KIND:market_decision]") is (route == "market_decision")
    assert runtime.calls == []


@pytest.mark.parametrize("mutation", ["prepend", "append", "instruction", "fake_scope", "wrong_task_header"])
def test_custom_objective_or_unmatched_prefix_keeps_full_forward_policy_identity(tmp_path, mutation):
    campaign, runtime, review = setup(tmp_path)
    _forward_template_probe(campaign, runtime)
    cycle_id = campaign.add()
    standard = review._objective(campaign.cycle(cycle_id))
    request = _api_review_request(standard, intent={"category": "market_decision", "scope": "market"})
    baseline = review.prepare_forward_review(cycle_id=cycle_id, run={"request": request}, model_receipt=ACTUAL)
    mutations = {
        "prepend": "額外指令：本輪只挑最近上漲最多的股票。\n" + request["objective"],
        "append": request["objective"] + "\n本輪改用不同的風險預算。",
        "instruction": request["objective"].replace("先以 autonomy.research", "只使用 web.search"),
        "fake_scope": "[MARKET_SCOPE] Arbitrary extra instructions.\n" + standard,
        "wrong_task_header": request["objective"].replace("[MODEL_TASK_KIND:market_decision]", "[MODEL_TASK_KIND:general_answer]", 1),
    }
    actual = mutations[mutation]
    result = review.prepare_forward_review(cycle_id=cycle_id, run={"request": {**request, "objective": actual}}, model_receipt=ACTUAL)
    assert result["prompt_template"] == actual
    assert result["policy_version"] != baseline["policy_version"]
    assert runtime.calls == []


def test_different_legitimate_api_task_routing_is_not_pooled_as_one_policy(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    _forward_template_probe(campaign, runtime)
    cycle_id = campaign.add()
    standard = review._objective(campaign.cycle(cycle_id))
    policies = [review.prepare_forward_review(cycle_id=cycle_id, run={"request": _api_review_request(
        standard, intent={"category": category, "scope": "market"})}, model_receipt=ACTUAL)
        for category in ("market_decision", "market_analysis")]
    assert policies[0]["policy_version"] != policies[1]["policy_version"]
    assert "[MODEL_TASK_KIND:market_information]" in policies[1]["prompt_template"]


def test_one_consolidated_review_restart_daily_cap_and_fixed_selection(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    first = campaign.add()
    review.configure(enabled=True)
    runtime.selected = {"selected_model": "gpt-6-astra", "selected_reasoning_effort": "ultra"}
    receipt = asyncio.run(review.request(cycle_id=first, now=NOW))
    assert receipt["status"] == "completed" and receipt["submission_attempted"]
    request = runtime.calls[0]
    assert request["driver_id"] == "codex" and request["autonomy"] == "paper_execute"
    assert request["symbols"] == [] and request["max_steps"] == 12
    from open_stock_ai.agent_runtime.routing import UnifiedMultiIntentRouter, normalized_market_symbols
    routing = UnifiedMultiIntentRouter().route(request["objective"], task_kind_hint="market_decision", supplied_symbols=())
    assert request["objective"].startswith("[MARKET_SCOPE]")
    assert not routing.requires_symbol and not any(row.symbol for row in routing.symbol_contexts)
    assert normalized_market_symbols(routing, objective=request["objective"], supplied_symbols=()) == ()
    assert "自主全市場紙上交易流水線" in request["objective"]
    assert "autonomy.propose_plan" in request["objective"] and '"universe_count": 1800' in request["objective"]
    assert request["run_metadata"]["autonomous_model_review"]["provider_model_selection"]["model"] == ACTUAL["model"]
    restarted = AutonomousModelReview(campaign=campaign, runtime=runtime, daily_limit=10)
    assert restarted.status(now=NOW)["daily_limit"] == 1  # Constructor cannot reset persistent policy.
    assert not asyncio.run(restarted.request(cycle_id=first, now=NOW))["submission_attempted"]
    assert asyncio.run(restarted.request(cycle_id=campaign.add(marker="b"), now=NOW))["status"] == "daily_limit_reached"
    tomorrow = NOW + timedelta(days=1)
    next_run = asyncio.run(restarted.request(cycle_id=campaign.add(now=tomorrow), now=tomorrow))
    assert next_run["status"] == "completed"
    assert runtime.calls[1]["session_id"] == request["session_id"]
    assert runtime.calls[1]["run_metadata"]["autonomous_model_review"]["provider_model_selection"]["model"] == ACTUAL["model"]
    assert len(runtime.calls) == 2


@pytest.mark.parametrize("mode,expected", [("uncertain_before", "submission_unknown"), ("uncertain_after", "queued")])
def test_uncertain_submission_reconciles_exact_receipt_never_resubmits(tmp_path, mode, expected):
    campaign, runtime, review = setup(tmp_path, daily_limit=3)
    cycle = campaign.add()
    review.configure(enabled=True)
    runtime.mode = mode
    result = asyncio.run(review.request(cycle_id=cycle, now=NOW))
    assert result["status"] == expected
    # Prove reconciliation is exact lookup rather than a capped recent-run list.
    for index in range(205):
        runtime.store.create_run(f"AR-unrelated-{index}", {"objective": "unrelated"})
    restarted = AutonomousModelReview(campaign=campaign, runtime=runtime)
    assert asyncio.run(restarted.request(cycle_id=cycle, now=NOW))["status"] == expected
    assert asyncio.run(restarted.request(cycle_id=campaign.add(marker="b"), now=NOW))["status"] == "prior_review_unresolved"
    assert len(runtime.calls) == 1 and restarted.status(now=NOW)["used_today"] == 1


def test_cancelled_submission_keeps_reservation_and_recovers_receipt(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    cycle = campaign.add()
    review.configure(enabled=True)
    runtime.mode = "cancelled_after"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(review.request(cycle_id=cycle, now=NOW))
    assert review.status(now=NOW)["reviews"][0]["status"] == "queued"
    asyncio.run(review.request(cycle_id=cycle, now=NOW))
    assert len(runtime.calls) == 1


def test_concurrent_process_instances_cannot_break_daily_limit(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    cycles = [campaign.add(marker=str(i)) for i in range(4)]
    review.configure(enabled=True)
    adapters = [AutonomousModelReview(campaign=campaign, runtime=runtime) for _ in cycles]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda pair: asyncio.run(pair[0].request(cycle_id=pair[1], now=NOW)), zip(adapters, cycles)))
    assert len(runtime.calls) == 1
    assert sum(r.get("submission_attempted", False) for r in results) == 1
    assert review.status(now=NOW)["used_today"] == 1


def test_simultaneous_same_cycle_returns_original_pending_run(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    cycle = campaign.add()
    review.configure(enabled=True)

    async def scenario():
        runtime.entered, runtime.release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(review.request(cycle_id=cycle, now=NOW))
        await runtime.entered.wait()
        duplicate = await review.request(cycle_id=cycle, now=NOW)
        assert duplicate["status"] == "queued"
        runtime.release.set()
        await first
    asyncio.run(scenario())
    assert len(runtime.calls) == 1


def test_register_current_run_adopts_session_actual_model_and_no_cloud_duplicate(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    cycle = campaign.add()
    runtime.store.create_run("AR-current", {"objective": "current review", "driver_id": "codex", "session_id": "AS-existing", "autonomy": "paper_execute"})
    receipt = review.register_current_review(cycle_id=cycle, run_id="AR-current", provider_model_metadata=ACTUAL, now=NOW)
    review.configure(enabled=True)
    status = review.status(now=NOW)
    assert receipt["run"]["provider_model_metadata"] == ACTUAL
    assert status["session_id"] == "AS-existing" and status["used_today"] == 1
    assert status["provider_model_selection"]["model"] == ACTUAL["model"]
    assert status["reviews"][0]["run"]["provider_model_metadata"] == ACTUAL
    asyncio.run(review.request(cycle_id=cycle, now=NOW))
    assert runtime.calls == []
    runtime.store.complete_run("AR-current", {"status": "completed"})
    runtime.selected = {"selected_model": "new-ui-model", "selected_reasoning_effort": "ultra"}
    tomorrow = NOW + timedelta(days=1)
    restarted = AutonomousModelReview(campaign=campaign, runtime=runtime)
    asyncio.run(restarted.request(cycle_id=campaign.add(now=tomorrow), now=tomorrow))
    assert runtime.calls[0]["session_id"] == "AS-existing"
    selection = runtime.calls[0]["run_metadata"]["autonomous_model_review"]["provider_model_selection"]
    assert (selection["model"], selection["reasoning_effort"]) == ("gpt-5.6-sol", "medium")


def test_registration_cannot_overwrite_uncertain_dispatch_or_missing_run(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    cycle = campaign.add()
    review.configure(enabled=True)
    runtime.mode = "uncertain_before"
    asyncio.run(review.request(cycle_id=cycle, now=NOW))
    with pytest.raises(ValueError, match="existing_codex"):
        review.register_current_review(cycle_id=cycle, run_id="invented", now=NOW)
    runtime.store.create_run("AR-other", {"driver_id": "codex", "session_id": "AS-other"})
    with pytest.raises(ValueError, match="already_reserved"):
        review.register_current_review(cycle_id=cycle, run_id="AR-other", now=NOW)


def test_same_registered_run_across_cycles_spends_one_daily_unit_and_admission_agrees(tmp_path):
    campaign, runtime, review = setup(tmp_path, daily_limit=2)
    runtime.store.create_run("AR-current", {"driver_id": "codex", "session_id": "AS-existing"})
    runtime.store.complete_run("AR-current", {"status": "completed", "provider_model_metadata": ACTUAL})
    for marker in ("original", "repaired"):
        review.register_current_review(cycle_id=campaign.add(marker=marker), run_id="AR-current", now=NOW)
    review.configure(enabled=True)

    restarted = AutonomousModelReview(campaign=campaign, runtime=runtime)
    status = restarted.status(now=NOW)
    assert len(status["reviews"]) == 2
    assert (status["used_today"], status["remaining_today"]) == (1, 1)
    assert asyncio.run(restarted.request(cycle_id=campaign.add(marker="next"), now=NOW))["status"] == "completed"
    assert restarted.status(now=NOW)["used_today"] == 2
    assert asyncio.run(restarted.request(cycle_id=campaign.add(marker="over-budget"), now=NOW))["status"] == "daily_limit_reached"
    assert len(runtime.calls) == 1


def test_distinct_registered_runs_spend_distinct_daily_units(tmp_path):
    campaign, runtime, review = setup(tmp_path, daily_limit=2)
    for index in range(2):
        run_id = f"AR-existing-{index}"
        runtime.store.create_run(run_id, {"driver_id": "codex", "session_id": "AS-existing"})
        runtime.store.complete_run(run_id, {"status": "completed", "provider_model_metadata": ACTUAL})
        review.register_current_review(cycle_id=campaign.add(marker=str(index)), run_id=run_id, now=NOW)
    review.configure(enabled=True)
    status = review.status(now=NOW)
    assert (status["used_today"], status["remaining_today"]) == (2, 0)
    assert asyncio.run(review.request(cycle_id=campaign.add(marker="new"), now=NOW))["status"] == "daily_limit_reached"
    assert runtime.calls == []


def test_distinct_uncertain_dispatch_keys_each_spend_budget_and_block_admission(tmp_path):
    campaign, runtime, review = setup(tmp_path, daily_limit=3)
    review.configure(enabled=True)
    # Retained reservations from interrupted dispatches have no accepted run ID.
    with campaign.plans.store._connect() as conn:
        for marker, state in (("unknown", "submission_unknown"), ("pending", "dispatching")):
            cycle_id = campaign.add(marker=marker)
            key = "autonomous-model-review:" + content_hash([review.account_id, cycle_id])
            conn.execute("insert into autonomous_model_reviews values (?,?,?,?,?,null,null,null,?,?)",
                         (review.account_id, cycle_id, NOW.date().isoformat(), state, key, NOW.isoformat(), NOW.isoformat()))
        conn.commit()
    restarted = AutonomousModelReview(campaign=campaign, runtime=runtime)
    status = restarted.status(now=NOW)
    assert (status["used_today"], status["remaining_today"]) == (2, 1)
    assert {row["status"] for row in status["reviews"]} == {"submission_unknown", "dispatching"}
    assert asyncio.run(restarted.request(cycle_id=campaign.add(marker="new"), now=NOW))["status"] == "prior_review_unresolved"
    assert runtime.calls == []


def test_stale_future_corrupt_or_other_account_cycles_cannot_spend(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    review.configure(enabled=True)
    for now in (NOW - timedelta(days=2), NOW + timedelta(seconds=1)):
        with pytest.raises(ValueError, match="requires_refresh"):
            asyncio.run(review.request(cycle_id=campaign.add(now=now), now=NOW))
    cycle = campaign.add()
    campaign.cycles[cycle]["deep_success_count"] = 999
    with pytest.raises(ValueError, match="corrupted"):
        asyncio.run(review.request(cycle_id=cycle, now=NOW))
    other = CampaignFixture(campaign.plans.store, "other-paper")
    other_cycle = other.add()
    campaign.cycles[other_cycle] = other.cycles[other_cycle]
    with pytest.raises(ValueError, match="account_mismatch"):
        asyncio.run(review.request(cycle_id=other_cycle, now=NOW))
    assert runtime.calls == []


def test_accounts_have_independent_daily_budgets(tmp_path):
    campaign, runtime, review = setup(tmp_path)
    other = CampaignFixture(campaign.plans.store, "other-paper")
    second = AutonomousModelReview(campaign=other, runtime=runtime)
    for service, adapter in ((campaign, review), (other, second)):
        adapter.configure(enabled=True)
        assert asyncio.run(adapter.request(cycle_id=service.add(), now=NOW))["status"] == "completed"
    assert len(runtime.calls) == 2
    assert runtime.calls[0]["session_id"] != runtime.calls[1]["session_id"]


def test_missing_known_run_receipt_blocks_new_admission(tmp_path):
    campaign, runtime, review = setup(tmp_path, daily_limit=2)
    review.configure(enabled=True)
    asyncio.run(review.request(cycle_id=campaign.add(), now=NOW))
    runtime.store.get_run = lambda run_id: None
    result = asyncio.run(review.request(cycle_id=campaign.add(marker="b"), now=NOW))
    assert result["status"] == "prior_review_unresolved"
    assert len(runtime.calls) == 1


def test_internal_selection_bridge_rejects_arbitrary_fields_and_preserves_checkpoint():
    identity = {"account_id": "paper", "cycle_id": "AC-known", "provider_model_selection": ACTUAL}
    request = {"driver_id": "codex", "autonomy": "paper_execute", "objective": "自主全市場紙上交易流水線",
               "idempotency_key": "autonomous-model-review:" + content_hash(["paper", "AC-known"]),
               "metadata": {"autonomous_model_review": identity}}
    selection = host_review_provider_selection(request)
    assert selection["model"] == ACTUAL["model"]
    for change in ({"driver_id": "external-agent"}, {"autonomy": "advisory"}, {"idempotency_key": "other"},
                   {"objective": "hello"}, {"metadata": {"intent": {"autonomous_model_review": identity}}}):
        assert host_review_provider_selection({**request, **change}) is None
    context = AgentRunContext(run_id="R", session_id="S", driver_id="codex", autonomy="paper_execute", symbols=())
    restore_provider_model_metadata(context, {"provider_model_selection": selection})
    assert context.state["provider_model_metadata"]["model"] == ACTUAL["model"]
    old = {**ACTUAL, "model": "checkpoint-old-model"}
    restore_provider_model_metadata(context, {"provider_model_selection": selection, "checkpoint": {"payload": {"context_state": {"provider_model_metadata": old}}}})
    assert context.state["provider_model_metadata"] == old


def test_default_model_explicit_effort_restores_without_mutating_global_provider():
    provider = CodexProvider(SimpleNamespace(), model="later-ui-model", reasoning_effort="ultra")
    provider.restore_session_selection("R", {"model": "", "reasoning_effort": "medium", "selection_source": "host_configured_selection"})
    assert provider._session_selections["R"] == {"model": "", "reasoning_effort": "medium"}
    assert provider.model == "later-ui-model" and provider.reasoning_effort == "ultra"


def test_durable_runtime_passes_scoped_selection_to_existing_orchestrator(tmp_path):
    from stock_ai.durable_agent_runtime import DurableAgentRuntime
    received = []

    class ServiceFixture:
        async def run(self, **kwargs):
            received.append(kwargs["resume_state"])
            return {"run_id": kwargs["run_id"], "status": "completed", "summary": "done", "activity": []}

    async def scenario():
        runtime = DurableAgentRuntime(service_provider=ServiceFixture, store=AgentRunStore(tmp_path / "bridge.sqlite"))
        identity = {"account_id": "paper", "cycle_id": "AC-known", "provider_model_selection": ACTUAL}
        run = await runtime.create_run(objective="自主全市場紙上交易流水線", driver_id="codex", autonomy="paper_execute",
                                       idempotency_key="autonomous-model-review:" + content_hash(["paper", "AC-known"]),
                                       run_metadata={"autonomous_model_review": identity})
        await runtime.wait(run["run_id"])
        await runtime.close()
    asyncio.run(scenario())
    assert received[0]["provider_model_selection"]["model"] == ACTUAL["model"]
    assert received[0]["provider_model_selection"]["reasoning_effort"] == ACTUAL["reasoning_effort"]


def test_postclose_wiring_reuses_retained_cycle_and_register_prevents_second_model(tmp_path, monkeypatch):
    from stock_ai import autonomous_trading_service as service_module
    from stock_ai import autonomous_model_review as review_module
    from stock_ai import agent_service

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    campaign, runtime, review = setup(tmp_path)
    cycle = campaign.add()
    outcomes = []
    campaign.status = lambda: {"enabled": campaign.enabled, "latest_cycle_id": cycle}
    campaign.finish_postclose_research = lambda **kwargs: outcomes.append(kwargs)

    async def positions(**kwargs):
        return {"errors": []}

    async def forbidden(**kwargs):
        raise AssertionError("retained cycle must not refetch or auto-select frozen plans")

    campaign.review_positions = positions
    campaign.research = campaign.create_plans = forbidden
    monkeypatch.setattr(service_module, "datetime", Clock)
    monkeypatch.setattr(review_module, "datetime", Clock)
    monkeypatch.setattr(agent_service, "get_agent_run_runtime", lambda: runtime)
    review.configure(enabled=True)
    asyncio.run(service_module._autonomous_postclose_research(campaign))
    assert outcomes == [{}] and len(runtime.calls) == 1
    restarted = service_module.get_autonomous_model_review(campaign)
    registered = restarted.register_current_review(cycle_id=cycle, run_id="AR-review-1", provider_model_metadata=ACTUAL, now=NOW)
    assert registered["run"]["provider_model_metadata"] == ACTUAL
    asyncio.run(service_module._autonomous_postclose_research(campaign))
    assert outcomes == [{}, {}] and len(runtime.calls) == 1


@pytest.mark.parametrize("paused_component", ["campaign", "model_review"])
def test_background_activation_cannot_revive_pause_after_model_started(tmp_path, monkeypatch, paused_component):
    from stock_ai import autonomous_trading_service as service_module
    from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider

    campaign, runtime, review = setup(tmp_path)
    now = datetime.now(timezone.utc)
    cycle = campaign.add(now=now)
    campaign.status = lambda: {"enabled": campaign.enabled, "plans": []}
    review.configure(enabled=True)
    runtime.mode = "queued"
    submitted = asyncio.run(review.request(cycle_id=cycle, now=now))
    if paused_component == "campaign":
        campaign.enabled = False
    else:
        review.configure(enabled=False)
    monkeypatch.setattr(service_module, "get_autonomous_campaign", lambda: campaign)
    monkeypatch.setattr(service_module, "get_autonomous_model_review", lambda service=None: review)
    context = AgentRunContext(run_id=submitted["run_id"], session_id=submitted["run"]["session_id"], driver_id="codex",
                              autonomy="paper_execute", symbols=(), allow_paper_orders=True)
    context.state.update(explicit_autonomous_campaign_authorized=True, provider_model_metadata=ACTUAL)
    with pytest.raises(PermissionError, match="paused_since_review_started"):
        asyncio.run(AutonomousTradingToolProvider().execute("autonomy.activate", {"cycle_id": cycle, "use_candidate_plans": False}, context))
    assert len(runtime.calls) == 1
    assert not campaign.enabled if paused_component == "campaign" else not review.status()["enabled"]


def test_current_native_activation_registers_actual_selection_without_new_model_task(tmp_path, monkeypatch):
    from stock_ai import autonomous_trading_service as service_module
    from stock_ai.tool_providers.autonomy import AutonomousTradingToolProvider

    campaign, runtime, review = setup(tmp_path)
    now = datetime.now(timezone.utc)
    cycle = campaign.add(now=now)
    campaign.enabled = False
    campaign.status = lambda: {"enabled": campaign.enabled, "plans": []}
    def configure(*, enabled, authorized_at):
        assert authorized_at == runtime.store.get_run("AR-native")["created_at"]
        campaign.enabled = enabled
        return campaign.status()
    async def manage():
        return {"account_id": campaign.broker.account_id, "enabled": campaign.enabled, "results": [], "errors": []}
    campaign.configure, campaign.manage = configure, manage
    campaign._retain = lambda kind, payload: "AE-" + content_hash([kind, payload])
    monkeypatch.setattr(service_module, "get_autonomous_campaign", lambda: campaign)
    monkeypatch.setattr(service_module, "get_autonomous_model_review", lambda service=None: review)
    runtime.store.create_run("AR-native", {"driver_id": "codex", "session_id": "AS-native", "autonomy": "paper_execute"})
    context = AgentRunContext(run_id="AR-native", session_id="AS-native", driver_id="codex", autonomy="paper_execute",
                              symbols=(), allow_paper_orders=True)
    context.state.update(explicit_autonomous_campaign_authorized=True, provider_model_metadata=ACTUAL)
    receipt = asyncio.run(AutonomousTradingToolProvider().execute("autonomy.activate", {"cycle_id": cycle, "use_candidate_plans": False}, context))
    assert receipt["management"]["enabled"] is True and receipt["plans"] == []
    assert receipt["campaign_receipt_id"].startswith("AE-")
    assert review.status()["session_id"] == "AS-native"
    assert review.status()["provider_model_selection"]["model"] == ACTUAL["model"]
    assert review.status()["provider_model_selection"]["reasoning_effort"] == ACTUAL["reasoning_effort"]
    asyncio.run(review.request(cycle_id=cycle, now=now))
    assert runtime.calls == []
