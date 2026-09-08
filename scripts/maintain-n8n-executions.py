#!/usr/bin/env python3
"""Inspect or prune archived Stock AI execution history through n8n's API.

The default is read-only.  ``--apply`` is intentionally required before any
execution is deleted.  The script does not print workflow data or secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_ai.n8n_runtime_hygiene import N8nExecutionHygiene  # noqa: E402


def _gateway_environment() -> dict[str, str]:
    path = ROOT / ".runtime" / "n8n" / "data" / "stock-ai-gateway.env"
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name in {"N8N_AUTOMATION_GATEWAY_URL", "N8N_AUTOMATION_GATEWAY_TOKEN"}:
            values[name] = value
    if not values.get("N8N_AUTOMATION_GATEWAY_URL") or not values.get("N8N_AUTOMATION_GATEWAY_TOKEN"):
        raise RuntimeError("project-local n8n gateway credentials are not ready")
    return values


def _api(gateway: Mapping[str, str], method: str, path: str) -> dict[str, Any]:
    request = Request(
        f"{gateway['N8N_AUTOMATION_GATEWAY_URL'].rstrip('/')}/api/v1{path}",
        method=method,
        headers={"X-N8N-API-KEY": gateway["N8N_AUTOMATION_GATEWAY_TOKEN"], "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - loopback config is owner-created
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"n8n API {method} {path} returned HTTP {exc.code}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("n8n API returned a non-object response")
    return value


def _paged(gateway: Mapping[str, str], path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = ""
    while True:
        response = _api(gateway, "GET", f"{path}{cursor}")
        page = response.get("data") or []
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise RuntimeError("n8n API returned an invalid list response")
        items.extend(page)
        next_cursor = response.get("nextCursor")
        if not next_cursor:
            return items
        cursor = f"&cursor={next_cursor}"


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="delete only archived Stock AI executions")
    group.add_argument(
        "--compact-offline",
        action="store_true",
        help="reclaim SQLite free pages only after the project-local n8n listener is stopped",
    )
    args = parser.parse_args()
    if args.compact_offline:
        hygiene = N8nExecutionHygiene(
            ROOT / ".runtime" / "n8n" / "data" / ".n8n" / "database.sqlite",
            list_workflows=lambda: (),
            list_executions=lambda _workflow_id: (),
            delete_execution=lambda _execution_id: {},
        )
        print(json.dumps(hygiene.compact_database_offline(), ensure_ascii=False, sort_keys=True))
        return 0
    gateway = _gateway_environment()
    hygiene = N8nExecutionHygiene(
        ROOT / ".runtime" / "n8n" / "data" / ".n8n" / "database.sqlite",
        list_workflows=lambda: _paged(gateway, "/workflows?limit=250&excludePinnedData=true"),
        list_executions=lambda workflow_id: _paged(
            gateway, f"/executions?workflowId={workflow_id}&limit=100&includeData=false"
        ),
        delete_execution=lambda execution_id: _api(gateway, "DELETE", f"/executions/{execution_id}"),
    )
    result = hygiene.prune_archived_stock_ai_executions() if args.apply else hygiene.inspect()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
