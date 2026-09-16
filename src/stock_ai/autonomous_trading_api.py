from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from open_stock_ai.execution.trading_plan import utc_time

from .autonomous_trading_service import get_autonomous_campaign, get_autonomous_model_review, autonomous_status_snapshot


router = APIRouter(prefix="/agent/autonomy", tags=["Autonomous Trading"])


class AutonomyControl(BaseModel):
    enabled: bool


class ResearchCycleRequest(BaseModel):
    deep_limit: int = Field(default=20, ge=1, le=20)


class ActivateCycleRequest(BaseModel):
    cycle_id: str = Field(min_length=1)
    enabled: bool = True
    use_candidate_plans: bool = False


@router.get("/status")
async def autonomy_status():
    return await autonomous_status_snapshot(get_autonomous_campaign())


@router.get("/coverage")
def autonomy_coverage(
    symbol: str | None = None,
    deep_status: str | None = None,
    domain: str | None = None,
    needs_update: bool | None = None,
    new_entry_eligible: bool | None = None,
    after: str | None = None,
    limit: int = 50,
):
    try:
        return get_autonomous_campaign().coverage_ledger.query(
            symbols=[symbol] if symbol else None,
            deep_status=deep_status,
            domain=domain,
            needs_update=needs_update,
            new_entry_eligible=new_entry_eligible,
            after=after,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/control")
def autonomy_control(request: AutonomyControl):
    service = get_autonomous_campaign()
    authorized_at = datetime.now(timezone.utc)
    # Explicit stop must reach the trading gate even if model control is unavailable.
    state = service.configure(enabled=request.enabled, **({"authorized_at": authorized_at} if request.enabled else {}))
    review = get_autonomous_model_review(service)
    review.configure(enabled=request.enabled, **({"authorized_at": authorized_at} if request.enabled else {}))
    return {**state, "model_review": review.status()}


@router.post("/research")
async def autonomy_research(request: ResearchCycleRequest):
    return await get_autonomous_campaign().research(deep_limit=request.deep_limit)


@router.get("/cycles/{cycle_id}")
def autonomy_cycle(cycle_id: str):
    try:
        return get_autonomous_campaign().cycle(cycle_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/activate")
async def autonomy_activate(request: ActivateCycleRequest):
    authorized_at = datetime.now(timezone.utc)
    service = get_autonomous_campaign()
    if not request.enabled:
        # Stopping entry must not depend on research freshness or availability.
        # Stop the trading control first; keep existing protection/reconciliation.
        service.configure(enabled=False)
        review = get_autonomous_model_review(service)
        review.configure(enabled=False)
        return {"cycle_id": request.cycle_id, "plans": [], "skipped": [],
                "activation_skipped": "campaign_disabled", "model_review": review.status(),
                "management": await service.manage()}
    try:
        cycle = service.cycle(request.cycle_id)
        if not 0 <= (datetime.now(timezone.utc) - utc_time(cycle["created_at"])).total_seconds() <= 86400:
            raise ValueError("research_cycle_requires_refresh")
        plans = await service.create_plans(cycle_id=request.cycle_id) if request.use_candidate_plans else {
            "cycle_id": request.cycle_id, "plans": [p for p in service.status()["plans"]
                if p["definition"].get("metadata", {}).get("cycle_id") == cycle["cycle_id"]], "skipped": []}
        if plans.get("status") == "account_busy":
            raise HTTPException(409, "autonomous_planning_account_busy")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    service.configure(enabled=True, authorized_at=authorized_at)
    review = get_autonomous_model_review(service)
    review.configure(enabled=True, authorized_at=authorized_at)
    receipt = await review.request(cycle_id=request.cycle_id) if request.enabled else review.status()
    return {**plans, "model_review": receipt, "management": await service.manage()}


@router.post("/manage")
async def autonomy_manage():
    return await get_autonomous_campaign().manage()
