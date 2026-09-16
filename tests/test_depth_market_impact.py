from __future__ import annotations

import pytest

from open_stock_ai.research.cost_model import (
    PointInTimeMarketImpactModel,
    hash_depth_snapshot,
)


def _snapshot() -> dict[str, object]:
    raw: dict[str, object] = {
        "snapshot_id": "L1L5-2330-1",
        "captured_at": "2026-08-26T01:00:00+00:00",
        "levels": [
            {"price": 100.0, "size": 4},
            {"price": 100.1, "size": 6},
        ],
    }
    return {**raw, "snapshot_sha256": hash_depth_snapshot(raw)}


def _sell_snapshot() -> dict[str, object]:
    raw: dict[str, object] = {
        "snapshot_id": "L1L5-2330-2",
        "captured_at": "2026-08-26T01:00:00+00:00",
        "levels": [
            {"price": 100.0, "size": 4},
            {"price": 99.9, "size": 6},
        ],
    }
    return {**raw, "snapshot_sha256": hash_depth_snapshot(raw)}


def _quote(*, side: str, snapshot: dict[str, object], quantity: int = 10):
    return PointInTimeMarketImpactModel().quote(
        side=side,
        reference_price=100.0,
        quantity=quantity,
        bid_ask_spread_bps=10.0,
        realized_volatility_bps=120.0,
        adv_volume_shares=50_000,
        bar_volume_shares=10_000,
        depth_snapshot=snapshot,
    )


def test_hash_verified_depth_snapshot_drives_pit_vwap_receipt() -> None:
    quote = _quote(side="buy", snapshot=_snapshot())

    assert quote.depth_simulation_eligible is True
    assert quote.fill_price() == pytest.approx(100.06)
    receipt = quote.receipt()
    assert receipt["simulation_mode"] == "order_book_depth"
    assert receipt["depth_data_status"] == "available"
    assert receipt["depth_levels"] == 2
    assert receipt["depth_vwap_price"] == pytest.approx(100.06)
    assert receipt["depth_impact_bps"] == pytest.approx(6.0)
    assert receipt["execution_evidence_eligible"] is False


def test_sell_depth_consumes_best_bid_first() -> None:
    quote = _quote(side="sell", snapshot=_sell_snapshot(), quantity=5)

    assert quote.depth_simulation_eligible is True
    assert quote.fill_price() == pytest.approx(99.98)
    assert quote.depth_impact_bps == pytest.approx(2.0)


def test_depth_hash_and_quantity_fail_closed() -> None:
    bad_hash = _snapshot()
    bad_hash["snapshot_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="depth_snapshot_hash_mismatch"):
        _quote(side="buy", snapshot=bad_hash)

    with pytest.raises(ValueError, match="depth_snapshot_insufficient_quantity"):
        _quote(side="buy", snapshot=_snapshot(), quantity=11)
