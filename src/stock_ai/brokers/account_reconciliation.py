from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import (
    BrokerAccountSnapshot,
    BrokerFill,
    BrokerId,
    BrokerOpenOrder,
    BrokerPosition,
    BrokerSettlementAmount,
)


class HostAccountLedger(BaseModel):
    """Host-owned ledger for one broker account and one currency."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.host_account_ledger.v1"] = (
        "stock_ai.host_account_ledger.v1"
    )
    broker_id: BrokerId
    account_id_masked: str
    account_type: str
    currency: str
    available_cash: Decimal | None = None
    settlement_due: Decimal | None = None
    positions: list[BrokerPosition] = Field(default_factory=list)
    open_orders: list[BrokerOpenOrder] = Field(default_factory=list)
    fills: list[BrokerFill] = Field(default_factory=list)
    settlements: list[BrokerSettlementAmount] = Field(default_factory=list)
    as_of: datetime

    @field_validator("account_id_masked")
    @classmethod
    def require_masked_account_id(cls, value: str) -> str:
        if not any(token in value for token in ("*", "•", "x", "X")):
            raise ValueError("Host account identifiers must be masked")
        return value


class AccountReconciliationMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    key: str | None = None
    broker_value: Any
    host_value: Any
    severity: Literal["warning", "critical"]


class AccountReconciliationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["stock_ai.account_reconciliation_result.v1"] = (
        "stock_ai.account_reconciliation_result.v1"
    )
    broker_id: BrokerId
    account_id_masked: str
    currency: str
    broker_as_of: datetime
    host_as_of: datetime
    status: Literal["matched", "mismatch", "incomplete"]
    mismatches: list[AccountReconciliationMismatch] = Field(default_factory=list)
    unverified_fields: list[str] = Field(default_factory=list)
    trading_allowed: bool = False


class BrokerAccountReconciliationEngine:
    """Compare broker truth with the Host ledger without filling missing data."""

    def reconcile(
        self,
        broker: BrokerAccountSnapshot,
        host: HostAccountLedger,
    ) -> AccountReconciliationResult:
        if broker.broker_id != host.broker_id:
            raise ValueError("accounts from different brokers cannot be reconciled")
        if broker.account_id_masked != host.account_id_masked:
            raise ValueError("accounts with different masked IDs cannot be reconciled")
        if broker.account_type != host.account_type:
            raise ValueError("different account types must be reconciled independently")
        if broker.currency != host.currency:
            raise ValueError("different currencies must be reconciled independently")

        mismatches: list[AccountReconciliationMismatch] = []
        unverified: list[str] = []
        self._compare_optional_decimal(
            "available_cash",
            broker.available_cash,
            host.available_cash,
            mismatches,
            unverified,
        )
        self._compare_optional_decimal(
            "settlement_due",
            broker.settlement_due,
            host.settlement_due,
            mismatches,
            unverified,
        )
        self._compare_positions(broker.positions, host.positions, mismatches)
        self._compare_open_orders(
            "open_orders",
            broker.open_orders,
            host.open_orders,
            mismatches,
        )
        self._compare_fills(
            "fills",
            broker.fills,
            host.fills,
            mismatches,
        )
        self._compare_settlements(
            broker.settlements,
            host.settlements,
            mismatches,
        )
        status: Literal["matched", "mismatch", "incomplete"]
        if mismatches:
            status = "mismatch"
        elif unverified:
            status = "incomplete"
        else:
            status = "matched"
        return AccountReconciliationResult(
            broker_id=broker.broker_id,
            account_id_masked=broker.account_id_masked,
            currency=broker.currency,
            broker_as_of=broker.as_of,
            host_as_of=host.as_of,
            status=status,
            mismatches=mismatches,
            unverified_fields=sorted(set(unverified)),
            trading_allowed=status == "matched",
        )

    @staticmethod
    def _compare_optional_decimal(
        field: str,
        broker_value: Decimal | None,
        host_value: Decimal | None,
        mismatches: list[AccountReconciliationMismatch],
        unverified: list[str],
    ) -> None:
        if broker_value is None or host_value is None:
            unverified.append(field)
            return
        if broker_value != host_value:
            mismatches.append(
                AccountReconciliationMismatch(
                    field=field,
                    broker_value=str(broker_value),
                    host_value=str(host_value),
                    severity="critical",
                )
            )

    @staticmethod
    def _compare_positions(
        broker_positions: list[BrokerPosition],
        host_positions: list[BrokerPosition],
        mismatches: list[AccountReconciliationMismatch],
    ) -> None:
        broker_map = {
            (item.instrument_id, item.position_type): item
            for item in broker_positions
        }
        host_map = {
            (item.instrument_id, item.position_type): item
            for item in host_positions
        }
        for key in sorted(set(broker_map) | set(host_map)):
            broker_item = broker_map.get(key)
            host_item = host_map.get(key)
            broker_value = broker_item.quantity if broker_item else None
            host_value = host_item.quantity if host_item else None
            if broker_value != host_value:
                mismatches.append(
                    AccountReconciliationMismatch(
                        field="position_quantity",
                        key=f"{key[0]}:{key[1]}",
                        broker_value=(
                            str(broker_value) if broker_value is not None else None
                        ),
                        host_value=(
                            str(host_value) if host_value is not None else None
                        ),
                        severity="critical",
                    )
                )

    @staticmethod
    def _compare_open_orders(
        field: str,
        broker_items: list[BrokerOpenOrder],
        host_items: list[BrokerOpenOrder],
        mismatches: list[AccountReconciliationMismatch],
    ) -> None:
        def values(items: list[BrokerOpenOrder]) -> dict[str, tuple[str, ...]]:
            return {
                item.broker_order_id_masked: (
                    item.instrument_id,
                    item.side,
                    str(item.quantity),
                    str(item.remaining_quantity),
                    item.status,
                    item.submitted_at.isoformat(),
                )
                for item in items
            }

        broker_values = values(broker_items)
        host_values = values(host_items)
        if broker_values == host_values:
            return
        mismatches.append(
            AccountReconciliationMismatch(
                field=field,
                broker_value=broker_values,
                host_value=host_values,
                severity="critical",
            )
        )

    @staticmethod
    def _compare_fills(
        field: str,
        broker_items: list[BrokerFill],
        host_items: list[BrokerFill],
        mismatches: list[AccountReconciliationMismatch],
    ) -> None:
        def values(items: list[BrokerFill]) -> dict[str, tuple[str | None, ...]]:
            return {
                item.broker_fill_id_masked: (
                    item.broker_order_id_masked,
                    item.instrument_id,
                    item.side,
                    str(item.quantity),
                    str(item.price),
                    str(item.fee) if item.fee is not None else None,
                    str(item.tax) if item.tax is not None else None,
                    item.filled_at.isoformat(),
                )
                for item in items
            }

        broker_values = values(broker_items)
        host_values = values(host_items)
        if broker_values == host_values:
            return
        mismatches.append(
            AccountReconciliationMismatch(
                field=field,
                broker_value=broker_values,
                host_value=host_values,
                severity="critical",
            )
        )

    @staticmethod
    def _compare_settlements(
        broker_items: list[BrokerSettlementAmount],
        host_items: list[BrokerSettlementAmount],
        mismatches: list[AccountReconciliationMismatch],
    ) -> None:
        def values(
            items: list[BrokerSettlementAmount],
        ) -> dict[tuple[str, str], tuple[Decimal | None, Decimal | None]]:
            return {
                (item.settlement_date, item.currency): (
                    item.receivable,
                    item.payable,
                )
                for item in items
            }

        broker_values = values(broker_items)
        host_values = values(host_items)
        if broker_values == host_values:
            return
        mismatches.append(
            AccountReconciliationMismatch(
                field="settlements",
                broker_value={
                    f"{key[0]}:{key[1]}": [
                        str(value[0]) if value[0] is not None else None,
                        str(value[1]) if value[1] is not None else None,
                    ]
                    for key, value in broker_values.items()
                },
                host_value={
                    f"{key[0]}:{key[1]}": [
                        str(value[0]) if value[0] is not None else None,
                        str(value[1]) if value[1] is not None else None,
                    ]
                    for key, value in host_values.items()
                },
                severity="critical",
            )
        )
