from __future__ import annotations

from copy import deepcopy

import pytest

from open_stock_ai.agent_runtime.autonomy_claims import campaign_summary_claims_check
from open_stock_ai.agent_runtime.plan_graph import PlanGraph
from open_stock_ai.agent_runtime.validators import ValidatorEngine
from test_autonomy_host_contract import OBJECTIVE, observations


def _completion(summary, *, extra=None, bind_extra=True):
    plan = PlanGraph.create(OBJECTIVE)
    criterion = "The user objective is satisfied by validated evidence."
    plan.completion_criteria = [criterion]
    evidence = {str(index): {**row, "validation": {"passed": True}} for index, row in enumerate(observations())}
    if extra:
        evidence["extra"] = extra
    ids = [key for key in evidence if bind_extra or key != "extra"]
    return ValidatorEngine().validate_completion(
        state="complete", objective=OBJECTIVE, task_kind="market_decision", final_summary=summary,
        has_pending_tool_calls=False, plan=plan.to_dict(), successful_observations=len(evidence), evidence_required=True,
        completion_evaluation={"criteria_met": True, "remaining_gaps": [], "evidence_ids": ids,
            "criterion_results": [{"criterion": criterion, "met": True, "evidence_ids": ids}]},
        known_evidence_ids=set(evidence), evidence_catalog=evidence,
    )


def _fill_evidence():
    fill = {"fill_id": "fill", "order_id": "order", "account_id": "isolated", "symbol": "2330.TW",
            "quantity": 1, "fill_price": 100}
    broker = {"schema_version": "open_stock_ai.paper_broker_result.v1",
              "order": {"order_id": "order", "account_id": "isolated", "symbol": "2330.TW", "filled_quantity": 1, "fill": fill}}
    return {"tool": "autonomy.status", "ok": True, "validation": {"passed": True},
            "result": {"account_id": "isolated", "mode": "paper", "plans": [{"state": {"entry_receipt": {"raw": broker}}}]}}


@pytest.mark.parametrize("summary", [
    "已啟動自主紙上流水線，目前等待條件，尚未成交；正期望值未獲證實。",
    "尚未證明正EV。", "平均收益正但不足正期望資格。", "尚待行情，不是已成交。",
    "正EV不成立。", "正期望值目前不合格。",
    "沒有任何已成交的委託，不能宣稱已擁有正期望值。", "不能保證持續賺錢。",
    "希望未來具有正期望值；仍需要驗證。", "The paper order was not filled; positive EV is not proven.",
])
def test_waiting_uncertainty_and_negative_claims_remain_valid(summary):
    result = _completion(summary)
    assert result.passed, [check for check in result.checks if not check["passed"]]


@pytest.mark.parametrize("summary", [
    # Verbatim conditional paragraph from native step 18. The comma and 且
    # previously discarded its requirement scope before 通過正期望值資格.
    "目前尚未證明正期望值。\n- 重新評估條件：取得新交易時段的即時成交或合格官方收盤，"
    "補齊 point-in-time 價格／財務／籌碼／事件資料、成本表與有效回測，"
    "且策略重新產生 decision-ready、Host 可執行並通過正期望值資格的訊號。"
    "屆時才會形成包含觸發價、停損、目標與期限的計畫。",
    "再評估條件：取得足夠的樣本外驗證資料，且策略通過正期望值資格。",
    "進場條件：取得新行情，且策略具備正期望值。尚未取得資格。",
    "如果未來資料足夠，且策略通過正期望值資格，才會重新評估。",
    "未來需要完成成本、樣本外資料與風險分析才能確認正期望值。",
    "希望完成足夠的樣本外資料與成本研究後具備正期望值。",
    "Reassessment conditions: obtain new data, and the strategy has positive EV.",
    "If new evidence is available, and positive EV is verified, then reassess.",
])
def test_future_qualification_conditions_do_not_claim_current_achievement(summary):
    result = _completion(summary)
    assert result.passed, [check for check in result.checks if not check["passed"]]


@pytest.mark.parametrize("summary,blocker", [
    ("重新評估條件：取得新行情，但策略具有正期望值。", "positive_ev_claim_requires_verified_symbol_and_strategy_qualification"),
    ("重新評估條件：取得新行情，目前策略具有正期望值。", "positive_ev_claim_requires_verified_symbol_and_strategy_qualification"),
    ("重新評估條件：取得新行情，策略已確認正期望值。", "positive_ev_claim_requires_verified_symbol_and_strategy_qualification"),
    ("重新評估條件已達成：策略通過正期望值資格。", "positive_ev_claim_requires_verified_symbol_and_strategy_qualification"),
    ("重新評估條件：取得新行情。策略擁有正期望值。", "positive_ev_claim_requires_verified_symbol_and_strategy_qualification"),
    ("再評估條件：取得新行情；策略通過正期望值資格。", "positive_ev_claim_requires_verified_symbol_and_strategy_qualification"),
    ("再評估條件：取得新行情\n策略通過正期望值資格。", "positive_ev_claim_requires_verified_symbol_and_strategy_qualification"),
    ("If new evidence arrives, then the strategy has positive EV.", "positive_ev_claim_requires_verified_symbol_and_strategy_qualification"),
    ("Reassessment conditions: new evidence. The system has positive EV.", "positive_ev_claim_requires_verified_symbol_and_strategy_qualification"),
    ("重新評估條件：取得新行情，且系統能夠持續賺錢。", "observed_results_cannot_guarantee_continuing_profit"),
    ("重新評估條件：取得新行情，且已完成紙上買進。", "campaign_fill_claim_requires_matching_broker_fill"),
])
def test_condition_context_cannot_launder_current_claims_or_other_outcomes(summary, blocker):
    check = campaign_summary_claims_check(objective=OBJECTIVE, task_kind="market_decision",
                                          final_summary=summary, observations=[])
    assert not check["passed"]
    assert blocker in check["blockers"]


