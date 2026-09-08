from __future__ import annotations

"""PIT coverage receipt for chip/ownership data streams."""

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .availability import get_data_availability_registry
from .borrow_history_receipts import short_borrow_history_coverage


SCHEMA_VERSION = "stock_ai.chip_pit_coverage.v1"
AUDIT_SCHEMA_VERSION = "stock_ai.chip_stream_ingestion_audit.v1"


class ChipPITCoverageReceiptStore:
    """Durable immutable ledger for chip PIT coverage decisions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists chip_pit_coverage_receipts (
                    receipt_sha256 text primary key,
                    payload_json text not null,
                    persisted_at text not null
                );
                create trigger if not exists chip_pit_coverage_receipts_immutable_update
                    before update on chip_pit_coverage_receipts
                    begin select raise(abort, 'chip PIT coverage receipts are immutable'); end;
                create trigger if not exists chip_pit_coverage_receipts_immutable_delete
                    before delete on chip_pit_coverage_receipts
                    begin select raise(abort, 'chip PIT coverage receipts are immutable'); end;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        verify_chip_pit_coverage_receipt(receipt)
        payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        receipt_hash = str(receipt["receipt_sha256"])
        with self._connect() as connection:
            existing = connection.execute(
                "select payload_json from chip_pit_coverage_receipts where receipt_sha256=?",
                (receipt_hash,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != payload:
                    raise ValueError("chip PIT coverage receipt hash is bound to different evidence")
                return dict(receipt)
            connection.execute(
                """
                insert into chip_pit_coverage_receipts(
                    receipt_sha256, payload_json, persisted_at
                ) values (?, ?, ?)
                """,
                (receipt_hash, payload, datetime.now(timezone.utc).isoformat()),
            )
        return dict(receipt)

    def receipts(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from chip_pit_coverage_receipts order by persisted_at"
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _stream_audit(
    *,
    items: tuple[Mapping[str, Any], ...],
    date_field: str,
    decisions: list[dict[str, Any]],
    source_ids: list[str],
) -> dict[str, Any]:
    """Bind a chip stream to immutable record and observation-key manifests."""

    observation_keys = [
        "|".join(
            (
                str(item.get(date_field) or "").strip(),
                str(item.get("symbol") or "").strip().upper(),
                str(item.get("source_id") or "").strip(),
            )
        )
        for item in items
    ]
    unique_keys = sorted(set(observation_keys))
    blockers: list[str] = []
    if not items:
        blockers.append("no_observations")
    if len(unique_keys) != len(observation_keys):
        blockers.append("duplicate_observation_key_in_ingestion")
    if any(not str(item.get(date_field) or "").strip() for item in items):
        blockers.append("observation_date_missing")
    if items and not all(bool(item.get("acquired_at")) for item in items):
        blockers.append("local_ingestion_timestamp_missing")
    if decisions and any(
        not bool(decision["historical_pit_eligible"])
        or not bool(decision["production_contract_covered"])
        for decision in decisions
    ):
        blockers.append("availability_contract_not_certified")
    payload: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "source_ids": source_ids,
        "observed_record_count": len(items),
        "unique_observation_count": len(unique_keys),
        "first_observation": min(
            (str(item.get(date_field) or "") for item in items if item.get(date_field)),
            default=None,
        ),
        "last_observation": max(
            (str(item.get(date_field) or "") for item in items if item.get(date_field)),
            default=None,
        ),
        "observation_manifest_sha256": _sha256({"observation_keys": unique_keys}),
        "record_manifest_sha256": _sha256(
            sorted(
                json.dumps(
                    dict(item),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                for item in items
            )
        ),
        "blockers": sorted(set(blockers)),
        "complete": not blockers,
    }
    payload["audit_sha256"] = _sha256(payload)
    return payload


def verify_chip_pit_coverage_receipt(receipt: Mapping[str, Any]) -> bool:
    """Verify receipt hashes without consulting live data or the registry."""

    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid chip PIT coverage schema")
    top_level = dict(receipt)
    actual_hash = str(top_level.pop("receipt_sha256", ""))
    if len(actual_hash) != 64 or actual_hash != _sha256(top_level):
        raise ValueError("chip PIT coverage receipt hash mismatch")
    streams = top_level.get("streams")
    if not isinstance(streams, Mapping):
        raise ValueError("chip PIT coverage streams are missing")
    for detail in streams.values():
        if not isinstance(detail, Mapping):
            raise ValueError("chip PIT stream detail is invalid")
        audit = detail.get("coverage_audit")
        if not isinstance(audit, Mapping):
            raise ValueError("chip PIT stream audit is missing")
        audit_payload = dict(audit)
        audit_hash = str(audit_payload.pop("audit_sha256", ""))
        if len(audit_hash) != 64 or audit_hash != _sha256(audit_payload):
            raise ValueError("chip PIT stream audit hash mismatch")
    return True


def chip_pit_coverage(
    *,
    institutional: Iterable[Mapping[str, Any]],
    margin: Iterable[Mapping[str, Any]],
    borrowed_short: Iterable[Mapping[str, Any]] = (),
    tdcc: Iterable[Mapping[str, Any]] = (),
    receipt_store: ChipPITCoverageReceiptStore | None = None,
) -> dict[str, Any]:
    """Return a fail-closed coverage receipt for exact research replay.

    The listed payloads are useful operational observations, but a historical
    replay additionally needs a reviewed availability rule and the time this
    installation ingested the observation.  This receipt records gaps instead
    of converting absent data into a zero chip signal.
    """

    streams = {
        "institutional_flows": ("twse_openapi", "institutional_flows", tuple(institutional), "trade_date"),
        "margin_trading": ("twse_openapi", "margin_trading", tuple(margin), "trade_date"),
        "borrowed_short": ("twse_official_web", "borrowed_short", tuple(borrowed_short), "trade_date"),
        "tdcc_holding_distribution": ("tdcc", "tdcc_holding_distribution", tuple(tdcc), "report_date"),
    }
    details: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    registry = get_data_availability_registry()
    for name, (default_source_id, dataset, items, date_field) in streams.items():
        dates = sorted(str(item.get(date_field) or "") for item in items if item.get(date_field))
        source_ids = sorted({str(item.get("source_id") or default_source_id) for item in items}) or [default_source_id]
        decisions = [
            registry.resolve(source_id=str(item.get("source_id") or default_source_id), dataset=dataset).receipt(
                observed_at=str(item.get(date_field) or "") or None,
                published_at=str(item.get("published_at") or "") or None,
                acquired_at=str(item.get("acquired_at") or "") or None,
            )
            for item in items
        ]
        # Operational chip responses carry a trade date but this lightweight
        # view has no immutable revision receipt proving when the local system
        # acquired the observation.  A reviewed market-schedule rule alone is
        # therefore insufficient for exact replay; keep this UI response
        # useful while withholding research certification.
        acquired_at_attested = all(bool(item.get("acquired_at")) for item in items)
        certified = bool(items) and acquired_at_attested and all(
            bool(item["historical_pit_eligible"])
            and bool(item["production_contract_covered"])
            for item in decisions
        )
        missingness = (
            "none"
            if certified
            else (
                "local_ingestion_timestamp_missing"
                if bool(items) and not acquired_at_attested
                else (decisions[0]["reason"] if decisions else "no_observations")
            )
        )
        if not certified:
            basis = decisions[0]["basis"] if decisions else registry.resolve(source_id=default_source_id, dataset=dataset).basis
            blockers.append(f"pit_chip_stream_not_certified:{name}:{basis}")
        coverage_audit = _stream_audit(
            items=items,
            date_field=date_field,
            decisions=decisions,
            source_ids=source_ids,
        )
        stream_certified = certified and coverage_audit["complete"] is True
        if not stream_certified and missingness == "none":
            missingness = str(coverage_audit["blockers"][0])
        if coverage_audit["complete"] is not True:
            blockers.append(
                f"pit_chip_stream_not_certified:{name}:{coverage_audit['blockers'][0]}"
            )
        details[name] = {
            "count": len(items),
            "first_observation": dates[0] if dates else None,
            "last_observation": dates[-1] if dates else None,
            "source_ids": source_ids,
            "dataset": dataset,
            "availability_basis": sorted({str(item["basis"]) for item in decisions}) or [registry.resolve(source_id=default_source_id, dataset=dataset).basis],
            "historical_pit_eligible": stream_certified,
            "production_contract_covered": bool(decisions) and all(bool(item["production_contract_covered"]) for item in decisions),
            "source_retention_days": sorted(
                {
                    item["source_retention_days"]
                    for item in decisions
                    if item.get("source_retention_days") is not None
                }
            ),
            "acquired_at_attested": acquired_at_attested,
            "missingness": missingness,
            "coverage_audit": coverage_audit,
        }
    borrow_history = short_borrow_history_coverage()
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "streams": details,
        "blockers": blockers,
        "exact_replay_eligible": not blockers,
        # Borrowed-short sell/return/balance observations describe activity,
        # not whether a new short could have been opened.  Do not allow the
        # former to masquerade as historical borrow availability, rate, recall
        # or fee evidence for a short-execution replay.
        "short_execution_replay": {
            "eligible": bool(borrow_history["execution_replay_eligible"]),
            "required_streams": [
                "borrow_availability",
                "borrow_rate",
                "borrow_recall",
                "borrow_fee_history",
            ],
            "available_streams": borrow_history["available_streams"],
            "blockers": borrow_history["blockers"],
            "coverage_receipt": borrow_history,
        },
        "zero_fill_used": False,
    }
    receipt["receipt_sha256"] = _sha256(receipt)
    if receipt_store is not None:
        receipt_store.record(receipt)
    return receipt
