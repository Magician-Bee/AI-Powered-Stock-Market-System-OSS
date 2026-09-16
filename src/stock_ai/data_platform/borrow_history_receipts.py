from __future__ import annotations

"""PIT coverage receipts for short-execution borrow evidence."""

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "stock_ai.short_execution_borrow_coverage.v1"
_STREAMS = {
    "borrow_availability": "available_quantity",
    "borrow_rate": "annual_rate_bps",
    "borrow_recall": "recall_at",
    "borrow_fee_history": "fee_bps",
}
_MISSING = {
    stream: f"short_execution_{stream}_history_missing" for stream in _STREAMS
}


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


class ShortBorrowHistoryReceiptStore:
    """Durable immutable ledger for borrow-history coverage decisions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists short_borrow_history_receipts (
                    receipt_sha256 text primary key,
                    payload_json text not null,
                    persisted_at text not null
                );
                create trigger if not exists short_borrow_history_receipts_immutable_update
                    before update on short_borrow_history_receipts
                    begin select raise(abort, 'short borrow history receipts are immutable'); end;
                create trigger if not exists short_borrow_history_receipts_immutable_delete
                    before delete on short_borrow_history_receipts
                    begin select raise(abort, 'short borrow history receipts are immutable'); end;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        verify_short_borrow_history_receipt(receipt)
        payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        receipt_hash = str(receipt["receipt_sha256"])
        with self._connect() as connection:
            existing = connection.execute(
                "select payload_json from short_borrow_history_receipts where receipt_sha256=?",
                (receipt_hash,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != payload:
                    raise ValueError("short borrow history hash is bound to different evidence")
                return dict(receipt)
            connection.execute(
                "insert into short_borrow_history_receipts(receipt_sha256, payload_json, persisted_at) values (?, ?, ?)",
                (receipt_hash, payload, datetime.now(timezone.utc).isoformat()),
            )
        return dict(receipt)

    def receipts(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from short_borrow_history_receipts order by persisted_at"
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]


def _stream_receipt(*, stream: str, items: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    required_value = _STREAMS[stream]
    keys = [
        "|".join(
            (
                str(item.get("as_of") or "").strip(),
                str(item.get("symbol") or "").strip().upper(),
                str(item.get("source_id") or "").strip(),
            )
        )
        for item in items
    ]
    blockers: list[str] = []
    if not items:
        blockers.append(_MISSING[stream])
    if any(not all(str(item.get(field) or "").strip() for field in ("as_of", "symbol", "source_id", "acquired_at")) for item in items):
        blockers.append(f"short_execution_{stream}_observation_metadata_missing")
    if len(keys) != len(set(keys)):
        blockers.append(f"short_execution_{stream}_duplicate_observation")
    if any(item.get(required_value) is None or str(item.get(required_value)).strip() == "" for item in items):
        blockers.append(f"short_execution_{stream}_value_missing")
    source_ids = sorted({str(item.get("source_id") or "") for item in items if item.get("source_id")})
    dates = sorted(str(item.get("as_of") or "") for item in items if item.get("as_of"))
    payload: dict[str, Any] = {
        "stream": stream,
        "required_value": required_value,
        "source_ids": source_ids,
        "symbols": sorted({str(item.get("symbol") or "").strip().upper() for item in items if item.get("symbol")}),
        "observed_record_count": len(items),
        "unique_observation_count": len(set(keys)),
        "first_observation": dates[0] if dates else None,
        "last_observation": dates[-1] if dates else None,
        "observation_manifest_sha256": _sha256(sorted(set(keys))),
        "record_manifest_sha256": _sha256([dict(item) for item in items]),
        "blockers": sorted(set(blockers)),
        "historical_pit_eligible": not blockers,
        "provider_verified": bool(items) and all(bool(item.get("provider_verified")) for item in items),
    }
    payload["audit_sha256"] = _sha256(payload)
    return payload


def verify_short_borrow_history_receipt(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid short borrow history schema")
    top_level = dict(receipt)
    actual_hash = str(top_level.pop("receipt_sha256", ""))
    if len(actual_hash) != 64 or actual_hash != _sha256(top_level):
        raise ValueError("short borrow history receipt hash mismatch")
    streams = top_level.get("streams")
    if not isinstance(streams, Mapping) or set(streams) != set(_STREAMS):
        raise ValueError("short borrow history streams are incomplete")
    detail_blockers: set[str] = set()
    detail_symbols: set[str] = set()
    detail_first: list[str] = []
    detail_last: list[str] = []
    detail_pit_eligible: list[bool] = []
    detail_provider_verified: list[bool] = []
    for stream in streams.values():
        if not isinstance(stream, Mapping):
            raise ValueError("short borrow history stream is invalid")
        audit = dict(stream)
        audit_hash = str(audit.pop("audit_sha256", ""))
        if len(audit_hash) != 64 or audit_hash != _sha256(audit):
            raise ValueError("short borrow history stream hash mismatch")
        detail_blockers.update(str(item) for item in stream.get("blockers") or [])
        detail_symbols.update(str(item).strip().upper() for item in stream.get("symbols") or [] if str(item).strip())
        if stream.get("first_observation"):
            detail_first.append(str(stream["first_observation"]))
        if stream.get("last_observation"):
            detail_last.append(str(stream["last_observation"]))
        detail_pit_eligible.append(stream.get("historical_pit_eligible") is True)
        detail_provider_verified.append(stream.get("provider_verified") is True)
    if "symbols" in top_level and set(top_level.get("symbols") or []) != detail_symbols:
        raise ValueError("short borrow history symbol manifest mismatch")
    if "first_as_of" in top_level and top_level.get("first_as_of") != (min(detail_first) if detail_first else None):
        raise ValueError("short borrow history first date mismatch")
    if "last_as_of" in top_level and top_level.get("last_as_of") != (max(detail_last) if detail_last else None):
        raise ValueError("short borrow history last date mismatch")
    expected_historical = all(detail_pit_eligible)
    expected_provider = expected_historical and all(detail_provider_verified)
    if top_level.get("historical_pit_eligible") is not expected_historical:
        raise ValueError("short borrow history PIT eligibility mismatch")
    if top_level.get("provider_verified") is not expected_provider:
        raise ValueError("short borrow history provider verification mismatch")
    if top_level.get("execution_replay_eligible") is not (expected_historical and expected_provider):
        raise ValueError("short borrow history execution eligibility mismatch")
    if not detail_blockers.issubset(set(top_level.get("blockers") or [])):
        raise ValueError("short borrow history blocker manifest mismatch")
    return True


def short_borrow_history_coverage(
    *,
    borrow_availability: Iterable[Mapping[str, Any]] = (),
    borrow_rate: Iterable[Mapping[str, Any]] = (),
    borrow_recall: Iterable[Mapping[str, Any]] = (),
    borrow_fee_history: Iterable[Mapping[str, Any]] = (),
    receipt_store: ShortBorrowHistoryReceiptStore | None = None,
) -> dict[str, Any]:
    streams = {
        "borrow_availability": tuple(borrow_availability),
        "borrow_rate": tuple(borrow_rate),
        "borrow_recall": tuple(borrow_recall),
        "borrow_fee_history": tuple(borrow_fee_history),
    }
    details = {
        name: _stream_receipt(stream=name, items=items)
        for name, items in streams.items()
    }
    blockers = sorted(
        {blocker for detail in details.values() for blocker in detail["blockers"]}
    )
    historical_eligible = all(bool(detail["historical_pit_eligible"]) for detail in details.values())
    provider_verified = historical_eligible and all(bool(detail["provider_verified"]) for detail in details.values())
    if historical_eligible and not provider_verified:
        blockers.append("short_execution_borrow_provider_not_verified")
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "streams": details,
        "symbols": sorted({symbol for detail in details.values() for symbol in detail["symbols"]}),
        "first_as_of": min(
            (detail["first_observation"] for detail in details.values() if detail["first_observation"]),
            default=None,
        ),
        "last_as_of": max(
            (detail["last_observation"] for detail in details.values() if detail["last_observation"]),
            default=None,
        ),
        "available_streams": [name for name, detail in details.items() if detail["historical_pit_eligible"]],
        "historical_pit_eligible": historical_eligible,
        "provider_verified": provider_verified,
        "execution_replay_eligible": historical_eligible and provider_verified,
        "blockers": sorted(set(blockers)),
        "zero_fill_used": False,
    }
    receipt["receipt_sha256"] = _sha256(receipt)
    if receipt_store is not None:
        receipt_store.record(receipt)
    return receipt
