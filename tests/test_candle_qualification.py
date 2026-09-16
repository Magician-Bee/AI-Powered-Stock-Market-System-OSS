from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta

from open_stock_ai.research.candle_qualification import evaluate_candidate, verify_qualification_receipt
from open_stock_ai.research.candle_qualification import _expectancy
from open_stock_ai.strategy.candle_candidates import CandleCandidate, frozen_candidates
from open_stock_ai.strategy.provenance import content_hash
from open_stock_ai.types import MarketSnapshot, StockRequest


def _rows(count=300):
    rows=[]
    for i in range(count):
        price=100 + i*.1 + (i % 30)*.2
        rows.append({"date":str(date(2024,1,1)+timedelta(days=i)),
                     "open":price-.1,"high":price+.2,"low":price-.2,
                     "close":price,"volume":100000})
    return rows


def test_two_fixed_candidates_declare_only_actual_inputs_and_no_model_prerequisites():
    candidates=frozen_candidates()
    assert len(candidates)==2
    assert {c.candidate_id for c in candidates} == {"tw_candle_breakout_20_60_v1","tw_candle_pullback_20_60_v1"}
    for c in candidates:
        m=c.policy_metadata()
        assert m["required_external_models"] == []
        assert m["positive_expectancy_proven"] is False
        assert "completed_ohlcv_bars" in m["required_inputs"]
    assert replace(candidates[0],position_size_pct=2).strategy_version_hash()!=candidates[0].strategy_version_hash()


def test_candidate_freezes_atr_levels_and_does_not_reenter_an_owned_position():
    candidate=CandleCandidate()
    rows=_rows(80)
    rows[-1].update(open=125,high=127,low=124,close=126)
    snapshot=MarketSnapshot(symbol="2330.TW",market="TW",price=126,ohlcv=rows,raw={"position_quantity":0})
    signal=candidate.generate_signal(StockRequest(symbol="2330.TW"),snapshot)
    assert signal.action=="buy" and signal.position_size_pct==5
    assert signal.stop_loss < signal.entry_price < signal.target_price
    snapshot.raw["position_quantity"]=5
    assert candidate.generate_signal(StockRequest(symbol="2330.TW"),snapshot).action=="hold"
    snapshot.raw["holding_bars"]=20
    assert candidate.generate_signal(StockRequest(symbol="2330.TW"),snapshot).action=="sell"


def test_synthetic_data_never_qualifies_or_becomes_real_paper_evidence():
    receipt=evaluate_candidate(CandleCandidate(),StockRequest(symbol="2330.TW"),_rows(),
                               data_evidence={"source_kind":"synthetic","source_id":"fixture"})
    assert not receipt["positive_ev_qualified"]
    assert not receipt["research_paper_candidate_eligible"]
    assert not verify_qualification_receipt(receipt)
    assert receipt["partitions"]=={}


def test_short_real_source_contract_can_collect_paper_evidence_but_is_not_positive_ev():
    receipt=evaluate_candidate(CandleCandidate(),StockRequest(symbol="2330.TW"),_rows(),
                               data_evidence={"source_kind":"exchange_official","source_id":"offline_test_exchange_contract"})
    assert receipt["research_paper_candidate_eligible"] is True
    assert receipt["passed"] is False
    assert receipt["positive_ev_qualified"] is False
    assert receipt["full_market_point_in_time_certified"] is False
    assert receipt["live_execution_eligible"] is False
    assert receipt["partitions"]["holdout"]["bar_count"]==45
    assert receipt["partitions"]["holdout"]["accounting_verified"] is True
    assert "holdout_insufficient_bars" in receipt["reasons"]
    assert "execution_costs_verified_missing" in receipt["reasons"]
    assert not verify_qualification_receipt(receipt)


def test_holdout_changes_do_not_change_training_validation_or_configuration():
    rows=_rows()
    kwargs={"data_evidence":{"source_kind":"exchange_official","source_id":"offline_test_exchange_contract"}}
    first=evaluate_candidate(CandleCandidate(),StockRequest(symbol="2330.TW"),rows,**kwargs)
    changed=deepcopy(rows)
    for row in changed[255:]:
        for key in ("open","high","low","close"):
            row[key] *= .5
    second=evaluate_candidate(CandleCandidate(),StockRequest(symbol="2330.TW"),changed,**kwargs)
    assert first["configuration"]==second["configuration"]
    assert first["evaluation_manifest"]==second["evaluation_manifest"]
    assert first["partitions"]["train"]==second["partitions"]["train"]
    assert first["partitions"]["validation"]==second["partitions"]["validation"]


