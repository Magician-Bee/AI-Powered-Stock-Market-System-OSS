from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
import json

from open_stock_ai.agent_runtime import default_external_transport_guard

from .config import get_settings
from .data_platform.source_registry import get_source_registry, source_endpoint

BASE = str(get_source_registry().source("twse_openapi").base_url)
SWAGGER = source_endpoint("twse_swagger")


@lru_cache(maxsize=1)
def load_twse_openapi_inventory() -> dict[str, Any]:
    """Load the full TWSE OpenAPI inventory generated from Swagger.

    This is the single registry for every TWSE OpenAPI path. UI/API code should
    search this registry rather than hard-coding a partial list.
    """
    path = get_settings().project_root / "config" / "twse_openapi_inventory.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    req = Request(SWAGGER, headers={"User-Agent": "Mozilla/5.0 StockAI/0.1"})
    def load() -> dict[str, Any]:
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    spec = default_external_transport_guard().call_sync(
        "source:twse_openapi:swagger",
        load,
    )
    endpoints = []
    by_tag: dict[str, int] = {}
    for p, ops in sorted(spec.get("paths", {}).items()):
        for method, op in ops.items():
            if method.lower() not in {"get", "post", "put", "delete", "patch"}:
                continue
            tags = op.get("tags") or ["untagged"]
            for tag in tags:
                by_tag[tag] = by_tag.get(tag, 0) + 1
            endpoints.append(
                {
                    "method": method.upper(),
                    "path": p,
                    "url": source_endpoint("twse_dynamic_openapi", path=p),
                    "tags": tags,
                    "summary": op.get("summary", ""),
                    "description": op.get("description", ""),
                }
            )
    return {"source": SWAGGER, "count": len(endpoints), "by_tag": by_tag, "endpoints": endpoints}


def search_twse_openapi(q: str = "") -> dict[str, Any]:
    inv = load_twse_openapi_inventory()
    query = q.strip().lower()
    if not query:
        items = inv["endpoints"]
    else:
        items = [
            e for e in inv["endpoints"]
            if query in e["path"].lower()
            or query in e.get("summary", "").lower()
            or query in " ".join(e.get("tags", [])).lower()
        ]
    return {"source": inv["source"], "total_count": inv["count"], "count": len(items), "by_tag": inv.get("by_tag", {}), "items": items}


@lru_cache(maxsize=64)
def fetch_twse_openapi_path(path: str, limit: int = 100) -> dict[str, Any]:
    inv = load_twse_openapi_inventory()
    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    endpoint = next((e for e in inv["endpoints"] if e["path"] == normalized), None)
    if endpoint is None:
        raise ValueError(f"TWSE OpenAPI path not in swagger inventory: {normalized}")
    if endpoint["method"] != "GET":
        raise ValueError(f"Only GET endpoints are supported by the generic fetcher: {normalized}")
    req = Request(endpoint["url"], headers={"User-Agent": "Mozilla/5.0 StockAI/0.1"})

    def load() -> Any:
        with urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8-sig"))

    data = default_external_transport_guard().call_sync(
        f"source:twse_openapi:dynamic:{normalized}",
        load,
    )
    total = len(data) if isinstance(data, list) else None
    preview = data[: max(0, min(limit, 1000))] if isinstance(data, list) else data
    return {"endpoint": endpoint, "total_rows": total, "data": preview}
