from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from hypothesis import given, settings, strategies as st
import pytest

from stock_ai.brokers import BrokerOrderManagementGateway
from stock_ai.brokers.contracts import (
    BrokerOrderIntent,
    BrokerOrderReport,
    BrokerOrderState,
    BrokerRawEvent,
)


_QUANTITY = Decimal("100")
_PROPERTY_SETTINGS = settings(derandomize=True, max_examples=40, deadline=None)


@st.composite
def _legal_report_trace(draw: st.DrawFn) -> tuple[list[int], BrokerOrderState]:
    partial_fills = sorted(
        draw(
            st.lists(
                st.integers(min_value=1, max_value=99),
                unique=True,
                max_size=8,
            )
        )
    )
    terminal_states = [BrokerOrderState.FILLED, BrokerOrderState.CANCELLED]
    if not partial_fills:
        terminal_states.append(BrokerOrderState.REJECTED)
    return partial_fills, draw(st.sampled_from(terminal_states))


def _intent() -> BrokerOrderIntent:
    return BrokerOrderIntent(
        intent_id="BOI-property",
        broker_id="fubon",
        account_alias="paper-property",
        instrument_id="TWSE:2330",
        side="buy",
        quantity=_QUANTITY,
        price_type="limit",
        limit_price=Decimal("1000"),
        time_in_force="ROD",
        session="regular_lot",
        order_purpose="property_test",
        environment="sandbox",
        user_approved=True,
        risk_approval_id="RISK-" + "property",
        idempotency_key="broker-" + "oms-property-test-0001",
    )


def _raw_event(report_id: str) -> BrokerRawEvent:
    return BrokerRawEvent(
        broker_id="fubon",
        received_at=datetime.now(timezone.utc),
        payload={"report_id": report_id},
    )


def _report(
    raw: BrokerRawEvent,
    *,
    report_id: str,
    status: BrokerOrderState,
    filled: Decimal,
) -> BrokerOrderReport:
    now = datetime.now(timezone.utc)
    return BrokerOrderReport(
        report_id=report_id,
        intent_id="BOI-property",
        broker_id="fubon",
        account_alias="paper-property",
        broker_order_id="FUBON-PROPERTY-1",
        event_at=now,
        received_at=now,
        status=status,
        filled_quantity=filled,
        remaining_quantity=_QUANTITY - filled,
        rejected_reason="broker rejected order"
        if status == BrokerOrderState.REJECTED
        else None,
        raw_payload_hash=raw.payload_hash,
    )


def _submitting_oms() -> BrokerOrderManagementGateway:
    oms = BrokerOrderManagementGateway()
    entry = oms.register(_intent())
    BrokerOrderManagementGateway._transition(entry, BrokerOrderState.SUBMITTING)
    return oms


@_PROPERTY_SETTINGS
@given(trace=_legal_report_trace())
def test_oms_report_sequences_preserve_quantities_and_evidence(
    trace: tuple[list[int], BrokerOrderState],
) -> None:
    """Every legal broker-report trace must preserve OMS accounting invariants."""
    partial_fills, terminal_state = trace
    oms = _submitting_oms()
    expected_reports = 0

    raw_ack = _raw_event("BOR-property-ack")
    entry = oms.apply_order_report(
        _report(
            raw_ack,
            report_id="BOR-property-ack",
            status=BrokerOrderState.ACKNOWLEDGED,
            filled=Decimal("0"),
        ),
        raw_event=raw_ack,
    )
    expected_reports += 1
    previous_filled = Decimal("0")

    for index, fill in enumerate(partial_fills):
        raw = _raw_event(f"BOR-property-partial-{index}")
        entry = oms.apply_order_report(
            _report(
                raw,
                report_id=f"BOR-property-partial-{index}",
                status=BrokerOrderState.PARTIALLY_FILLED,
                filled=Decimal(fill),
            ),
            raw_event=raw,
        )
        expected_reports += 1
        assert entry.filled_quantity >= previous_filled
        previous_filled = entry.filled_quantity
        assert entry.filled_quantity + entry.remaining_quantity == _QUANTITY
        assert entry.broker_order_id == "FUBON-PROPERTY-1"

    terminal_fill = _QUANTITY if terminal_state == BrokerOrderState.FILLED else previous_filled
    raw_terminal = _raw_event("BOR-property-terminal")
    entry = oms.apply_order_report(
        _report(
            raw_terminal,
            report_id="BOR-property-terminal",
            status=terminal_state,
            filled=terminal_fill,
        ),
        raw_event=raw_terminal,
    )
    expected_reports += 1

    assert entry.state == terminal_state
    assert entry.filled_quantity >= previous_filled
    assert entry.filled_quantity + entry.remaining_quantity == _QUANTITY
    assert len(entry.report_ids) == expected_reports
    assert len(set(entry.report_ids)) == expected_reports
    assert len(oms.event_bus.order_reports(intent_id="BOI-property")) == expected_reports


@_PROPERTY_SETTINGS
@given(first_fill=st.integers(min_value=2, max_value=99))
def test_oms_rejects_any_backward_fill_report(first_fill: int) -> None:
    """No generated partial-fill history can be reduced by a later broker report."""
    oms = _submitting_oms()
    raw_ack = _raw_event("BOR-property-ack")
    oms.apply_order_report(
        _report(
            raw_ack,
            report_id="BOR-property-ack",
            status=BrokerOrderState.ACKNOWLEDGED,
            filled=Decimal("0"),
        ),
        raw_event=raw_ack,
    )
    raw_first = _raw_event("BOR-property-first-fill")
    oms.apply_order_report(
        _report(
            raw_first,
            report_id="BOR-property-first-fill",
            status=BrokerOrderState.PARTIALLY_FILLED,
            filled=Decimal(first_fill),
        ),
        raw_event=raw_first,
    )

    raw_backward = _raw_event("BOR-property-backward-fill")
    with pytest.raises(ValueError, match="filled quantity cannot move backwards"):
        oms.apply_order_report(
            _report(
                raw_backward,
                report_id="BOR-property-backward-fill",
                status=BrokerOrderState.PARTIALLY_FILLED,
                filled=Decimal(first_fill - 1),
            ),
            raw_event=raw_backward,
        )


@_PROPERTY_SETTINGS
@given(fill=st.integers(min_value=0, max_value=99))
def test_oms_duplicate_report_is_idempotent_for_every_valid_ack_or_partial_fill(
    fill: int,
) -> None:
    """Replaying a retained broker callback must never duplicate ledger evidence."""
    oms = _submitting_oms()
    status = (
        BrokerOrderState.ACKNOWLEDGED
        if fill == 0
        else BrokerOrderState.PARTIALLY_FILLED
    )
    raw = _raw_event("BOR-property-replay")
    report = _report(
        raw,
        report_id="BOR-property-replay",
        status=status,
        filled=Decimal(fill),
    )

    first = oms.apply_order_report(report, raw_event=raw)
    second = oms.apply_order_report(report, raw_event=raw)

    assert second is first
    assert first.report_ids == ["BOR-property-replay"]
    assert oms.event_bus.order_reports(intent_id="BOI-property") == [report]
