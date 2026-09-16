"""Product admission faults use isolated Host fixtures, never market evidence."""
import asyncio
import json
from copy import deepcopy
from datetime import timedelta

import pytest

from open_stock_ai.execution.agent_campaign_actions import propose_plan
from open_stock_ai.execution.agent_plan_proposal import build_agent_plan_proposal
from open_stock_ai.execution.product_admission import assess_new_entry_product
from open_stock_ai.execution.trading_plan import content_hash
from product_admission_fixtures import product_snapshot, product_resolver, product_feature
from test_agent_campaign_actions import CONTEXT, setup as campaign_setup
from test_agent_plan_proposal import fixture as proposal_fixture, resign_cycle
from test_autonomous_campaign import NOW as CAMPAIGN_NOW
from test_autonomous_trading_plans import NOW, setup, tick, market


@pytest.mark.parametrize("field,value,reason", [
    ("status", "unknown", "product_classification_unverified"),
    ("status", "conflict", "product_classification_unverified"),
    ("product_type", "etn", "product_type_not_supported"),
    ("product_type", "depositary_receipt", "product_type_not_supported"),
    ("product_type", "preferred_stock", "product_type_not_supported"),
    ("product_type", "etf", "product_type_not_supported"),
    ("product_type", {}, "product_classification_unverified"),
    ("market_segment", "innovation", "product_segment_not_supported"),
    ("market_segment", None, "product_classification_unverified"),
    ("symbol", "2330.TWO", "product_symbol_venue_mismatch"),
    ("venue", "TPEx", "product_symbol_venue_mismatch"),
    ("source_id", "model_claim", "product_classification_source_mismatch"),
    ("source_dataset", "tpex_isin_otc", "product_classification_source_mismatch"),
    ("source_url", "https://isin.twse.com.tw.invalid/", "product_classification_source_mismatch"),
    ("raw_sha256", None, "product_classification_hash_missing"),
    ("row_sha256", "0" * 64, "product_classification_row_hash_mismatch"),
    ("isin", None, "product_classification_identifiers_missing"),
    ("cfi_code", None, "product_classification_identifiers_missing"),
    ("acquired_at", (NOW + timedelta(seconds=1)).isoformat(), "product_classification_from_future"),
    ("acquired_at", (NOW - timedelta(days=8)).isoformat(), "product_classification_stale"),
    ("source_updated_on", (NOW + timedelta(days=1)).date().isoformat(), "product_classification_from_future"),
    ("source_updated_on", (NOW - timedelta(days=8)).date().isoformat(), "product_classification_stale"),
])
def test_unusable_product_receipts_cannot_admit_new_entry(field, value, reason):
    snapshot = product_snapshot("2330.TW", now=NOW)
    snapshot["classification"][field] = value
    result = assess_new_entry_product(symbol="2330.TW", market="TW", now=NOW, product_snapshot=snapshot)
    assert not result["allowed"] and reason in result["reasons"]


def test_receipt_is_copied_seven_calendar_days_are_explicit_and_identity_cannot_fallback():
    snapshot = product_snapshot("2330.TW", now=NOW - timedelta(days=7))
    original = deepcopy(snapshot)
    result = assess_new_entry_product(symbol="2330.TW", market="TW", now=NOW, product_snapshot=snapshot)
    assert result["allowed"] and snapshot == original
    snapshot["classification"]["status"] = "unknown"
    assert result["classification"]["status"] == "verified"
    for entity in (None, "2330.TW", "2330"):
        changed = {**original, "entity_id": entity}
        assert not assess_new_entry_product(symbol="2330.TW", market="TW", now=NOW, product_snapshot=changed)["allowed"]
    assert not assess_new_entry_product(symbol="2330.TW", market="TW", now=NOW, product_snapshot=original,
        expected_entity_id="ENT-" + "b" * 32)["allowed"]


