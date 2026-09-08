from __future__ import annotations

import hashlib
import json

import pytest

from open_stock_ai.research.differential_metrics import qlib_product_differential_receipt


def _reference(returns: list[float]) -> dict:
    equity = peak = 1.0
    drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1.0)
    return {
        "project": "qlib",
        "action": "reference_risk_metrics",
        "executed_function": "qlib.contrib.evaluate.risk_analysis",
        "result": {
            "mode": "product", "periods_per_year": 252, "sample_count": len(returns),
            "return_path_sha256": hashlib.sha256(json.dumps(returns, sort_keys=True).encode()).hexdigest(),
            "metrics": {"annualized_return": equity ** (252 / len(returns)) - 1.0, "max_drawdown": drawdown},
        },
    }


def test_qlib_product_differential_receipt_compares_shared_metrics_only():
    returns = [0.01, -0.02, 0.03]
    receipt = qlib_product_differential_receipt(returns, _reference(returns), periods_per_year=252, subject="fixture")

    assert receipt["passed"] is True
    assert receipt["not_compared"] == ["mean", "std", "information_ratio"]


def test_qlib_product_differential_receipt_fails_closed_for_path_or_metric_mismatch():
    returns = [0.01, -0.02, 0.03]
    altered = _reference(returns)
    altered["result"]["metrics"]["max_drawdown"] = -0.5

    receipt = qlib_product_differential_receipt(returns, altered, periods_per_year=252, subject="fixture")
    assert receipt["passed"] is False
    assert next(item for item in receipt["checks"] if item["metric"] == "max_drawdown")["passed"] is False

    altered = _reference(returns)
    altered["result"]["return_path_sha256"] = "wrong"
    with pytest.raises(ValueError, match="qlib_reference_receipt_incomplete_or_input_mismatch"):
        qlib_product_differential_receipt(returns, altered, periods_per_year=252, subject="fixture")
