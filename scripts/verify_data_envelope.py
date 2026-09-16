from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import json

import httpx

from stock_ai.data_platform.contracts import (
    EntityRecord,
    TemporalCoordinates,
    payload_leaf_pointers,
    utc_now,
)
from stock_ai.data_platform.service import MarketDataPlatform, stable_entity_id


FIELD_MAP = {
    "code": "公司代號",
    "legal_name": "公司名稱",
    "short_name": "公司簡稱",
    "business_no": "營利事業統一編號",
    "industry": "產業別",
    "listed_at": "上市日期",
}


def _compact_date(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 7 and text.isdigit():
        return f"{int(text[:3]) + 1911:04d}-{text[3:5]}-{text[5:7]}"
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    raise RuntimeError(f"Unexpected official date value: {value!r}")


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _resolve_pointer(document: Any, pointer: str) -> Any:
    current = document
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def verify(database_path: Path) -> dict[str, Any]:
    platform = MarketDataPlatform(
        database_path=database_path,
        code_version="data-envelope-v2-acceptance",
    )
    registry = platform.source_registry_service
    endpoint = registry.endpoint("twse_companies")
    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "StockAI-DataEnvelope-Acceptance/1.0"},
    ) as client:
        response = client.get(endpoint)
        response.raise_for_status()
        source_rows = response.json()
    if not isinstance(source_rows, list) or len(source_rows) < 3:
        raise RuntimeError("TWSE company source did not return enough object rows")
    if not all(isinstance(row, dict) for row in source_rows[:3]):
        raise RuntimeError("TWSE company source returned a non-object row")

    acquired_at = utc_now()
    raw_payload_id = platform.warehouse.record_raw_payload(
        source_id="twse_openapi",
        payload=source_rows,
        request_url=endpoint,
        requested_at=acquired_at,
        received_at=acquired_at,
        metadata={
            "dataset": "twse_companies",
            "record_count": len(source_rows),
            "acceptance": "DATA-004",
        },
    )
    source_date = _compact_date(source_rows[0]["出表日期"])
    temporal = TemporalCoordinates(
        observed_at=source_date,
        published_at=source_date,
        available_at=source_date,
        acquired_at=acquired_at,
        effective_at=source_date,
    )

    revisions = []
    for index, source_row in enumerate(source_rows[:3]):
        code = str(source_row[FIELD_MAP["code"]]).strip()
        payload = {
            "code": code,
            "legal_name": str(source_row[FIELD_MAP["legal_name"]]).strip(),
            "short_name": str(source_row[FIELD_MAP["short_name"]]).strip(),
            "business_no": str(source_row[FIELD_MAP["business_no"]]).strip(),
            "industry": str(source_row[FIELD_MAP["industry"]]).strip(),
            "listed_at": _compact_date(source_row[FIELD_MAP["listed_at"]]),
        }
        entity_id = stable_entity_id(
            market="taiwan",
            exchange="TWSE",
            source_code=code,
        )
        platform.warehouse.upsert_entity(
            EntityRecord(
                entity_id=entity_id,
                entity_type="stock",
                canonical_name=payload["legal_name"],
                market="taiwan",
                exchange="TWSE",
                currency="TWD",
                industry=payload["industry"],
                listed_at=payload["listed_at"],
                metadata={"acceptance": "DATA-004"},
            ),
            identifiers=(
                {
                    "source_id": "twse_openapi",
                    "identifier_type": "exchange_code",
                    "identifier_value": code,
                    "valid_from": payload["listed_at"],
                    "confidence": 1.0,
                    "is_primary": True,
                },
            ),
        )
        field_provenance = {}
        for output_field, source_field in FIELD_MAP.items():
            output_pointer = f"/{_escape_pointer(output_field)}"
            raw_pointer = f"/{index}/{_escape_pointer(source_field)}"
            field_provenance[output_pointer] = {
                "source_id": "twse_openapi",
                "temporal": temporal.model_dump(mode="json"),
                "updated_at": acquired_at,
                "raw_payload_id": raw_payload_id,
                "raw_json_pointer": raw_pointer,
                "quality_status": "valid",
                "quality_flags": ["live_official_source"],
                "transformation_id": "stock_ai.twse_company_normalizer.v2",
                "input_fields": [raw_pointer],
            }
        revisions.append(
            platform.warehouse.write_revision(
                dataset="twse_companies",
                entity_id=entity_id,
                observation_key=f"{source_date}:{code}",
                source_id="twse_openapi",
                temporal=temporal,
                payload=payload,
                raw_payload_id=raw_payload_id,
                quality_status="valid",
                quality_flags=["live_official_source"],
                transformation_id="stock_ai.twse_company_normalizer.v2",
                code_version="data-envelope-v2-acceptance",
                field_provenance=field_provenance,
            )
        )

    queried = platform.query(dataset="twse_companies", limit=10)
    if len(queried) != len(revisions):
        raise RuntimeError(f"Expected {len(revisions)} revisions, got {len(queried)}")
    with platform.warehouse._connect() as conn:
        raw_document = json.loads(
            conn.execute(
                "select payload_json from raw_data_payloads where raw_payload_id=?",
                (raw_payload_id,),
            ).fetchone()["payload_json"]
        )

    traced_fields = 0
    sources: set[str] = set()
    quality_statuses: set[str] = set()
    for revision in queried:
        expected = set(payload_leaf_pointers(revision.payload))
        if set(revision.field_provenance) != expected:
            raise RuntimeError(
                f"Envelope {revision.revision_id} does not trace every payload leaf"
            )
        for pointer, provenance in revision.field_provenance.items():
            if provenance.raw_payload_id != raw_payload_id:
                raise RuntimeError(f"{pointer} lost its immutable raw payload")
            if not provenance.temporal.available_at or not provenance.temporal.acquired_at:
                raise RuntimeError(f"{pointer} lost source/acquisition time")
            if not provenance.updated_at:
                raise RuntimeError(f"{pointer} lost update time")
            _resolve_pointer(raw_document, provenance.raw_json_pointer)
            traced_fields += 1
            sources.add(provenance.source_id)
            quality_statuses.add(provenance.quality_status)

    lineage = platform.warehouse.lineage(queried[0].revision_id)
    raw_lineage = lineage.get("raw_payload") or {}
    if raw_lineage.get("raw_payload_id") != raw_payload_id:
        raise RuntimeError("Revision lineage did not resolve the raw payload")
    if not raw_lineage.get("payload_hash"):
        raise RuntimeError("Revision lineage has no immutable raw hash")
    warehouse_status = platform.warehouse.status()
    tables = warehouse_status["tables"]
    if tables["untraced_revisions"] != 0:
        raise RuntimeError(f"Untraced revisions remain: {tables['untraced_revisions']}")
    if tables["traced_fields"] != traced_fields:
        raise RuntimeError(
            f"Trace count mismatch: {tables['traced_fields']} != {traced_fields}"
        )

    return {
        "schema_version": "stock_ai.data_envelope_acceptance.v2",
        "status": "passed",
        "source": "twse_openapi",
        "source_endpoint": endpoint,
        "source_row_count": len(source_rows),
        "verified_revision_count": len(queried),
        "verified_payload_field_count": traced_fields,
        "untraced_revision_count": tables["untraced_revisions"],
        "field_sources": sorted(sources),
        "field_quality_statuses": sorted(quality_statuses),
        "source_time": source_date,
        "acquired_at": acquired_at,
        "raw_payload_id": raw_payload_id,
        "raw_payload_hash": raw_lineage["payload_hash"],
        "database": str(database_path),
    }


def main() -> int:
    parser = ArgumentParser(description="Verify DATA-004 against a live TWSE response")
    parser.add_argument(
        "--database",
        type=Path,
        help="Persist the acceptance database at this path for UI verification",
    )
    args = parser.parse_args()
    if args.database:
        result = verify(args.database.expanduser().resolve())
    else:
        with TemporaryDirectory(prefix="stock-ai-envelope-verification-") as temp_dir:
            result = verify(Path(temp_dir) / "market-data.sqlite")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
