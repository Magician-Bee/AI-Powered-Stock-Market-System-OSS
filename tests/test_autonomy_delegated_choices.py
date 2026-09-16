"""A delegated trading decision is not an invitation to invent a decision card."""
from copy import deepcopy

import pytest

from open_stock_ai.agent_runtime.orchestrator import (
    _explicit_choice_options,
    _explicit_choice_recommendation_reprompt_required,
    _materialize_explicit_choice_interaction,
)


# Original AR-77 objective, with opaque receipt contents shortened; the full
# decision-delegation and choice-producing clauses are retained verbatim.
OBJECTIVE = '''[MARKET_SCOPE] This is a whole-market autonomous campaign. Discover and compare securities using broad market evidence, then choose supported candidates within the authorized paper account. The currently selected chart security and classifier-invented tickers are not implicit targets.
請啟動台灣上市櫃全市場的自主紙上交易流水線，並用一個整合任務完成研究、決策、保存交易計畫、啟用執行與核對狀態。這是紙上交易的正式流程驗收，使用隔離的自主帳戶。
先呼叫 autonomy.research(cycle_id="AC-retained") 回讀資料層已完成的 deep_limit=2 全市場掃描與深查證據，避免重複下載；用 autonomy.evidence 讀取候選的實際K線，視決策需要搭配既有基本面、籌碼、事件與市場工具。由你自己選擇策略、標的、股數、開始時間、價格條件、停損、目標與最晚出場時間。每筆風險預算上限5%、總實驗曝險上限20%，交由Host再核對。
有合理機會時，用 autonomy.propose_plan 保存你自己的計畫；你可以決定現在、幾天後或觸發特定價格才進場。資料或進場依據不足時保留現金，說明等待及重新評估條件。再用 autonomy.activate 啟動同一個cycle，use_candidate_plans=false，讓後台持續管理委託、持倉和每日有預算的Codex複查；以 autonomy.status 核對結果。
整體只使用這一個任務完成；後續監控交給後台，這次取證與啟用完成即可回答。請交代真正掃描及深查範圍、具體計畫、費稅與滑價假設、Host保存的收據，以及目前實際成交和正期望值資格。各項事實以工具證據為準。
本次沿用正式流水線與既有隔離帳戶。資料層新版已核對成本版本文件並完成兩檔各250根日K；先前失敗收據留作歷史，本次以此保留cycle為依據完成一個整合決策。'''


@pytest.mark.parametrize("objective", [
    OBJECTIVE,
    "由你自己選擇策略、標的、股數、開始時間、價格條件、停損、目標與最晚出場時間。",
    "請選擇策略、標的、股數與停損。",
    "請你自主在方案A與方案B之間選擇。",
    "由你自行選擇保守型或平衡型，不用等待我的偏好。",
    "請自行選擇方案A或方案B。",
    "請自主選擇方案A或方案B。",
])
def test_delegation_and_field_lists_never_materialize_a_user_choice(objective):
    turn = {"state": "complete", "summary": "最晚出場時間不適用。已自主採用方案A，保守型條件未符合。", "tool_calls": []}
    before = deepcopy(turn)
    assert _explicit_choice_options(objective) == []
    assert _materialize_explicit_choice_interaction(objective=objective, turn=turn, transcript=[]) is None
    assert turn == before
    assert not _explicit_choice_recommendation_reprompt_required(
        objective=objective, turn={**turn, "state": "waiting_decision"}, transcript=[])


@pytest.mark.parametrize("objective,expected", [
    ("請在方案A與方案B之間讓我選擇。", ["方案A", "方案B"]),
    ("請選擇方案A或方案B。", ["方案A", "方案B"]),
    ("請在保守型與平衡型投資計畫中幫我選一個。", ["保守型", "平衡型投資計畫"]),
    ("請你自主分析後在方案A與方案B之間讓我選擇。", ["方案A", "方案B"]),
    ("由你自己選擇策略、股數與停損；請在方案A與方案B之間讓我選擇。", ["方案A", "方案B"]),
])
def test_true_user_binary_choice_is_still_materialized(objective, expected):
    turn = {"state": "complete", "summary": f"暫定建議採用{expected[0]}。", "tool_calls": []}
    assert _explicit_choice_options(objective) == expected
    result = _materialize_explicit_choice_interaction(objective=objective, turn=turn, transcript=[])
    assert result["host_explicit_choice_checkpoint"] is True
    assert [row["label"] for row in result["options"]] == expected
    assert result["preferred_option"] == "explicit_choice_1"


def test_delegation_does_not_remove_an_existing_external_authorization_request():
    turn = {"state": "waiting_user_input", "summary": "對外發布尚待使用者明確授權。", "tool_calls": [],
            "interaction": {"prompt": "是否授權對外發布這份分析？", "options": [{"label": "授權"}, {"label": "取消"}]}}
    before = deepcopy(turn)
    assert _materialize_explicit_choice_interaction(
        objective="由你自行選擇方案A或方案B，對外發布前必須先取得我的授權。", turn=turn, transcript=[]) is None
    assert turn == before and turn["state"] == "waiting_user_input"


def test_explicit_publication_choice_survives_a_separate_delegated_analysis_clause():
    objective = "由你自己選擇策略、股數與停損；請在允許發布與取消發布之間讓我選擇。"
    result = _materialize_explicit_choice_interaction(objective=objective,
        turn={"state": "waiting_decision", "summary": "建議取消發布，尚未取得外部發布授權。", "tool_calls": []}, transcript=[])
    assert [row["label"] for row in result["options"]] == ["允許發布", "取消發布"]
    assert result["preferred_option"] == "explicit_choice_2"
