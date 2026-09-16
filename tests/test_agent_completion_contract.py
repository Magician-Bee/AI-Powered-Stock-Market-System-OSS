from __future__ import annotations

from open_stock_ai.agent_runtime.completion_contract import (
    evaluate_objective_completion,
    observation_is_substantive,
    objective_completion_contract,
)
from open_stock_ai.agent_runtime.orchestrator import _classify_task, _recovery_links_for_call


def _candidate(symbol: str) -> dict:
    return {
        "schema_version": "stock_ai.market_universe_observation.v1",
        "items": [
            {
                "ok": True,
                "analysis": {
                    "symbol": symbol,
                    "data_status": {"decision_ready": True},
                    "risk_summary": {"approved": True},
                },
            }
        ],
    }


def test_explicit_order_request_cannot_be_downgraded_by_model_task_header() -> None:
    objective = "[MODEL_TASK_KIND:market_information] 找出最佳股票後下單"

    assert _classify_task(objective) == "market_decision"
    assert objective_completion_contract(objective, "market_information")["order_requested"] is True


def test_analysis_only_with_paper_trade_wording_does_not_require_order_receipts() -> None:
    objective = "請分析 3105.TWO 的資料品質與主要風險；僅分析，不建立紙上交易。"

    contract = objective_completion_contract(objective, "market_information")

    assert contract == {
        "decision_requested": False,
        "best_candidate_requested": False,
        "order_requested": False,
        "paper_order_requested": False,
    }


def test_analysis_explicitly_forbidding_paper_and_live_trade_remains_advisory() -> None:
    objective = "請分析 2887.TW 的資料狀態與主要風險；不要紙上或實盤交易。"

    contract = objective_completion_contract(objective, "market_information")

    assert contract == {
        "decision_requested": False,
        "best_candidate_requested": False,
        "order_requested": False,
        "paper_order_requested": False,
    }


def test_punctuation_separated_no_paper_trade_constraint_remains_advisory() -> None:
    objective = "只做研究，不建立 Artifact、紙上交易、實盤交易或自動化。"

    contract = objective_completion_contract(objective, "market_information")

    assert contract["paper_order_requested"] is False
    assert contract["order_requested"] is False


def test_paper_trade_uses_verified_paper_receipts_without_production_research_gate() -> None:
    objective = "請分析 3105.TWO 並完成一筆紙上模擬買進交易"
    preview = {
        "schema_version": "open_stock_ai.paper_broker_preview.v1",
        "can_submit": True,
        "market": {"price": 405.0, "source_envelope": {"signature": "verified-price"}},
    }
    execution = {"schema_version": "open_stock_ai.paper_training_execution.v2"}

    contract = objective_completion_contract(objective, "market_decision")
    check = evaluate_objective_completion(
        objective=objective,
        task_kind="market_decision",
        observations=[
            {"ok": True, "tool": "paper.preview_order", "call_id": "preview", "result": preview},
            {"ok": True, "tool": "paper.submit_order", "call_id": "submit", "result": execution},
        ],
        decision=None,
    )

    assert contract["paper_order_requested"] is True
    assert check["passed"] is True


def test_nested_data_blocked_universe_is_not_substantive() -> None:
    observation = {
        "ok": True,
        "result": {
            "schema_version": "stock_ai.market_universe_observation.v1",
            "items": [
                {
                    "ok": True,
                    "analysis": {
                        "symbol": "SYMBOL1",
                        "recommendation_bucket": "data_blocked",
                        "data_status": {"decision_ready": False},
                    },
                }
            ],
        },
    }

    assert observation_is_substantive(observation) is False


def test_signed_official_close_workspace_is_substantive_but_not_decision_ready() -> None:
    observation = {
        "ok": True,
        "result": {
            "schema_version": "open_stock_ai.agent_workspace.v1",
            "symbol": "2887.TW",
            "recommendation_bucket": "watch",
            "analysis_only": True,
            "execution_permission": "blocked",
            "data_status": {
                "decision_ready": False,
                "analysis_ready": True,
                "analysis_mode": "official_close_advisory",
            },
        },
    }

    assert observation_is_substantive(observation) is True
    assert evaluate_objective_completion(
        objective="只做 2887.TW 資料分析，不要交易。",
        task_kind="market_information",
        observations=[observation],
        decision=None,
    )["passed"] is True


def test_valid_research_and_fake_universe_cannot_complete_best_order_objective() -> None:
    observations = [
        {
            "ok": True,
            "tool": "web.research",
            "result": {
                "schema_version": "open_stock_ai.web_research.v1",
                "source_count": 3,
                "sources": [{"url": "https://twse.example/official"}],
            },
        },
        {
            "ok": True,
            "tool": "market.analyze_universe",
            "result": {
                "schema_version": "stock_ai.market_universe_observation.v1",
                "items": [
                    {
                        "ok": True,
                        "analysis": {
                            "symbol": "SYMBOL1",
                            "data_status": {"decision_ready": False},
                            "recommendation_bucket": "data_blocked",
                        },
                    },
                    {
                        "ok": True,
                        "analysis": {
                            "symbol": "SYMBOL2",
                            "data_status": {"decision_ready": False},
                            "recommendation_bucket": "data_blocked",
                        },
                    },
                ],
            },
        },
    ]

    check = evaluate_objective_completion(
        objective="現在有什麼股票可以買，找一個最好股票然後下單",
        task_kind="market_decision",
        observations=observations,
        decision=None,
    )

    assert check["passed"] is False
    assert "decision_ready_market_candidate" in check["missing_requirements"]
    assert "approved_order_execution_receipt" in check["missing_requirements"]


def test_example_fetch_cannot_close_market_failure_recovery() -> None:
    trace = [
        {
            "node_id": "market-node",
            "call_id": "failed",
            "tool": "market.search_taiwan_securities",
            "arguments": {"query": "台股候選"},
            "ok": False,
        }
    ]
    links = _recovery_links_for_call(
        trace,
        {
            "id": "example",
            "name": "web.fetch",
            "arguments": {"url": "https://example.com"},
        },
        [
            {"name": "web.fetch"},
            {"name": "market.search_taiwan_securities"},
        ],
        objective="找出台股候選並分析哪一支可以買",
        symbols=(),
        result={"status_code": 200, "title": "Example Domain", "content": "Example"},
    )

    assert links == []