@pytest.mark.parametrize("fault", ["source_after_acquisition", "missing_row", "wrong_code", "wrong_isin",
                                    "wrong_venue", "missing_name", "ordinary_label_on_etn", "cfi_relabelled"])
def test_rehashed_derived_labels_must_match_original_official_row(fault):
    snapshot = product_snapshot("2330.TW", now=NOW)
    receipt = snapshot["classification"]
    if fault == "source_after_acquisition":
        receipt["acquired_at"] = (NOW - timedelta(days=1)).isoformat()
    elif fault == "missing_row":
        receipt.pop("source_row")
    elif fault == "wrong_code":
        receipt["source_row"]["cells"][0] = "2317 離線fixture"
    elif fault == "wrong_isin":
        receipt["source_row"]["cells"][1] = "TW0002330008"
    elif fault == "wrong_venue":
        receipt["source_row"]["cells"][3] = "上櫃"
    elif fault == "missing_name":
        receipt["source_row"]["cells"][0] = "2330"
    elif fault == "ordinary_label_on_etn":
        receipt["source_row"]["section"] = "ETN"
        receipt["source_row"]["cells"][5] = receipt["cfi_code"] = "CMVUFR"
    else:
        receipt["source_row"]["cells"][5] = receipt["cfi_code"] = "CMXXXU"
    if "source_row" in receipt:
        receipt["row_sha256"] = content_hash(receipt["source_row"])
    result = assess_new_entry_product(symbol="2330.TW", market="TW", now=NOW, product_snapshot=snapshot)
    assert result["allowed"] is False and result["classification_verified"] is False
    if fault == "source_after_acquisition":
        assert "product_classification_source_after_acquisition" in result["reasons"]
    assert "product_classification_row_hash_mismatch" not in result["reasons"]


def test_model_cannot_supply_classification_and_builder_requires_latest_host_snapshot():
    args, host = proposal_fixture()
    with pytest.raises(ValueError, match="fields_not_allowed"):
        build_agent_plan_proposal({**args, "product_snapshot": host["product_snapshot"]}, **host)
    host["product_snapshot"] = None
    with pytest.raises(ValueError, match="product_"):
        build_agent_plan_proposal(args, **host)


@pytest.mark.parametrize("new_type", ["etn", "depositary_receipt", "preferred_stock"])
def test_old_cycle_cannot_create_fixed_or_discretionary_plan_after_classification_changes(tmp_path, new_type):
    service, _ = campaign_setup(tmp_path, symbols=("2330.TW",))
    async def scenario():
        cycle = await service.research(now=CAMPAIGN_NOW, symbols=["2330.TW"])
        original = deepcopy(cycle)
        assert cycle["results"][0]["candidates"]
        service.product_resolver = lambda **kw: product_snapshot(kw["symbol"], now=kw["now"], product_type=new_type)
        fixed = await service.create_plans(cycle_id=cycle["cycle_id"], now=CAMPAIGN_NOW)
        assert fixed["plans"] == [] and fixed["skipped"][0]["reason"] == "product_admission_rejected"
        with pytest.raises(ValueError, match="product_admission_rejected"):
            await propose_plan(service, {"cycle_id": cycle["cycle_id"], "symbol": "2330.TW", "quantity_shares": 1,
                "stop_loss": 95, "rationale": "offline"}, host_context=CONTEXT, now=CAMPAIGN_NOW)
        assert service.cycle(cycle["cycle_id"]) == original
        assert not service.plans.list(account_id=service.broker.account_id) and service.broker.submissions == []
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["before_preview", "after_preview", "missing_resolver", "missing_plan_identity"])
def test_executor_rechecks_before_dispatch_and_rejection_persists_no_order_intent(tmp_path, stage):
    from test_autonomous_trading_plans import plan
    from dataclasses import replace
    definition = replace(plan(), metadata={}) if stage == "missing_plan_identity" else None
    store, broker, pid, executor = setup(tmp_path, definition)
    def nonstock(**kw):
        return product_snapshot(kw["symbol"], now=kw["now"], product_type="depositary_receipt")
    if stage == "before_preview":
        executor.product_resolver = nonstock
    elif stage == "after_preview":
        old = broker.preview
        async def change(*args):
            result = await old(*args)
            executor.product_resolver = nonstock
            return result
        broker.preview = change
    elif stage == "missing_resolver":
        executor.product_resolver = None
    original_hash = store.get(pid)["definition_hash"]
    result = tick(executor, pid)
    assert result["state"]["wait_reason"] == "product_admission_rejected"
    assert not result["state"].get("entry_order_id") and not broker.submissions
    assert store.get(pid)["definition_hash"] == original_hash
    assert store.events(pid)[-1]["event_type"] == "plan.product_admission_wait"


