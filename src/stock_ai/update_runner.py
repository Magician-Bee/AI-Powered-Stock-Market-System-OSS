from __future__ import annotations

from typing import Any

from .schedule_guard import active_schedule_phase, evaluate_task_policy, parse_taipei_datetime
from .source_policy import evaluate_source_policy
from .system_contract import UPDATE_SCHEDULE


SCHEMA_VERSION = "stock_ai.update_runner.v1"


TASK_ACTIONS: dict[str, dict[str, Any]] = {
    "更新美股、費半、ADR、匯率、期貨": {
        "data_modules": ["market_indices", "news"],
        "source_candidates": ["Yahoo Finance", "Google News", "TAIFEX"],
        "candidate_endpoints": ["/api/overview/indices", "/api/official/taifex/derivatives-summary/import", "/api/official/taifex/derivatives-summary/import-csv", "/api/official/taifex/derivatives-summary", "/api/news/center"],
        "payload_type": "auxiliary",
        "operation": "refresh_global_context",
    },
    "抓取重大新聞": {
        "data_modules": ["news"],
        "source_candidates": ["Google News", "MoneyDJ", "MOPS"],
        "candidate_endpoints": ["/api/news/center", "/api/official/mops/company-events/import", "/api/official/mops/company-events/import-csv", "/api/official/mops/company-events", "/api/market/{symbol}/events"],
        "payload_type": "news",
        "operation": "refresh_major_news",
    },
    "檢查 MOPS 重大訊息": {
        "data_modules": ["company_events", "news"],
        "source_candidates": ["MOPS", "TWSE OpenAPI"],
        "candidate_endpoints": ["/api/official/mops/company-events/import", "/api/official/mops/company-events/import-csv", "/api/official/mops/company-events", "/api/market/{symbol}/events"],
        "payload_type": "event",
        "operation": "refresh_material_events",
    },
    "產生盤前觀察清單": {
        "data_modules": ["ai_signals", "news", "market_indices"],
        "source_candidates": ["TWSE OpenAPI", "MOPS", "Google News"],
        "candidate_endpoints": ["/api/reports/daily", "/api/watchlist/overview"],
        "payload_type": "decision",
        "operation": "build_pre_market_watchlist",
    },
    "使用授權行情或券商 API streaming": {
        "data_modules": ["real_time_quotes", "ohlcv"],
        "source_candidates": ["TWSE/TPEx/TAIFEX 授權即時行情", "正式券商 API"],
        "candidate_endpoints": ["/api/realtime/stream/{symbol}", "/api/realtime/quote/{symbol}"],
        "payload_type": "quote",
        "operation": "connect_authorized_realtime_stream",
    },
    "監控自選股、持股、候選股": {
        "data_modules": ["real_time_quotes", "portfolio", "ai_signals"],
        "source_candidates": ["TWSE MIS", "local_preview_portfolio"],
        "candidate_endpoints": ["/api/watchlist/overview", "/api/assets/summary"],
        "payload_type": "quote",
        "operation": "monitor_watchlist_and_positions",
    },
    "偵測急漲急跌、爆量、突破、跌破、法人/籌碼異常": {
        "data_modules": ["real_time_quotes", "ohlcv", "chip_data", "institutional_investors"],
        "source_candidates": ["TWSE MIS", "TWSE OpenAPI", "TPEx OpenAPI"],
        "candidate_endpoints": ["/api/realtime/quote/{symbol}", "/api/flow/institutional", "/api/flow/margin"],
        "payload_type": "quote",
        "operation": "detect_intraday_anomalies",
    },
    "抓取官方收盤資料": {
        "data_modules": ["ohlcv", "market_indices"],
        "source_candidates": ["TWSE OpenAPI", "TPEx OpenAPI"],
        "candidate_endpoints": ["/api/market/{symbol}/history", "/api/overview/indices"],
        "payload_type": "ohlcv",
        "operation": "refresh_official_close",
    },
    "更新日 K": {
        "data_modules": ["ohlcv"],
        "source_candidates": ["TWSE OpenAPI", "TPEx OpenAPI"],
        "candidate_endpoints": ["/api/market/{symbol}/history"],
        "payload_type": "ohlcv",
        "operation": "refresh_daily_ohlcv",
    },
    "更新三大法人、融資融券、成交排行": {
        "data_modules": ["institutional_investors", "chip_data"],
        "source_candidates": ["TWSE OpenAPI", "TPEx OpenAPI"],
        "candidate_endpoints": ["/api/flow/institutional", "/api/flow/margin"],
        "payload_type": "official_flow",
        "operation": "refresh_chip_and_institutional",
    },
    "產生收盤報告": {
        "data_modules": ["ai_signals", "news", "ohlcv", "institutional_investors"],
        "source_candidates": ["TWSE OpenAPI", "TPEx OpenAPI", "MOPS", "Google News"],
        "candidate_endpoints": ["/api/reports/daily"],
        "payload_type": "decision",
        "operation": "build_close_report",
    },
    "更新月營收、財報、重大訊息": {
        "data_modules": ["fundamentals", "company_events"],
        "source_candidates": ["MOPS", "TWSE OpenAPI"],
        "candidate_endpoints": ["/api/fundamentals/revenue", "/api/official/mops/company-events/import", "/api/official/mops/company-events/import-csv", "/api/official/mops/company-events", "/api/market/{symbol}/events"],
        "payload_type": "fundamental",
        "operation": "refresh_fundamentals_and_events",
    },
    "更新新聞與產業資料": {
        "data_modules": ["news"],
        "source_candidates": ["Google News", "MoneyDJ", "鉅亨網", "MOPS"],
        "candidate_endpoints": ["/api/news/center"],
        "payload_type": "news",
        "operation": "refresh_news_and_industry",
    },
    "更新回測資料": {
        "data_modules": ["ohlcv", "ai_signals"],
        "source_candidates": ["TWSE OpenAPI", "TPEx OpenAPI"],
        "candidate_endpoints": ["/api/market/{symbol}/history", "/api/open-stock-ai/session/after_market"],
        "payload_type": "ohlcv",
        "operation": "refresh_backtest_dataset",
    },
    "反思今日交易與訊號品質": {
        "data_modules": ["ai_signals", "portfolio", "orders_executions"],
        "source_candidates": ["decision_log", "paper_order_payload", "local_preview_portfolio"],
        "candidate_endpoints": ["/api/open-stock-ai/decision-review", "/api/open-stock-ai/paper-orders"],
        "payload_type": "decision",
        "operation": "review_signal_quality",
    },
    "更新 TDCC 集保股權分散": {
        "data_modules": ["chip_data"],
        "source_candidates": ["TDCC"],
        "candidate_endpoints": ["/api/official/tdcc/holding-distribution/import", "/api/official/tdcc/holding-distribution/import-csv", "/api/official/tdcc/holding-distribution"],
        "payload_type": "official_chip",
        "operation": "refresh_tdcc_distribution",
    },
    "檢查大戶比例、小股東人數、籌碼集中度": {
        "data_modules": ["chip_data"],
        "source_candidates": ["TDCC"],
        "candidate_endpoints": ["/api/official/tdcc/holding-distribution"],
        "payload_type": "official_chip",
        "operation": "analyze_tdcc_concentration",
    },
    "產生週報": {
        "data_modules": ["ai_signals", "chip_data", "fundamentals", "news"],
        "source_candidates": ["TDCC", "TWSE OpenAPI", "TPEx OpenAPI", "MOPS", "Google News"],
        "candidate_endpoints": ["/api/open-stock-ai/session/after_market"],
        "payload_type": "decision",
        "operation": "build_weekly_report",
    },
}


