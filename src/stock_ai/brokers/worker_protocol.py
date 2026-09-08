from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import BrokerId
from .gateway import UnifiedBrokerGateway


class BrokerWorkerOperation(StrEnum):
    HEALTH = "health"
    VERSION = "version"
    LOGIN_READONLY = "login_readonly"
    MARKET = "market"
    ACCOUNT = "account"
    ORDER = "order"
    LOGOUT = "logout"


class BrokerWorkerRequest(BaseModel):
    """Host-only IPC envelope; it never contains a secret value."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_worker_request.v1"] = (
        "stock_ai.broker_worker_request.v1"
    )
    request_id: str = Field(default_factory=lambda: f"BWR-{uuid4().hex}")
    broker_id: BrokerId
    operation: BrokerWorkerOperation
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    secret_reference: str | None = None
    human_authorization_receipt_id: str | None = None
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("secret_reference")
    @classmethod
    def require_secure_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = (
            "keychain://",
            "credential-manager://",
            "keyring://",
            "vault://",
        )
        if not value.startswith(allowed):
            raise ValueError("broker Worker accepts secure-store references only")
        return value

    @field_validator("arguments")
    @classmethod
    def reject_secret_values_in_arguments(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        forbidden_fragments = {
            "apikey",
            "certificatepassword",
            "credential",
            "password",
            "privatekey",
            "secret",
            "token",
            "otp",
        }

        def inspect(item: Any) -> None:
            if isinstance(item, dict):
                for key, nested in item.items():
                    normalized = "".join(
                        character
                        for character in str(key).casefold()
                        if character.isalnum()
                    )
                    if any(fragment in normalized for fragment in forbidden_fragments):
                        raise ValueError(
                            "secret values must not be placed in Broker Worker arguments"
                        )
                    inspect(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    inspect(nested)

        inspect(value)
        return value

    @model_validator(mode="after")
    def require_host_receipt_for_sensitive_operations(self) -> "BrokerWorkerRequest":
        protected_operation = self.operation in {
            BrokerWorkerOperation.LOGIN_READONLY,
            BrokerWorkerOperation.ACCOUNT,
            BrokerWorkerOperation.ORDER,
        } or (
            self.operation == BrokerWorkerOperation.MARKET
            and self.action == "readonly_probe"
        )
        if protected_operation and not self.human_authorization_receipt_id:
            raise ValueError(
                "protected Broker Worker operations require a Host authorization receipt"
            )
        if (
            self.operation == BrokerWorkerOperation.MARKET
            and self.action == "readonly_probe"
            and not self.secret_reference
        ):
            raise ValueError("read-only quote probe requires a Host secure-store reference")
        return self


class BrokerWorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.broker_worker_response.v1"] = (
        "stock_ai.broker_worker_response.v1"
    )
    request_id: str
    broker_id: BrokerId
    operation: BrokerWorkerOperation
    completed: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BrokerWorkerInterface:
    """Five-class Host interface intended to run inside each isolated SDK process."""

    def __init__(self, gateway: UnifiedBrokerGateway | None = None) -> None:
        self.gateway = gateway or UnifiedBrokerGateway()

    async def execute(self, request: BrokerWorkerRequest) -> BrokerWorkerResponse:
        try:
            result = await self._dispatch(request)
            return BrokerWorkerResponse(
                request_id=request.request_id,
                broker_id=request.broker_id,
                operation=request.operation,
                completed=True,
                result=result,
            )
        except Exception as exc:
            return BrokerWorkerResponse(
                request_id=request.request_id,
                broker_id=request.broker_id,
                operation=request.operation,
                completed=False,
                error_type=type(exc).__name__,
                error_message=(
                    f"{request.operation.value} operation failed inside the "
                    "Host-isolated Broker Worker"
                ),
            )

    async def _dispatch(self, request: BrokerWorkerRequest) -> dict[str, Any]:
        if request.operation == BrokerWorkerOperation.HEALTH:
            workers = (await self.gateway.health())["workers"]
            return next(item for item in workers if item["broker_id"] == request.broker_id)
        if request.operation == BrokerWorkerOperation.VERSION:
            profile = await self.gateway.registry.get(
                request.broker_id
            ).probe_capabilities()
            return profile.model_dump(mode="json")
        if request.operation == BrokerWorkerOperation.LOGIN_READONLY:
            result = await self.gateway.login_readonly(
                request.broker_id,
                secret_reference=request.secret_reference,
            )
            return dict(result)
        if request.operation == BrokerWorkerOperation.MARKET:
            if request.action == "readonly_probe":
                return await self.gateway.probe_readonly_quote(
                    request.broker_id,
                    secret_reference=request.secret_reference,
                    request=request.arguments,
                )
            if request.action != "snapshot":
                raise ValueError("market Worker accepts snapshot or readonly_probe action only")
            result = await self.gateway.market_snapshot(
                request.broker_id,
                request.arguments,
            )
            return result.model_dump(mode="json")
        if request.operation == BrokerWorkerOperation.ACCOUNT:
            if request.action != "snapshot":
                raise ValueError("account Worker currently accepts snapshot action only")
            result = await self.gateway.account_snapshot(
                request.broker_id,
                str(request.arguments["account_alias"]),
            )
            return result.model_dump(mode="json")
        if request.operation == BrokerWorkerOperation.ORDER:
            if request.action == "modify":
                result = await self.gateway.modify_order(
                    request.broker_id,
                    request.arguments,
                )
            elif request.action == "cancel":
                result = await self.gateway.cancel_order(
                    request.broker_id,
                    request.arguments,
                )
            elif request.action == "reconcile":
                result = await self.gateway.reconcile_orders(request.broker_id)
                return dict(result)
            else:
                raise ValueError(
                    "direct order placement is not exposed by the generic Worker interface"
                )
            return result.model_dump(mode="json")
        if request.operation == BrokerWorkerOperation.LOGOUT:
            await self.gateway.logout(request.broker_id)
            return {"logged_out": True}
        raise ValueError(f"unsupported broker Worker operation: {request.operation}")
