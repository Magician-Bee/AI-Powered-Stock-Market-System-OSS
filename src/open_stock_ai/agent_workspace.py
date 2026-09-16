from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any

from .runtime import get_runtime_engine
from .types import MissingSymbolError, StockDecision, StockRequest, SymbolContext, UniverseSnapshot


AGENT_TOOL_MANIFEST = {
    "schema_version": "open_stock_ai.agent_tool_manifest.v1",
    "name": "Open Stock AI Agent Workspace",
    "purpose": (
        "Give an embedded Agent one structured interface for market research, candidate ranking, "
        "risk inspection and paper-ledger review without requiring it to scrape the UI."
    ),
    "analysis_endpoints": [
        "/api/agents",
        "/api/agents/tools",
        "/api/open-stock-ai/agent/workspace",
        "/api/open-stock-ai/agent/watchlist",
        "/api/open-stock-ai/agent/portfolio",
        "/api/open-stock-ai/decision-review",
        "/api/open-stock-ai/paper-exposure",
        "/api/open-stock-ai/paper-orders",
        "/api/open-stock-ai/agent/research-pack",
        "/api/open-stock-ai/agent/paper-training/account",
        "/api/open-stock-ai/agent/paper-training/learning",
    ],
    "paper_training_endpoints": [
        "POST /api/agents/run (autonomy=paper_execute)",
        "POST /api/open-stock-ai/agent/paper-training/reset",
        "POST /api/open-stock-ai/agent/paper-training/episode",
        "POST /api/open-stock-ai/agent/paper-training/order",
        "POST /api/open-stock-ai/agent/paper-training/mark-to-market",
        "POST /api/open-stock-ai/agent/paper-training/evaluate",
        "POST /api/open-stock-ai/agent/paper-training/reflection",
    ],
    "computer_use_role": (
        "Computer Use may navigate charts, compare panels and operate the local UI, but structured API data "
        "is the source of truth for prices, timestamps, research status and risk gates."
    ),
    "rules": [
        "Treat buy_candidate and sell_candidate as research buckets, not order permission.",
        "Check execution_permission and risk.gate_checks before proposing any order action.",
        "Never present fallback, delayed or simulated data as realtime.",
        "A blocked research gate may still produce a watchlist candidate, but it may not create an order.",
        "Use source envelopes and market_data_contract blockers when explaining uncertainty.",
        "Use Agent portfolio/paper-exposure data instead of the UI demo asset workspace for position decisions.",
        "Paper or live execution must be an explicit separate action; workspace calls never submit orders.",
        "Training experiments deliberately bypass research and risk gates; those gates remain advisory evidence.",
        "Training still requires a verified real symbol, a verified market price, sufficient virtual cash and sufficient holdings.",
        "Never send a caller-supplied price to paper training; the server resolves the last trade or official close.",
    ],
}


def build_agent_tool_manifest() -> dict[str, Any]:
    return dict(AGENT_TOOL_MANIFEST)


def build_agent_portfolio() -> dict[str, Any]:
    """Return the real local paper ledger view used by RiskEngine.

    This intentionally does not read ``stock_ai.services.get_asset_workspace``
    because that screen may contain clearly labelled demo data for UI development.
    """
    engine = get_runtime_engine()
    trade_store = engine.pipeline.trade_store
    exposure = trade_store.exposure() if trade_store is not None else {
        "schema_version": "open_stock_ai.paper_portfolio_exposure.v1",
        "total_position_size_pct": 0.0,
        "symbols": {},
    }
    orders = trade_store.recent(limit=100) if trade_store is not None else []
    return {
        "schema_version": "open_stock_ai.agent_portfolio.v1",
        "method": "paper_trade_ledger_read_model",
        "mode": "paper",
        "source_of_truth": "open_stock_ai.trade_store",
        "demo_asset_workspace_used": False,
        "exposure": exposure,
        "order_count": len(orders),
        "orders": orders,
        "tool_manifest": build_agent_tool_manifest(),
        "execution_boundary": "read_only_paper_ledger",
    }


def build_agent_workspace(
    symbol: str | None = None,
    market: str = "TW",
    horizon: str = "swing",
    *,
    symbol_context: SymbolContext | None = None,
) -> dict[str, Any]:
    context = symbol_context or SymbolContext(
        symbol=str(symbol or "").strip() or None,
        source="user_explicit" if str(symbol or "").strip() else "none",
        confidence=1.0 if str(symbol or "").strip() else 0.0,
        evidence=("function_argument",) if str(symbol or "").strip() else (),
        user_confirmed=bool(str(symbol or "").strip()),
    )
    resolved_symbol = context.require_symbol()
    engine = get_runtime_engine()
    request = StockRequest(symbol=resolved_symbol, market=market, horizon=horizon)  # type: ignore[arg-type]
    decision = engine.analyze_for_agent(request)
    portfolio = _portfolio_from_engine(engine)
    return {
        **_workspace_payload(decision, portfolio=portfolio),
        "symbol_context": asdict(context),
    }


