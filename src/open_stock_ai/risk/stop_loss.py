from __future__ import annotations


def fixed_stop_loss(entry_price: float, loss_pct: float) -> float:
    return round(entry_price * (1 - loss_pct / 100), 4)