def test_boolean_or_hash_only_claim_cannot_override_missing_ev_evidence():
    receipt=evaluate_candidate(CandleCandidate(),StockRequest(symbol="2330.TW"),_rows(),
                               data_evidence={"source_kind":"exchange_official","source_id":"offline_test_exchange_contract"})
    receipt["passed"]=receipt["positive_ev_qualified"]=True
    receipt["receipt_sha256"]=content_hash({k:v for k,v in receipt.items() if k!="receipt_sha256"})
    assert not verify_qualification_receipt(receipt)
    assert not verify_qualification_receipt(receipt,strategy_id="other")


def test_source_hash_identity_and_coverage_are_bound_to_the_evaluated_rows():
    for evidence,reason in [
        ({"symbol":"2317.TW"},"source_instrument_identity_mismatch"),
        ({"coverage_complete":False},"declared_source_range_incomplete"),
        ({"data_sha256":"0"*64},"source_data_hash_mismatch"),
    ]:
        receipt=evaluate_candidate(CandleCandidate(),StockRequest(symbol="2330.TW"),_rows(),
            data_evidence={"source_kind":"exchange_official","source_id":"offline_test_exchange_contract",**evidence})
        assert reason in receipt["input_reasons"]
        assert not receipt["research_paper_candidate_eligible"]


def test_parameter_variants_cannot_reuse_the_two_frozen_candidates_multiplicity_claim():
    receipt=evaluate_candidate(CandleCandidate(stop_atr=1.5),StockRequest(symbol="2330.TW"),_rows(),
                               data_evidence={"source_kind":"exchange_official","source_id":"offline_test_exchange_contract"})
    assert "configuration_requires_new_preregistered_candidate_family" in receipt["reasons"]
    assert receipt["research_paper_candidate_eligible"]  # still an explicit research experiment
    assert not receipt["positive_ev_qualified"]


def test_one_winning_trade_has_no_fabricated_positive_confidence_bound():
    result=_expectancy([{"net_return_pct":5.0,"net_pnl":100.0}],4)
    assert result["mean_net_return_pct"]==5
    assert result["lower_confidence_bound_pct"] is None
    assert result["resamples"]==0


def test_larger_declared_test_family_never_makes_the_confidence_bound_more_optimistic():
    trades=[{"net_return_pct":float((i%7)-3),"net_pnl":float(i-20)} for i in range(40)]
    assert _expectancy(trades,4)["lower_confidence_bound_pct"] <= _expectancy(trades,2)["lower_confidence_bound_pct"]


def test_exploration_context_cannot_promote_repeated_holdout_even_if_metrics_and_source_flags_pass():
    from open_stock_ai.research.candle_qualification import QUALIFICATION_REQUIREMENTS, _qualification_reasons
    receipt=evaluate_candidate(CandleCandidate(),StockRequest(symbol="2330.TW"),_rows(),
        data_evidence={"source_kind":"exchange_official","source_id":"offline_test_exchange_contract"},
        evaluation_context={"purpose":"exploratory_market_campaign","prior_registered_cycle_count":4})
    # Deliberately fabricate an otherwise passing fixture to isolate the
    # protocol check. This is never persisted as market-performance evidence.
    for field in QUALIFICATION_REQUIREMENTS["source_evidence_required"]:
        receipt["data_evidence"][field]=True
    for name in ("validation","holdout"):
        part=receipt["partitions"][name]
        part.update(bar_count=1000,accounting_verified=True,performance={"compounded_return_pct":1})
        part["net_expectancy"]={"closed_trade_count":100,"evaluation_family_size":2,
                               "tail_resample_count":25,"lower_confidence_bound_pct":1}
    assert _qualification_reasons(receipt)==["exploratory_campaign_requires_independent_locked_confirmation"]
    receipt["passed"]=receipt["positive_ev_qualified"]=True
    receipt["receipt_sha256"]=content_hash({k:v for k,v in receipt.items() if k!="receipt_sha256"})
    assert not verify_qualification_receipt(receipt)
