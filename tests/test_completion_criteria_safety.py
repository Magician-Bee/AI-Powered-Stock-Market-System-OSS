from __future__ import annotations

import pytest

from open_stock_ai.agent_runtime.plan_graph import PlanGraph
from open_stock_ai.agent_runtime.validators import ValidatorEngine, _criterion_check


SAFE_CRITERION = "未將候選訊號表述為交易許可，且未虛構價格、持倉或成交"
MARKET_EVIDENCE = {
    "call_id": "market", "tool": "market.research_pack", "ok": True,
    "validation": {"passed": True},
    "result": {"symbol": "2330.TW", "technical_features": {"rsi_14": 64.0625}, "price": 2450.0},
}


@pytest.mark.parametrize("criterion", [
    SAFE_CRITERION,
    "未虛構價格、持倉或成交",
    "未將訊號當交易許可",
    "Critic 未執行交易、下單或自動化並完成本地結論",
    "已分析成交量與市場最新成交價2450",
    "已說明交易日／交易所資料與市場最後成交價、成交量",
    "No fabricated prices, positions or trades",
    "Do not invent positions or orders",
])
def test_answer_integrity_and_market_data_criteria_use_bound_market_evidence(criterion):
    check = _criterion_check(
        criterion, {"met": True, "evidence_ids": ["market"]},
        known_evidence_ids={"market"}, evidence_catalog={"market": MARKET_EVIDENCE}, required=True,
    )
    assert check["passed"] is True
    assert check["required_tool_prefixes"] == []


@pytest.mark.parametrize("criterion", [
    "已成交且未虛構",
    "尚未完成買進",
    "已完成模擬買進且未虛構成交",
    "不交易；但已送出委託",
    "No trade advice; the order was filled at 2450",
    "本次買進100股，成交價2450，未虛構",
    "未虛構成交，且不存在任何持倉",
    "本次交易已成交",
    "已核對帳戶持倉",
    "Verify holdings without trading",
])
def test_trade_or_portfolio_assertions_still_require_financial_tool_receipts(criterion):
    check = _criterion_check(
        criterion, {"met": True, "evidence_ids": ["market"]},
        known_evidence_ids={"market"}, evidence_catalog={"market": MARKET_EVIDENCE}, required=True,
    )
    assert check["passed"] is False
    assert check["required_tool_prefixes"] == ["paper.", "portfolio."]
    assert check["evidence_type_matched"] is False
    with_receipt = _criterion_check(
        criterion, {"met": True, "evidence_ids": ["paper"]},
        known_evidence_ids={"paper"},
        evidence_catalog={"paper": {"tool": "paper.submit_order", "ok": True, "validation": {"passed": True}}},
        required=True,
    )
    assert with_receipt["passed"] is True


def _validate_safety_answer(*, summary: str, evidence_id: str = "market", evidence_valid: bool = True):
    plan = PlanGraph.create("分析2330.TW技術面")
    plan.completion_criteria = [SAFE_CRITERION]
    evidence = {**MARKET_EVIDENCE, "validation": {"passed": evidence_valid}}
    return ValidatorEngine().validate_completion(
        state="complete", objective="分析2330.TW技術面，不交易", task_kind="market_information",
        final_summary=summary, has_pending_tool_calls=False, plan=plan.to_dict(),
        successful_observations=1, evidence_required=True,
        completion_evaluation={
            "criteria_met": True, "remaining_gaps": [], "evidence_ids": [evidence_id],
            "criterion_results": [{"criterion": SAFE_CRITERION, "met": True, "evidence_ids": [evidence_id]}],
        },
        known_evidence_ids={"market"}, evidence_catalog={"market": evidence},
    )


def test_negative_safety_criterion_does_not_bypass_numeric_or_host_evidence_checks():
    valid = _validate_safety_answer(summary="RSI 14 為64.0625；價格2450.0，本次只分析。")
    assert valid.passed is True
    assert next(c for c in valid.checks if c["name"] == "semantic_validation_layer")["advisory_uncertain_entailment_accepted"] is True

    fabricated = _validate_safety_answer(summary="RSI 14 為99.987；價格2450.0，本次只分析。")
    assert fabricated.passed is False
    assert next(c for c in fabricated.checks if c["name"] == "final_answer_numeric_claims_are_evidence_grounded")["passed"] is False
    assert _validate_safety_answer(summary="價格2450.0", evidence_id="unknown").passed is False
    assert _validate_safety_answer(summary="價格2450.0", evidence_valid=False).passed is False
