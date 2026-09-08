from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.request import Request, urlopen

from open_stock_ai.agent_runtime import default_external_transport_guard

from .realtime_data import normalize_symbol


GUIDANCE_SCHEMA_VERSION = "stock_ai.financial_guidance_tracking.v1"
TWSE_FORECAST_ACHIEVEMENT_URL = (
    "https://openapi.twse.com.tw/v1/opendata/t187ap15_L"
)


class GuidanceTrackingError(ValueError):
    pass


def _number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _range(value: Any) -> tuple[float | None, float | None]:
    numbers = [
        _number(item)
        for item in re.split(r"[~～－—]", str(value or "").strip())
    ]
    values = [item for item in numbers if item is not None]
    if not values:
        return None, None
    return (values[0], values[-1]) if len(values) > 1 else (values[0], values[0])


def compare_guidance(
    *,
    forecast_low: float | None,
    forecast_high: float | None,
    actual: float | None,
) -> dict[str, Any]:
    if forecast_low is None or forecast_high is None or actual is None:
        return {
            "comparable": False,
            "status": "not_comparable",
            "variance_to_midpoint": None,
            "variance_percent": None,
            "achievement_percent": None,
        }
    low, high = sorted((float(forecast_low), float(forecast_high)))
    midpoint = (low + high) / 2
    variance = float(actual) - midpoint
    return {
        "comparable": True,
        "status": "above_range" if actual > high else "below_range" if actual < low else "within_range",
        "variance_to_midpoint": variance,
        "variance_percent": variance / abs(midpoint) * 100 if midpoint else None,
        "achievement_percent": float(actual) / midpoint * 100 if midpoint else None,
    }


def normalize_guidance_record(record: dict[str, Any]) -> dict[str, Any]:
    guidance_type = str(record.get("guidance_type") or "").strip()
    if guidance_type not in {
        "company_forecast",
        "investor_conference_guidance",
        "management_outlook",
    }:
        raise GuidanceTrackingError("unsupported guidance_type")
    low = _number(record.get("forecast_low"))
    high = _number(record.get("forecast_high"))
    actual = _number(record.get("actual"))
    source_url = str(record.get("source_url") or "").strip()
    if not source_url.startswith("https://"):
        raise GuidanceTrackingError("guidance requires an HTTPS source_url")
    return {
        **record,
        "guidance_type": guidance_type,
        "forecast_low": low,
        "forecast_high": high,
        "actual": actual,
        "comparison": compare_guidance(
            forecast_low=low,
            forecast_high=high,
            actual=actual,
        ),
    }


def _fetch_json(url: str) -> list[dict[str, Any]]:
    request = Request(url, headers={"User-Agent": "StockAI/1.0 guidance"})
    def load() -> list[dict[str, Any]]:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed official URL
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise GuidanceTrackingError("TWSE OpenAPI response must be a list")
        return [dict(item) for item in payload if isinstance(item, dict)]

    payload = default_external_transport_guard().call_sync(
        "source:twse_openapi:financial_guidance",
        load,
    )
    if not isinstance(payload, list):
        raise GuidanceTrackingError("TWSE OpenAPI response must be a list")
    return [dict(item) for item in payload if isinstance(item, dict)]


def query_financial_guidance(
    symbol: str,
    *,
    fetch_json: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if not normalized:
        raise GuidanceTrackingError("symbol is required")
    code = normalized.split(".", 1)[0]
    try:
        rows = (fetch_json or _fetch_json)(TWSE_FORECAST_ACHIEVEMENT_URL)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        rows = []
        issues = [f"TWSE OpenAPI: {exc}"]
    else:
        issues = []
    items: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("公司代號") or "").strip() != code:
            continue
        year = int(str(row.get("年度") or "0") or 0)
        quarter = str(row.get("季別") or "").strip()
        low, high = _range(row.get("截至該季綜合損益預測數"))
        actual = _number(row.get("截至該季經會計師查核或核閱數"))
        items.append(
            normalize_guidance_record(
                {
                    "guidance_id": (
                        f"twse-forecast-{code}-{year}-{quarter}-"
                        f"{row.get('財測序號') or '0'}"
                    ),
                    "symbol": normalized,
                    "company_name": row.get("公司名稱"),
                    "guidance_type": "company_forecast",
                    "metric": "comprehensive_income",
                    "metric_label": "截至該季綜合損益",
                    "period": f"{year + 1911:04d}-Q{quarter}",
                    "coverage": row.get("涵蓋期間"),
                    "forecast_low": low,
                    "forecast_high": high,
                    "actual": actual,
                    "unit": "千元 TWD",
                    "audited_or_reviewed": True,
                    "source_id": "twse_openapi",
                    "source_url": TWSE_FORECAST_ACHIEVEMENT_URL,
                    "source_fields": {
                        "forecast": "截至該季綜合損益預測數",
                        "actual": "截至該季經會計師查核或核閱數",
                    },
                }
            )
        )
    return {
        "schema_version": GUIDANCE_SCHEMA_VERSION,
        "symbol": normalized,
        "count": len(items),
        "items": items,
        "issues": issues,
        "empty_reason": None if items else "no_voluntary_quantified_forecast_found",
        "supported_guidance_types": [
            "company_forecast",
            "investor_conference_guidance",
            "management_outlook",
        ],
        "comparison_policy": (
            "Only guidance with a sourced numeric range and a period-matched "
            "actual is compared. Qualitative outlook remains not_comparable."
        ),
    }
