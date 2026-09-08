from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import hashlib
import io
import json
import sqlite3

import httpx

from stock_ai.data_platform.service import MarketDataPlatform


def verify(database_path: Path) -> dict[str, object]:
    platform = MarketDataPlatform(
        database_path=database_path,
        code_version="raw-data-lake-v1-acceptance",
    )
    endpoint = platform.source_registry_service.endpoint("twse_revenue")
    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={
            "Accept": "application/json",
            "User-Agent": "StockAI-Raw-Lake-Acceptance/1.0",
        },
    ) as client:
        response = client.get(endpoint)
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError("TWSE revenue endpoint returned no object rows")

    raw_payload_id = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload=payload,
        request_url=str(response.url),
        requested_at=response.request.headers.get("date"),
        http_status=response.status_code,
        content_type=response.headers.get("content-type") or "application/json",
        content_encoding="utf-8-sig",
        raw_body=response.content,
        metadata={
            "dataset": "revenues_monthly",
            "acceptance": "DATA-006",
            "transport": "official_api",
        },
    )
    raw = platform.warehouse.raw_payload(raw_payload_id)
    if raw is None:
        raise RuntimeError("Captured raw payload could not be read")
    expected_wire_hash = hashlib.sha256(response.content).hexdigest()
    if raw["wire_hash"] != expected_wire_hash or raw["integrity_status"] != "passed":
        raise RuntimeError("Exact TWSE response bytes failed SHA-256 verification")
    api_replay = platform.warehouse.reprocess_raw_payload(
        raw_payload_id,
        dataset="revenues_monthly",
        code_version="raw-data-lake-v1-acceptance",
    )

    sample_fields = ["公司代號", "公司名稱", "資料年月"]
    csv_rows = [
        {field: str(row.get(field) or "") for field in sample_fields}
        for row in payload[:3]
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=sample_fields)
    writer.writeheader()
    writer.writerows(csv_rows)
    csv_text = stream.getvalue()
    csv_payload_id = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload=csv_rows,
        request_url=f"{endpoint}#csv-replay-fixture",
        content_type="text/csv",
        raw_body=csv_text,
        metadata={
            "dataset": "revenues_monthly",
            "acceptance": "DATA-006",
            "transport": "csv",
        },
    )
    csv_replay = platform.warehouse.reprocess_raw_payload(
        csv_payload_id,
        dataset="revenues_monthly",
        code_version="raw-data-lake-v1-acceptance",
    )

    immutable_guard = False
    try:
        with sqlite3.connect(platform.warehouse.path) as conn:
            conn.execute(
                "update raw_data_objects set byte_length=0 where raw_object_id=?",
                (raw["raw_object_id"],),
            )
    except sqlite3.IntegrityError:
        immutable_guard = True
    if not immutable_guard:
        raise RuntimeError("Raw-object immutable update trigger did not reject mutation")

    status = platform.status()["warehouse"]
    return {
        "schema_version": "stock_ai.raw_data_lake_acceptance.v1",
        "status": "passed",
        "official_source": "TWSE monthly revenue OpenAPI",
        "official_url": endpoint,
        "official_row_count": len(payload),
        "raw_payload_id": raw_payload_id,
        "raw_object_id": raw["raw_object_id"],
        "wire_hash": raw["wire_hash"],
        "byte_length": raw["byte_length"],
        "integrity_status": raw["integrity_status"],
        "api_reprocessing": api_replay,
        "csv_reprocessing": csv_replay,
        "immutable_update_rejected": immutable_guard,
        "raw_data_lake": status["raw_data_lake"],
        "database": str(database_path),
    }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    if args.database:
        result = verify(args.database.expanduser().resolve())
    else:
        with TemporaryDirectory(prefix="stock-ai-raw-lake-") as temp_dir:
            result = verify(Path(temp_dir) / "market-data.sqlite")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