def build_agent_watchlist(
    *,
    horizon: str = "swing",
    limit: int = 20,
) -> dict[str, Any]:
    engine = get_runtime_engine()
    portfolio = _portfolio_from_engine(engine)
    requests = list(engine.pipeline.batch_requests)[: max(1, min(limit, 100))]
    items = [
        _workspace_payload(
            engine.analyze_for_agent(replace(request, horizon=horizon)),  # type: ignore[arg-type]
            portfolio=portfolio,
        )
        for request in requests
    ]
    items.sort(key=lambda item: item["ranking"]["priority_score"], reverse=True)
    buckets = {
        "buy_candidates": [item for item in items if item["recommendation_bucket"] == "buy_candidate"],
        "sell_candidates": [item for item in items if item["recommendation_bucket"] == "sell_candidate"],
        "watch": [item for item in items if item["recommendation_bucket"] == "watch"],
        "data_blocked": [item for item in items if item["recommendation_bucket"] == "data_blocked"],
    }
    return {
        "schema_version": "open_stock_ai.agent_watchlist.v1",
        "method": "single_engine_structured_agent_ranking",
        "horizon": horizon,
        "count": len(items),
        "universe": asdict(
            UniverseSnapshot(
                source="user_watchlist" if requests else "none",
                symbols=tuple(request.symbol for request in requests),
            )
        )
        | {"count": len(requests)},
        "portfolio": portfolio,
        "tool_manifest": build_agent_tool_manifest(),
        "bucket_counts": {key: len(value) for key, value in buckets.items()},
        "buckets": buckets,
        "items": items,
        "execution_boundary": "analysis_only_no_order_submission",
    }


def _portfolio_from_engine(engine: Any) -> dict[str, Any]:
    trade_store = getattr(engine.pipeline, "trade_store", None)
    exposure = trade_store.exposure() if trade_store is not None else {
        "schema_version": "open_stock_ai.paper_portfolio_exposure.v1",
        "total_position_size_pct": 0.0,
        "symbols": {},
    }
    return {
        "schema_version": "open_stock_ai.agent_portfolio_summary.v1",
        "source_of_truth": "open_stock_ai.trade_store",
        "demo_asset_workspace_used": False,
        "total_position_size_pct": exposure.get("total_position_size_pct", 0.0),
        "symbols": exposure.get("symbols") or {},
    }


