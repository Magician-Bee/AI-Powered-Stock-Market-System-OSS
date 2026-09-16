"""Application wiring for the shared autonomous trading campaign."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone, time
import logging
from pathlib import Path
from threading import Event, RLock
from zoneinfo import ZoneInfo

from open_stock_ai.execution.autonomous_campaign import AutonomousCampaign
from open_stock_ai.execution.broker_port import PaperBrokerPort
from open_stock_ai.execution.paper_broker import PaperBrokerSimulator
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.paper_odd_lot import OddLotBoardProxyModel
from open_stock_ai.execution.trading_plan import content_hash
from open_stock_ai.execution.trading_plan_store import TradingPlanStore
from open_stock_ai.research.official_candle_loader import load_official_candles
from open_stock_ai.runtime import get_runtime_engine
from .market_calendar import default_taiwan_market_calendar


_SERVICE: AutonomousCampaign | None = None
_LOCK = RLock()
logger = logging.getLogger(__name__)


_SECURITY_MASTER_UPDATE_SLO_POLICY = {
    "schema_version": "stock_ai.autonomous_market_update_slo_policy.v1",
    "scope": "taiwan_current_security_master_baseline_with_separate_historical_gap_ledger",
    "required_partition_keys": [
        "twse_isin_listed",
        "tpex_isin_otc",
        "tpex_isin_emerging",
        "twse_official_master",
        "tpex_official_master",
        "tpex_delisted_history",
    ],
    "accepted_partition_statuses": ["succeeded", "skipped_fresh"],
    "max_completion_seconds": 300.0,
    "max_attempt_failure_rate": 0.0,
    "measurement": "declaration_before_source_refresh_through_local_broad_scan_completion",
}


def _declare_security_master_update_slo(*, declared_at: datetime) -> dict:
    """Freeze the update target before any source request can start."""
    policy = dict(_SECURITY_MASTER_UPDATE_SLO_POLICY)
    policy["policy_id"] = "AMUSLO-" + content_hash(policy)
    return {
        "schema_version": "stock_ai.autonomous_market_update_slo_declaration.v1",
        "policy": policy,
        "declared_at": declared_at.isoformat(),
        "deadline_at": (declared_at + timedelta(seconds=policy["max_completion_seconds"])).isoformat(),
    }


async def _scan():
    from collections import Counter
    from .data_platform.security_loader import OfficialSecurityMasterLoader
    from .data_platform.service import get_market_data_platform
    from .market_intelligence.broad_scanner import BroadScanner
    stop_event = Event()
    update_slo = _declare_security_master_update_slo(declared_at=datetime.now(timezone.utc))

    def refresh():
        # This is the explicit research path. Execution and UI reads only use
        # local classifications; native checkpoints/leases decide what is due.
        try:
            return OfficialSecurityMasterLoader(get_market_data_platform()).run(force=False, stop_event=stop_event)
        except Exception as exc:
            return {"status": "failed", "partitions": [],
                    "error": {"type": type(exc).__name__, "message": str(exc)[:512]}}

    try:
        refreshed = await asyncio.to_thread(refresh)
    except asyncio.CancelledError:
        # The current native partition may finish and release its lease. The
        # loader checks this signal before starting any later source partition.
        stop_event.set()
        raise

    def compact(record):
        if not isinstance(record, dict):
            return {}
        result = {key: value[:180] if isinstance(value, str) else value for key, value in record.items()
                  if key in {"status", "source_id", "partition_key", "dataset", "run_id", "cursor", "ttl_seconds",
                             "record_count", "batch_count", "raw_payload_id", "raw_sha256", "row_count", "source_updated_on",
                             "snapshot_hash", "acquired_at", "updated_count", "invalidated_count", "stale_ignored_count",
                             "count", "source_observation_count", "revision_count", "reused_revision_count", "created_event_count"}
                  and (isinstance(value, (str, int, bool)) or value is None)}
        if isinstance(record.get("error"), dict):
            result["error"] = {key: str(record["error"].get(key) or "")[:512] for key in ("type", "message")}
        for key in ("classification", "sync"):
            if isinstance(record.get(key), dict):
                result[key] = compact(record[key])
        for key in ("unmatched_bindings", "ambiguous_bindings"):
            if isinstance(record.get(key), list):
                result[key + "_count"] = len(record[key])
        return result

    partitions = refreshed.get("partitions") or []
    summary = {"schema_version": "stock_ai.autonomous_security_master_refresh.v1",
               "status": refreshed.get("status", "unknown"), "force": False,
               "scope": "attempted_security_master_source_partitions_only",
               "partition_count": len(partitions), "partitions": [compact(item) for item in partitions[:12]],
               "partition_status_counts": dict(Counter(str(item.get("status") or "unknown") for item in partitions)),
               **({"error": compact(refreshed)["error"]} if isinstance(refreshed.get("error"), dict) else {})}
    # Keep this a separate await: cancellation during refresh must not start a
    # scan or turn partially refreshed sources into a completed research cycle.
    scanned = await asyncio.to_thread(BroadScanner(position_loader=lambda: {}).scan)
    completed_at = datetime.now(timezone.utc)
    update_slo["completed_at"] = completed_at.isoformat()
    update_slo["observed_duration_seconds"] = round(
        (completed_at - datetime.fromisoformat(update_slo["declared_at"])).total_seconds(), 6
    )
    summary["update_slo"] = update_slo
    return {**scanned, "security_master_refresh": summary}


async def _history(symbol: str, now: datetime):
    local = now.astimezone(ZoneInfo("Asia/Taipei"))
    # Only request completed daily candles, including no same-day preclose.
    end = local.date() if local.time().replace(tzinfo=None) >= time(14, 30) else local.date()-timedelta(days=1)
    # Four years yields roughly 1,000 sessions, enough for the frozen 70/15/15
    # protocol to give validation and holdout at least 120 completed bars.  A
    # one-year request made those gates structurally unreachable.  Monthly raw
    # responses are cached, and the 20-symbol rotation stays inside the Agent's
    # 600-second research boundary (the pre-change native probe measured 37
    # uncached TWSE months in 4.4 seconds for one symbol).
    start = (end-timedelta(days=4*365)).replace(day=1)
    stop_event = Event()
    try:
        payload = await asyncio.to_thread(load_official_candles, symbol, start=start.isoformat(), end=end.isoformat(),
                                        max_months=50, cache_dir=Path(".runtime/autonomous-official-candles"), timeout_seconds=8,
                                        stop_event=stop_event)
    except asyncio.CancelledError:
        # Cancelling to_thread's await does not stop its running function.
        # Signal the existing loader so it cannot start another month/retry.
        stop_event.set()
        raise
    if payload["data_evidence"].get("data_sha256") != content_hash(payload["rows"]):
        raise ValueError("official_history_hash_mismatch_before_normalization")
    payload["rows"] = [{**row, "timestamp": datetime.combine(datetime.fromisoformat(row["date"]).date(), time(13, 30),
                                                                 tzinfo=ZoneInfo("Asia/Taipei")).isoformat()}
                       for row in payload["rows"]]
    payload["data_evidence"]["bar_completion_normalization"] = "official_daily_date_to_Taipei_13:30; requested_after_14:30"
    payload["data_evidence"]["raw_source_data_sha256"] = payload["data_evidence"]["data_sha256"]
    payload["data_evidence"]["data_sha256"] = content_hash(payload["rows"])
    return payload


async def _quote(symbol: str):
    from .paper_training_api import _verified_price
    return await asyncio.to_thread(_verified_price, symbol, require_execution_quote=False)


def _resolve_product(*, symbol: str, market: str, now: datetime):
    # Host-owned exact local lookup. Proposal and order admission must not use
    # model supplied product labels or trigger transport on the execution path.
    from .data_platform.service import get_market_data_platform
    return get_market_data_platform().resolve_product_classification(
        symbol=symbol, market=market, now=now,
    )


async def autonomous_status_snapshot(service: AutonomousCampaign | None = None, *, authorized: bool | None = None) -> dict:
    """Expose the campaign's broker account, never the legacy UI account."""
    service = service or get_autonomous_campaign()
    observed_at = datetime.now(timezone.utc)
    account = await service.broker.account(now=observed_at)
    if account.get("account_id") != service.broker.account_id:
        raise ValueError("autonomous_status_broker_account_mismatch")
    from open_stock_ai.execution.paper_decision_context import build_paper_decision_context
    state = service.status()
    decision_context = build_paper_decision_context(
        risk=service.risk, account_summary=account, account_id=service.broker.account_id,
        mode=service.broker.mode, authorized=state["enabled"] if authorized is None else authorized,
        experiment_limits=service.experiment_limits, active_plans=state["plans"],
        campaign_enabled=state["enabled"], now=observed_at,
    )
    return {**state, "schema_version": "open_stock_ai.autonomous_status.v1",
            "account": account, "account_observed_at": observed_at.isoformat(),
            "paper_decision_context": decision_context,
            "model_review": get_autonomous_model_review(service).status()}


