"""Receipt-driven execution of a persisted, time/price conditional trade plan."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
from typing import Any

from open_stock_ai.data.execution_quote import plan_quote_eligibility
from open_stock_ai.risk.order_policy import order_risk_evidence_receipt
from open_stock_ai.risk.risk_engine import RiskEngine
from open_stock_ai.strategy.execution_policy import single_order_quantity
from .broker_port import BrokerPort
from .trading_plan import TradingPlan, content_hash, utc_time
from .trading_plan_store import TradingPlanStore, TERMINAL_PLAN_STATES
from .product_admission import assess_new_entry_product, resolve_product_snapshot


# Delivery retries for the same risk-reducing cancellation are separate from
# the plan's budget for replacement sell orders.
_MAX_EXIT_CANCEL_ATTEMPTS = 3


class TradingPlanExecutor:
    """The same order state machine is used regardless of the broker backend.

    Intent is committed before submission. An interrupted external call is only
    reconciled by its durable order ID; absence of a receipt never means it is
    safe to invent a second order. Account leases serialize competing plans.
    """

    def __init__(self, *, store: TradingPlanStore, broker: BrokerPort,
                 risk: RiskEngine, evidence_resolver, eligibility: str = "bounded_experiment", entry_permission=None,
                 product_resolver=None):
        self.store, self.broker, self.risk = store, broker, risk
        self.evidence_resolver, self.eligibility = evidence_resolver, eligibility
        self.entry_permission = entry_permission or (lambda: True)
        self.product_resolver = product_resolver

    async def tick(self, plan_id: str, *, market: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        instant = utc_time(now or datetime.now(timezone.utc))
        record = self.store.get(plan_id)
        if record["account_id"] != self.broker.account_id:
            raise ValueError("plan_broker_account_mismatch")
        if record["symbol"] != str(market.get("symbol") or "").upper():
            raise ValueError("plan_quote_symbol_mismatch")
        with self.store.account_lease(record["account_id"], now=instant) as account_owner:
            if account_owner is None:
                return {"status": "account_busy", "plan_id": plan_id}
            with self.store.lease(plan_id, now=instant) as owner:
                if owner is None:
                    return {"status": "plan_busy", "plan_id": plan_id}
                # Finish before either lease can expire. Cancellation/timeout
                # after intent persistence leaves reconciliation evidence.
                return await asyncio.wait_for(self._tick(plan_id, market, instant, owner), timeout=45)

    async def _tick(self, plan_id: str, market: dict[str, Any], now: datetime, owner: str) -> dict[str, Any]:
        record = self.store.get(plan_id)
        plan, state = TradingPlan.from_dict(record["definition"]), dict(record["state"])
        if state["status"] in TERMINAL_PLAN_STATES:
            return record
        requested_exit = self.store.exit_request(plan_id, strategy_version=plan.strategy_version)
        if requested_exit:
            state["exit_reason"] = requested_exit["reason"]
            state["host_exit_request"] = requested_exit

        def save(event: str) -> dict[str, Any]:
            nonlocal record
            record = self.store.save_state(plan_id, state=state, revision=record["revision"],
                                           event_type=event, now=now, lease_owner=owner)
            return record

        def uncertain(reason: str) -> dict[str, Any]:
            state.update(status="reconciliation_required", wait_reason=reason)
            return save("plan.reconciliation_required")

        def position(entry: dict[str, Any], exit_order: dict[str, Any] | None) -> None:
            filled = float(entry["filled_quantity"])
            sold = float(state.get("sold_before_retry", 0)) + (float(exit_order["filled_quantity"]) if exit_order else 0.0)
            if not math.isfinite(sold) or sold < 0 or sold > filled:
                raise ValueError("invalid_broker_cumulative_fills")
            state.update(filled_quantity=filled, remaining_quantity=filled - sold,
                         entry_receipt=entry, exit_receipt=exit_order)

        def exit_attention(reason: str) -> dict[str, Any]:
            prior = state.get("exit_alert") or {}
            intent = (state.get("exit_intent") if state.get("exit_order_id")
                      else state.get("last_exit_preview_intent") or state.get("exit_intent")) or {}
            preview_blockers = []
            if reason in {"broker_preview_or_frozen_budget", "invalid_broker_cost_estimate"}:
                preview = state.get("last_preview") or {}
                for item in (preview.get("market_rules") or {}).get("blockers", []):
                    code = item.get("code") if isinstance(item, dict) else item
                    if isinstance(code, str) and code:
                        preview_blockers.append(code)
                if validation_reason := (preview.get("validation") or {}).get("reason"):
                    preview_blockers.append(str(validation_reason))
            state.update(wait_reason=reason, exit_alert={
                "reason": reason, "raised_at": prior.get("raised_at") or now.isoformat(),
                "last_checked_at": now.isoformat(), "order_id": state.get("exit_order_id"),
                "remaining_quantity": state.get("remaining_quantity", 0),
                "limit_price": intent.get("limit_price"),
                "replacement_count": int(state.get("exit_replacement_count", 0)),
                "cancel_attempt_count": int((state.get("exit_cancel_intent") or {}).get("attempt_count", 0)),
                "requires_attention": True,
                **({"preview_blockers": list(dict.fromkeys(preview_blockers))} if preview_blockers else {}),
            })
            return save("plan.exit_attention_required")

        if requested_exit and not state.get("entry_order_id"):
            state.update(status="cancelled", wait_reason="agent_cancelled_before_entry")
            return save("plan.cancelled_before_entry")

        entry_allowed = bool(self.entry_permission())
        if not entry_allowed and not state.get("entry_order_id"):
            state["wait_reason"] = "campaign_entry_paused"
            return save("plan.entry_paused")

        checked_receipts: dict[str, dict[str, Any]] = {}

        def checked(receipt: dict[str, Any] | None, phase: str) -> dict[str, Any] | None:
            if receipt is None:
                return None
            intent = state.get(f"{phase}_intent") or {}
            maximum = int(intent.get("quantity_shares") or plan.quantity_shares)
            prior = checked_receipts.get(phase) or state.get(f"{phase}_receipt") or {}
            result = _checked_receipt(
                receipt, order_id=state[f"{phase}_order_id"], account_id=record["account_id"],
                symbol=plan.symbol, maximum=maximum, previous=prior, now=now,
            )
            checked_receipts[phase] = result
            return result

        async def orders() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            entry = checked(await self.broker.order(state["entry_order_id"]), "entry") if state.get("entry_order_id") else None
            exit_order = checked(await self.broker.order(state["exit_order_id"]), "exit") if state.get("exit_order_id") else None
            return entry, exit_order

        def product_admission():
            frozen = plan.metadata.get("product_admission") or {}
            return assess_new_entry_product(symbol=plan.symbol, market=plan.market, now=now,
                expected_entity_id=frozen.get("entity_id") or "",
                product_snapshot=resolve_product_snapshot(self.product_resolver, symbol=plan.symbol, market=plan.market, now=now))

        price = float(market.get("price") or 0)
        quote = plan_quote_eligibility(
            dict(market.get("source_envelope") or {}), mode=self.broker.mode,
            paper_execution_model=getattr(self.broker, "paper_quote_execution_model", None),
            quantity_shares=plan.quantity_shares, now=now,
        )
        executable = quote["execution_eligible"] and math.isfinite(price) and price > 0
        fingerprint = _market_fingerprint(market)
        mark = getattr(self.broker, "mark", None)
        if (callable(mark) and quote["research_eligible"] and math.isfinite(price) and price > 0
                and state.get("last_mark_fingerprint") != fingerprint):
            await mark(market)
            state["last_mark_fingerprint"] = fingerprint
            save("plan.valuation_marked")
        try:
            entry, exit_order = await orders()
        except ValueError as exc:
            return uncertain(str(exc))
        if state.get("entry_order_id") and entry is None or state.get("exit_order_id") and exit_order is None:
            return uncertain("submitted_order_receipt_unavailable")
        if entry:
            try:
                position(entry, exit_order)
            except ValueError as exc:
                return uncertain(str(exc))

        if entry and entry["filled_quantity"] and not state.get("entered_at"):
            # A restart must not reset a holding deadline. Prefer the broker's
            # first real fill; an uncertain legacy receipt uses the earlier
            # journalled dispatch time conservatively, never recovery time.
            state["entered_at"] = (
                entry.get("first_filled_at") or state.get("entry_dispatched_at") or record["created_at"]
            )
            state["holding_time_basis"] = "broker_first_fill" if entry.get("first_filled_at") else "conservative_dispatch_time"
        time_exit = (
            plan.time_exit_reason(entered_at=state["entered_at"], now=now)
            if entry and entry["filled_quantity"] and state.get("entered_at") else None
        )
        reason = (
            plan.exit_reason(price=price, entered_at=state.get("entered_at") or now.isoformat(), now=now)
            if executable else time_exit
        )
        expired_entry = bool(
            plan.expires_at and now >= utc_time(plan.expires_at)
            or plan.exit_not_after and now >= utc_time(plan.exit_not_after)
        )
        if reason and entry and state.get("exit_reason") != reason:
            # The stop decision must survive an interrupted/pending cancel and
            # a subsequent price rebound. Persist it before broker effects.
            state["exit_reason"] = reason
            save("plan.exit_triggered")
        product_invalidation = state.get("entry_product_invalidation") or {}
        product_cancel_required = bool(entry and product_invalidation.get("order_id") == entry["order_id"])
        pending_protection = bool(entry and entry["is_open"] and (
            reason or expired_entry or state.get("exit_reason") or exit_order or not entry_allowed or product_cancel_required
        ))
        if entry and entry["is_open"] and not pending_protection:
            # An open buy still commits new exposure. Reconcile its fills first,
            # then revoke only the remaining buy quantity before another tick
            # can match it. Existing exit triggers never depend on this lookup.
            state["last_product_admission"] = product_admission()
            if not state["last_product_admission"]["allowed"]:
                product_cancel_required = pending_protection = True
                state["entry_product_invalidation"] = {
                    "order_id": entry["order_id"], "reason": "product_admission_rejected",
                    "observed_at": now.isoformat(), "product_admission": state["last_product_admission"],
                }
                save("plan.entry_product_invalidated")
        # Cancellation only reduces an outstanding commitment. Its time limit
        # must work through a quote outage; stale prices never trigger a stop.
        if pending_protection:
            try:
                cancel_reason = reason or state.get("exit_reason") or ("campaign_entry_paused" if not entry_allowed else
                    "product_admission_rejected" if product_cancel_required else "entry_window_expired")
                entry = checked(await self.broker.cancel(entry["order_id"], reason=cancel_reason), "entry")
            except ValueError as exc:
                return uncertain(str(exc))
            if entry is None:
                return uncertain("entry_cancel_receipt_unavailable")
            try:
                position(entry, exit_order)
            except ValueError as exc:
                return uncertain(str(exc))
            if entry["is_open"]:
                state["wait_reason"] = "awaiting_entry_cancellation"
                return save("plan.cancel_pending")

        # Identity comes from the exchange observation, never the host's fetch
        # time. A retry cannot repeatedly consume the same quoted liquidity.
        if executable and not pending_protection and state.get("last_matching_fingerprint") != fingerprint:
            state["last_matching_fingerprint"] = fingerprint
            save("plan.market_observation_reserved")
            await self.broker.observe(market)
            try:
                entry, exit_order = await orders()
            except ValueError as exc:
                return uncertain(str(exc))
            if state.get("entry_order_id") and entry is None or state.get("exit_order_id") and exit_order is None:
                return uncertain("submitted_order_receipt_unavailable")
        if entry:
            filled = float(entry["filled_quantity"])
            try:
                position(entry, exit_order)
            except ValueError as exc:
                return uncertain(str(exc))
            if filled and not state.get("entered_at"):
                state["entered_at"] = entry.get("first_filled_at") or state.get("entry_dispatched_at") or record["created_at"]
                state["holding_time_basis"] = "broker_first_fill" if entry.get("first_filled_at") else "conservative_dispatch_time"
            if exit_order and not exit_order["is_open"] and state["remaining_quantity"] == 0 and filled > 0:
                state.update(status="closed", closed_at=now.isoformat(), wait_reason=None, exit_alert=None)
                return save("plan.closed")
            if exit_order and exit_order["is_open"]:
                state.update(status="exit_submitted", wait_reason="awaiting_exit_fill")
                policy = plan.exit_order_policy
                wait_seconds = policy.wait_seconds if policy else 60
                cancellation = state.get("exit_cancel_intent") or {}
                if cancellation.get("order_id") == exit_order["order_id"]:
                    attempts = int(cancellation.get("attempt_count", 1))
                    last_attempt = cancellation.get("last_attempt_at") or cancellation["requested_at"]
                    if attempts >= _MAX_EXIT_CANCEL_ATTEMPTS:
                        return exit_attention("exit_cancellation_limit_reached")
                    if (now - utc_time(last_attempt)).total_seconds() < wait_seconds:
                        return exit_attention("awaiting_exit_cancellation")
                    # Reconciliation above confirmed this exact order is open.
                    # A prior saved intent may have crashed before dispatch;
                    # retry only its cancellation, even through a quote outage.
                    state["exit_cancel_intent"] = {
                        **cancellation, "attempt_count": attempts + 1, "last_attempt_at": now.isoformat(),
                    }
                else:
                    dispatched = state.get("exit_dispatched_at") or record["created_at"]
                    if (now - utc_time(dispatched)).total_seconds() < wait_seconds:
                        return save("plan.exit_reconciled")
                    if policy is None:
                        return exit_attention("exit_fill_timeout")
                    if int(state.get("exit_replacement_count", 0)) >= policy.max_replacements:
                        return exit_attention("exit_replacement_limit_reached")
                    if not executable:
                        state["quote_gate"] = quote
                        return exit_attention("exit_quote_ineligible")
                    if price < plan.executable_exit_price_floor():
                        return exit_attention("exit_price_floor_reached")
                    if price >= float(state["exit_intent"]["limit_price"]):
                        return exit_attention("exit_fill_timeout")
                    if state.get("last_submission_fingerprint") == fingerprint:
                        return exit_attention("awaiting_new_market_observation")
                    state["exit_cancel_intent"] = {
                        "order_id": exit_order["order_id"], "requested_at": now.isoformat(),
                        "reason": "exit_fill_timeout", "minimum_limit_price": policy.minimum_limit_price,
                        "attempt_count": 1, "last_attempt_at": now.isoformat(),
                    }
                state["wait_reason"] = "awaiting_exit_cancellation"
                save("plan.exit_cancel_intent_persisted")
                try:
                    exit_order = checked(await self.broker.cancel(exit_order["order_id"], reason="exit_fill_timeout"), "exit")
                except ValueError as exc:
                    return uncertain(str(exc))
                if exit_order is None:
                    return uncertain("exit_cancel_receipt_unavailable")
                try:
                    position(entry, exit_order)
                except ValueError as exc:
                    return uncertain(str(exc))
                if exit_order["is_open"]:
                    if state["exit_cancel_intent"]["attempt_count"] >= _MAX_EXIT_CANCEL_ATTEMPTS:
                        return exit_attention("exit_cancellation_limit_reached")
                    return exit_attention("awaiting_exit_cancellation")
                if state["remaining_quantity"] == 0:
                    state.update(status="closed", closed_at=now.isoformat(), wait_reason=None, exit_alert=None)
                    return save("plan.closed")
            if exit_order and not exit_order["is_open"]:
                state["exit_attempt"] = int(state.get("exit_attempt", 0)) + 1
                state["exit_order_id"] = None
                state["status"] = "open"
                state["sold_before_retry"] = filled - state["remaining_quantity"]
                state["exit_previous_limit_price"] = state["exit_intent"]["limit_price"]
                state["exit_retry_required"] = exit_order["status"] != "filled"
                state.pop("exit_cancel_intent", None)
            if not entry["is_open"] and filled == 0:
                state.update(status="expired" if expired_entry else "rejected" if entry["status"] == "rejected" else "cancelled")
                return save("plan.entry_ended")

        if not entry and expired_entry:
            state.update(status="expired", wait_reason="entry_window_expired")
            return save("plan.expired")
        if reason and entry and state.get("remaining_quantity", 0) > 0:
            state["exit_reason"] = reason
        if not executable:
            state["wait_reason"] = "quote_ineligible"
            state["quote_gate"] = quote
            return save("plan.waiting_quote")

        if not entry:
            trigger = plan.entry_status(price=price, now=now)
            if trigger != "triggered":
                if trigger == "expired":
                    state["status"] = "expired"
                elif trigger in {"thesis_invalidated", "opportunity_passed"}:
                    state["status"] = "invalidated"
                state["wait_reason"] = trigger
                return save("plan.entry_condition")
            unresolved = self.store.unresolved_dispatches(account_id=record["account_id"], excluding_plan_id=plan_id)
            if unresolved:
                state.update(wait_reason="account_reconciliation_required", unresolved_plan_ids=unresolved)
                return save("plan.account_reconciliation_wait")
            phase, quantity = "entry", plan.quantity_shares
        else:
            if not reason and not state.get("exit_reason"):
                state.update(status="entry_submitted" if entry["is_open"] else "open", wait_reason="awaiting_entry_fill" if entry["is_open"] else "monitoring_exit")
                return save("plan.position_managed")
            state["exit_reason"] = reason or state["exit_reason"]
            phase = "exit"
            quantity = single_order_quantity(int(state["remaining_quantity"]), market=plan.market)["quantity"]
            policy = plan.exit_order_policy
            if policy and state.get("exit_retry_required"):
                if int(state.get("exit_replacement_count", 0)) >= policy.max_replacements:
                    return exit_attention("exit_replacement_limit_reached")
                if price < plan.executable_exit_price_floor():
                    return exit_attention("exit_price_floor_reached")
            if state.get("last_submission_fingerprint") == fingerprint:
                state["wait_reason"] = "awaiting_new_market_observation"
                return save("plan.waiting_market_observation")

        if phase == "entry":
            state["last_product_admission"] = product_admission()
            if not state["last_product_admission"]["allowed"]:
                state["wait_reason"] = "product_admission_rejected"
                return save("plan.product_admission_wait")

        account = await self.broker.account(now=now)
        # Fixed decision quantity cannot increase as price/equity changes.
        # A price gap above its cash allocation waits for re-analysis instead.
        intent = {
            "order_id": f"{plan_id}-{phase}-{int(state.get('exit_attempt', 0)) if phase == 'exit' else 0}",
            "symbol": plan.symbol, "market": plan.market, "side": "buy" if phase == "entry" else "sell",
            "quantity_shares": quantity, "reference_price": price, "limit_price": price,
            "stop_loss": plan.stop_loss, "strategy_id": plan.strategy_id,
            "strategy_version": plan.strategy_version, "account_id": self.broker.account_id,
            "strategy_version_hash": plan.strategy_version,
            "reduce_only": phase == "exit", "rationale": plan.rationale,
        }
        if plan.exit_order_policy:
            intent["exit_minimum_limit_price"] = plan.exit_order_policy.minimum_limit_price
        if phase == "entry":
            intent["product_admission"] = state["last_product_admission"]
        if phase == "exit":
            if plan.exit_order_policy:
                intent["limit_price"] = max(price, plan.executable_exit_price_floor())
            else:
                # Legacy plans authorize their submitted limit. An expired or
                # partial child does not grant permission to chase a new price.
                intent["limit_price"] = state.get("exit_previous_limit_price", price)
        preview = await self.broker.preview(intent, market)
        total = float(preview.get("estimated_costs", {}).get("estimated_total", float("nan")))
        if not preview.get("can_submit") or not math.isfinite(total) or phase == "entry" and total > plan.cash_budget + 0.01:
            state.update(wait_reason="broker_preview_or_frozen_budget", last_preview=preview)
            if phase == "exit":
                state.update(status="open", last_exit_preview_intent=intent)
                return exit_attention("broker_preview_or_frozen_budget")
            return save("plan.admission_wait")
        costs = _preview_cost_components(preview, quantity=quantity, price=price, side=intent["side"])
        if costs is None:
            state.update(wait_reason="invalid_broker_cost_estimate", last_preview=preview)
            if phase == "exit":
                state.update(status="open", last_exit_preview_intent=intent)
                return exit_attention("invalid_broker_cost_estimate")
            return save("plan.admission_wait")
        state["last_cost_estimate"] = costs
        evidence = dict(self.evidence_resolver(plan))
        receipts = dict(evidence.get("receipts") or {})
        receipts["cost_model"] = order_risk_evidence_receipt(
            kind="cost_model", source="broker.preview", passed=True,
            payload={**preview.get("cost_evidence", {}), **intent, **costs},
        )
        evidence["receipts"] = receipts
        risk = self.risk.evaluate_order_intent(intent=intent, account_summary=account, evidence=evidence,
                                             mode=self.broker.mode, eligibility=self.eligibility, now=now)
        state["last_risk"] = risk
        if not risk.get("approved"):
            state["wait_reason"] = "risk_admission"
            return save("plan.risk_wait")
        if phase == "entry" and not self.entry_permission():
            state["wait_reason"] = "campaign_entry_paused"
            return save("plan.entry_paused")
        if phase == "entry":
            # Awaited account/preview work may outlive a classification update.
            # Recheck locally before persisting any dispatch ID, with no await
            # between this decision and the broker's submission boundary.
            state["last_product_admission"] = product_admission()
            if not state["last_product_admission"]["allowed"]:
                state["wait_reason"] = "product_admission_rejected"
                return save("plan.product_admission_wait")
            intent["product_admission"] = state["last_product_admission"]
        state.update(status=f"{phase}_dispatching", wait_reason=None)
        if phase == "exit" and state.get("exit_retry_required"):
            state["exit_replacement_count"] = int(state.get("exit_replacement_count", 0)) + 1
        if phase == "exit":
            state["exit_retry_required"] = False
            state["exit_alert"] = None
        state[f"{phase}_order_id"] = intent["order_id"]
        state[f"{phase}_intent"] = intent
        state[f"{phase}_dispatched_at"] = now.isoformat()
        state["last_submission_fingerprint"] = fingerprint
        save("plan.intent_persisted")
        receipt = await self.broker.submit(intent, market)
        try:
            receipt = checked(receipt, phase)
        except ValueError as exc:
            return uncertain(str(exc))
        if receipt is None:
            return uncertain("submitted_order_receipt_unavailable")
        state.update(status=f"{phase}_submitted")
        state[f"{phase}_receipt"] = receipt
        if phase == "entry" and float(receipt.get("filled_quantity") or 0) > 0:
            state["entered_at"] = receipt.get("first_filled_at") or now.isoformat()
            state["holding_time_basis"] = "broker_first_fill" if receipt.get("first_filled_at") else "conservative_dispatch_time"
        state["last_matching_fingerprint"] = fingerprint
        # A single tick can submit at most one order. Next tick reconciles its
        # cumulative receipt, preventing same-tick speculative round trips.
        return save("plan.order_submitted")


def _preview_cost_components(preview: dict[str, Any], *, quantity: int, price: float,
                             side: str) -> dict[str, Any] | None:
    costs = preview.get("estimated_costs") or {}
    try:
        total = float(costs["estimated_total"])
        gross = float(costs.get("gross_amount", quantity * float(preview.get("estimated_fill_price", price))))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(total) or not math.isfinite(gross) or gross <= 0:
        return None
    # The broker's gross/net invariant isolates commissions, taxes and fees.
    # A favorable resting-limit price must not offset those charges, and price
    # impact belongs in a separate component rather than an absolute net delta.
    fees = total - gross if side == "buy" else gross - total
    if fees < -0.01:
        return None
    fees = max(0.0, fees)
    adverse_price_cost = max(0.0, (gross - quantity * price) * (1 if side == "buy" else -1))
    return {
        "estimated_total_cost": fees + adverse_price_cost,
        "estimated_fees": fees, "estimated_adverse_price_cost": adverse_price_cost,
        "estimated_gross_amount": gross,
        "cost_basis": "broker_preview_gross_net" if "gross_amount" in costs else "legacy_preview_fill_notional",
    }


def _market_fingerprint(market: dict[str, Any]) -> str:
    envelope = dict(market.get("source_envelope") or {})
    observed_at = envelope.get("exchange_timestamp") or market.get("source_timestamp")
    return content_hash({
        "exchange_timestamp": observed_at,
        "source": {key: envelope.get(key) for key in ("provider_id", "connector_id", "quote_kind", "trading_state")},
        **{key: market.get(key) for key in (
            "symbol", "price", "available_quantity", "available_volume", "bid", "ask",
            "trading_state", "odd_lot_auction_matched",
        )},
    })


def _checked_receipt(receipt: dict[str, Any], *, order_id: str, account_id: str,
                     symbol: str, maximum: int, previous: dict[str, Any], now: datetime) -> dict[str, Any]:
    if receipt.get("order_id") != order_id:
        raise ValueError("broker_receipt_id_mismatch")
    if receipt.get("account_id") is not None and receipt["account_id"] != account_id:
        raise ValueError("broker_receipt_account_mismatch")
    if receipt.get("symbol") is not None and str(receipt["symbol"]).upper() != symbol:
        raise ValueError("broker_receipt_symbol_mismatch")
    try:
        filled, remaining = float(receipt["filled_quantity"]), float(receipt["remaining_quantity"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid_broker_cumulative_fills") from None
    if any(not math.isfinite(value) or value < 0 or not value.is_integer() for value in (filled, remaining)):
        raise ValueError("invalid_broker_cumulative_fills")
    if filled > maximum or filled + remaining > maximum:
        raise ValueError("invalid_broker_cumulative_fills")
    if previous.get("order_id") == order_id and filled < float(previous.get("filled_quantity") or 0):
        raise ValueError("broker_cumulative_fills_regressed")
    if not isinstance(receipt.get("is_open"), bool):
        raise ValueError("invalid_broker_open_status")
    if receipt["is_open"] and (remaining <= 0 or filled + remaining != maximum):
        raise ValueError("invalid_broker_open_quantity")
    if not receipt["is_open"] and receipt.get("status") not in {"filled", "canceled", "cancelled", "expired", "rejected"}:
        raise ValueError("invalid_broker_terminal_status")
    if receipt.get("status") == "filled" and (filled != maximum or remaining != 0):
        raise ValueError("invalid_broker_filled_quantity")
    if receipt.get("first_filled_at"):
        if (utc_time(receipt["first_filled_at"]) - now).total_seconds() > 5:
            raise ValueError("broker_fill_timestamp_in_future")
    return {**receipt, "filled_quantity": filled, "remaining_quantity": remaining}
