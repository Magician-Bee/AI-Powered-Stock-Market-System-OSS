from __future__ import annotations

"""Deterministic block-bootstrap tail-risk stability receipts."""

import hashlib
import json
from math import ceil, isfinite
from random import Random
from statistics import mean
from typing import Iterable


def simulate_tail_risk(
    period_returns: Iterable[float],
    *,
    scenario_count: int = 2_000,
    block_size: int = 5,
    confidence_level: float = 0.95,
    stability_batches: int = 5,
) -> dict[str, object]:
    """Resample contiguous return blocks and publish reproducible tail metrics."""

    values = [float(value) for value in period_returns]
    if scenario_count < 1_000 or block_size < 1 or stability_batches < 2:
        raise ValueError("scenario_count, block_size and stability_batches are below the minimum")
    if not 0.5 < confidence_level < 1.0:
        raise ValueError("confidence_level must be in (0.5, 1)")
    if any(not isfinite(value) for value in values):
        raise ValueError("period_returns must be finite")
    if len(values) < 20:
        return _unavailable(values, scenario_count, block_size, confidence_level, ["monte_carlo_insufficient_return_observations"])

    seed_document = {
        "period_returns": values,
        "scenario_count": scenario_count,
        "block_size": block_size,
        "confidence_level": confidence_level,
        "stability_batches": stability_batches,
    }
    seed = int(hashlib.sha256(json.dumps(seed_document, separators=(",", ":")).encode("utf-8")).hexdigest()[:16], 16)
    random = Random(seed)
    scenario_terminal_returns: list[float] = []
    max_drawdowns: list[float] = []
    for _ in range(scenario_count):
        path = _bootstrap_path(values, block_size=block_size, random=random)
        scenario_terminal_returns.append(_compound(path) - 1.0)
        max_drawdowns.append(_max_drawdown(path))
    terminal_returns = sorted(scenario_terminal_returns)
    max_drawdowns.sort()
    var_threshold = _quantile(terminal_returns, 1.0 - confidence_level)
    tail_returns = [value for value in terminal_returns if value <= var_threshold]
    cvar_return = mean(tail_returns)
    batch_size = scenario_count // stability_batches
    batch_cvars = [
        -mean(batch[: max(1, ceil(len(batch) * (1.0 - confidence_level)))]) * 100.0
        for batch in (
            scenario_terminal_returns[index : index + batch_size]
            for index in range(0, batch_size * stability_batches, batch_size)
        )
    ]
    cvar_loss_pct = max(0.0, -cvar_return * 100.0)
    stability_spread_pct = max(batch_cvars) - min(batch_cvars)
    stability_limit_pct = max(0.25, cvar_loss_pct * 0.35)
    stability_passed = stability_spread_pct <= stability_limit_pct
    return {
        "schema_version": "open_stock_ai.monte_carlo_tail_risk.v1",
        "method": "deterministic_circular_block_bootstrap_return_paths",
        "passed": stability_passed,
        "sample_size": len(values),
        "scenario_count": scenario_count,
        "block_size": block_size,
        "confidence_level": confidence_level,
        "seed": seed,
        "input_sha256": hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "terminal_return_quantiles_pct": {
            "p01": _round(_quantile(terminal_returns, 0.01) * 100.0),
            "p05": _round(_quantile(terminal_returns, 0.05) * 100.0),
            "p50": _round(_quantile(terminal_returns, 0.50) * 100.0),
        },
        "value_at_risk_loss_pct": _round(max(0.0, -var_threshold * 100.0)),
        "conditional_value_at_risk_loss_pct": _round(cvar_loss_pct),
        "worst_terminal_loss_pct": _round(max(0.0, -terminal_returns[0] * 100.0)),
        "max_drawdown_quantile_pct": _round(_quantile(max_drawdowns, confidence_level) * 100.0),
        "tail_risk_stability": {
            "batch_count": stability_batches,
            "batch_cvar_loss_pct": [_round(value) for value in batch_cvars],
            "spread_pct": _round(stability_spread_pct),
            "limit_pct": _round(stability_limit_pct),
            "passed": stability_passed,
        },
        "blockers": [] if stability_passed else ["monte_carlo_tail_risk_not_stable_across_batches"],
    }


def _bootstrap_path(values: list[float], *, block_size: int, random: Random) -> list[float]:
    path: list[float] = []
    while len(path) < len(values):
        start = random.randrange(len(values))
        path.extend(values[(start + offset) % len(values)] for offset in range(block_size))
    return path[: len(values)]


def _max_drawdown(values: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    return abs(drawdown)


def _quantile(values: list[float], probability: float) -> float:
    index = max(0, min(len(values) - 1, round((len(values) - 1) * probability)))
    return values[index]


def _compound(values: list[float]) -> float:
    equity = 1.0
    for value in values:
        equity *= 1.0 + value
    return equity


def _unavailable(
    values: list[float], scenario_count: int, block_size: int, confidence_level: float, blockers: list[str]
) -> dict[str, object]:
    return {
        "schema_version": "open_stock_ai.monte_carlo_tail_risk.v1",
        "method": "deterministic_circular_block_bootstrap_return_paths",
        "passed": False,
        "sample_size": len(values),
        "scenario_count": scenario_count,
        "block_size": block_size,
        "confidence_level": confidence_level,
        "blockers": blockers,
    }


def _round(value: float) -> float:
    return round(value, 6)