@pytest.mark.parametrize("summary", [
    "已成交。", "紙上已成交。", "已完成模擬買進。",
    "尚未成交，但已成功紙上買進。", "The paper order was filled.",
    "已確認此系統擁有正期望值，能夠持續賺錢。", "The system has positive expected value.",
    "平均收益不足證明正EV，但是已證明正EV。",
])
def test_activation_only_cannot_prove_fills_qualification_or_continuing_profits(summary):
    result = _completion(summary)
    assert not result.passed
    check = next(c for c in result.checks if c["name"] == "autonomous_campaign_summary_claims_are_grounded")
    assert not check["passed"]


def test_actual_bound_paper_fill_supports_only_paper_and_matching_instrument():
    evidence = _fill_evidence()
    assert _completion("紙上已成交。", extra=evidence).passed
    assert not _completion("已成交。", extra=evidence).passed  # must identify simulated execution
    assert not _completion("已實盤成交。紙上流程運作正常。", extra=evidence).passed
    assert not _completion("2317.TW紙上已成交。", extra=evidence).passed
    assert not _completion("紙上已成交。", extra=evidence, bind_extra=False).passed
    wrong = deepcopy(evidence)
    wrong["result"]["plans"][0]["state"]["entry_receipt"]["raw"]["order"]["fill"]["account_id"] = "other"
    assert not _completion("紙上已成交。", extra=wrong).passed
    other_account = deepcopy(evidence)
    other_order = other_account["result"]["plans"][0]["state"]["entry_receipt"]["raw"]["order"]
    other_account["result"]["account_id"] = other_order["account_id"] = other_order["fill"]["account_id"] = "manual-paper-account"
    assert not _completion("紙上已成交。", extra=other_account).passed
    for summary in ("2317.TW紙上已成交。", "2317紙上已成交。"):
        assert not campaign_summary_claims_check(objective=OBJECTIVE, task_kind="market_decision",
            final_summary=summary, observations=[evidence])["passed"]


def test_positive_boolean_alone_is_not_strategy_qualification():
    extra = {"tool": "autonomy.research", "ok": True, "validation": {"passed": True}, "result": {
        "schema_version": "open_stock_ai.candle_qualification.v1", "symbol": "2330.TW",
        "candidate_id": "candle", "strategy_version_hash": "a"*64, "data_evidence": {"symbol": "2330.TW"},
        "passed": True, "positive_ev_qualified": True, "receipt_sha256": "b"*64}}
    assert not _completion("2330.TW candle 已確認正期望值。", extra=extra).passed


def test_verified_qualification_is_scoped_to_explicit_symbol_and_strategy(monkeypatch):
    calls = []
    def verify(receipt, *, strategy_id, strategy_version_hash):
        calls.append((receipt["symbol"], strategy_id, strategy_version_hash))
        return True
    monkeypatch.setattr("open_stock_ai.research.candle_qualification.verify_qualification_receipt", verify)
    evidence = {"tool": "autonomy.research", "ok": True, "validation": {"passed": True}, "result": {
        "schema_version": "open_stock_ai.candle_qualification.v1", "symbol": "2330.TW", "candidate_id": "candle",
        "strategy_version_hash": "a"*64, "data_evidence": {"symbol": "2330.TW"}}}
    def check(summary):
        return campaign_summary_claims_check(objective=OBJECTIVE, task_kind="market_decision",
                                            final_summary=summary, observations=[evidence])
    assert check("2330.TW candle 已確認正期望值。")["passed"]
    assert not check("2317.TW candle 已確認正期望值。")["passed"]
    assert not check("2330.TW other 已確認正期望值。")["passed"]
    assert not check("系統已確認正期望值。")["passed"]
    assert calls and all(call == ("2330.TW", "candle", "a"*64) for call in calls)


def test_retained_qualification_view_requires_complete_matching_payload_and_real_verifier(monkeypatch):
    from open_stock_ai.execution.trading_plan import content_hash

    payload = {"schema_version": "open_stock_ai.candle_qualification.v1", "symbol": "2330.TW",
               "candidate_id": "candle", "strategy_version_hash": "a"*64, "data_evidence": {"symbol": "2330.TW"}}
    row = {"tool": "autonomy.evidence", "ok": True, "validation": {"passed": True}, "result": {
        "kind": "qualification", "evidence_id": "AE-" + "b"*64,
        "retained_payload_sha256": content_hash(payload), "payload_view": payload}}

    def check(evidence):
        return campaign_summary_claims_check(objective=OBJECTIVE, task_kind="market_decision",
            final_summary="2330.TW candle 已確認正期望值。", observations=[evidence])["passed"]

    # A valid retention hash alone never supplies the missing empirical qualification.
    assert not check(row)
    calls = []
    def verify(receipt, *, strategy_id, strategy_version_hash):
        calls.append((receipt, strategy_id, strategy_version_hash))
        return True
    monkeypatch.setattr("open_stock_ai.research.candle_qualification.verify_qualification_receipt", verify)
    assert check(row)
    assert calls == [(payload, "candle", "a"*64)]
    for mutation in (
        lambda r: r["result"].update(kind="model_opinion"),
        lambda r: r["result"].update(is_partial_view=True),
        lambda r: r["result"].update(retained_payload_sha256="c"*64),
        lambda r: r["result"].pop("evidence_id"),
        lambda r: r["result"]["payload_view"].update(positive_ev_qualified=True),
        lambda r: r.update(ok=False),
        lambda r: r["validation"].update(passed=False),
    ):
        bad = deepcopy(row)
        mutation(bad)
        assert not check(bad)
    assert len(calls) == 1
