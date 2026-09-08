from __future__ import annotations

from pathlib import Path
from typing import Any
import json

import httpx

from stock_ai.data_platform.source_registry import get_source_registry


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_READERS = (
    "src/stock_ai/taiwan_official.py",
    "src/stock_ai/phase1_data.py",
    "src/stock_ai/official_derivatives.py",
    "src/stock_ai/official_events.py",
    "src/stock_ai/twse_openapi.py",
    "src/stock_ai/realtime_quotes.py",
    "src/stock_ai/realtime_data.py",
    "src/stock_ai/services.py",
    "src/stock_ai/data_platform/security_loader.py",
    "src/stock_ai/data_platform/security_lifecycle.py",
    "src/stock_ai/data_platform/service.py",
)
FORBIDDEN_HOSTS = (
    "openapi.twse.com.tw",
    "www.twse.com.tw",
    "www.tpex.org.tw",
    "mops.twse.com.tw",
    "openapi.taifex.com.tw",
    "mis.twse.com.tw",
    "api.fugle.tw",
    "finance.yahoo.com",
    "news.google.com",
)


def _json_rows(client: httpx.Client, url: str) -> list[dict[str, Any]]:
    response = client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"Expected non-empty JSON rows from {url}")
    if not isinstance(payload[0], dict):
        raise RuntimeError(f"Expected object rows from {url}")
    return payload


def main() -> int:
    registry = get_source_registry()
    violations = {
        relative: host
        for relative in PRODUCTION_READERS
        for host in FORBIDDEN_HOSTS
        if host in (ROOT / relative).read_text(encoding="utf-8")
    }
    if violations:
        raise RuntimeError(f"Production readers own upstream hosts: {violations}")

    contracts = registry.as_dict()
    datasets = contracts["datasets"]
    if any(not item["fields"] for item in datasets):
        raise RuntimeError("Every source dataset must declare fields")
    if any(not item["failure_strategy"] for item in datasets):
        raise RuntimeError("Every source dataset must declare a failure strategy")
    if any(not item["license_status"] for item in datasets):
        raise RuntimeError("Every source dataset must inherit a license status")

    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "StockAI-SourceRegistry-Acceptance/1.0"},
    ) as client:
        twse_rows = _json_rows(client, registry.endpoint("twse_companies"))
        tpex_rows = _json_rows(client, registry.endpoint("tpex_companies"))
        taifex_rows = _json_rows(
            client,
            registry.endpoint("taifex_futures_institutional"),
        )
        mis_response = client.get(
            registry.endpoint("twse_mis_quote"),
            params={"ex_ch": "tse_2330.tw", "json": "1", "delay": "0"},
            headers={
                "Referer": registry.endpoint("twse_mis_referer", symbol="2330")
            },
        )
        mis_response.raise_for_status()
        mis_payload = mis_response.json()
        if str(mis_payload.get("rtcode")) != "0000":
            raise RuntimeError(f"TWSE MIS returned {mis_payload.get('rtcode')}")

    resolved_back = {
        dataset_id: (
            registry.identify_dataset_url(registry.endpoint(dataset_id)).dataset_id
        )
        for dataset_id in (
            "twse_companies",
            "tpex_companies",
            "taifex_futures_institutional",
            "twse_mis_quote",
        )
    }
    report = {
        "schema_version": "stock_ai.source_registry_acceptance.v1",
        "status": "passed",
        "registry_schema": contracts["schema_version"],
        "source_count": contracts["count"],
        "dataset_count": contracts["dataset_count"],
        "datasets_with_fields": len(datasets),
        "datasets_with_failure_strategy": len(datasets),
        "hardcoded_reader_host_violations": len(violations),
        "real_source_rows": {
            "twse_companies": len(twse_rows),
            "tpex_companies": len(tpex_rows),
            "taifex_futures_institutional": len(taifex_rows),
            "twse_mis_quotes": len(mis_payload.get("msgArray") or []),
        },
        "round_trip_dataset_resolution": resolved_back,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