@pytest.mark.parametrize("failure", ["unavailable", "cancelled", "nonstock"])
def test_existing_partial_entry_can_cancel_and_exit_without_classification_lookup(tmp_path, failure):
    store, broker, pid, executor = setup(tmp_path)
    broker.fill_fraction = .5
    entered = tick(executor, pid)
    assert entered["state"]["entry_receipt"]["filled_quantity"] == 5
    lookups = []
    def failed(**kw):
        lookups.append(kw)
        if failure == "cancelled":
            raise asyncio.CancelledError()
        if failure == "unavailable":
            raise OSError("offline storage fault")
        return product_snapshot(kw["symbol"], now=kw["now"], product_type="etn")
    executor.product_resolver = failed
    broker.fill_fraction = 1
    result = tick(executor, pid, price=94, now=NOW + timedelta(seconds=1))
    assert len(broker.submissions) == 2 and broker.submissions[-1]["side"] == "sell"
    assert broker.submissions[-1]["quantity_shares"] == 5 and lookups == []
    assert not broker.orders[entered["state"]["entry_order_id"]]["is_open"]
    assert tick(executor, pid, price=94, now=NOW + timedelta(seconds=2))["status"] == "closed"


@pytest.mark.parametrize("fraction,classification", [(0, "etn"), (.5, "etn"), (.5, "unavailable")])
def test_product_invalidation_cancels_remaining_buy_before_matching_without_selling_holding(tmp_path, fraction, classification):
    store, broker, pid, executor = setup(tmp_path)
    broker.fill_fraction = fraction
    entered = tick(executor, pid)
    order_id = entered["state"]["entry_order_id"]
    seen = []
    def changed(**kw):
        seen.append(kw)
        if classification == "unavailable":
            raise OSError("isolated product storage outage")
        return product_snapshot(kw["symbol"], now=kw["now"], product_type=classification)
    executor.product_resolver = changed
    async def forbidden_observe(_):
        raise AssertionError("invalidated buy must not receive another matching observation")
    broker.observe = forbidden_observe
    result = tick(executor, pid, price=100, now=NOW + timedelta(seconds=1))
    assert seen and len(broker.submissions) == 1
    assert broker.orders[order_id]["is_open"] is False
    assert result["state"]["remaining_quantity"] == int(10 * fraction)
    assert not result["state"].get("exit_reason") and not result["state"].get("exit_order_id")
    assert result["state"]["entry_product_invalidation"]["order_id"] == order_id
    assert result["status"] == ("open" if fraction else "cancelled")


