"""Persistent market research -> frozen plans -> execution campaign.

Bulk screening and ongoing position management do not spend model tokens.
The Agent can review the retained cycle once, then activate its versioned plans.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone, time
import json
import math
import re
from statistics import mean
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from open_stock_ai.research.candle_qualification import evaluate_candidate
from open_stock_ai.risk.order_policy import order_risk_evidence_receipt
from open_stock_ai.strategy.candle_candidates import frozen_candidates
from open_stock_ai.strategy.execution_policy import plan_signal_order
from open_stock_ai.types import MarketSnapshot, StockRequest
from .trading_plan import TradingPlan, content_hash, utc_time
from .trading_plan_executor import TradingPlanExecutor
from .trading_plan_store import TradingPlanStore
from .trade_outcomes import AutonomousOutcomeLedger
from .product_admission import assess_new_entry_product, resolve_product_snapshot
from .research_coverage import SecurityResearchCoverageLedger


def validate_research_symbols(symbols: list[str] | None, *, deep_limit: int) -> tuple[str, ...] | None:
    """Validate the whole requested batch without silently changing its scope."""
    if type(deep_limit) is not int or not 1 <= deep_limit <= 20:
        raise ValueError("deep_limit_between_1_and_20")
    if symbols is None:
        return None
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 20:
        raise ValueError("research_symbols_must_be_nonempty_list")
    if len(symbols) > deep_limit:
        raise ValueError("research_symbols_exceed_deep_limit")
    if any(not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{4}[A-Z0-9]{0,2}\.TW(?:O)?", symbol) for symbol in symbols):
        raise ValueError("canonical_taiwan_research_symbol_required")
    if len(set(symbols)) != len(symbols):
        raise ValueError("duplicate_research_symbols")
    return tuple(symbols)


def _requested_research_features(symbols: tuple[str, ...], all_features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for symbol in symbols:
        code, venue = symbol.split(".")
        matches = [row for row in all_features if str(row.get("symbol", "")).split(".")[0] == code]
        if len(matches) != 1:
            reason = "ambiguous" if matches else "unknown"
            raise ValueError(f"requested_research_symbol_{reason}:{symbol}")
        row = matches[0]
        if row.get("symbol") != symbol:
            raise ValueError(f"requested_research_symbol_identity_mismatch:{symbol}")
        exchange = str(row.get("exchange") or "").upper()
        if {"TWSE": "TW", "TPEX": "TWO", "TPEX-ESB": "TWO"}.get(exchange) != venue:
            raise ValueError(f"requested_research_symbol_identity_mismatch:{symbol}")
        selected.append(row)
    return selected


def _evaluate_research_candidate(strategy, *, version, symbol, rows, source, family_size, research_family, costs):
    """Run pure strategy work in a worker; only the caller persists receipts."""
    stage = "signal_generation"
    try:
        snapshot = MarketSnapshot(symbol=symbol, market="TW", price=float(rows[-1]["close"]), ohlcv=rows)
        request = StockRequest(symbol=symbol, market="TW", horizon="swing")
        signal = asdict(strategy.generate_signal(request, snapshot))
        stage = "candidate_evaluation"
        qualification = evaluate_candidate(strategy, request, rows, data_evidence=source,
            evaluation_family_size=family_size, evaluation_context=research_family,
            cost_assumptions={"venue": "TPEX" if symbol.endswith(".TWO") else "TWSE", "product_type": "stock",
                "broker_commission_bps": costs["commission_bps"], "broker_minimum_commission_twd": costs["minimum_commission"],
                "adverse_execution_floor_bps": float(costs.get("slippage_bps", 0))+float(costs.get("market_impact_bps", 0)),
                "adverse_floor_source": "host_declared_oms_slippage_plus_execution_scenario_impact_not_empirical_calibration"})
        candidate = {"strategy_id": strategy.candidate_id, "strategy_version": version, "signal": signal,
            "positive_ev_qualified": qualification["positive_ev_qualified"], "qualification_reasons": qualification.get("reasons", []),
            "research_paper_candidate_eligible": qualification["research_paper_candidate_eligible"]}
        return candidate, qualification, None
    except Exception as exc:
        return None, None, {"strategy_id": strategy.candidate_id, "strategy_version": version,
                            "stage": stage, "error": f"{type(exc).__name__}: {exc}"}


def _research_product_admissions(features, instant):
    # The scanner supplies retained Host classification. Research does not run
    # thousands of latest-master lookups while protecting existing positions.
    return {str(row.get("symbol")): assess_new_entry_product(
        symbol=str(row.get("symbol") or ""), market=str(row.get("exchange") or ""), now=instant,
        product_snapshot={"entity_id": row.get("entity_id"), "lifecycle_status": row.get("lifecycle_status"),
                          "classification": row.get("product_classification")}) for row in features}


class AutonomousCampaign:
    forward_monitor = None

    def __init__(self, *, plans: TradingPlanStore, broker, risk, scanner, history_loader,
                 quote_loader, calendar, costs: dict[str, float] | None = None,
                 history_lookback_days: int = 0, quote_timeout_seconds: float = 10, product_resolver=None):
        self.plans, self.broker, self.risk = plans, broker, risk
        self.scanner, self.history_loader, self.quote_loader = scanner, history_loader, quote_loader
        self.calendar = calendar
        self.product_resolver = product_resolver
        if (isinstance(quote_timeout_seconds, bool) or not isinstance(quote_timeout_seconds, (int, float))
                or not math.isfinite(quote_timeout_seconds) or quote_timeout_seconds <= 0):
            raise ValueError("quote_timeout_must_be_positive_finite_seconds")
        self.quote_timeout_seconds = float(quote_timeout_seconds)
        if history_lookback_days < 0:
            raise ValueError("history_lookback_days_must_be_nonnegative")
        self.history_lookback_days = history_lookback_days
        self.outcomes = AutonomousOutcomeLedger(plans.store)
        self.forward_monitor = None
        self.experiment_limits = {"max_order_notional_pct": min(5, risk.max_position_size_pct),
                                  "max_total_exposure_pct": min(20, risk.max_total_paper_exposure_pct)}
        self.costs = costs or {"commission_bps": 14.25, "minimum_commission": 20, "slippage_bps": 5, "sell_tax_bps": 30}
        with plans.store._connect() as conn:
            conn.executescript("""
                create table if not exists autonomous_campaign_state (
                    account_id text primary key, enabled integer not null default 0,
                    research_cursor integer not null default 0, last_research_at text,
                    updated_at text not null
                );
                create table if not exists autonomous_research_cycles (
                    cycle_id text primary key, account_id text not null,
                    created_at text not null, payload_json text not null
                );
                create table if not exists autonomous_evidence (
                    evidence_id text primary key, account_id text not null,
                    kind text not null, payload_json text not null
                );
                create table if not exists autonomous_deep_research_coverage (
                    account_id text not null, symbol text not null,
                    last_selected_at text not null, selection_count integer not null,
                    last_success_at text, success_count integer not null,
                    last_error text,
                    primary key(account_id,symbol)
                );
            """)
            columns = {row[1] for row in conn.execute("pragma table_info(autonomous_campaign_state)")}
            for column, sql_type in {
                "postclose_day": "text", "postclose_attempts": "integer not null default 0",
                "postclose_next_retry_at": "text", "postclose_completed_at": "text", "postclose_error": "text",
                "last_disabled_at": "text",
            }.items():
                if column not in columns:
                    conn.execute(f"alter table autonomous_campaign_state add column {column} {sql_type}")
            conn.execute("insert or ignore into autonomous_campaign_state(account_id,updated_at) values (?,?)",
                         (broker.account_id, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        self.coverage_ledger = SecurityResearchCoverageLedger(
            plans.store,
            account_id=broker.account_id,
            calendar=calendar,
        )
        self.executor = TradingPlanExecutor(store=plans, broker=broker, risk=risk, evidence_resolver=self._plan_evidence,
                                            entry_permission=lambda: self.status()["enabled"], product_resolver=product_resolver)

    def product_snapshot(self, symbol: str, *, now: datetime) -> dict[str, Any]:
        return resolve_product_snapshot(self.product_resolver, symbol=symbol, market="TW", now=now)

    def _product_admission(self, feature: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        symbol = str(feature.get("symbol") or "")
        assessment = assess_new_entry_product(symbol=symbol, market="TW", now=now,
            product_snapshot=self.product_snapshot(symbol, now=now), expected_entity_id=feature.get("entity_id") or "")
        if str(feature.get("exchange") or "").upper() != str(assessment["classification"].get("venue") or "").upper():
            assessment.update(allowed=False, reasons=[*assessment["reasons"], "product_research_identity_mismatch"])
            assessment["receipt_sha256"] = content_hash({k: v for k, v in assessment.items() if k != "receipt_sha256"})
        return assessment

    def configure(self, *, enabled: bool, authorized_at: str | datetime | None = None) -> dict[str, Any]:
        if self.broker.mode != "paper":
            raise ValueError("campaign_live_activation_requires_verified_broker_rollout")
        with self.plans.store._connect() as conn:
            conn.execute("begin immediate")
            instant = datetime.now(timezone.utc).isoformat()
            if enabled:
                if authorized_at is not None:
                    self._assert_activation_authorized(conn, authorized_at)
                # A deliberate resume may revisit this day's retained cycle;
                # research and model daily budgets remain consumed.
                conn.execute("update autonomous_campaign_state set postclose_completed_at=null,postclose_next_retry_at=null where account_id=? and enabled=0",
                             (self.broker.account_id,))
            conn.execute("update autonomous_campaign_state set enabled=?,updated_at=?,last_disabled_at=case when ?=0 then ? else last_disabled_at end where account_id=?",
                         (int(enabled), instant, int(enabled), instant, self.broker.account_id))
            conn.commit()
        return self.status()

    def assert_activation_authorized(self, *, authorized_at: str | datetime) -> None:
        """Only a Host run created after the latest explicit stop may add risk."""
        with self.plans.store._connect() as conn:
            self._assert_activation_authorized(conn, authorized_at)

    def _assert_activation_authorized(self, conn, authorized_at: str | datetime) -> None:
        try:
            authority_time = utc_time(authorized_at)
        except (TypeError, ValueError):
            raise PermissionError("campaign_activation_host_timestamp_required") from None
        row = conn.execute("select last_disabled_at from autonomous_campaign_state where account_id=?", (self.broker.account_id,)).fetchone()
        if row is None:
            raise ValueError("autonomous_campaign_account_not_initialized")
        if row[0] and authority_time <= utc_time(row[0]):
            raise PermissionError("autonomous_campaign_disabled_after_run_authorization")

    def status(self) -> dict[str, Any]:
        with self.plans.store._connect() as conn:
            row = conn.execute("select enabled,research_cursor,last_research_at,postclose_day,postclose_attempts,postclose_next_retry_at,postclose_completed_at,postclose_error,last_disabled_at from autonomous_campaign_state where account_id=?", (self.broker.account_id,)).fetchone()
            cycle = conn.execute("select cycle_id,created_at from autonomous_research_cycles where account_id=? order by created_at desc limit 1", (self.broker.account_id,)).fetchone()
            coverage = conn.execute("""
                select count(*),coalesce(sum(case when success_count>0 then 1 else 0 end),0),
                       coalesce(sum(selection_count),0)
                from autonomous_deep_research_coverage where account_id=?
            """, (self.broker.account_id,)).fetchone()
        return {"account_id": self.broker.account_id, "mode": self.broker.mode, "enabled": bool(row[0]),
                "last_disabled_at": row[8],
                "research_cursor": row[1], "last_research_at": row[2], "latest_cycle_id": cycle[0] if cycle else None,
                "deep_research_coverage": {"symbols_attempted": coverage[0], "symbols_succeeded": coverage[1],
                                           "total_attempts": coverage[2],
                                           "selection_policy": "bounded_failed_retry_then_never_or_least_recently_selected"},
                "security_research_coverage": self.coverage_ledger.latest_summary(),
                "automatic_research": {"day": row[3], "attempts": row[4], "next_retry_at": row[5],
                                       "completed_at": row[6], "error": row[7], "daily_attempt_limit": 3},
                "plans": self.plans.list(account_id=self.broker.account_id),
                "learning": self.outcomes.summary(account_id=self.broker.account_id),
                **({"forward_validation": self.forward_monitor.status()} if self.forward_monitor else {}),
                "eligibility": "bounded_experiment", "positive_ev_qualified": False,
                "scope": "configured_venue_universe_with_attributed_coverage"}

    def claim_postclose_research(self, *, now: datetime | None = None) -> bool:
        """Bound automatic network work to three durable daily attempts."""
        instant = utc_time(now or datetime.now(timezone.utc))
        local = instant.astimezone(ZoneInfo("Asia/Taipei"))
        if local.time().replace(tzinfo=None) < time(14, 30) or not self.calendar.day_status(local.date())["trading_day"]:
            return False
        day = local.date().isoformat()
        with self.plans.store._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select enabled,postclose_day,postclose_attempts,postclose_next_retry_at,postclose_completed_at from autonomous_campaign_state where account_id=?",
                (self.broker.account_id,),
            ).fetchone()
            attempts = int(row[2] or 0) if row[1] == day else 0
            positions = conn.execute("select 1 from autonomous_trading_plans where account_id=? and status not in ('closed','cancelled','expired','invalidated','rejected') and (json_extract(state_json,'$.remaining_quantity')>0 or json_extract(state_json,'$.entry_receipt.filled_quantity')>0) limit 1", (self.broker.account_id,)).fetchone()
            if (not row[0] and not positions) or attempts >= 3 or (row[1] == day and (row[4] or row[3] and instant < utc_time(row[3]))):
                return False
            conn.execute(
                """update autonomous_campaign_state set postclose_day=?,postclose_attempts=?,
                       postclose_next_retry_at=?,postclose_completed_at=null,postclose_error=null where account_id=?""",
                (day, attempts+1, (instant+timedelta(hours=1)).isoformat(), self.broker.account_id),
            )
            conn.commit()
        return True

    def finish_postclose_research(self, *, error: str | None = None, now: datetime | None = None) -> None:
        instant = utc_time(now or datetime.now(timezone.utc)).isoformat()
        with self.plans.store._connect() as conn:
            conn.execute(
                "update autonomous_campaign_state set postclose_completed_at=?,postclose_error=? where account_id=?",
                (None if error else instant, error, self.broker.account_id),
            )
            conn.commit()

    def _retain(self, kind: str, payload: dict[str, Any]) -> str:
        evidence_id = "AE-" + content_hash({"account_id": self.broker.account_id, "kind": kind, "payload": payload})
        with self.plans.store._connect() as conn:
            conn.execute("insert or ignore into autonomous_evidence values (?,?,?,?)",
                         (evidence_id, self.broker.account_id, kind, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            conn.commit()
        return evidence_id

    def _evidence(self, evidence_id: str, kind: str | None = None) -> dict[str, Any]:
        with self.plans.store._connect() as conn:
            row = conn.execute("select kind,payload_json from autonomous_evidence where evidence_id=? and account_id=?",
                               (evidence_id, self.broker.account_id)).fetchone()
        if not row or kind is not None and row[0] != kind:
            raise ValueError("retained_campaign_evidence_not_found")
        payload = json.loads(row[1])
        hashes = {"AE-" + content_hash({"kind": row[0], "payload": payload}),
                  "AE-" + content_hash({"account_id": self.broker.account_id, "kind": row[0], "payload": payload})}
        if evidence_id not in hashes:
            raise ValueError("retained_campaign_evidence_corrupted")
        return payload

    def cycle(self, cycle_id: str) -> dict[str, Any]:
        with self.plans.store._connect() as conn:
            row = conn.execute("select payload_json from autonomous_research_cycles where cycle_id=? and account_id=?", (cycle_id, self.broker.account_id)).fetchone()
        if not row:
            raise ValueError("autonomous_cycle_not_found")
        payload = json.loads(row[0])
        if "AC-" + content_hash({k: v for k, v in payload.items() if k != "cycle_id"}) != cycle_id:
            raise ValueError("retained_campaign_cycle_corrupted")
        return payload

    def _completed_day(self, now: datetime):
        local = utc_time(now).astimezone(ZoneInfo("Asia/Taipei"))
        day = local.date() if local.time().replace(tzinfo=None) >= time(14, 30) else local.date()-timedelta(days=1)
        while not self.calendar.day_status(day)["trading_day"]:
            day -= timedelta(days=1)
        return day

    async def _history_for(self, symbol: str, now: datetime) -> dict[str, Any]:
        with self.plans.store._connect() as conn:
            row = conn.execute(
                "select evidence_id from autonomous_evidence where account_id=? and kind='price_history' and json_extract(payload_json,'$.data_evidence.symbol')=? order by rowid desc limit 1",
                (self.broker.account_id, symbol),
            ).fetchone()
        if row:
            history = self._evidence(row[0], "price_history")
            bars = history.get("rows") or []
            completed_day = self._completed_day(now)
            requested_start = (history.get("data_evidence") or {}).get("requested_start")
            requested_window_covered = self.history_lookback_days == 0
            if requested_start and self.history_lookback_days:
                required_start = (completed_day-timedelta(days=self.history_lookback_days)).replace(day=1)
                try:
                    requested_window_covered = datetime.fromisoformat(requested_start).date() <= required_start
                except (TypeError, ValueError):
                    requested_window_covered = False
            if (bars and requested_window_covered
                    and utc_time(bars[-1]["timestamp"]).astimezone(ZoneInfo("Asia/Taipei")).date() == completed_day):
                return history
        return await self.history_loader(symbol, now)

    async def review_positions(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Evaluate each owned position on one new completed bar, without an LLM."""
        instant = utc_time(now or datetime.now(timezone.utc))
        completed_day = self._completed_day(instant)
        reviewed, skipped, errors = [], [], []
        candidates = {candidate.candidate_id: candidate for candidate in frozen_candidates()}
        for record in self.plans.list(account_id=self.broker.account_id, active_only=True):
            state, definition = record["state"], record["definition"]
            quantity = float(state.get("remaining_quantity") or (state.get("entry_receipt") or {}).get("filled_quantity") or 0)
            if quantity <= 0:
                continue
            candidate = candidates.get(definition["strategy_id"])
            if candidate is None or candidate.strategy_version_hash() != definition["strategy_version"]:
                skipped.append({"plan_id": record["plan_id"], "reason": "strategy_version_requires_revalidation"})
                continue
            prior = self.plans.position_review(record["plan_id"])
            if prior and utc_time(prior["observed_bar_at"]).astimezone(ZoneInfo("Asia/Taipei")).date() >= completed_day:
                skipped.append({"plan_id": record["plan_id"], "reason": "completed_bar_already_reviewed"})
                continue
            try:
                history = await self._history_for(record["symbol"], instant)
                rows, source = history["rows"], history["data_evidence"]
                stamps = [utc_time(bar["timestamp"]) for bar in rows]
                entered = utc_time(state.get("entered_at") or state.get("entry_dispatched_at") or record["created_at"])
                if (source.get("source_provenance_verified") is not True or source.get("symbol") != record["symbol"]
                        or source.get("data_sha256") != content_hash(rows) or len(rows) < candidate.slow_window+1
                        or any(stamp > instant for stamp in stamps)
                        or any(right <= left for left, right in zip(stamps, stamps[1:]))
                        or stamps[-1] <= entered):
                    raise ValueError("position_review_history_not_current_verified_completed_bars")
                history_id = self._retain("price_history", history)
                holding_bars = sum(stamp >= entered for stamp in stamps)
                signal = candidate.generate_signal(
                    StockRequest(symbol=record["symbol"], market="TW", horizon="swing"),
                    MarketSnapshot(symbol=record["symbol"], market="TW", price=float(rows[-1]["close"]), ohlcv=rows,
                                   raw={"position_quantity": quantity, "holding_bars": holding_bars}),
                )
                reason = None
                if signal.action == "sell":
                    reason = "close_below_slow_mean" if float(rows[-1]["close"]) < mean(float(bar["close"]) for bar in rows[-candidate.slow_window:]) else "holding_period_expired"
                reviewed.append(self.plans.record_position_review(
                    plan_id=record["plan_id"], strategy_version=definition["strategy_version"], evidence_id=history_id,
                    observed_bar_at=rows[-1]["timestamp"], action="sell" if signal.action == "sell" else "hold",
                    reason=reason, now=instant,
                ))
            except Exception as exc:
                errors.append({"plan_id": record["plan_id"], "error": f"{type(exc).__name__}: {exc}"})
        return {"reviewed": reviewed, "skipped": skipped, "errors": errors, "model_calls": 0}

    async def research(self, *, now: datetime | None = None, deep_limit: int = 20,
                       symbols: list[str] | None = None) -> dict[str, Any]:
        """Research configured instruments; only supported ordinary shares get stock candidates.

        Every result records both full scan coverage and the smaller detailed
        coverage. Neither a watch list nor six downloaded histories is called
        a complete historical evaluation of the entire market.
        """
        requested_symbols = validate_research_symbols(symbols, deep_limit=deep_limit)
        instant = utc_time(now or datetime.now(timezone.utc))
        scanned = await self.scanner()
        if now is None:
            # A live scan may acquire a newer official product catalogue. Use
            # one post-scan cutoff for its admission, history and cycle receipt.
            instant = utc_time(datetime.now(timezone.utc))
        all_features = scanned["all_features"]
        features = list(scanned["features"])
        product_admissions = await asyncio.to_thread(_research_product_admissions, all_features, instant)
        ordinary_features = [row for row in all_features if product_admissions[str(row.get("symbol"))]["allowed"]]
        requested_features = _requested_research_features(requested_symbols, all_features) if requested_symbols is not None else None
        strategies = frozen_candidates()
        # Register the whole declared exploration family BEFORE selecting or
        # downloading detailed histories. Failed downloads and thin/invalid
        # current quotes must not make its multiplicity denominator smaller.
        family_symbols = sorted({str(row.get("symbol", "")).strip().upper() for row in ordinary_features
                                 if re.fullmatch(r"[0-9]{4,6}\.TW(?:O)?", str(row.get("symbol", "")).strip().upper())})
        strategy_versions = [[strategy.candidate_id, strategy.strategy_version_hash()] for strategy in strategies]
        evaluation_id = "AFR-" + uuid4().hex
        with self.plans.store._connect() as conn:
            conn.executescript("""
                create table if not exists autonomous_research_family_members (
                    account_id text not null, symbol text not null,
                    strategy_id text not null, strategy_version text not null,
                    first_registered_at text not null,
                    primary key(account_id,symbol,strategy_id,strategy_version)
                );
                create table if not exists autonomous_research_family_runs (
                    evaluation_id text primary key, account_id text not null,
                    registered_at text not null, registry_sha256 text not null,
                    family_size integer not null
                );
            """)
            conn.execute("begin immediate")
            conn.executemany("insert or ignore into autonomous_research_family_members values (?,?,?,?,?)",
                             [(self.broker.account_id, symbol, strategy_id, version, instant.isoformat())
                              for symbol in family_symbols for strategy_id, version in strategy_versions])
            members = [list(row) for row in conn.execute(
                "select symbol,strategy_id,strategy_version from autonomous_research_family_members "
                "where account_id=? order by symbol,strategy_id,strategy_version", (self.broker.account_id,))]
            prior_runs = conn.execute("select count(*) from autonomous_research_family_runs where account_id=?",
                                      (self.broker.account_id,)).fetchone()[0]
            family_size = max(2, len(members))
            registry_hash = content_hash(members)
            conn.execute("insert into autonomous_research_family_runs values (?,?,?,?,?)",
                         (evaluation_id, self.broker.account_id, instant.isoformat(), registry_hash, family_size))
            conn.commit()
        research_family = {
            "schema_version": "open_stock_ai.exploratory_research_family.v1",
            "purpose": "exploratory_market_campaign", "evaluation_id": evaluation_id,
            "registered_at": instant.isoformat(), "registration_stage": "before_deep_selection_and_history_evaluation",
            "current_universe_symbol_count": len(family_symbols), "current_strategy_count": len(strategies),
            "current_symbol_strategy_pair_count": len(family_symbols)*len(strategies),
            "cumulative_symbol_strategy_version_count": len(members), "evaluation_family_size": family_size,
            "registry_sha256": registry_hash, "prior_registered_cycle_count": prior_runs,
            "current_universe_symbols": family_symbols, "current_strategy_versions": strategy_versions,
            "scope": "account_cumulative_supported_ordinary_stock_candidates_by_strategy_version_not_all_research_products",
            "historical_universe_point_in_time_verified": False,
            "repeated_holdout_policy": "overlapping_or_repeated_windows_are_exploration_not_new_independent_samples",
            "positive_ev_promotion_allowed": False,
            "confirmation_required": "new_locked_hypothesis_data_window_and_family_protocol_before_outcomes",
        }
        # Whole-market research need not be rejected merely because current
        # daily bulk data is not a live executable quote.
        usable = [row for row in features if isinstance(row.get("close"), (int, float))
                  and math.isfinite(row["close"]) and row["close"] > 0 and row.get("data_as_of")]
        # The automatic rotation exists to find supported stock candidates.
        # Keep non-stock products in the full market evidence and allow an
        # explicit on-demand request to research them, but do not spend the
        # bounded default deep-history budget on instruments that the current
        # entry policy cannot admit.
        rotation_usable = [
            row for row in usable
            if (product_admissions.get(str(row.get("symbol"))) or {}).get("allowed") is True
        ]
        with self.plans.store._connect() as conn:
            prior_coverage = {row[0]: {"last_selected_at": row[1], "last_error": row[2]} for row in conn.execute(
                "select symbol,last_selected_at,last_error from autonomous_deep_research_coverage where account_id=?",
                (self.broker.account_id,))}
        # Retry only a bounded slice of prior failures. The remaining capacity
        # keeps expanding market coverage, so one bad symbol cannot stall a run.
        retry_candidates = [
            row for row in rotation_usable
            if (prior_coverage.get(str(row["symbol"])) or {}).get("last_error")
        ]
        retry_candidates.sort(key=lambda row: (
            prior_coverage[str(row["symbol"])]["last_selected_at"],
            -float(row.get("trade_value") or 0), str(row["symbol"])))
        retry_count = min(len(retry_candidates), deep_limit//4)
        retries = retry_candidates[:retry_count]
        retry_symbols = {str(row["symbol"]) for row in retries}
        # Liquidity is only the tie-breaker. Persisted per-symbol recency makes
        # the rest of the rotation complete even when daily rankings move.
        rotation_usable.sort(key=lambda row: (
            str(row["symbol"]) in prior_coverage,
            (prior_coverage.get(str(row["symbol"])) or {}).get("last_selected_at", ""),
            -float(row.get("trade_value") or 0),
            str(row["symbol"]),
        ))
        cursor = int(self.status()["research_cursor"])
        selected = requested_features if requested_features is not None else (
            retries + [row for row in rotation_usable if str(row["symbol"]) not in retry_symbols]
        )[:min(deep_limit, len(rotation_usable))]
        history_results, errors = [], []
        for feature in selected:
            await asyncio.sleep(0)  # Cached histories must still honor cancellation before the next symbol.
            symbol = str(feature["symbol"])
            history_id = None
            source_attempt_id = None
            try:
                history = await self._history_for(symbol, instant)
                rows, source = history["rows"], history["data_evidence"]
                if history.get("source_request_receipts"):
                    source_attempt_id = self._retain("history_source_attempt", {
                        "symbol": symbol, "row_count": len(rows), "data_evidence": source,
                        "source_request_receipts": history["source_request_receipts"],
                    })
                if source.get("source_provenance_verified") is not True or source.get("symbol") != symbol:
                    raise ValueError("history_identity_or_provenance_not_verified")
                if len(rows) < 62:
                    raise ValueError("insufficient_completed_history")
                # Provider must label daily bars with their completion time.
                # A future or same-day unfinished bar cannot create an entry.
                timestamps = [utc_time(row["timestamp"]) for row in rows]
                if any(stamp > instant for stamp in timestamps):
                    raise ValueError("history_contains_future_bar")
                if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
                    raise ValueError("history_not_strictly_chronological")
                if source.get("data_sha256") and source["data_sha256"] != content_hash(rows):
                    raise ValueError("history_data_hash_mismatch")
                if source.get("normalized_data_sha256") and source["normalized_data_sha256"] != content_hash(rows):
                    raise ValueError("history_normalized_hash_mismatch")
                history_id = self._retain("price_history", history)
            except Exception as exc:
                errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}",
                               "entity_id": feature.get("entity_id"),
                               "exchange": feature.get("exchange"),
                               "lifecycle_status": feature.get("lifecycle_status"),
                               **({"history_id": history_id} if history_id else {}),
                               **({"source_attempt_id": source_attempt_id} if source_attempt_id else {})})
                continue
            result = {"symbol": symbol, "history_id": history_id, "bar_count": len(rows),
                      "last_bar": rows[-1]["timestamp"], "feature": feature, "candidates": [], "evaluation_errors": [],
                      **({"source_attempt_id": source_attempt_id, "coverage_complete": source.get("coverage_complete")}
                         if source_attempt_id else {})}
            history_results.append(result)
            admission = product_admissions.get(symbol) or self._product_admission(feature, now=instant)
            result["product_admission"] = admission
            if not admission["allowed"]:
                result.update(candidate_evaluation_status="not_evaluated_unsupported_product",
                              candidate_evaluation_reasons=admission["reasons"])
                continue
            for strategy, (_, version) in zip(strategies, strategy_versions):
                await asyncio.sleep(0)
                candidate, qualification, failure = await asyncio.to_thread(_evaluate_research_candidate, strategy,
                    version=version, symbol=symbol, rows=rows, source=source, family_size=family_size,
                    research_family=research_family, costs=self.costs)
                if failure is None:
                    try:
                        candidate["qualification_id"] = self._retain("qualification", qualification)
                        result["candidates"].append(candidate)
                    except Exception as exc:
                        failure = {"strategy_id": strategy.candidate_id, "strategy_version": version,
                                   "stage": "qualification_retention", "error": f"{type(exc).__name__}: {exc}"}
                if failure is not None:
                    result["evaluation_errors"].append(failure)
            result["candidate_evaluation_status"] = ("complete" if not result["evaluation_errors"] else
                "partial" if result["candidates"] else "failed")
        payload = {"schema_version": "open_stock_ai.autonomous_research_cycle.v1", "account_id": self.broker.account_id,
                   "created_at": instant.isoformat(),
                   "universe_count": len(all_features), "ordinary_stock_count": len(ordinary_features),
                   "usable_bulk_count": len(usable),
                   "ordinary_usable_bulk_count": len(rotation_usable),
                   "deep_selected_count": len(selected),
                   "deep_success_count": len(history_results), "results": history_results, "errors": errors,
                   "deep_success_count_scope": "verified_history_retained",
                   "candidate_evaluation_success_count": sum(len(row["candidates"]) for row in history_results),
                   "candidate_evaluation_error_count": sum(len(row["evaluation_errors"]) for row in history_results),
                   "bulk_evidence_id": self._retain("market_screen", {"features": all_features,
                       **({"security_master_refresh": scanned["security_master_refresh"]} if "security_master_refresh" in scanned else {})}),
                   **({"security_master_refresh_status": scanned["security_master_refresh"].get("status", "unknown")}
                      if "security_master_refresh" in scanned else {}),
                   "model_calls": 0, "cost_assumptions": self.costs,
                   "research_family": research_family,
                   "requested_symbols": list(requested_symbols) if requested_symbols is not None else None,
                   "selection": {"mode": "on_demand" if requested_symbols is not None else "rotation",
                       "policy": "requested_official_instruments_in_request_order" if requested_symbols is not None else
                       "supported_ordinary_stock_bulk_bounded_failed_retry_then_never_or_least_recently_selected_liquidity_tiebreak_no_holdout_ranking"}}
        cycle_id = "AC-" + content_hash(payload)
        payload["cycle_id"] = cycle_id
        with self.plans.store._connect() as conn:
            succeeded = {item["symbol"] for item in history_results}
            failure_by_symbol = {item["symbol"]: item["error"] for item in errors}
            conn.executemany("""
                insert into autonomous_deep_research_coverage(
                    account_id,symbol,last_selected_at,selection_count,last_success_at,success_count,last_error
                ) values (?,?,?,1,?,?,?)
                on conflict(account_id,symbol) do update set
                    last_selected_at=excluded.last_selected_at,
                    selection_count=autonomous_deep_research_coverage.selection_count+1,
                    last_success_at=coalesce(excluded.last_success_at,autonomous_deep_research_coverage.last_success_at),
                    success_count=autonomous_deep_research_coverage.success_count+excluded.success_count,
                    last_error=excluded.last_error
            """, [(self.broker.account_id, str(item["symbol"]), instant.isoformat(),
                    instant.isoformat() if str(item["symbol"]) in succeeded else None,
                    int(str(item["symbol"]) in succeeded), failure_by_symbol.get(str(item["symbol"])))
                   for item in selected])
            conn.execute("insert or ignore into autonomous_research_cycles values (?,?,?,?)", (cycle_id, self.broker.account_id, instant.isoformat(), json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            conn.execute("update autonomous_campaign_state set research_cursor=?,last_research_at=?,updated_at=? where account_id=?",
                         (cursor if requested_symbols is not None else (cursor+len(selected)) % max(1, len(rotation_usable)),
                          instant.isoformat(), instant.isoformat(), self.broker.account_id))
            conn.commit()
        await asyncio.to_thread(
            self.coverage_ledger.sync,
            all_features,
            observed_at=instant,
            product_admissions=product_admissions,
            source_cycle_id=cycle_id,
        )
        return payload

    async def create_plans(self, *, cycle_id: str, now: datetime | None = None) -> dict[str, Any]:
        instant = utc_time(now or datetime.now(timezone.utc))
        with self.plans.account_lease(self.broker.account_id, now=instant) as owner:
            if owner is None:
                return {"cycle_id": cycle_id, "plans": [], "skipped": [], "model_calls": 0, "status": "account_busy"}
            return await asyncio.wait_for(self._create_plans(cycle_id=cycle_id, instant=instant), timeout=45)

    async def _create_plans(self, *, cycle_id: str, instant: datetime) -> dict[str, Any]:
        cycle = self.cycle(cycle_id)
        if not 0 <= (instant - utc_time(cycle["created_at"])).total_seconds() <= 86400:
            raise ValueError("research_cycle_requires_refresh")
        account = await self.broker.account(now=instant)
        active = self.plans.list(account_id=self.broker.account_id, active_only=True)
        pending_budget = sum(float(p["definition"]["cash_budget"]) for p in active if not p["state"].get("entry_order_id"))
        reservations = account.get("open_order_reservations") or []
        if any(item.get("valid") is False or item.get("blockers") for item in reservations):
            raise ValueError("account_open_order_reservations_unverified")
        buy_reserved = sum(float(item["remaining_quantity"])*float(item["reservation_price"])
                           + float(item["estimated_remaining_cost"]) for item in reservations if item["side"] == "buy")
        exposure = sum(max(0.0, float(position.get("market_value") or 0)) for position in account["positions"])
        planning_cash = min(float(account.get("available_cash", account["cash_balance"])) - buy_reserved - pending_budget,
                            float(account["total_equity"])*.20 - exposure - buy_reserved - pending_budget)
        created, skipped, reused = [], [], []
        for result in cycle["results"]:
            # Two predeclared hypotheses are checked in fixed order. Test-set
            # P&L does not choose a winner after seeing the evaluation.
            candidate = next((c for c in result["candidates"] if c["signal"]["action"] == "buy"
                              and c["research_paper_candidate_eligible"]), None)
            if candidate is None:
                skipped.append({"symbol": result["symbol"], "reason": "no_frozen_candidate_entry"})
                continue
            decision_key = content_hash([result["symbol"], result["last_bar"], candidate["strategy_version"]])
            prior = self.plans.get_by_idempotency(account_id=self.broker.account_id, idempotency_key=decision_key)
            if prior is not None:
                created.append(prior)
                reused.append(prior["plan_id"])
                continue
            if any(p["symbol"] == result["symbol"] for p in self.plans.list(account_id=self.broker.account_id, active_only=True)):
                skipped.append({"symbol": result["symbol"], "reason": "existing_plan_manages_symbol"})
                continue
            if any(p["symbol"] == result["symbol"] and float(p["quantity"]) != 0 for p in account["positions"]):
                skipped.append({"symbol": result["symbol"], "reason": "existing_position_requires_adoption"})
                continue
            retained_feature = result.get("feature") or {}
            if retained_feature.get("symbol") != result["symbol"]:
                skipped.append({"symbol": result["symbol"], "reason": "product_research_identity_mismatch"})
                continue
            product_admission = self._product_admission(retained_feature, now=instant)
            if not product_admission["allowed"]:
                skipped.append({"symbol": result["symbol"], "reason": "product_admission_rejected",
                                "product_admission": product_admission})
                continue
            from open_stock_ai.types import TradingSignal
            signal = TradingSignal(**candidate["signal"])
            sized = plan_signal_order(signal, equity=account["total_equity"], cash=max(0.0, planning_cash),
                                      reference_price=float(signal.entry_price), position_quantity=0,
                                      commission_bps=self.costs["commission_bps"], minimum_commission=self.costs["minimum_commission"])
            if not sized["eligible"]:
                skipped.append({"symbol": result["symbol"], "reason": sized["reason"]})
                continue
            local = instant.astimezone(ZoneInfo("Asia/Taipei"))
            bar_day = utc_time(result["last_bar"]).astimezone(ZoneInfo("Asia/Taipei")).date()
            entry_day = self.calendar.next_trading_day(bar_day)
            not_before = datetime.combine(entry_day, time(9, 5), tzinfo=ZoneInfo("Asia/Taipei"))
            last_holding_day = entry_day
            for _ in range(19):
                last_holding_day = self.calendar.next_trading_day(last_holding_day)
            exit_deadline = datetime.combine(last_holding_day, time(13, 25), tzinfo=ZoneInfo("Asia/Taipei"))
            if local >= not_before + timedelta(hours=4):
                skipped.append({"symbol": result["symbol"], "reason": "completed_candle_signal_entry_session_elapsed"})
                continue
            definition = TradingPlan(
                symbol=result["symbol"], strategy_id=candidate["strategy_id"], strategy_version=candidate["strategy_version"],
                evidence_ids=(result["history_id"], cycle["bulk_evidence_id"]), reference_price=sized["reference_price"],
                position_size_pct=sized["position_size_pct"], stop_loss=sized["stop_loss"], target_price=sized["target_price"],
                quantity_shares=sized["quantity"], cash_budget=sized["cash_budget"],
                not_before=not_before.isoformat(), expires_at=(not_before+timedelta(hours=4)).isoformat(),
                max_holding_seconds=int((exit_deadline-not_before).total_seconds()),
                exit_not_after=exit_deadline.isoformat(), rationale=signal.reason,
                qualification_id=candidate["qualification_id"],
                metadata={"cycle_id": cycle_id, "sizing": sized, "strategy_inputs": "completed_candles",
                          "fundamental_and_event_overlay": "retained_bulk_inputs_for_agent_review",
                          "product_admission": product_admission,
                          "eligibility": "bounded_experiment"},
            )
            created.append(self.plans.create(account_id=self.broker.account_id, plan=definition,
                                            idempotency_key=decision_key, now=instant))
            planning_cash -= definition.cash_budget
        return {"cycle_id": cycle_id, "plans": created, "skipped": skipped, "reused_plan_ids": reused, "model_calls": 0}

    def _plan_evidence(self, plan: TradingPlan) -> dict[str, Any]:
        history = self._evidence(plan.evidence_ids[0], "price_history")
        source = history["data_evidence"]
        return {"required_evidence": ["price_history", "cost_model"],
                "receipts": {"price_history": order_risk_evidence_receipt(kind="price_history", source=str(source.get("source_id")),
                    payload={"symbol": plan.symbol, "bars": len(history["rows"]), "evidence_id": plan.evidence_ids[0]},
                    passed=source.get("source_provenance_verified") is True and source.get("symbol") == plan.symbol)},
                "qualification": self._evidence(plan.qualification_id, "qualification") if plan.qualification_id else None,
                "experiment_limits": dict(self.experiment_limits)}

    async def manage(self, *, now: datetime | None = None, entry_symbols: frozenset[str] | None = None) -> dict[str, Any]:
        """An optional Host scope restricts new entries, never existing protection."""
        instant = utc_time(now or datetime.now(timezone.utc))
        enabled = self.status()["enabled"]
        results, errors, skipped_entries = [], [], []
        active_plans = self.plans.list(account_id=self.broker.account_id, active_only=True)
        # A slow new-entry quote must not delay reconciliation of existing
        # commitments. Preserve creation order within each group.
        active_plans.sort(key=lambda plan: not bool(plan["state"].get("entry_order_id")))
        for plan in active_plans:
            has_exit_request = self.plans.exit_request(plan["plan_id"], strategy_version=plan["definition"]["strategy_version"])
            if (entry_symbols is not None and plan["symbol"] not in entry_symbols
                    and not plan["state"].get("entry_order_id") and not has_exit_request):
                skipped_entries.append({"plan_id": plan["plan_id"], "symbol": plan["symbol"], "reason": "outside_host_entry_scope"})
                continue
            # Pausing entry does not abandon already submitted orders/positions.
            if (not enabled and not plan["state"].get("entry_order_id")
                    and not has_exit_request):
                continue
            try:
                try:
                    market = await asyncio.wait_for(
                        self.quote_loader(plan["symbol"]), timeout=self.quote_timeout_seconds,
                    )
                except Exception as exc:
                    # Missing quotes forbid fills, not reconciliation or the
                    # cancellation of an expired/paused outstanding buy.
                    errors.append({"plan_id": plan["plan_id"], "stage": "quote",
                                   "error": f"{type(exc).__name__}: {exc}",
                                   **({"timeout_seconds": self.quote_timeout_seconds}
                                      if isinstance(exc, TimeoutError) else {})})
                    market = {"symbol": plan["symbol"], "price": 0, "source_envelope": {}}
                # Network latency can span the quote clock-skew tolerance.
                # Use current host time after fetching, never the time of the
                # first plan in this pass. Explicit clocks remain replayable.
                tick_now = instant if now is not None else datetime.now(timezone.utc)
                results.append(await self.executor.tick(plan["plan_id"], market=market, now=tick_now))
            except Exception as exc:
                errors.append({"plan_id": plan["plan_id"], "error": f"{type(exc).__name__}: {exc}"})
        fills_loader = getattr(self.broker, "fills", None)
        if callable(fills_loader):
            completed = {item["plan_id"] for item in self.outcomes.list(account_id=self.broker.account_id)}
            for record in self.plans.list(account_id=self.broker.account_id):
                if record["status"] != "closed" or record["plan_id"] in completed:
                    continue
                try:
                    order_ids = {
                        str((event["payload"].get(f"{phase}_intent") or {}).get("order_id"))
                        for event in self.plans.events(record["plan_id"]) for phase in ("entry", "exit")
                        if (event["payload"].get(f"{phase}_intent") or {}).get("order_id")
                    }
                    fills = await fills_loader(sorted(order_ids))
                    self.outcomes.record(plan=record, fills=fills, mode=self.broker.mode)
                except Exception as exc:
                    errors.append({"plan_id": record["plan_id"], "stage": "outcome", "error": f"{type(exc).__name__}: {exc}"})
        forward = None
        if self.forward_monitor:
            try:
                forward = await self.forward_monitor.sync()
            except Exception as exc:
                forward = {"errors": [{"error": f"{type(exc).__name__}: {exc}"}], "model_calls": 0}
        return {"account_id": self.broker.account_id, "enabled": enabled, "results": results, "errors": errors, "model_calls": 0,
                **({"forward_validation": forward} if forward is not None else {}),
                **({"entry_symbols": sorted(entry_symbols), "skipped_entries": skipped_entries} if entry_symbols is not None else {})}
