from __future__ import annotations

from stock_ai.codex_api import _enrich_prompt, _infer_market
from stock_ai.codex_market import _fallback_item, _merge_and_enforce


def _agent_item(**overrides):
    item = {
        "symbol": "2330.TW",
        "name": "台積電",
        "candidate_bucket": "buy_candidate",
        "execution_permission": "blocked",
        "supplied_signal": "buy",
        "risk_approved": False,
        "data_ready": True,
        "data_realtime": True,
        "data_fallback": False,
        "research_advisory_ready": True,
        "research_execution_eligible": False,
        "confidence": 0.82,
        "unified_score": 0.4,
        "technical": "positive",
        "risk_reason": "Research validation failed.",
        "blockers": ["exact_strategy_replay_not_implemented"],
    }
    item.update(overrides)
    return item


def test_codex_prompt_receives_structured_agent_context():
    prompt = _enrich_prompt(
        "這檔股票可以買嗎？",
        {
            "workspace_attached": True,
            "symbol": "2330.TW",
            "recommendation_bucket": "buy_candidate",
            "execution_permission": "blocked",
            "blockers": ["exact_strategy_replay_not_implemented"],
        },
    )

    assert "AGENT_CONTEXT=" in prompt
    assert '"recommendation_bucket":"buy_candidate"' in prompt
    assert '"execution_permission":"blocked"' in prompt
    assert "不得暗中建立委託" in prompt
    assert "USER_REQUEST=這檔股票可以買嗎？" in prompt


def test_codex_market_inference_supports_taiwan_us_and_crypto_symbols():
    assert _infer_market("2330.TW") == "TW"
    assert _infer_market("0050") == "TW"
    assert _infer_market("AAPL") == "US"
    assert _infer_market("BTC-USD") == "CRYPTO"


def test_codex_agent_cannot_convert_candidate_to_buy_now_without_all_gates():
    sources = [_agent_item()]
    ranked = [
        {
            "symbol": "2330.TW",
            "action": "buy_now",
            "timing": "現在",
            "confidence": 0.99,
            "reason": "模型看多",
        }
    ]

    result = _merge_and_enforce(sources, ranked, explain=True)[0]

    assert result["action"] == "wait_to_buy"
    assert result["next_action"] == "列入買進候選，設定提醒"
    assert "研究候選" in result["reason"]
    assert "研究、風控通過" in result["trigger"]


def test_codex_agent_allows_paper_buy_only_when_data_research_and_risk_pass():
    source = _agent_item(
        execution_permission="paper_approved",
        risk_approved=True,
        research_execution_eligible=True,
        blockers=[],
    )

    result = _fallback_item(source, explain=True)

    assert result["action"] == "buy_now"
    assert result["next_action"] == "由使用者明確啟動紙上委託"
    assert "資料、研究與風控已通過" in result["trigger"]


def test_codex_agent_blocks_execution_when_primary_data_is_not_ready():
    source = _agent_item(
        execution_permission="paper_approved",
        risk_approved=True,
        research_execution_eligible=True,
        data_ready=False,
        data_realtime=False,
        data_fallback=True,
        blockers=["fallback_price_not_execution_eligible"],
    )

    result = _fallback_item(source, explain=True)

    assert result["action"] == "hold"
    assert result["timing"] == "先更新主要行情資料"
    assert result["next_action"] == "保持觀察，不送出委託"
    assert result["trigger"] == "主要行情恢復後重新分析"
    assert "資料未就緒" in result["reason"]


def test_codex_agent_sell_candidate_is_not_immediate_without_execution_permission():
    source = _agent_item(
        candidate_bucket="sell_candidate",
        supplied_signal="sell",
        confidence=0.78,
        unified_score=-0.35,
    )
    ranked = [
        {
            "symbol": "2330.TW",
            "action": "sell_now",
            "timing": "現在",
            "confidence": 0.95,
            "reason": "應立即賣出",
        }
    ]

    result = _merge_and_enforce([source], ranked, explain=False)[0]

    assert result["action"] == "wait_to_sell"
    assert result["next_action"] == "列入減碼候選，檢查既有曝險"