def test_product_cancel_intent_survives_lost_response_and_restart_without_auto_liquidation(tmp_path):
    from open_stock_ai.execution.trading_plan_executor import TradingPlanExecutor
    from open_stock_ai.risk.risk_engine import RiskEngine
    store, broker, pid, executor = setup(tmp_path)
    broker.fill_fraction = .5
    entered = tick(executor, pid)
    order_id = entered["state"]["entry_order_id"]
    executor.product_resolver = None
    original_cancel = broker.cancel
    async def undelivered(*args, **kwargs):
        raise TimeoutError("isolated cancel delivery lost")
    broker.cancel = undelivered
    with pytest.raises(TimeoutError):
        tick(executor, pid, price=100, now=NOW + timedelta(seconds=1))
    assert store.get(pid)["state"]["entry_product_invalidation"]["order_id"] == order_id
    def no_lookup(**_):
        raise AssertionError("durable cancel must not require recovered classification")
    resumed = TradingPlanExecutor(store=store, broker=broker, risk=RiskEngine(),
        evidence_resolver=executor.evidence_resolver, product_resolver=no_lookup)
    broker.cancel = original_cancel
    reconciled = tick(resumed, pid, price=100, now=NOW + timedelta(seconds=2))
    assert reconciled["status"] == "open" and reconciled["state"]["remaining_quantity"] == 5
    assert len(broker.submissions) == 1 and not reconciled["state"].get("exit_reason")
    broker.fill_fraction = 1
    exited = tick(resumed, pid, price=94, now=NOW + timedelta(seconds=3))
    assert exited["state"]["exit_intent"]["quantity_shares"] == 5
    assert broker.submissions[-1]["side"] == "sell"


def test_cancellation_during_new_entry_product_read_propagates_without_dispatch(tmp_path):
    _, broker, pid, executor = setup(tmp_path)
    def cancel(**_):
        raise asyncio.CancelledError()
    executor.product_resolver = cancel
    with pytest.raises(asyncio.CancelledError):
        tick(executor, pid)
    assert broker.submissions == []


def test_real_paper_ticket_preserves_admission_and_restart_reconciles_without_lookup(tmp_path):
    from test_trading_plan_paper_exit_policy import PaperHarness, plan
    from open_stock_ai.execution.trading_plan_executor import TradingPlanExecutor
    from open_stock_ai.risk.risk_engine import RiskEngine
    harness = PaperHarness(tmp_path, plan())
    submitted = asyncio.run(harness.tick(100, 0))
    order_id = submitted["state"]["entry_order_id"]
    native = harness.broker.get_order(order_id)
    frozen = submitted["state"]["entry_intent"]["product_admission"]
    assert json.loads(native["order"]["payload_json"])["ticket"]["product_admission"] == frozen
    assert frozen["allowed"] and frozen["entity_id"].startswith("ENT-")
    # Reinstantiation has no resolver; this is already accepted exposure.
    harness.executor = TradingPlanExecutor(store=harness.plans, broker=harness.port, risk=RiskEngine(),
        evidence_resolver=harness.executor.evidence_resolver)
    result = asyncio.run(harness.tick(94, 1))
    assert result["state"]["exit_intent"]["side"] == "sell"
    assert "product_admission" not in result["state"]["exit_intent"]


@pytest.mark.parametrize("symbol,kind", [("020000.TW", "etn"), ("9103.TW", "depositary_receipt"),
                                       ("2887Z1.TW", "preferred_stock"), ("2330.TW", "unknown")])
def test_broad_research_retains_nonstock_history_without_stock_cost_qualification(tmp_path, symbol, kind):
    service, _ = campaign_setup(tmp_path, symbols=(symbol,))
    original = service.scanner
    async def scanned():
        payload = await original()
        for key in ("features", "all_features"):
            for feature in payload[key]:
                if feature["symbol"] == symbol:
                    feature.update(product_feature(symbol, now=CAMPAIGN_NOW, product_type=kind))
        return payload
    service.scanner = scanned
    cycle = asyncio.run(service.research(now=CAMPAIGN_NOW, symbols=[symbol]))
    assert cycle["deep_success_count"] == 1 and cycle["results"][0]["history_id"]
    assert cycle["results"][0]["candidates"] == []
    assert cycle["results"][0]["candidate_evaluation_status"] == "not_evaluated_unsupported_product"
    assert cycle["candidate_evaluation_success_count"] == 0
    assert not service.plans.list(account_id=service.broker.account_id)
