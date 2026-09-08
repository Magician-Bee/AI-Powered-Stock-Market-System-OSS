from __future__ import annotations

"""Independent host checks for product-return metrics supplied by Qlib.

Only annualized return and maximum drawdown are compared: they share exact
product-path definitions with Qlib.  Qlib's information ratio and standard
deviation use different conventions, so presenting those as equal would be a
false differential check.
"""

import hashlib
import json
from math import isclose, isfinite
from typing import Any, Iterable


def qlib_product_differential_receipt(
    returns: Iterable[float],
    reference: dict[str, Any],
    *,
    periods_per_year: int,
    subject: str,
) -> dict[str, Any]:
    values = [float(value) for value in returns]
    if len(values) < 2 or any(not isfinite(value) or value <= -1.0 for value in values):
        raise ValueError("differential returns must be finite, > -1, and contain at least two observations")
    result = reference.get("result") if isinstance(reference.get("result"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    if (
        reference.get("project") != "qlib"
        or reference.get("action") != "reference_risk_metrics"
        or reference.get("executed_function") != "qlib.contrib.evaluate.risk_analysis"
        or result.get("mode") != "product"
        or result.get("periods_per_year") != periods_per_year
        or result.get("sample_count") != len(values)
        or result.get("return_path_sha256") != _sha256_json(values)
    ):
        raise ValueError("qlib_reference_receipt_incomplete_or_input_mismatch")

    host = _product_metrics(values, periods_per_year=periods_per_year)
    checks = []
    for host_key, qlib_key in (("annualized_return", "annualized_return"), ("max_drawdown", "max_drawdown")):
        observed = _finite(metrics.get(qlib_key))
        if observed is None:
            raise ValueError(f"qlib_reference_metric_missing:{qlib_key}")
        expected = host[host_key]
        checks.append({
            "metric": host_key,
            "host_value": expected,
            "qlib_value": observed,
            "absolute_difference": abs(expected - observed),
            "passed": isclose(expected, observed, rel_tol=1e-7, abs_tol=1e-9),
        })
    return {
        "schema_version": "open_stock_ai.qlib_differential_receipt.v1",
        "subject": subject,
        "reference_framework": "qlib",
        "reference_executed_function": reference["executed_function"],
        "return_path_sha256": _sha256_json(values),
        "sample_count": len(values),
        "periods_per_year": periods_per_year,
        "host_metrics": host,
        "qlib_metrics": {"annualized_return": _finite(metrics.get("annualized_return")), "max_drawdown": _finite(metrics.get("max_drawdown"))},
        "checks": checks,
        "passed": all(item["passed"] for item in checks),
        "not_compared": ["mean", "std", "information_ratio"],
        "reference_receipt": reference,
    }


def _product_metrics(values: list[float], *, periods_per_year: int) -> dict[str, float]:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    return {
        "annualized_return": equity ** (periods_per_year / len(values)) - 1.0,
        "max_drawdown": max_drawdown,
    }


def _sha256_json(value: list[float]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None