def _workspace_payload(decision: StockDecision, *, portfolio: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = asdict(decision)
    data_contract = (
        payload.get("market_snapshot", {}).get("raw", {}).get("data_contract", {})
        if isinstance(payload.get("market_snapshot"), dict)
        else {}
    )
    validation_status = (
        payload.get("research", {}).get("raw", {}).get("validation_status", {})
        if isinstance(payload.get("research"), dict)
        else {}
    )
    recommendation_bucket = _recommendation_bucket(payload, data_contract)
    execution_permission = "paper_approved" if payload.get("risk", {}).get("approved") is True else "blocked"
    priority_score, score_factors = _priority_score(payload, data_contract, validation_status)
    blockers = _unique(
        list(data_contract.get("blockers") or [])
        + list(validation_status.get("blockers") or [])
        + [
            str(item.get("code"))
            for item in payload.get("risk", {}).get("gate_checks", [])
            if isinstance(item, dict) and item.get("applies", True) and item.get("passed") is False
        ]
    )
    portfolio = portfolio or {
        "schema_version": "open_stock_ai.agent_portfolio_summary.v1",
        "source_of_truth": "open_stock_ai.trade_store",
        "demo_asset_workspace_used": False,
        "total_position_size_pct": 0.0,
        "symbols": {},
    }
    symbol = str(payload.get("request", {}).get("symbol") or "")
    symbol_exposure = (portfolio.get("symbols") or {}).get(symbol) or {}

    return {
        "schema_version": "open_stock_ai.agent_workspace.v1",
        "method": "full_pipeline_analysis_without_execution",
        "symbol": symbol,
        "market": payload.get("request", {}).get("market"),
        "horizon": payload.get("request", {}).get("horizon"),
        "recommendation_bucket": recommendation_bucket,
        "execution_permission": execution_permission,
        "analysis_only": True,
        "ranking": {
            "priority_score": priority_score,
            "score_factors": score_factors,
        },
        "portfolio_status": {
            "source_of_truth": portfolio.get("source_of_truth"),
            "demo_asset_workspace_used": False,
            "total_position_size_pct": portfolio.get("total_position_size_pct", 0.0),
            "symbol_exposure": symbol_exposure,
        },
        "data_status": {
            "decision_ready": data_contract.get("decision_ready") is True,
            "execution_eligible": data_contract.get("execution_eligible") is True,
            "analysis_ready": data_contract.get("analysis_ready") is True,
            "analysis_mode": data_contract.get("analysis_mode"),
            "analysis_blockers": data_contract.get("analysis_blockers") or [],
            "is_realtime": data_contract.get("is_realtime") is True,
            "is_fallback": data_contract.get("is_fallback") is True,
            "is_simulated": data_contract.get("is_simulated") is True,
            "price_source": data_contract.get("price_source"),
            "exchange_timestamp": data_contract.get("exchange_timestamp"),
            "blockers": data_contract.get("blockers") or [],
        },
        "research_status": {
            "passed": payload.get("research", {}).get("passed") is True,
            "advisory_ready": validation_status.get("advisory_ready") is True,
            "execution_evidence_eligible": validation_status.get("execution_evidence_eligible") is True,
            "blockers": validation_status.get("blockers") or [],
        },
        "signal_summary": {
            "action": payload.get("signal", {}).get("action"),
            "confidence": payload.get("signal", {}).get("confidence"),
            "confidence_type": payload.get("signal", {}).get("confidence_type"),
            "confidence_calibrated": payload.get("signal", {}).get("confidence_calibrated") is True,
            "rule_score": payload.get("signal", {}).get("rule_score"),
            "rule_set_id": payload.get("signal", {}).get("rule_set_id"),
            "decision_status": payload.get("signal", {}).get("decision_status"),
            "entry_price": payload.get("signal", {}).get("entry_price"),
            "target_price": payload.get("signal", {}).get("target_price"),
            "stop_loss": payload.get("signal", {}).get("stop_loss"),
            "price_method_id": payload.get("signal", {}).get("price_method_id"),
            "position_size_pct": payload.get("signal", {}).get("position_size_pct"),
            "reason": payload.get("signal", {}).get("reason"),
        },
        # The Agent tool is intentionally a bounded projection rather than the
        # complete decision document.  It still has to contain the facts that
        # make an analysis meaningful, though.  Previously the compact tool
        # response only retained gates and recommendation metadata.  A model
        # would therefore see a successful call with no price, OHLCV or
        # technical evidence and (correctly) report that the content was
        # empty.  Keep a small, provenance-labelled evidence slice here; the
        # surrounding data/research status remains authoritative for whether
        # it can support a decision or execution.
        "analysis_snapshot": _analysis_snapshot(payload, data_contract),
        "risk_summary": {
            "approved": payload.get("risk", {}).get("approved") is True,
            "reason": payload.get("risk", {}).get("reason"),
            "adjusted_position_size_pct": payload.get("risk", {}).get("adjusted_position_size_pct"),
            "gate_checks": payload.get("risk", {}).get("gate_checks") or [],
        },
        "blockers": blockers,
        "agent_next_actions": _next_actions(recommendation_bucket, execution_permission, blockers),
        "tool_manifest": build_agent_tool_manifest(),
        "decision": payload,
        "execution_boundary": "analysis_only_no_order_submission",
    }


def _analysis_snapshot(payload: dict[str, Any], data_contract: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded factual evidence needed for an advisory analysis.

    This is deliberately a read-only projection.  It exposes neither a full
    raw provider payload nor an execution recommendation, and it carries the
    same source/trust context used by the Host completion gate.
    """
    snapshot = payload.get("market_snapshot") if isinstance(payload.get("market_snapshot"), dict) else {}
    intelligence = payload.get("intelligence") if isinstance(payload.get("intelligence"), dict) else {}
    raw_intelligence = intelligence.get("raw") if isinstance(intelligence.get("raw"), dict) else {}
    tradingagents = raw_intelligence.get("tradingagents") if isinstance(raw_intelligence.get("tradingagents"), dict) else {}
    fingpt = raw_intelligence.get("fingpt") if isinstance(raw_intelligence.get("fingpt"), dict) else {}
    finrobot = raw_intelligence.get("finrobot") if isinstance(raw_intelligence.get("finrobot"), dict) else {}
    technical = tradingagents.get("technical_metrics") if isinstance(tradingagents.get("technical_metrics"), dict) else {}
    financials = snapshot.get("financials") if isinstance(snapshot.get("financials"), dict) else {}
    revenue = financials.get("revenue") if isinstance(financials.get("revenue"), dict) else {}
    forecast = fingpt.get("forecast_projection") if isinstance(fingpt.get("forecast_projection"), dict) else {}
    report = finrobot.get("report_projection") if isinstance(finrobot.get("report_projection"), dict) else {}
    ohlcv = snapshot.get("ohlcv") if isinstance(snapshot.get("ohlcv"), list) else []

    return {
        "schema_version": "open_stock_ai.agent_analysis_snapshot.v1",
        "method": "bounded_pipeline_evidence_projection",
        "price": snapshot.get("price"),
        "latest_ohlcv": [
            {
                key: row.get(key)
                for key in ("date", "timestamp", "open", "high", "low", "close", "volume")
                if row.get(key) is not None
            }
            for row in ohlcv[-3:]
            if isinstance(row, dict)
        ],
        "technical": {
            key: technical.get(key)
            for key in (
                "technical_view",
                "change_percent",
                "momentum_score",
                "volatility_percent",
                "ma_short",
                "ma_long",
            )
            if technical.get(key) is not None
        },
        "fundamentals": {
            "view": intelligence.get("fundamental_view"),
            "revenue": {
                key: revenue.get(key)
                for key in ("period", "amount", "yoy_change_percent", "mom_change_percent")
                if revenue.get(key) is not None
            },
            "valuation": report.get("valuation") if isinstance(report.get("valuation"), dict) else {},
        },
        "sentiment": {
            "score": intelligence.get("sentiment_score"),
            "label": intelligence.get("sentiment_label"),
            "forecast_direction": forecast.get("direction"),
            "forecast_bin": forecast.get("bin_label"),
        },
        "source_context": {
            "price_source": data_contract.get("price_source"),
            "exchange_timestamp": data_contract.get("exchange_timestamp"),
            "is_realtime": data_contract.get("is_realtime") is True,
            "is_fallback": data_contract.get("is_fallback") is True,
            "is_simulated": data_contract.get("is_simulated") is True,
            "analysis_ready": data_contract.get("analysis_ready") is True,
            "decision_ready": data_contract.get("decision_ready") is True,
            "blockers": list(data_contract.get("blockers") or []),
        },
        "intelligence_summary": str(intelligence.get("summary") or "")[:2_000],
        "risks": list(intelligence.get("risks") or [])[:12],
    }


def _recommendation_bucket(payload: dict[str, Any], data_contract: dict[str, Any]) -> str:
    if data_contract and data_contract.get("decision_ready") is not True:
        if data_contract.get("analysis_ready") is True:
            return "watch"
        return "data_blocked"
    action = str(payload.get("signal", {}).get("action") or "hold")
    rule_score = abs(_number(payload.get("signal", {}).get("rule_score")) or 0.0)
    if action in {"buy", "add"} and rule_score >= 0.70:
        return "buy_candidate"
    if action in {"sell", "reduce"} and rule_score >= 0.70:
        return "sell_candidate"
    return "watch"


def _priority_score(
    payload: dict[str, Any],
    data_contract: dict[str, Any],
    validation_status: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    action = str(payload.get("signal", {}).get("action") or "hold")
    rule_score = abs(_number(payload.get("signal", {}).get("rule_score")) or 0.0)
    score = rule_score * 50.0
    factors: list[dict[str, Any]] = [
        {
            "factor": "signal_rule_score_magnitude",
            "value": round(rule_score * 50.0, 3),
            "method_id": "strategy.weighted_rule_score.v2",
        }
    ]

    action_bonus = 20.0 if action in {"buy", "add", "sell", "reduce"} else 5.0
    score += action_bonus
    factors.append({"factor": "action_strength", "value": action_bonus})

    data_bonus = 15.0 if data_contract.get("decision_ready") is True else -25.0
    score += data_bonus
    factors.append({"factor": "decision_ready_data", "value": data_bonus})

    research_bonus = 10.0 if validation_status.get("advisory_ready") is True else -10.0
    score += research_bonus
    factors.append({"factor": "advisory_research", "value": research_bonus})

    risk_bonus = 15.0 if payload.get("risk", {}).get("approved") is True else 0.0
    score += risk_bonus
    factors.append({"factor": "risk_permission", "value": risk_bonus})

    return round(max(0.0, min(100.0, score)), 3), factors


def _next_actions(bucket: str, permission: str, blockers: list[str]) -> list[str]:
    actions = ["Review source timestamps and evidence before forming a conclusion."]
    if bucket == "buy_candidate":
        actions.append("Compare entry, target, stop and invalidation conditions against current market context.")
    elif bucket == "sell_candidate":
        actions.append("Check current paper exposure and whether the signal means reduce or fully exit.")
    elif bucket == "data_blocked":
        actions.append("Refresh primary market data before discussing a tradable action.")
    else:
        actions.append("Keep the symbol on the observation list and identify the event that would change the rating.")
    if permission == "blocked":
        actions.append("Do not submit an order; explain the blocking research or risk gates.")
    if blockers:
        actions.append("Resolve or explicitly disclose blockers: " + ", ".join(blockers[:8]))
    return actions


def _number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))
