from datetime import datetime, timezone
import sqlite3

import pytest

from open_stock_ai.governance.promotion_ladder import (
    CapabilityPromotionLadder,
    SQLitePromotionReceiptStore,
    required_evidence,
)


_NOW = datetime(2026, 8, 26, 2, tzinfo=timezone.utc)


def _evidence(level: str) -> dict[str, str]:
    return {key: f"verified-{key}" for key in required_evidence(level)}


def test_capability_promotion_receipts_restore_level_across_restart(tmp_path):
    store = SQLitePromotionReceiptStore(tmp_path / "governance.sqlite")
    ladder = CapabilityPromotionLadder(store=store)

    paper = ladder.promote(
        "paper", evidence=_evidence("paper"), approved_by="owner", now=_NOW
    )
    shadow = CapabilityPromotionLadder(store=store).promote(
        "shadow", evidence=_evidence("shadow"), approved_by="owner", now=_NOW
    )
    downgraded = CapabilityPromotionLadder(store=store).downgrade(
        "paper", reason="shadow_evidence_expired", now=_NOW
    )
    restarted = CapabilityPromotionLadder(store=store)

    assert paper.verify() and shadow.verify() and downgraded.verify()
    assert restarted.current_level == "paper"
    assert [item.action for item in restarted.receipts] == [
        "promote",
        "promote",
        "downgrade",
    ]
    assert [item.receipt_sha256 for item in restarted.receipts] == [
        item.receipt_sha256 for item in store.receipts()
    ]


def test_promotion_store_rejects_level_conflict_and_is_immutable(tmp_path):
    store = SQLitePromotionReceiptStore(tmp_path / "governance.sqlite")
    CapabilityPromotionLadder(store=store).promote(
        "paper", evidence=_evidence("paper"), approved_by="owner", now=_NOW
    )

    with pytest.raises(ValueError, match="level_conflict"):
        CapabilityPromotionLadder("shadow", store=store)
    with store._connect() as connection, pytest.raises(
        sqlite3.IntegrityError, match="immutable"
    ):
        connection.execute("delete from governed_capability_promotion_receipts")
