from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .contracts import BrokerId
from .registry import BrokerCapabilityRegistry


class BrokerWorkerHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_id: BrokerId
    state: Literal[
        "not_installed",
        "requires_user_action",
        "starting",
        "healthy",
        "degraded",
        "crashed",
        "stopped",
    ]
    process_isolated: bool = False
    process_running: bool = False
    sdk_installed: bool = False
    sdk_version: str = "unverified"
    last_checked_at: datetime
    message: str


class BrokerConnectionSupervisor:
    """Reports worker state without loading vendor SDKs into the web process."""

    def __init__(self, registry: BrokerCapabilityRegistry | None = None) -> None:
        self.registry = registry or BrokerCapabilityRegistry()

    async def health(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        result: list[dict] = []
        for broker_id in self.registry.list_broker_ids():
            authorization = await self.registry.get(broker_id).authorization_status()
            profile = await self.registry.get(broker_id).probe_capabilities()
            result.append(
                BrokerWorkerHealth(
                    broker_id=broker_id,
                    state="requires_user_action",
                    process_isolated=False,
                    process_running=False,
                    sdk_installed=False,
                    sdk_version=profile.sdk_version,
                    last_checked_at=now,
                    message=(
                        "Official SDK worker is not installed or verified; "
                        f"complete {len(authorization.required_actions)} owner actions first."
                    ),
                ).model_dump(mode="json")
            )
        return result
