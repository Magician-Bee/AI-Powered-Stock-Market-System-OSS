from __future__ import annotations

"""Immutable provider-coverage evidence for point-in-time news replay."""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import normalize_timestamp


_SCHEMA = "stock_ai.news_history_coverage_receipt.v1"
_INGESTION_AUDIT_SCHEMA = "stock_ai.news_history_ingestion_coverage_audit.v1"


@dataclass(frozen=True, slots=True)
class NewsHistoryCoverageReceipt:
    receipt_id: str
    source_id: str
    coverage_start: str
    coverage_end: str
    provider_document_sha256: str
    reviewed_provider_coverage: bool
    provider_contract_receipt_id: str | None
    account_owner_review_receipt_sha256: str | None
    original_publication_timestamps_complete: bool
    source_event_count: int
    observed_at: str
    receipt_sha256: str

    @classmethod
    def issue(
        cls,
        *,
        receipt_id: str,
        source_id: str,
        coverage_start: str,
        coverage_end: str,
        provider_document_sha256: str,
        reviewed_provider_coverage: bool = False,
        provider_contract_receipt_id: str | None = None,
        account_owner_review_receipt_sha256: str | None = None,
        original_publication_timestamps_complete: bool,
        source_event_count: int,
        observed_at: str | None = None,
    ) -> "NewsHistoryCoverageReceipt":
        observed = normalize_timestamp(observed_at or datetime.now(timezone.utc), required=True)
        receipt = cls(
            receipt_id=str(receipt_id).strip(),
            source_id=str(source_id).strip(),
            coverage_start=str(normalize_timestamp(coverage_start, required=True)),
            coverage_end=str(normalize_timestamp(coverage_end, required=True)),
            provider_document_sha256=str(provider_document_sha256).lower().strip(),
            reviewed_provider_coverage=bool(reviewed_provider_coverage),
            provider_contract_receipt_id=(
                str(provider_contract_receipt_id).strip() or None
                if provider_contract_receipt_id is not None
                else None
            ),
            account_owner_review_receipt_sha256=(
                str(account_owner_review_receipt_sha256).lower().strip() or None
                if account_owner_review_receipt_sha256 is not None
                else None
            ),
            original_publication_timestamps_complete=bool(original_publication_timestamps_complete),
            source_event_count=int(source_event_count),
            observed_at=str(observed),
            receipt_sha256="",
        )
        receipt = replace(receipt, receipt_sha256=_sha(receipt.payload()))
        receipt.verify()
        return receipt

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA,
            "receipt_id": self.receipt_id,
            "source_id": self.source_id,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "provider_document_sha256": self.provider_document_sha256,
            "reviewed_provider_coverage": self.reviewed_provider_coverage,
            "provider_contract_receipt_id": self.provider_contract_receipt_id,
            "account_owner_review_receipt_sha256": self.account_owner_review_receipt_sha256,
            "original_publication_timestamps_complete": self.original_publication_timestamps_complete,
            "source_event_count": self.source_event_count,
            "observed_at": self.observed_at,
        }

    def model_dump(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def model_validate(cls, value: dict[str, Any]) -> "NewsHistoryCoverageReceipt":
        if value.get("schema_version") != _SCHEMA:
            raise ValueError("invalid news history coverage receipt schema")
        receipt = cls(
            receipt_id=str(value.get("receipt_id") or ""),
            source_id=str(value.get("source_id") or ""),
            coverage_start=str(value.get("coverage_start") or ""),
            coverage_end=str(value.get("coverage_end") or ""),
            provider_document_sha256=str(value.get("provider_document_sha256") or "").lower(),
            reviewed_provider_coverage=value.get("reviewed_provider_coverage") is True,
            provider_contract_receipt_id=(
                str(value.get("provider_contract_receipt_id") or "").strip() or None
            ),
            account_owner_review_receipt_sha256=(
                str(value.get("account_owner_review_receipt_sha256") or "").lower().strip()
                or None
            ),
            original_publication_timestamps_complete=(value.get("original_publication_timestamps_complete") is True),
            source_event_count=int(value.get("source_event_count") or 0),
            observed_at=str(value.get("observed_at") or ""),
            receipt_sha256=str(value.get("receipt_sha256") or "").lower(),
        )
        receipt.verify()
        return receipt

    def verify(self) -> None:
        if not self.receipt_id or not self.source_id:
            raise ValueError("news coverage receipt requires receipt and source IDs")
        if len(self.provider_document_sha256) != 64 or len(self.receipt_sha256) != 64:
            raise ValueError("news coverage receipt requires SHA-256 hashes")
        if self.reviewed_provider_coverage and (
            not self.provider_contract_receipt_id
            or not _is_sha256(self.account_owner_review_receipt_sha256)
        ):
            raise ValueError("reviewed news coverage requires provider contract and owner review receipt")
        if self.source_event_count < 0:
            raise ValueError("news coverage receipt event count must be non-negative")
        if normalize_timestamp(self.coverage_end, required=True) < normalize_timestamp(self.coverage_start, required=True):
            raise ValueError("news coverage receipt range is invalid")
        if self.receipt_sha256 != _sha(self.payload()):
            raise ValueError("news coverage receipt hash mismatch")

    def covers(self, *, source_id: str, published_at: str) -> bool:
        self.verify()
        instant = normalize_timestamp(published_at, required=True)
        return (
            self.original_publication_timestamps_complete
            and self.source_id == str(source_id).strip()
            and normalize_timestamp(self.coverage_start, required=True) <= instant <= normalize_timestamp(self.coverage_end, required=True)
        )

    def audit_ingestion(self, *, event_keys: Iterable[str]) -> dict[str, Any]:
        """Bind this provider claim to the exact event set ingested locally.

        A date range and a provider document hash are not enough to prove that
        a replay contains the complete source response.  The caller must
        compare the provider-declared event count with the event keys actually
        written to the immutable lake.  Any mismatch is deliberately
        fail-closed, even when every individual event has a valid publication
        timestamp.
        """

        self.verify()
        keys = [str(value).strip() for value in event_keys]
        unique_keys = sorted(set(keys))
        blockers: list[str] = []
        if len(unique_keys) != len(keys):
            blockers.append("duplicate_event_key_in_ingestion")
        if self.source_event_count != len(unique_keys):
            blockers.append("provider_event_count_does_not_match_ingested_event_count")
        if not self.original_publication_timestamps_complete:
            blockers.append("provider_publication_timestamps_incomplete")
        if not self.reviewed_provider_coverage:
            blockers.append("provider_coverage_review_missing")
        payload: dict[str, Any] = {
            "schema_version": _INGESTION_AUDIT_SCHEMA,
            "receipt_id": self.receipt_id,
            "receipt_sha256": self.receipt_sha256,
            "source_id": self.source_id,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "provider_event_count": self.source_event_count,
            "ingested_event_count": len(keys),
            "unique_ingested_event_count": len(unique_keys),
            "ingested_event_manifest_sha256": _sha({"event_keys": unique_keys}),
            "blockers": sorted(set(blockers)),
            "complete": not blockers,
        }
        payload["audit_sha256"] = _sha(payload)
        return payload


class NewsHistoryCoverageStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """create table if not exists news_history_coverage_receipts (
                    receipt_id text primary key, source_id text not null,
                    receipt_sha256 text not null unique, receipt_json text not null,
                    persisted_at text not null)"""
            )
            for action in ("update", "delete"):
                connection.execute(
                    f"""create trigger if not exists news_history_coverage_receipts_immutable_{action}
                    before {action} on news_history_coverage_receipts
                    begin select raise(abort, 'news history coverage receipts are immutable'); end"""
                )
            connection.execute(
                """create table if not exists news_history_ingestion_audits (
                    audit_sha256 text primary key, source_id text not null,
                    audit_json text not null, persisted_at text not null)"""
            )
            for action in ("update", "delete"):
                connection.execute(
                    f"""create trigger if not exists news_history_ingestion_audits_immutable_{action}
                    before {action} on news_history_ingestion_audits
                    begin select raise(abort, 'news history ingestion audits are immutable'); end"""
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, receipt: NewsHistoryCoverageReceipt) -> NewsHistoryCoverageReceipt:
        receipt.verify()
        payload = _json(receipt.model_dump())
        with self._connect() as connection:
            existing = connection.execute("select receipt_json from news_history_coverage_receipts where receipt_id=?", (receipt.receipt_id,)).fetchone()
            if existing is not None:
                if str(existing["receipt_json"]) != payload:
                    raise ValueError("news coverage receipt ID is bound to different evidence")
                return receipt
            connection.execute(
                "insert into news_history_coverage_receipts (receipt_id,source_id,receipt_sha256,receipt_json,persisted_at) values (?,?,?,?,?)",
                (receipt.receipt_id, receipt.source_id, receipt.receipt_sha256, payload, datetime.now(timezone.utc).isoformat()),
            )
        return receipt

    def covering_receipt(self, *, source_id: str, published_at: str) -> NewsHistoryCoverageReceipt | None:
        matches = [item for item in self.receipts() if item.covers(source_id=source_id, published_at=published_at)]
        if len(matches) > 1:
            raise ValueError("news coverage receipts are ambiguous")
        return matches[0] if matches else None

    def receipts(self) -> list[NewsHistoryCoverageReceipt]:
        with self._connect() as connection:
            rows = connection.execute("select receipt_json from news_history_coverage_receipts order by receipt_id").fetchall()
        return [NewsHistoryCoverageReceipt.model_validate(json.loads(str(row["receipt_json"]))) for row in rows]

    def record_ingestion_audit(self, audit: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the exact event-set completeness decision for one source."""

        payload = dict(audit)
        audit_hash = str(payload.pop("audit_sha256", "")).lower()
        _validate_ingestion_audit_payload(payload)
        if len(audit_hash) != 64 or audit_hash != _sha(payload):
            raise ValueError("news ingestion audit hash mismatch")
        stored = {**payload, "audit_sha256": audit_hash}
        if stored["complete"] is True:
            receipt_id = str(stored.get("receipt_id") or "")
            receipt = next((item for item in self.receipts() if item.receipt_id == receipt_id), None)
            if (
                receipt is None
                or not receipt.reviewed_provider_coverage
                or not _audit_matches_receipt(stored, receipt)
            ):
                raise ValueError("complete news ingestion audit must bind a locally stored coverage receipt")
        serialized = _json(stored)
        with self._connect() as connection:
            existing = connection.execute(
                "select audit_json from news_history_ingestion_audits where audit_sha256=?",
                (audit_hash,),
            ).fetchone()
            if existing is not None:
                if str(existing["audit_json"]) != serialized:
                    raise ValueError("news ingestion audit hash is bound to different evidence")
                return stored
            connection.execute(
                """
                insert into news_history_ingestion_audits(
                    audit_sha256, source_id, audit_json, persisted_at
                ) values (?, ?, ?, ?)
                """,
                (
                    audit_hash,
                    str(payload.get("source_id") or ""),
                    serialized,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return stored

    def ingestion_audits(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select audit_json from news_history_ingestion_audits order by persisted_at"
            ).fetchall()
        return [json.loads(str(row["audit_json"])) for row in rows]

    def status_summary(self) -> dict[str, Any]:
        """Return a conservative, read-only PIT eligibility projection.

        This is deliberately a per-source view of the latest immutable
        ingestion audit.  It does not certify that a provider's whole history
        has been acquired: that requires reviewed provider coverage evidence
        outside this local store.
        """

        receipts = {item.receipt_id: item for item in self.receipts()}
        latest_audits: dict[str, dict[str, Any]] = {}
        for audit in self.ingestion_audits():
            source_id = str(audit.get("source_id") or "").strip()
            if source_id:
                latest_audits[source_id] = audit

        source_ids = sorted({item.source_id for item in receipts.values()} | set(latest_audits))
        sources: list[dict[str, Any]] = []
        for source_id in source_ids:
            audit = latest_audits.get(source_id)
            receipt = None
            blockers: list[str] = []
            if audit is not None:
                receipt = receipts.get(str(audit.get("receipt_id") or ""))
                try:
                    payload = dict(audit)
                    audit_hash = str(payload.pop("audit_sha256", "")).lower()
                    _validate_ingestion_audit_payload(payload)
                    if audit_hash != _sha(payload):
                        blockers.append("ingestion_audit_hash_invalid")
                except ValueError:
                    blockers.append("ingestion_audit_payload_invalid")
                blockers.extend(str(item) for item in audit.get("blockers", []) if str(item))
                if receipt is None or not _audit_matches_receipt(audit, receipt):
                    blockers.append("coverage_receipt_not_bound_to_latest_audit")
            else:
                matching = [item for item in receipts.values() if item.source_id == source_id]
                receipt = matching[-1] if matching else None
                blockers.append("ingestion_coverage_audit_missing")

            eligible = bool(
                audit
                and audit.get("complete") is True
                and not blockers
                and receipt is not None
                and receipt.reviewed_provider_coverage
            )
            status = "pit_replay_eligible" if eligible else (
                "receipt_recorded_waiting_ingestion_audit" if audit is None and receipt else "pit_replay_blocked"
            )
            sources.append(
                {
                    "source_id": source_id,
                    "status": status,
                    "latest_ingestion_pit_eligible": eligible,
                    "blockers": sorted(set(blockers)),
                    "receipt": (
                        {
                            "receipt_id": receipt.receipt_id,
                            "receipt_sha256": receipt.receipt_sha256,
                            "coverage_start": receipt.coverage_start,
                            "coverage_end": receipt.coverage_end,
                            "source_event_count": receipt.source_event_count,
                            "original_publication_timestamps_complete": receipt.original_publication_timestamps_complete,
                            "reviewed_provider_coverage": receipt.reviewed_provider_coverage,
                        }
                        if receipt is not None
                        else None
                    ),
                    "latest_ingestion_audit": audit,
                }
            )

        return {
            "schema_version": "stock_ai.news_history_coverage_status.v1",
            "coverage_receipt_required_for_pit": True,
            "provider_wide_historical_coverage_certified": False,
            "provider_wide_blocker": "reviewed_provider_wide_historical_coverage_receipt_missing",
            "receipt_count": len(receipts),
            "audit_count": len(latest_audits),
            "verified_source_count": sum(item["latest_ingestion_pit_eligible"] for item in sources),
            "source_count": len(sources),
            "sources": sources,
            "message": "Only a source's latest immutable ingestion audit is shown. A displayed news item is not historical PIT evidence unless this audit is eligible.",
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: dict[str, Any]) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: str | None) -> bool:
    raw = str(value or "").lower()
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


def _audit_matches_receipt(audit: Mapping[str, Any], receipt: NewsHistoryCoverageReceipt) -> bool:
    return (
        str(audit.get("receipt_id") or "") == receipt.receipt_id
        and str(audit.get("receipt_sha256") or "").lower() == receipt.receipt_sha256
        and str(audit.get("source_id") or "") == receipt.source_id
        and str(audit.get("coverage_start") or "") == receipt.coverage_start
        and str(audit.get("coverage_end") or "") == receipt.coverage_end
        and audit.get("provider_event_count") == receipt.source_event_count
    )


def _validate_ingestion_audit_payload(payload: Mapping[str, Any]) -> None:
    """Reject internally contradictory completeness evidence before persistence.

    The audit hash proves that a payload was not changed after it was issued; it
    does not prove that the payload was meaningful when it was issued.  Validate
    the semantic invariants here so a caller cannot persist an audit that claims
    completion while omitting a provider receipt, mismatching event counts, or
    carrying an inconsistent blocker list.
    """

    if payload.get("schema_version") != _INGESTION_AUDIT_SCHEMA:
        raise ValueError("invalid news ingestion audit schema")
    if not str(payload.get("source_id") or "").strip():
        raise ValueError("news ingestion audit requires source ID")

    provider_count = payload.get("provider_event_count")
    if provider_count is not None and type(provider_count) is not int:
        raise ValueError("news ingestion provider event count must be an integer or null")
    if provider_count is not None and provider_count < 0:
        raise ValueError("news ingestion provider event count cannot be negative")

    for field in ("ingested_event_count", "unique_ingested_event_count"):
        value = payload.get(field)
        if type(value) is not int or value < 0:
            raise ValueError(f"news ingestion {field} must be a non-negative integer")
    if payload["unique_ingested_event_count"] > payload["ingested_event_count"]:
        raise ValueError("news ingestion unique event count cannot exceed ingested count")

    manifest_hash = str(payload.get("ingested_event_manifest_sha256") or "").lower()
    if len(manifest_hash) != 64:
        raise ValueError("news ingestion audit requires an event manifest SHA-256")
    blockers = payload.get("blockers")
    if not isinstance(blockers, list) or any(not isinstance(item, str) or not item for item in blockers):
        raise ValueError("news ingestion audit blockers must be a list of non-empty strings")
    if blockers != sorted(set(blockers)):
        raise ValueError("news ingestion audit blockers must be sorted and unique")
    complete = payload.get("complete")
    if type(complete) is not bool or complete != (not blockers):
        raise ValueError("news ingestion audit completion must match its blockers")

    if provider_count is None:
        if complete or "coverage_receipt_missing_or_ambiguous" not in blockers:
            raise ValueError("news ingestion without a provider receipt must fail closed")
    elif provider_count != payload["unique_ingested_event_count"] and (
        "provider_event_count_does_not_match_ingested_event_count" not in blockers
    ):
        raise ValueError("news ingestion count mismatch must be recorded as a blocker")

    if complete:
        receipt_id = str(payload.get("receipt_id") or "").strip()
        receipt_hash = str(payload.get("receipt_sha256") or "").lower()
        if not receipt_id or len(receipt_hash) != 64:
            raise ValueError("complete news ingestion audit requires its coverage receipt")