def _decision_sample_payload(action: dict[str, Any], sources: list[str]) -> dict[str, Any]:
    return {
        "payload_type": "decision",
        "technical_reason": "技術面資料待更新",
        "chip_reason": "籌碼資料待更新",
        "fundamental_reason": "基本面資料待更新",
        "news_reason": "新聞與事件資料待更新",
        "risk": "乾跑模式不產生實際部位",
        "entry_price": "dry_run",
        "stop_loss": "dry_run",
        "take_profit": "dry_run",
        "invalid_condition": "資料不足或來源衝突",
        "confidence": 0.0,
        "confidence_type": "none",
        "confidence_calibrated": False,
        "data_timestamp": "dry_run",
        "data_sources": sources,
    }


def _policy_payload(action: dict[str, Any]) -> dict[str, Any]:
    payload_type = action["payload_type"]
    sources = action["source_candidates"]
    if payload_type in {"quote", "ohlcv"}:
        return {"payload_type": payload_type, "last_price": 0, "data_sources": sources}
    if payload_type == "decision":
        return _decision_sample_payload(action, sources)
    return {"payload_type": payload_type, "data_sources": sources}


def build_update_plan(
    *,
    phase: str | None = None,
    as_of: str | None = None,
    broker_api_connected: bool = False,
    authorized_realtime_feed: bool = False,
    live_ordering_enabled: bool = False,
) -> dict[str, Any]:
    local = parse_taipei_datetime(as_of)
    active = phase or active_schedule_phase(local)
    phases = []
    for schedule in UPDATE_SCHEDULE:
        phase_name = schedule["phase"]
        jobs = []
        for order, task in enumerate(schedule["tasks"], start=1):
            task_policy = evaluate_task_policy(
                task,
                broker_api_connected=broker_api_connected,
                authorized_realtime_feed=authorized_realtime_feed,
                live_ordering_enabled=live_ordering_enabled,
            )
            action = TASK_ACTIONS[task]
            source_gate = evaluate_source_policy(_policy_payload(action))
            not_active = phase is None and phase_name != active
            blockers = list(task_policy["blockers"])
            if source_gate["price_policy"]["blockers"]:
                blockers.extend(source_gate["price_policy"]["blockers"])
            if source_gate["decision_policy"]["blockers"] and action["payload_type"] == "decision":
                blockers.extend(source_gate["decision_policy"]["blockers"])
            jobs.append(
                {
                    "job_id": f"{phase_name}-{order:02d}",
                    "phase": phase_name,
                    "task": task,
                    "operation": action["operation"],
                    "dry_run": True,
                    "active_now": phase_name == active,
                    "selected_for_run": phase_name == active if phase is None else phase_name == phase,
                    "status": "not_active" if not_active else ("blocked" if blockers else "ready"),
                    "blockers": blockers,
                    "source_tier": task_policy["source_tier"],
                    "data_modules": action["data_modules"],
                    "source_candidates": action["source_candidates"],
                    "candidate_endpoints": action["candidate_endpoints"],
                    "source_policy": {
                        "overall_allowed": source_gate["overall_allowed"],
                        "price_allowed": source_gate["price_policy"]["allowed"],
                        "decision_allowed": source_gate["decision_policy"]["allowed"],
                        "source_summary": source_gate["source_summary"],
                    },
                }
            )
        phases.append(
            {
                "phase": phase_name,
                "window": schedule["window"],
                "active": phase_name == active,
                "selected": phase_name == active if phase is None else phase_name == phase,
                "job_count": len(jobs),
                "ready_job_count": sum(1 for item in jobs if item["status"] == "ready"),
                "blocked_job_count": sum(1 for item in jobs if item["status"] == "blocked"),
                "jobs": jobs,
            }
        )
    selected_jobs = [job for item in phases for job in item["jobs"] if job["selected_for_run"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "method": "dry_run_update_plan_from_goal_schedule",
        "timezone": "Asia/Taipei",
        "as_of": local.isoformat(),
        "active_phase": active,
        "requested_phase": phase,
        "dry_run": True,
        "broker_api_connected": broker_api_connected,
        "authorized_realtime_feed": authorized_realtime_feed,
        "live_ordering_enabled": live_ordering_enabled,
        "phase_count": len(phases),
        "job_count": sum(item["job_count"] for item in phases),
        "selected_job_count": len(selected_jobs),
        "ready_selected_job_count": sum(1 for item in selected_jobs if item["status"] == "ready"),
        "blocked_selected_job_count": sum(1 for item in selected_jobs if item["status"] == "blocked"),
        "phases": phases,
    }


def dry_run_update(
    *,
    phase: str | None = None,
    as_of: str | None = None,
    broker_api_connected: bool = False,
    authorized_realtime_feed: bool = False,
    live_ordering_enabled: bool = False,
) -> dict[str, Any]:
    plan = build_update_plan(
        phase=phase,
        as_of=as_of,
        broker_api_connected=broker_api_connected,
        authorized_realtime_feed=authorized_realtime_feed,
        live_ordering_enabled=live_ordering_enabled,
    )
    selected_jobs = [job for item in plan["phases"] for job in item["jobs"] if job["selected_for_run"]]
    return {
        "schema_version": "stock_ai.update_run.v1",
        "method": "dry_run_only_no_mutation",
        "dry_run": True,
        "plan_schema_version": plan["schema_version"],
        "active_phase": plan["active_phase"],
        "requested_phase": plan["requested_phase"],
        "executed_job_count": sum(1 for item in selected_jobs if item["status"] == "ready"),
        "blocked_job_count": sum(1 for item in selected_jobs if item["status"] == "blocked"),
        "mutated": False,
        "live_ordering_allowed": broker_api_connected and live_ordering_enabled,
        "jobs": [
            {
                "job_id": job["job_id"],
                "task": job["task"],
                "operation": job["operation"],
                "status": "dry_run_ready" if job["status"] == "ready" else job["status"],
                "blockers": job["blockers"],
                "would_call": job["candidate_endpoints"],
                "would_update_modules": job["data_modules"],
            }
            for job in selected_jobs
        ],
    }