def get_autonomous_campaign() -> AutonomousCampaign:
    global _SERVICE
    with _LOCK:
        if _SERVICE is None:
            engine = get_runtime_engine()
            store = engine.pipeline.trade_store.store
            # This account is intentionally distinct from existing manual or
            # legacy Agent experiments. Explicit constructor values survive ENV.
            oms = PaperOMS(store, account_id="autonomous-paper-v1", initial_cash=1_000_000,
                           commission_bps=14.25, minimum_commission=20, sell_tax_bps=30,
                           slippage_bps=5, read_environment=False)
            campaign = AutonomousCampaign(plans=TradingPlanStore(store), broker=PaperBrokerPort(
                PaperBrokerSimulator(store, oms, odd_lot_execution_model=OddLotBoardProxyModel()),
                public_board_quote_simulation=True),
                                          risk=engine.pipeline.risk, scanner=_scan, history_loader=_history,
                                          quote_loader=_quote, calendar=default_taiwan_market_calendar(),
                                          product_resolver=_resolve_product,
                                          history_lookback_days=4*365,
                                          costs={"commission_bps": 14.25, "minimum_commission": 20,
                                                 "slippage_bps": 5, "market_impact_bps": 20, "sell_tax_bps": 30})
            from .autonomous_deployment import bind_campaign_execution_context
            campaign.execution_context = bind_campaign_execution_context(campaign)
            campaign.broker.broker.host_execution_context = campaign.execution_context
            from .autonomous_forward_monitor import AutonomousForwardMonitor
            campaign.forward_monitor = AutonomousForwardMonitor(campaign)
            # A transient evidence/monitor setup failure must be retried, not
            # publish a partially initialized singleton to background workers.
            _SERVICE = campaign
        return _SERVICE


