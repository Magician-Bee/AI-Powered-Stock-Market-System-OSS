from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .governance import (
    ConsolidationResult,
    MemoryAuditEntry,
    MemoryCandidate,
    MemoryDecision,
    MemoryRecord,
    MemoryStatus,
    _is_expired,
    _normalize,
)
from .policies import MemoryLayer, preference_semantics
from .store import MemoryStore
from .trust import assess_memory_candidate


class DurableMemoryGovernanceEngine:
    """SQLite-backed P43-P46 candidate, conflict and lesson governance.

    Candidate decisions and procedural observations are committed in the same
    transaction as the resulting memory. Reopening this class, including from a
    different process, therefore preserves the gate and conflict audit state.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        namespace: str,
        save_threshold: float = 0.55,
        minimum_distinct_runs: int = 2,
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.save_threshold = float(save_threshold)
        self.minimum_distinct_runs = max(2, int(minimum_distinct_runs))

    def consider(self, candidate: MemoryCandidate) -> ConsolidationResult:
        candidate_id = f"MCAN-{uuid4().hex}"
        now = _now()
        payload = _candidate_payload(candidate)
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            session_id = _existing_id(conn, "agent_sessions", "session_id", candidate.session_id)
            run_id = _existing_id(conn, "agent_runs", "run_id", candidate.run_id)
            conn.execute(
                """
                insert into agent_memory_candidates(
                    candidate_id, namespace, session_id, run_id, kind, status,
                    importance, future_relevance, confidence, durability,
                    created_at, decided_at, payload_json
                ) values (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, null, ?)
                """,
                (
                    candidate_id,
                    self.namespace,
                    session_id,
                    run_id,
                    candidate.layer.value,
                    candidate.importance,
                    candidate.future_relevance,
                    candidate.confidence,
                    candidate.durability,
                    candidate.created_at,
                    _json(payload),
                ),
            )

            if _is_expired(candidate.expires_at, datetime.now(timezone.utc)):
                return self._finish(
                    conn,
                    candidate_id,
                    candidate,
                    MemoryDecision.EXPIRE,
                    "candidate_expired",
                    payload=payload,
                    decided_at=now,
                )

            trust = assess_memory_candidate(candidate)
            if trust.quarantine:
                record = self._insert_memory(
                    conn,
                    candidate_id,
                    candidate,
                    MemoryDecision.QUARANTINE,
                    session_id=session_id,
                    run_id=run_id,
                )
                return self._finish(
                    conn,
                    candidate_id,
                    candidate,
                    MemoryDecision.QUARANTINE,
                    trust.reason or "memory_candidate_quarantined",
                    payload=payload,
                    decided_at=now,
                    record=record,
                )

            if candidate.layer is MemoryLayer.PROCEDURAL:
                eligible = self._observe_procedural(
                    conn,
                    candidate_id=candidate_id,
                    candidate=candidate,
                    now=now,
                )
                if not eligible:
                    return self._finish(
                        conn,
                        candidate_id,
                        candidate,
                        MemoryDecision.REJECT,
                        "procedural_requires_repeated_runs_or_host_verification",
                        payload=payload,
                        decided_at=now,
                    )

            if candidate.durability == "long_term" and candidate.score < self.save_threshold:
                return self._finish(
                    conn,
                    candidate_id,
                    candidate,
                    MemoryDecision.REJECT,
                    "candidate_score_below_threshold",
                    payload=payload,
                    decided_at=now,
                )

            active = self._active_for_key(conn, candidate)
            if active and _normalize(active.content) == _normalize(candidate.content):
                previous = self._deactivate(conn, active, MemoryStatus.SUPERSEDED, now)
                record = self._insert_memory(
                    conn,
                    candidate_id,
                    candidate,
                    MemoryDecision.MERGE,
                    confidence=max(active.confidence, candidate.confidence),
                    supersedes_id=active.memory_id,
                    merged_from_ids=(*active.merged_from_ids, active.memory_id),
                    session_id=session_id,
                    run_id=run_id,
                )
                self._insert_supersession(
                    conn,
                    old_memory_id=active.memory_id,
                    new_memory_id=record.memory_id,
                    candidate_id=candidate_id,
                    reason="same_memory_reconfirmed",
                    now=now,
                )
                result = self._finish(
                    conn,
                    candidate_id,
                    candidate,
                    MemoryDecision.MERGE,
                    "same_memory_reconfirmed",
                    payload=payload,
                    decided_at=now,
                    record=record,
                    previous=previous,
                )
                self._mark_lesson_promoted(conn, candidate, record.memory_id, now)
                return result

            if active:
                conflict_id = self._insert_conflict(
                    conn,
                    candidate_id=candidate_id,
                    candidate=candidate,
                    existing=active,
                    now=now,
                )
                explicit = candidate.source_type in {
                    "user_explicit",
                    "user_instruction",
                    "user_correction",
                }
                if explicit or candidate.score > active.score:
                    previous = self._deactivate(conn, active, MemoryStatus.SUPERSEDED, now)
                    record = self._insert_memory(
                        conn,
                        candidate_id,
                        candidate,
                        MemoryDecision.SUPERSEDE,
                        supersedes_id=active.memory_id,
                        session_id=session_id,
                        run_id=run_id,
                    )
                    self._resolve_conflict(
                        conn,
                        conflict_id,
                        status="resolved_superseded",
                        resulting_memory_id=record.memory_id,
                        now=now,
                    )
                    self._insert_supersession(
                        conn,
                        old_memory_id=active.memory_id,
                        new_memory_id=record.memory_id,
                        candidate_id=candidate_id,
                        reason="explicit_or_higher_confidence_conflict",
                        now=now,
                    )
                    result = self._finish(
                        conn,
                        candidate_id,
                        candidate,
                        MemoryDecision.SUPERSEDE,
                        "explicit_or_higher_confidence_conflict",
                        payload=payload,
                        decided_at=now,
                        record=record,
                        previous=previous,
                        conflict_id=conflict_id,
                    )
                    self._mark_lesson_promoted(conn, candidate, record.memory_id, now)
                    return result

                self._resolve_conflict(
                    conn,
                    conflict_id,
                    status="resolved_preserved_existing",
                    resulting_memory_id=active.memory_id,
                    now=now,
                )
                return self._finish(
                    conn,
                    candidate_id,
                    candidate,
                    MemoryDecision.REJECT,
                    "lower_confidence_conflict_preserved_existing_memory",
                    payload=payload,
                    decided_at=now,
                    previous=active,
                    conflict_id=conflict_id,
                )

            record = self._insert_memory(
                conn,
                candidate_id,
                candidate,
                MemoryDecision.SAVE,
                session_id=session_id,
                run_id=run_id,
            )
            result = self._finish(
                conn,
                candidate_id,
                candidate,
                MemoryDecision.SAVE,
                "candidate_accepted",
                payload=payload,
                decided_at=now,
                record=record,
            )
            self._mark_lesson_promoted(conn, candidate, record.memory_id, now)
            return result

    @property
    def audit_log(self) -> tuple[MemoryAuditEntry, ...]:
        entries: list[MemoryAuditEntry] = []
        for item in self.candidates():
            decision = item.get("decision") or {}
            if not decision.get("value"):
                continue
            entries.append(
                MemoryAuditEntry(
                    audit_id=str(decision.get("audit_id") or item["candidate_id"]),
                    decision=MemoryDecision(str(decision["value"])),
                    candidate_fingerprint=str(item.get("fingerprint") or ""),
                    reason=str(decision.get("reason") or ""),
                    previous_memory_id=decision.get("previous_memory_id"),
                    resulting_memory_id=decision.get("resulting_memory_id"),
                    created_at=str(item.get("decided_at") or item["created_at"]),
                )
            )
        return tuple(entries)

    def records(self, *, include_inactive: bool = True) -> tuple[MemoryRecord, ...]:
        where = "namespace=?"
        if not include_inactive:
            where += " and archived_at is null"
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"select * from agent_memory where {where} order by created_at",
                (self.namespace,),
            ).fetchall()
        records = []
        for row in rows:
            source = _loads(row["source_json"])
            if source.get("governance"):
                records.append(_record_from_row(row))
        return tuple(records)

    def candidates(self, *, status: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [self.namespace]
        where = "namespace=?"
        if status:
            where += " and status=?"
            params.append(status)
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"select * from agent_memory_candidates where {where} order by created_at, candidate_id",
                params,
            ).fetchall()
        return [_candidate_row(row) for row in rows]

    def conflicts(self) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select c.* from agent_memory_conflicts c
                join agent_memory_candidates m on m.candidate_id=c.candidate_id
                where m.namespace=? order by c.created_at, c.conflict_id
                """,
                (self.namespace,),
            ).fetchall()
        return [_payload_row(row) for row in rows]

    def supersessions(self) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select s.* from agent_memory_supersession s
                join agent_memory old on old.memory_id=s.old_memory_id
                where old.namespace=? order by s.created_at, s.supersession_id
                """,
                (self.namespace,),
            ).fetchall()
        return [dict(row) for row in rows]

    def procedural_lessons(self) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from agent_procedural_lessons
                where namespace=? order by created_at, lesson_id
                """,
                (self.namespace,),
            ).fetchall()
        return [_payload_row(row) for row in rows]

    def expire_due(self, *, at: datetime | None = None) -> tuple[MemoryRecord, ...]:
        now_dt = at or datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expired: list[MemoryRecord] = []
        with self.store._connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("begin immediate")
            rows = conn.execute(
                """
                select * from agent_memory
                where namespace=? and archived_at is null and expires_at is not null
                """,
                (self.namespace,),
            ).fetchall()
            for row in rows:
                if not _is_expired(row["expires_at"], now_dt):
                    continue
                if not _loads(row["source_json"]).get("governance"):
                    continue
                record = _record_from_row(row)
                expired_record = self._deactivate(conn, record, MemoryStatus.EXPIRED, now)
                candidate = MemoryCandidate(
                    content=record.content,
                    kind=record.layer,
                    importance=record.score,
                    future_relevance=record.score,
                    confidence=record.confidence,
                    source=record.source,
                    durability=record.durability,
                    fingerprint=record.fingerprint,
                    semantic_key=record.semantic_key,
                )
                candidate_id = f"MCAN-{uuid4().hex}"
                payload = _candidate_payload(candidate)
                conn.execute(
                    """
                    insert into agent_memory_candidates(
                        candidate_id, namespace, kind, status, importance,
                        future_relevance, confidence, durability, created_at,
                        decided_at, payload_json
                    ) values (?, ?, ?, 'expire', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        self.namespace,
                        record.layer.value,
                        record.score,
                        record.score,
                        record.confidence,
                        record.durability,
                        now,
                        now,
                        _json(
                            {
                                **payload,
                                "decision": {
                                    "audit_id": f"MAUD-{uuid4().hex}",
                                    "value": "expire",
                                    "reason": "stored_memory_expired",
                                    "previous_memory_id": record.memory_id,
                                    "resulting_memory_id": None,
                                },
                            }
                        ),
                    ),
                )
                expired.append(expired_record)
            conn.commit()
        return tuple(expired)

    def _observe_procedural(
        self,
        conn: sqlite3.Connection,
        *,
        candidate_id: str,
        candidate: MemoryCandidate,
        now: str,
    ) -> bool:
        row = conn.execute(
            """
            select * from agent_procedural_lessons
            where namespace=? and fingerprint=?
            order by created_at limit 1
            """,
            (self.namespace, candidate.resolved_fingerprint),
        ).fetchone()
        payload = _loads(row["payload_json"]) if row else {}
        run_ids = set(str(item) for item in payload.get("run_ids", []) if item)
        candidate_ids = list(payload.get("candidate_ids", []))
        if candidate.run_id:
            run_ids.add(candidate.run_id)
        candidate_ids.append(candidate_id)
        host_verified = bool(candidate.host_verified or (row and row["verified_by_host"]))
        occurrence_count = len(run_ids)
        eligible = host_verified or occurrence_count >= self.minimum_distinct_runs
        payload.update(
            {
                "fingerprint": candidate.resolved_fingerprint,
                "content": candidate.content,
                "semantic_key": candidate.resolved_semantic_key,
                "run_ids": sorted(run_ids),
                "candidate_ids": candidate_ids,
                "minimum_distinct_runs": self.minimum_distinct_runs,
                "eligible": eligible,
            }
        )
        if row:
            conn.execute(
                """
                update agent_procedural_lessons
                set status=?, verified_by_host=?, occurrence_count=?,
                    updated_at=?, payload_json=?
                where lesson_id=?
                """,
                (
                    "eligible" if eligible else "observed",
                    int(host_verified),
                    occurrence_count,
                    now,
                    _json(payload),
                    row["lesson_id"],
                ),
            )
        else:
            conn.execute(
                """
                insert into agent_procedural_lessons(
                    lesson_id, namespace, fingerprint, status, verified_by_host,
                    occurrence_count, created_at, updated_at, payload_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"MPRO-{uuid4().hex}",
                    self.namespace,
                    candidate.resolved_fingerprint,
                    "eligible" if eligible else "observed",
                    int(host_verified),
                    occurrence_count,
                    now,
                    now,
                    _json(payload),
                ),
            )
        return eligible

    def _mark_lesson_promoted(
        self,
        conn: sqlite3.Connection,
        candidate: MemoryCandidate,
        memory_id: str,
        now: str,
    ) -> None:
        if candidate.layer is not MemoryLayer.PROCEDURAL:
            return
        row = conn.execute(
            """
            select * from agent_procedural_lessons
            where namespace=? and fingerprint=? order by created_at limit 1
            """,
            (self.namespace, candidate.resolved_fingerprint),
        ).fetchone()
        if not row:
            return
        payload = _loads(row["payload_json"])
        payload["memory_id"] = memory_id
        payload["promoted_at"] = now
        conn.execute(
            """
            update agent_procedural_lessons
            set status='promoted', updated_at=?, payload_json=? where lesson_id=?
            """,
            (now, _json(payload), row["lesson_id"]),
        )

    def _active_for_key(
        self,
        conn: sqlite3.Connection,
        candidate: MemoryCandidate,
    ) -> MemoryRecord | None:
        rows = conn.execute(
            """
            select * from agent_memory
            where namespace=? and kind=? and archived_at is null
            order by created_at desc
            """,
            (self.namespace, candidate.layer.value),
        ).fetchall()
        for row in rows:
            governance = _loads(row["source_json"]).get("governance") or {}
            if (
                governance.get("status") == MemoryStatus.ACTIVE.value
                and governance.get("semantic_key") == candidate.resolved_semantic_key
            ):
                return _record_from_row(row)
        return None

    def _insert_memory(
        self,
        conn: sqlite3.Connection,
        candidate_id: str,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
        *,
        confidence: float | None = None,
        supersedes_id: str | None = None,
        merged_from_ids: tuple[str, ...] = (),
        session_id: str | None,
        run_id: str | None,
    ) -> MemoryRecord:
        memory_id = f"AMEM-{uuid4().hex}"
        now = _now()
        raw_source = {"type": candidate.source} if isinstance(candidate.source, str) else dict(candidate.source)
        raw_source.setdefault("type", "unknown")
        trust = assess_memory_candidate(candidate)
        advisory = candidate.layer is MemoryLayer.USER_PREFERENCE
        source = preference_semantics(raw_source) if advisory else raw_source
        source = {
            **source,
            "governance": {
                "candidate_id": candidate_id,
                "decision": decision.value,
                "status": (
                    MemoryStatus.QUARANTINED.value
                    if decision is MemoryDecision.QUARANTINE
                    else MemoryStatus.ACTIVE.value
                ),
                "score": candidate.score,
                "confidence": candidate.confidence if confidence is None else confidence,
                "durability": candidate.durability,
                "semantic_key": candidate.resolved_semantic_key,
                "fingerprint": candidate.resolved_fingerprint,
                "supersedes_id": supersedes_id,
                "merged_from_ids": list(merged_from_ids),
                "advisory": advisory,
                "evidence_trust": trust.score,
                "provenance": trust.provenance,
                "quarantine_reason": trust.reason,
            },
        }
        conn.execute(
            """
            insert into agent_memory(
                memory_id, namespace, session_id, run_id, kind, fact_type,
                content, source_json, created_at, updated_at, expires_at, archived_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                self.namespace,
                session_id,
                run_id,
                candidate.layer.value,
                _fact_type(candidate.layer),
                candidate.content.strip(),
                _json(source),
                now,
                now,
                candidate.expires_at,
                now if decision is MemoryDecision.QUARANTINE else None,
            ),
        )
        return MemoryRecord(
            memory_id=memory_id,
            content=candidate.content.strip(),
            layer=candidate.layer,
            score=candidate.score,
            confidence=candidate.confidence if confidence is None else confidence,
            source=source,
            durability=candidate.durability,
            semantic_key=candidate.resolved_semantic_key,
            fingerprint=candidate.resolved_fingerprint,
            status=(
                MemoryStatus.QUARANTINED
                if decision is MemoryDecision.QUARANTINE
                else MemoryStatus.ACTIVE
            ),
            evidence_trust=trust.score,
            provenance=trust.provenance,
            quarantine_reason=trust.reason,
            advisory=advisory,
            supersedes_id=supersedes_id,
            merged_from_ids=merged_from_ids,
            created_at=now,
            expires_at=candidate.expires_at,
        )

    def _deactivate(
        self,
        conn: sqlite3.Connection,
        record: MemoryRecord,
        status: MemoryStatus,
        now: str,
    ) -> MemoryRecord:
        row = conn.execute(
            "select source_json from agent_memory where memory_id=?",
            (record.memory_id,),
        ).fetchone()
        source = _loads(row["source_json"])
        governance = dict(source.get("governance") or {})
        governance.update({"status": status.value, "status_changed_at": now})
        source["governance"] = governance
        conn.execute(
            """
            update agent_memory
            set archived_at=?, updated_at=?, source_json=? where memory_id=?
            """,
            (now, now, _json(source), record.memory_id),
        )
        return MemoryRecord(
            memory_id=record.memory_id,
            content=record.content,
            layer=record.layer,
            score=record.score,
            confidence=record.confidence,
            source=source,
            durability=record.durability,
            semantic_key=record.semantic_key,
            fingerprint=record.fingerprint,
            status=status,
            advisory=record.advisory,
            supersedes_id=record.supersedes_id,
            merged_from_ids=record.merged_from_ids,
            created_at=record.created_at,
            status_changed_at=now,
            expires_at=record.expires_at,
        )

    def _insert_conflict(
        self,
        conn: sqlite3.Connection,
        *,
        candidate_id: str,
        candidate: MemoryCandidate,
        existing: MemoryRecord,
        now: str,
    ) -> str:
        conflict_id = f"MCON-{uuid4().hex}"
        conn.execute(
            """
            insert into agent_memory_conflicts(
                conflict_id, candidate_id, existing_memory_id, status,
                created_at, resolved_at, payload_json
            ) values (?, ?, ?, 'open', ?, null, ?)
            """,
            (
                conflict_id,
                candidate_id,
                existing.memory_id,
                now,
                _json(
                    {
                        "semantic_key": candidate.resolved_semantic_key,
                        "existing_content": existing.content,
                        "candidate_content": candidate.content,
                        "candidate_source_type": candidate.source_type,
                    }
                ),
            ),
        )
        return conflict_id

    def _resolve_conflict(
        self,
        conn: sqlite3.Connection,
        conflict_id: str,
        *,
        status: str,
        resulting_memory_id: str,
        now: str,
    ) -> None:
        row = conn.execute(
            "select payload_json from agent_memory_conflicts where conflict_id=?",
            (conflict_id,),
        ).fetchone()
        payload = _loads(row["payload_json"])
        payload["resulting_memory_id"] = resulting_memory_id
        conn.execute(
            """
            update agent_memory_conflicts
            set status=?, resolved_at=?, payload_json=? where conflict_id=?
            """,
            (status, now, _json(payload), conflict_id),
        )

    def _insert_supersession(
        self,
        conn: sqlite3.Connection,
        *,
        old_memory_id: str,
        new_memory_id: str,
        candidate_id: str,
        reason: str,
        now: str,
    ) -> None:
        conn.execute(
            """
            insert into agent_memory_supersession(
                supersession_id, old_memory_id, new_memory_id,
                candidate_id, created_at, reason
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                f"MSUP-{uuid4().hex}",
                old_memory_id,
                new_memory_id,
                candidate_id,
                now,
                reason,
            ),
        )

    def _finish(
        self,
        conn: sqlite3.Connection,
        candidate_id: str,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
        reason: str,
        *,
        payload: dict[str, Any],
        decided_at: str,
        record: MemoryRecord | None = None,
        previous: MemoryRecord | None = None,
        conflict_id: str | None = None,
    ) -> ConsolidationResult:
        audit = MemoryAuditEntry(
            audit_id=f"MAUD-{uuid4().hex}",
            decision=decision,
            candidate_fingerprint=candidate.resolved_fingerprint,
            reason=reason,
            previous_memory_id=previous.memory_id if previous else None,
            resulting_memory_id=record.memory_id if record else None,
            created_at=decided_at,
        )
        payload = {
            **payload,
            "decision": {
                "audit_id": audit.audit_id,
                "value": decision.value,
                "reason": reason,
                "previous_memory_id": audit.previous_memory_id,
                "resulting_memory_id": audit.resulting_memory_id,
                "conflict_id": conflict_id,
            },
        }
        conn.execute(
            """
            update agent_memory_candidates
            set status=?, decided_at=?, payload_json=? where candidate_id=?
            """,
            (decision.value, decided_at, _json(payload), candidate_id),
        )
        return ConsolidationResult(
            decision=decision,
            reason=reason,
            record=record,
            previous_record=previous,
            audit=audit,
            candidate_id=candidate_id,
            conflict_id=conflict_id,
        )


