from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any
from uuid import uuid4

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec
from open_stock_ai.agent_runtime.mutation_receipts import build_mutation_receipt

from .brokers import (
    BrokerCapabilityRegistry,
    UnifiedBrokerGateway,
    build_runtime_broker_gateway,
)
from .brokers.contracts import BrokerOrderIntent


_BROKER_ID = {
    "type": "string",
    "enum": ["taishin", "fubon", "sinopac", "yuanta", "masterlink"],
}


class BrokerAgentToolProvider:
    """Model-safe broker tools routed through the host gateway.

    No login, secret, certificate, direct-submit or bank-transfer capability is
    published here.
    """

    provider_id = "broker_gateway"

    def __init__(
        self,
        *,
        registry: BrokerCapabilityRegistry | None = None,
        gateway: UnifiedBrokerGateway | None = None,
    ) -> None:
        self.registry = registry or BrokerCapabilityRegistry()
        self.gateway = gateway or build_runtime_broker_gateway(self.registry)
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="broker.list_connections",
                    description="List five broker authorization states without secrets or account identifiers.",
                    category="broker_status",
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                    packages=("UnifiedBrokerGateway",),
                ),
                AgentToolSpec(
                    name="broker.health",
                    description="Read isolated broker-worker, SDK and authorization health.",
                    category="broker_status",
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                    packages=("BrokerConnectionSupervisor",),
                ),
                AgentToolSpec(
                    name="broker.market.snapshot",
                    description="Read one broker market snapshot through the unified canonical contract.",
                    category="broker_market",
                    requires_external_execution=True,
                    execution_backend="broker",
                    packages=("UnifiedBrokerGateway",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["broker_id", "instrument_id"],
                        "properties": {
                            "broker_id": _BROKER_ID,
                            "instrument_id": {"type": "string"},
                            "channel": {
                                "type": "string",
                                "enum": ["trade", "quote", "book", "odd_lot", "futures", "options"],
                            },
                            "market_session": {"type": "string"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="broker.market.subscribe",
                    description="Start a host-controlled broker market subscription and return first verified event.",
                    category="broker_market",
                    mutating=True,
                    requires_external_execution=True,
                    execution_backend="broker",
                    side_effects=("network_subscription",),
                    packages=("BrokerSubscriptionManager",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["broker_id", "instrument_id", "channel"],
                        "properties": {
                            "broker_id": _BROKER_ID,
                            "instrument_id": {"type": "string"},
                            "channel": {"type": "string"},
                            "market_session": {"type": "string"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="broker.market.history",
                    description="Read official broker history through the canonical market contract.",
                    category="broker_market",
                    requires_external_execution=True,
                    execution_backend="broker",
                    packages=("UnifiedBrokerGateway",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["broker_id", "instrument_id"],
                        "properties": {
                            "broker_id": _BROKER_ID,
                            "instrument_id": {"type": "string"},
                            "interval": {"type": "string", "enum": ["1m", "5m", "1d"]},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                        },
                    },
                ),
                AgentToolSpec(
                    name="broker.account.summary",
                    description="Read a user-authorized masked account snapshot from one broker.",
                    category="broker_account",
                    requires_external_execution=True,
                    execution_backend="broker",
                    packages=("AccountSettlementGateway",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["broker_id", "account_alias"],
                        "properties": {
                            "broker_id": _BROKER_ID,
                            "account_alias": {"type": "string"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="broker.account.positions",
                    description="Read positions from a user-authorized masked broker account snapshot.",
                    category="broker_account",
                    requires_external_execution=True,
                    execution_backend="broker",
                    packages=("AccountSettlementGateway",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["broker_id", "account_alias"],
                        "properties": {
                            "broker_id": _BROKER_ID,
                            "account_alias": {"type": "string"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="broker.account.open_orders",
                    description="Read open orders from a user-authorized masked broker account snapshot.",
                    category="broker_account",
                    requires_external_execution=True,
                    execution_backend="broker",
                    packages=("AccountSettlementGateway",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["broker_id", "account_alias"],
                        "properties": {
                            "broker_id": _BROKER_ID,
                            "account_alias": {"type": "string"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="broker.order.preview",
                    description="Ask the host and broker sandbox to validate an order without submitting it.",
                    category="broker_order_proposal",
                    requires_external_execution=True,
                    execution_backend="broker",
                    packages=("CentralBrokerRiskGate", "OrderManagementGateway"),
                    input_schema=_order_schema(),
                ),
                AgentToolSpec(
                    name="broker.order.propose",
                    description="Create a traceable order proposal. This never submits to a broker.",
                    category="broker_order_proposal",
                    packages=("CentralBrokerRiskGate", "OrderManagementGateway"),
                    input_schema=_order_schema(),
                    postconditions=({"predicate": "host_validated_mutation"},),
                    mutating=True,
                    idempotency="arguments",
                ),
                AgentToolSpec(
                    name="broker.order.status",
                    description="Read a proposal state from the current Agent run; no broker submission occurs.",
                    category="broker_order_proposal",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["intent_id"],
                        "properties": {"intent_id": {"type": "string"}},
                    },
                ),
                AgentToolSpec(
                    name="broker.order.cancel_proposal",
                    description="Cancel a local unsubmitted order proposal.",
                    category="broker_order_proposal",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["intent_id"],
                        "properties": {"intent_id": {"type": "string"}},
                    },
                    mutating=True,
                    idempotency="arguments",
                    postconditions=({"predicate": "host_validated_mutation"},),
                ),
            )
        }

    def manifest(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def describe(self) -> dict[str, Any]:
        return {
            "configured": True,
            "runtime_ready": True,
            "health": "authorization_required",
            "broker_count": 5,
            "live_trading": False,
            "direct_submit_exposed": False,
        }

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        _enforce_task_scope(name, context)
        if name == "broker.list_connections":
            return await self.gateway.list_connections()
        if name == "broker.health":
            return await self.gateway.health()
        if name in {"broker.market.snapshot", "broker.market.history"}:
            request = dict(arguments)
            broker_id = request.pop("broker_id")
            if name.endswith("history"):
                request["query_type"] = "history"
            result = await self.gateway.market_snapshot(broker_id, request)
            return result.model_dump(mode="json")
        if name == "broker.market.subscribe":
            request = dict(arguments)
            broker_id = request.pop("broker_id")
            stream = await self.gateway.subscribe_market_data(broker_id, request)
            first = await anext(stream)
            return {
                "schema_version": "stock_ai.broker_subscription_receipt.v1",
                "broker_id": broker_id,
                "first_event": first.model_dump(mode="json"),
            }
        if name.startswith("broker.account."):
            snapshot = await self.gateway.account_snapshot(
                arguments["broker_id"], arguments["account_alias"]
            )
            payload = snapshot.model_dump(mode="json")
            if name.endswith(".positions"):
                return {
                    "schema_version": "stock_ai.broker_positions.v1",
                    "broker_id": payload["broker_id"],
                    "account_id_masked": payload["account_id_masked"],
                    "as_of": payload["as_of"],
                    "positions": payload["positions"],
                }
            if name.endswith(".open_orders"):
                return {
                    "schema_version": "stock_ai.broker_open_orders.v1",
                    "broker_id": payload["broker_id"],
                    "account_id_masked": payload["account_id_masked"],
                    "as_of": payload["as_of"],
                    "open_orders": payload["open_orders"],
                }
            return payload
        if name == "broker.order.preview":
            intent = _order_intent(arguments, user_approved=False)
            return await self.gateway.preview_order(intent)
        if name == "broker.order.propose":
            intent = _order_intent(arguments, user_approved=False)
            proposals = context.state.setdefault("broker_order_proposals", {})
            before = {"broker_order_proposals": deepcopy(proposals)}
            proposals[intent.intent_id] = {
                **intent.model_dump(mode="json"),
                "state": "USER_APPROVAL_PENDING",
                "submitted": False,
            }
            payload = {
                "schema_version": "stock_ai.broker_order_proposal.v1",
                "intent_id": intent.intent_id,
                "state": "USER_APPROVAL_PENDING",
                "submitted": False,
                "mutation_performed": True,
            }
            receipt = build_mutation_receipt(
                name=name,
                arguments=arguments,
                result=payload,
                before=before,
                after={"broker_order_proposals": deepcopy(proposals)},
                accepted=True,
            )
            return {
                **payload,
                "receipt": receipt,
                "mutation_receipt": receipt,
            }
        if name == "broker.order.status":
            proposal = (context.state.get("broker_order_proposals") or {}).get(
                arguments["intent_id"]
            )
            if proposal is None:
                raise KeyError("broker order proposal not found in this Agent run")
            return {
                "schema_version": "stock_ai.broker_order_proposal.v1",
                **proposal,
            }
        if name == "broker.order.cancel_proposal":
            proposals = context.state.setdefault("broker_order_proposals", {})
            before = {"broker_order_proposals": deepcopy(proposals)}
            proposal = proposals.pop(arguments["intent_id"], None)
            payload = {
                "schema_version": "stock_ai.broker_order_proposal_cancel.v1",
                "intent_id": arguments["intent_id"],
                "cancelled": proposal is not None,
                "submitted": False,
                "mutation_performed": proposal is not None,
            }
            receipt = build_mutation_receipt(
                name=name,
                arguments=arguments,
                result=payload,
                before=before,
                after={"broker_order_proposals": deepcopy(proposals)},
                accepted=proposal is not None,
            )
            return {
                **payload,
                "receipt": receipt,
                "mutation_receipt": receipt,
            }
        raise ValueError(f"unknown broker Agent tool: {name}")


def _enforce_task_scope(name: str, context: AgentRunContext) -> None:
    task_kind = str(context.state.get("task_kind") or "general_answer")
    if name in {"broker.list_connections", "broker.health"}:
        return
    market_task_kinds = {"market_information", "market_radar", "market_decision"}
    if name.startswith("broker.market.") and task_kind not in market_task_kinds:
        raise PermissionError(
            f"{name} is unavailable for task kind {task_kind}; broker market data "
            "is isolated from general, project and UI tasks"
        )
    if name.startswith("broker.account."):
        if task_kind not in {"market_radar", "market_decision"}:
            raise PermissionError(
                f"{name} is unavailable for task kind {task_kind}; project tasks "
                "cannot access broker accounts"
            )
        if context.state.get("broker_account_access_approved") is not True:
            raise PermissionError(
                "broker account access requires an explicit host-issued approval "
                "for this Agent run"
            )
    if name.startswith("broker.order.") and task_kind != "market_decision":
        raise PermissionError(
            f"{name} is unavailable for task kind {task_kind}; order proposals "
            "are isolated to confirmed market-decision tasks"
        )


def _order_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "broker_id",
            "account_alias",
            "instrument_id",
            "side",
            "quantity",
            "price_type",
            "time_in_force",
            "session",
            "order_purpose",
            "risk_approval_id",
            "idempotency_key",
        ],
        "properties": {
            "broker_id": _BROKER_ID,
            "account_alias": {"type": "string"},
            "instrument_id": {"type": "string"},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "quantity": {"type": "number", "exclusiveMinimum": 0},
            "price_type": {
                "type": "string",
                "enum": ["market", "limit", "limit_up", "limit_down", "reference"],
            },
            "limit_price": {
                "anyOf": [
                    {"type": "number", "exclusiveMinimum": 0},
                    {"type": "null"},
                ]
            },
            "time_in_force": {"type": "string", "enum": ["ROD", "IOC", "FOK"]},
            "session": {"type": "string"},
            "order_purpose": {"type": "string"},
            "environment": {"type": "string", "enum": ["sandbox", "live"]},
            "risk_approval_id": {"type": "string"},
            "idempotency_key": {"type": "string", "minLength": 16, "maxLength": 200},
        },
    }


def _order_intent(arguments: dict[str, Any], *, user_approved: bool) -> BrokerOrderIntent:
    payload = {
        **arguments,
        "intent_id": f"BOI-{uuid4().hex}",
        "quantity": Decimal(str(arguments["quantity"])),
        "limit_price": (
            Decimal(str(arguments["limit_price"]))
            if arguments.get("limit_price") is not None
            else None
        ),
        "environment": arguments.get("environment") or "sandbox",
        "user_approved": user_approved,
    }
    return BrokerOrderIntent.model_validate(payload)