def get_autonomous_model_review(campaign=None):
    from .agent_service import get_agent_run_runtime
    from .autonomous_model_review import AutonomousModelReview
    return AutonomousModelReview(campaign=campaign or get_autonomous_campaign(), runtime=get_agent_run_runtime())


async def autonomous_trading_loop() -> None:
    """Resume positions continuously; automatic research has a durable budget."""
    research_task = None
    try:
        while True:
            try:
                service = _SERVICE
                if service is None:
                    engine = get_runtime_engine()
                    with engine.pipeline.trade_store.store._connect() as conn:
                        exists = conn.execute("select 1 from sqlite_master where type='table' and name='autonomous_campaign_state'").fetchone()
                        active = conn.execute("select 1 from autonomous_campaign_state where account_id='autonomous-paper-v1'").fetchone() if exists else None
                    if active:
                        service = get_autonomous_campaign()
                if service is not None:
                    result = await service.manage()
                    if result["errors"]:
                        logger.warning("Autonomous plan management: %s", result["errors"])
                    if getattr(service, "forward_monitor", None):
                        get_autonomous_model_review(service).reconcile_forward_reviews()
                    if research_task is not None and research_task.done():
                        if not research_task.cancelled():
                            research_task.exception()
                        research_task = None
                    if research_task is None and service.claim_postclose_research():
                        research_task = asyncio.create_task(_autonomous_postclose_research(service), name="autonomous-postclose-research")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Autonomous trading management pass failed")
            await asyncio.sleep(30)
    finally:
        # This also runs when cancellation arrives during the polling sleep.
        if research_task is not None:
            research_task.cancel()
            await asyncio.gather(research_task, return_exceptions=True)


async def _autonomous_postclose_research(service: AutonomousCampaign) -> None:
    try:
        now = datetime.now(timezone.utc)
        position_review = await service.review_positions(now=now)
        if position_review["errors"]:
            raise RuntimeError(f"postclose_position_review_failed: {position_review['errors']}")
        state = service.status()
        if not state["enabled"]:
            service.finish_postclose_research()
            return
        prior = service.cycle(state["latest_cycle_id"]) if state.get("latest_cycle_id") else None
        close = datetime.combine(now.astimezone(ZoneInfo("Asia/Taipei")).date(), time(14, 30), tzinfo=ZoneInfo("Asia/Taipei"))
        # A failed account/plan activation retries the retained successful data
        # cycle instead of downloading all histories again.
        cycle = prior if prior and datetime.fromisoformat(prior["created_at"]) >= close and prior["deep_success_count"] else await service.research()
        if not cycle["deep_success_count"]:
            raise RuntimeError("postclose_research_has_no_verified_history")
        if service.status()["enabled"]:
            result = await get_autonomous_model_review(service).request(cycle_id=cycle["cycle_id"])
            if result.get("status") in {"submission_unknown", "receipt_unavailable"}:
                raise RuntimeError("postclose_model_review_requires_reconciliation")
        service.finish_postclose_research()
    except asyncio.CancelledError:
        service.finish_postclose_research(error="cancelled")
        raise
    except Exception as exc:
        service.finish_postclose_research(error=f"{type(exc).__name__}: {exc}")
        logger.exception("Autonomous postclose research failed")