def _candidate_payload(candidate: MemoryCandidate) -> dict[str, Any]:
    return {
        "content": candidate.content,
        "kind": candidate.layer.value,
        "importance": candidate.importance,
        "future_relevance": candidate.future_relevance,
        "confidence": candidate.confidence,
        "source": candidate.source,
        "source_type": candidate.source_type,
        "durability": candidate.durability,
        "fingerprint": candidate.resolved_fingerprint,
        "semantic_key": candidate.resolved_semantic_key,
        "score": candidate.score,
        "run_id": candidate.run_id,
        "session_id": candidate.session_id,
        "host_verified": candidate.host_verified,
        "evidence_trust": candidate.evidence_trust,
        "provenance": candidate.provenance,
        "expires_at": candidate.expires_at,
    }


def _candidate_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    payload = _loads(result.pop("payload_json"))
    result.update(payload)
    return result


def _payload_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = _loads(result.pop("payload_json"))
    return result


def _record_from_row(row: sqlite3.Row) -> MemoryRecord:
    source = _loads(row["source_json"])
    governance = source.get("governance") or {}
    raw_status = governance.get("status") or (
        MemoryStatus.SUPERSEDED.value if row["archived_at"] else MemoryStatus.ACTIVE.value
    )
    return MemoryRecord(
        memory_id=str(row["memory_id"]),
        content=str(row["content"]),
        layer=MemoryLayer(str(row["kind"])),
        score=float(governance.get("score", 0.0)),
        confidence=float(governance.get("confidence", 0.0)),
        source=source,
        durability=str(governance.get("durability") or "long_term"),
        semantic_key=str(governance.get("semantic_key") or ""),
        fingerprint=str(governance.get("fingerprint") or ""),
        status=MemoryStatus(str(raw_status)),
        evidence_trust=float(governance.get("evidence_trust", 1.0)),
        provenance=dict(governance.get("provenance") or {}),
        quarantine_reason=governance.get("quarantine_reason"),
        advisory=bool(governance.get("advisory")),
        supersedes_id=governance.get("supersedes_id"),
        merged_from_ids=tuple(governance.get("merged_from_ids") or ()),
        created_at=str(row["created_at"]),
        status_changed_at=governance.get("status_changed_at"),
        expires_at=row["expires_at"],
    )


def _existing_id(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    value: str | None,
) -> str | None:
    if not value:
        return None
    row = conn.execute(
        f"select {column} from {table} where {column}=?",
        (value,),
    ).fetchone()
    return value if row else None


def _fact_type(layer: MemoryLayer) -> str:
    if layer is MemoryLayer.USER_PREFERENCE:
        return "preference"
    if layer in {MemoryLayer.WORKING, MemoryLayer.SESSION}:
        return "temporary_state"
    return "fact"


def _loads(value: str | bytes | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
