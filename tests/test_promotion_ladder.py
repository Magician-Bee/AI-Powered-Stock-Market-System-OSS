from datetime import datetime, timezone

import pytest

from open_stock_ai.governance.promotion_ladder import (
    CapabilityPromotionLadder,
    PromotionError,
    ordered_levels,
    required_evidence,
    verify_promotion_receipt,
)


_T0 = datetime(2026, 8, 26, 5, 15, tzinfo=timezone.utc)


def _evidence(level):
    return {key: f"verified-{key}" for key in required_evidence(level)}


def test_promotion_must_follow_order_and_requires_human_evidence():
    ladder = CapabilityPromotionLadder()
    assert ordered_levels() == ("research", "paper", "shadow", "broker_sandbox", "restricted_live", "production_live")
    with pytest.raises(PromotionError, match="one_level"):
        ladder.promote("shadow", evidence={}, approved_by="owner", now=_T0)
    with pytest.raises(PromotionError, match="human_approval"):
        ladder.promote("paper", evidence=_evidence("paper"), approved_by="agent", now=_T0)
    receipt = ladder.promote("paper", evidence=_evidence("paper"), approved_by="owner", now=_T0)
    assert ladder.current_level == "paper"
    assert receipt.verify() is True
    assert verify_promotion_receipt(receipt.as_dict()) is True


def test_downgrade_is_allowed_for_host_observed_failure_and_is_receipted():
    ladder = CapabilityPromotionLadder("broker_sandbox")
    receipt = ladder.downgrade("shadow", reason="broker_reconciliation_mismatch", now=_T0)
    assert ladder.current_level == "shadow"
    assert receipt.action == "downgrade"
    assert receipt.reason == "broker_reconciliation_mismatch"
    with pytest.raises(PromotionError, match="lower_level"):
        ladder.downgrade("broker_sandbox", reason="not a downgrade", now=_T0)


def test_tampered_promotion_receipt_is_rejected():
    ladder = CapabilityPromotionLadder()
    receipt = ladder.promote("paper", evidence=_evidence("paper"), approved_by="owner", now=_T0)
    payload = receipt.as_dict()
    payload["reason"] = "fake"
    assert verify_promotion_receipt(payload) is False
