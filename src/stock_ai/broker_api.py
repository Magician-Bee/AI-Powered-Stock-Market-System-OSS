from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from .brokers import (
    BrokerCapabilityRegistry,
    BrokerRuntimeStatusRegistry,
    HumanAuthorizationWorkflow,
    build_runtime_broker_gateway,
)
from .brokers.contracts import BrokerId
from .brokers.taishin import build_taishin_readonly_onboarding_plan


router = APIRouter(prefix="/api/brokers", tags=["broker-gateway"])
_registry = BrokerCapabilityRegistry()
_gateway = build_runtime_broker_gateway(_registry)
_authorization = HumanAuthorizationWorkflow(_registry)
_runtime_status = BrokerRuntimeStatusRegistry()


def broker_runtime_snapshot() -> dict:
    """Return the Host-owned broker metrics without going through HTTP."""

    return _runtime_status.snapshot().model_dump(mode="json")


@router.get("/connections")
async def broker_connections() -> dict:
    return await _gateway.list_connections()


@router.get("/capabilities")
async def broker_capabilities() -> dict:
    return await _gateway.capability_profiles()


@router.get("/health")
async def broker_health() -> dict:
    return await _gateway.health()


@router.get("/runtime")
async def broker_runtime() -> dict:
    return broker_runtime_snapshot()


@router.get("/{broker_id}/authorization")
async def broker_authorization(broker_id: BrokerId) -> dict:
    return (await _authorization.status(broker_id)).model_dump(mode="json")


@router.get("/taishin/readonly-onboarding")
async def taishin_readonly_onboarding() -> dict:
    """Describe the user-owned, read-only Taishin setup without starting it."""

    return build_taishin_readonly_onboarding_plan().model_dump(mode="json")


@router.post("/{broker_id}/authorization/open")
async def open_broker_authorization(
    broker_id: BrokerId,
    payload: dict = Body(default_factory=dict),
) -> dict:
    if payload.get("user_requested") is not True:
        raise HTTPException(
            status_code=409,
            detail="The account owner must explicitly request opening the official page.",
        )
    return await _authorization.launch_official_page(
        broker_id,
        user_requested=True,
    )
