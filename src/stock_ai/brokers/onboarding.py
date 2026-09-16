from __future__ import annotations

from datetime import datetime, timezone
import webbrowser

from .contracts import BrokerId, HumanAuthorizationStatus
from .registry import BrokerCapabilityRegistry


class HumanAuthorizationWorkflow:
    """Host-owned workflow; models can request launch but cannot complete it."""

    def __init__(self, registry: BrokerCapabilityRegistry | None = None) -> None:
        self.registry = registry or BrokerCapabilityRegistry()

    async def status(self, broker_id: BrokerId) -> HumanAuthorizationStatus:
        return await self.registry.get(broker_id).authorization_status()

    async def launch_official_page(
        self,
        broker_id: BrokerId,
        *,
        user_requested: bool,
    ) -> dict:
        authorization = await self.status(broker_id)
        if not user_requested:
            return {
                "opened": False,
                "reason": "explicit_user_action_required",
                "authorization": authorization.model_dump(mode="json"),
            }
        opened = bool(webbrowser.open(authorization.official_url, new=2))
        return {
            "opened": opened,
            "opened_at": datetime.now(timezone.utc).isoformat() if opened else None,
            "authorization": authorization.model_dump(mode="json"),
            "checkpoint": "waiting_for_account_owner",
        }

    async def confirm_user_completed_actions(
        self,
        broker_id: BrokerId,
        *,
        confirmed_by_user: bool,
    ) -> HumanAuthorizationStatus:
        current = await self.status(broker_id)
        if not confirmed_by_user:
            return current
        return current.model_copy(
            update={
                "state": "ready_for_readonly_probe",
                "required_actions": [],
                "user_confirmed_at": datetime.now(timezone.utc),
            }
        )
