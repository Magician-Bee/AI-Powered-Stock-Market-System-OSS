"""Current official product identity; independent of issuer/lifecycle reconciliation.

The catalogue's update date is a source vintage, not a historical publication
attestation. Exact response bytes are retained before parsing, including rejected
responses. Neither symbol prefixes nor display names confer trading eligibility.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from threading import Event
import re
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from open_stock_ai.agent_runtime import default_external_transport_guard
from open_stock_ai.product_identity import OFFICIAL_PRODUCT_SECTIONS

from .incremental import IncrementalLoader
from .contracts import normalize_timestamp, utc_now
from .source_registry import get_source_registry, source_endpoint
from .warehouse import content_hash

CATALOGUES = {
    "twse_isin_listed": ("TWSE", "上市", "TW"),
    "tpex_isin_otc": ("TPEx", "上櫃", "TWO"),
    "tpex_isin_emerging": ("TPEx-ESB", "興櫃", "TWO"),
}
MAX_CATALOGUE_BYTES = 16 * 1024 * 1024
_CODE = re.compile(r"[0-9]{4}[A-Z0-9]{0,2}")
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
# Both the official category heading and CFI family must agree. A newly added
# category or changed CFI stays visible as unknown/conflict until reviewed.
_SECTIONS = OFFICIAL_PRODUCT_SECTIONS


class _CatalogueTable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.in_catalogue = False
        self.ended = False
        self.header_seen = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.ended:
            if tag in {"table", "tr", "td", "th"}:
                raise ValueError("product_catalogue_additional_table_unreviewed")
            return
        if tag == "tr":
            if self.row is not None:
                raise ValueError("product_catalogue_malformed_row")
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            if self.cell is not None:
                raise ValueError("product_catalogue_malformed_cell")
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.ended:
            return
        if tag in {"td", "th"} and self.cell is not None:
            assert self.row is not None
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.cell is not None:
                raise ValueError("product_catalogue_unclosed_cell")
            if not self.header_seen:
                if len(self.row) != 7 or self.row[5] != "CFICode" or "ISIN Code" not in self.row[1]:
                    raise ValueError("product_catalogue_header_unrecognized")
                self.header_seen = self.in_catalogue = True
            else:
                self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.in_catalogue:
            if self.row is not None:
                raise ValueError("product_catalogue_truncated_row")
            self.ended = True


def parse_product_catalogue(
    raw: bytes, *, dataset_id: str, acquired_at: str, source_url: str | None = None,
) -> dict[str, Any]:
    """Parse a complete MS950 catalogue without inventing missing identities."""
    if dataset_id not in CATALOGUES:
        raise ValueError("product_catalogue_dataset_unknown")
    url = source_url or source_endpoint(dataset_id)
    if url != source_endpoint(dataset_id):
        raise ValueError("product_catalogue_source_url_mismatch")
    if not raw or len(raw) > MAX_CATALOGUE_BYTES:
        raise ValueError("product_catalogue_empty_or_oversize")
    text = raw.decode("cp950", errors="strict")
    updated = re.findall(r"最近更新日期\s*:\s*(\d{4}/\d{2}/\d{2})", text)
    if len(set(updated)) != 1:
        raise ValueError("product_catalogue_source_date_missing_or_conflicting")
    source_date = date.fromisoformat(updated[0].replace("/", "-"))
    acquired = datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
    if acquired.tzinfo is None:
        raise ValueError("product_catalogue_acquisition_timezone_required")
    if source_date > acquired.astimezone(ZoneInfo("Asia/Taipei")).date():
        raise ValueError("product_catalogue_future_source_date")
    parser = _CatalogueTable()
    parser.feed(text)
    parser.close()
    if not parser.header_seen or not parser.ended or not parser.rows:
        raise ValueError("product_catalogue_incomplete_table")
    venue, market_label, suffix = CATALOGUES[dataset_id]
    section = "興櫃" if dataset_id == "tpex_isin_emerging" else ""
    wire_hash = sha256(raw).hexdigest()
    receipts: list[dict[str, Any]] = []
    for cells in parser.rows:
        if len(cells) == 1 and cells[0]:
            section = cells[0]
            continue
        if len(cells) != 7 or not section:
            raise ValueError("product_catalogue_row_shape_invalid")
        name_parts = cells[0].split(maxsplit=1)
        code = name_parts[0] if name_parts else ""
        if not _CODE.fullmatch(code) or len(name_parts) != 2:
            raise ValueError("product_catalogue_security_code_invalid")
        source_row = {"cells": cells, "section": section}
        product_type, cfi_families, segment = _SECTIONS.get(section, ("unknown", (), "other"))
        reasons: list[str] = []
        if not cfi_families:
            reasons.append("unrecognized_official_product_section")
        if not _ISIN.fullmatch(cells[1]) or not re.fullmatch(r"[A-Z]{6}", cells[5]):
            reasons.append("official_security_identifiers_invalid")
        if cfi_families and not cells[5].startswith(cfi_families):
            reasons.append("official_section_cfi_conflict")
        expected_market = "上市臺灣創新板" if section == "創新板" and venue == "TWSE" else market_label
        if cells[3] != expected_market:
            reasons.append("official_market_venue_conflict")
        status = "verified" if not reasons else "unknown" if product_type == "unknown" else "conflict"
        receipts.append({
            "schema_version": "stock_ai.product_classification.v1",
            "status": status, "product_type": product_type,
            "symbol": f"{code}.{suffix}", "venue": venue,
            "market_segment": segment, "isin": cells[1], "cfi_code": cells[5],
            "source_id": "twse_isin", "source_dataset": dataset_id,
            "source_url": url, "acquired_at": acquired_at,
            "source_updated_on": source_date.isoformat(),
            "raw_sha256": wire_hash, "row_sha256": content_hash(source_row),
            "source_row": source_row, "reasons": reasons,
        })
    # Duplicate source rows are retained as conflicting evidence, never last-win.
    keys = Counter((r["venue"], r["symbol"]) for r in receipts)
    for receipt in receipts:
        if keys[(receipt["venue"], receipt["symbol"])] != 1:
            receipt["status"] = "conflict"
            receipt["reasons"].append("duplicate_official_product_identity")
    if not any(r["product_type"] == "ordinary_stock" for r in receipts):
        raise ValueError("product_catalogue_ordinary_section_missing")
    return {
        "receipts": receipts, "complete_source_datasets": [dataset_id],
        "raw_sha256": wire_hash, "source_updated_on": source_date.isoformat(),
        "row_count": len(receipts),
        "product_type_counts": dict(Counter(r["product_type"] for r in receipts)),
        "classification_status_counts": dict(Counter(r["status"] for r in receipts)),
    }


def fetch_product_catalogue(dataset_id: str) -> dict[str, Any]:
    """One bounded request under the existing transport guard; no silent fallback."""
    dataset = get_source_registry().dataset(dataset_id)
    if dataset_id not in CATALOGUES or dataset.source_id != "twse_isin":
        raise ValueError("product_catalogue_dataset_unknown")
    url = source_endpoint(dataset_id)
    requested_at = datetime.now(timezone.utc).isoformat()

    def fetch() -> dict[str, Any]:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 StockAI/0.1"})
        try:
            response = urlopen(request, timeout=dataset.failure_strategy.timeout_seconds)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_CATALOGUE_BYTES + 1)
            status = response.status
            effective_url = response.geturl()
            content_type = response.headers.get("Content-Type", "application/octet-stream")
        return {"raw": raw, "http_status": status, "effective_url": effective_url,
                "source_url": url, "content_type": content_type,
                "requested_at": requested_at,
                "acquired_at": datetime.now(timezone.utc).isoformat()}

    return default_external_transport_guard().call_sync(f"source:twse_isin:{dataset_id}", fetch)


class OfficialProductClassificationLoader:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.incremental = IncrementalLoader(platform)

    def run(self, *, force: bool = False, as_of: str | None = None, stop_event: Event | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for dataset_id in CATALOGUES:
            if stop_event is not None and stop_event.is_set():
                raise asyncio.CancelledError("product_catalogue_refresh_cancelled")
            summary: dict[str, Any] = {}

            def fetch(_cursor: str | None) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
                captured = fetch_product_catalogue(dataset_id)
                raw = captured["raw"]
                # Store exact received bytes even when a source returns an HTML
                # error, changes its format, redirects, or exceeds the byte cap.
                raw_id = self.platform.warehouse.record_raw_payload(
                    source_id="twse_isin", payload={"dataset_id": dataset_id,
                        "wire_sha256": sha256(raw).hexdigest(), "acquired_at": captured["acquired_at"]},
                    raw_body=raw, request_url=captured["source_url"],
                    requested_at=captured["requested_at"], received_at=captured["acquired_at"],
                    http_status=captured["http_status"], content_type=captured["content_type"],
                    content_encoding="cp950", parser_id="stock_ai.product_classification.v1",
                    metadata={"dataset": "security_master", "source_dataset": dataset_id,
                              "effective_url": captured["effective_url"],
                              "capture_truncated": len(raw) > MAX_CATALOGUE_BYTES},
                )
                summary.update(raw_payload_id=raw_id, raw_sha256=sha256(raw).hexdigest())
                if captured["http_status"] != 200:
                    raise ValueError(f"product_catalogue_http_status:{captured['http_status']}")
                if captured["effective_url"] != captured["source_url"]:
                    raise ValueError("product_catalogue_redirected_source")
                parsed = parse_product_catalogue(raw, dataset_id=dataset_id,
                    acquired_at=captured["acquired_at"], source_url=captured["source_url"])
                for receipt in parsed["receipts"]:
                    receipt["raw_payload_id"] = raw_id
                summary.update({key: value for key, value in parsed.items() if key != "receipts"})
                summary["raw_payload_id"] = raw_id
                return parsed["receipts"], parsed["raw_sha256"], dict(summary)

            def persist(records: list[dict[str, Any]], _acquired_at: str) -> None:
                if not records:
                    raise ValueError("product_catalogue_empty")
                summary["sync"] = self.platform.sync_product_classifications(
                    records, acquired_at=records[0]["acquired_at"],
                    complete_source_datasets=(dataset_id,),
                )

            try:
                result = self.incremental.run(source_id="twse_isin", dataset="security_master",
                    partition_key=dataset_id, fetch=fetch, persist=persist, force=force, as_of=as_of)
            except Exception as exc:
                # IncrementalLoader retains the failed checkpoint. Other venues
                # can still refresh; none of these failures changes lifecycle.
                self.platform.cache_policy_service.invalidate(
                    source_id="twse_isin", dataset="security_master", partition_key=dataset_id,
                    reason="catalogue_refresh_failed", invalidated_at=as_of,
                    metadata={"error_type": type(exc).__name__},
                )
                result = {"status": "failed", "source_id": "twse_isin", "partition_key": dataset_id,
                          "error": {"type": type(exc).__name__, "message": str(exc)}}
            results.append({**result, "classification": summary or None})
        if stop_event is not None and stop_event.is_set():
            raise asyncio.CancelledError("product_catalogue_refresh_cancelled")
        # Capture checkpoints are now committed. Identity ingestion also runs
        # on a fresh cache hit, so deployment, retries and listing-day changes
        # do not wait for a new HTTP capture. It retains lifecycle authority.
        try:
            identities = self.platform.warehouse.sync_catalogue_identities(
                as_of=normalize_timestamp(as_of or utc_now(), required=True),
                code_version=self.platform.code_version,
            )
            reports = {p["source_dataset"]: p for p in identities["partitions"]}
            for result in results:
                result["identity_ingestion"] = reports[result["partition_key"]]
                if result["status"] in {"succeeded", "skipped_fresh"} and result["identity_ingestion"]["status"] != "succeeded":
                    result["status"] = "partial"
        except Exception as exc:
            for result in results:
                result["identity_ingestion"] = {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}}
                if result["status"] in {"succeeded", "skipped_fresh"}:
                    result["status"] = "partial"
        return results
