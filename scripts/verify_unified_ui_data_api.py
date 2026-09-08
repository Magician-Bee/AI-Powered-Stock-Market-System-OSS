#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import json

from fastapi.testclient import TestClient

import stock_ai.main as stock_main
from stock_ai.data_platform.ui_api import (
    audit_ui_data_access,
    unified_data_api_contract,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    static_root = root / "src" / "stock_ai" / "ui" / "static"
    audit = audit_ui_data_access(static_root)
    if audit["status"] != "passed":
        raise AssertionError(f"legacy UI market-data paths remain: {audit['violations']}")

    contract = unified_data_api_contract()
    route_paths = {
        str(route.path)
        for route in stock_main.app.routes
        if getattr(route, "path", None) is not None
    }
    missing = [
        item["path"]
        for item in contract["items"]
        if item["path"] not in route_paths
    ]
    if missing:
        raise AssertionError(f"declared unified routes are not registered: {missing}")

    stock_main.load_catalog = lambda: {
        "schema_version": "stock_ai.data_catalog.verification.v1",
        "fixture": True,
    }
    stock_main.list_supported_sources = lambda: {
        "count": 0,
        "items": [],
        "fixture": True,
    }
    stock_main.explain_linkage = lambda source, target: {
        "source": source,
        "target": target,
        "fixture": True,
    }
    client = TestClient(stock_main.app)
    representative_paths = [
        "/api/data/ui/v1/contract",
        "/api/data/ui/v1/catalog",
        "/api/data/ui/v1/sources",
        "/api/data/ui/v1/linkage?source=SOX&target=2330",
    ]
    responses = {
        path: client.get(path)
        for path in representative_paths
    }
    failures = {
        path: response.status_code
        for path, response in responses.items()
        if response.status_code != 200
    }
    if failures:
        raise AssertionError(f"unified façade smoke requests failed: {failures}")
    if (
        client.get("/api/data/ui/v1/catalog").json()
        != client.get("/api/catalog").json()
    ):
        raise AssertionError("compatibility and unified catalog routes diverged")

    print(
        json.dumps(
            {
                "schema_version": "stock_ai.unified_ui_data_api_verification.v1",
                "status": "passed",
                "contract": {
                    "prefix": contract["prefix"],
                    "route_count": contract["route_count"],
                    "consumer_count": contract["consumer_count"],
                    "ui_connector_access": contract["ui_connector_access"],
                    "registered_route_count": len(contract["items"]) - len(missing),
                },
                "static_audit": audit,
                "representative_requests": {
                    path: response.status_code
                    for path, response in responses.items()
                },
                "compatibility_parity": "passed",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
