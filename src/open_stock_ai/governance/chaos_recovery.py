"""Deterministic staging-chaos catalog and recovery receipt contract."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


CHAOS_SCENARIOS = ("process_kill", "network_partition", "disk_full", "timeout")


@dataclass(frozen=True, slots=True)
class ChaosRecoveryReceipt:
    scenario: str
    seed: int
    status: str
    invariants: dict[str, bool]
    recovery: dict[str, Any]
    receipt_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.chaos_recovery_receipt.v1",
            "scenario": self.scenario,
            "seed": self.seed,
            "status": self.status,
            "invariants": self.invariants,
            "recovery": self.recovery,
        }

    def verify(self) -> bool:
        return hashlib.sha256(_canonical(self.payload())).hexdigest() == self.receipt_sha256

    def as_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_sha256": self.receipt_sha256}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChaosRecoveryReceipt":
        receipt = cls(
            scenario=str(payload["scenario"]),
            seed=int(payload["seed"]),
            status=str(payload["status"]),
            invariants={str(key): bool(value) for key, value in dict(payload["invariants"]).items()},
            recovery=dict(payload["recovery"]),
            receipt_sha256=str(payload["receipt_sha256"]),
        )
        if receipt.scenario not in CHAOS_SCENARIOS:
            raise ValueError("unknown chaos scenario in receipt")
        if receipt.status not in {"recovered", "blocked"} or not receipt.verify():
            raise ValueError("invalid chaos recovery receipt")
        return receipt


class DurableChaosRecoveryStore:
    """Append-only proof that a safe, deterministic chaos campaign recovered.

    This store intentionally records only recovery evidence produced elsewhere.
    Reading or recording it never kills a process, partitions a network, or
    consumes disk.  Those destructive staging exercises remain an explicit
    operator action outside the desktop application.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma journal_mode=WAL")
        connection.execute("pragma synchronous=FULL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists chaos_recovery_receipts (
                    receipt_sha256 text primary key,
                    scenario text not null,
                    seed integer not null,
                    status text not null,
                    invariants_json text not null,
                    recovery_json text not null,
                    recorded_at text not null
                );
                create index if not exists idx_chaos_recovery_receipts_recorded
                    on chaos_recovery_receipts(recorded_at desc);
                create trigger if not exists chaos_recovery_receipts_immutable_update
                    before update on chaos_recovery_receipts
                    begin select raise(abort, 'chaos recovery receipts are append-only'); end;
                create trigger if not exists chaos_recovery_receipts_immutable_delete
                    before delete on chaos_recovery_receipts
                    begin select raise(abort, 'chaos recovery receipts are append-only'); end;
                """
            )
            connection.commit()

    def record(self, receipt: ChaosRecoveryReceipt, *, recorded_at: str | None = None) -> bool:
        if not receipt.verify():
            raise ValueError("cannot persist an invalid chaos recovery receipt")
        timestamp = recorded_at or datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """insert or ignore into chaos_recovery_receipts(
                    receipt_sha256, scenario, seed, status, invariants_json,
                    recovery_json, recorded_at
                ) values (?, ?, ?, ?, ?, ?, ?)""",
                (
                    receipt.receipt_sha256,
                    receipt.scenario,
                    receipt.seed,
                    receipt.status,
                    _json(receipt.invariants),
                    _json(receipt.recovery),
                    timestamp,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def receipts(self, *, limit: int = 100) -> list[ChaosRecoveryReceipt]:
        bounded = max(1, min(int(limit), 1_000))
        with self._connect() as connection:
            rows = connection.execute(
                """select scenario, seed, status, invariants_json, recovery_json,
                          receipt_sha256
                   from chaos_recovery_receipts
                   order by recorded_at desc, receipt_sha256 desc limit ?""",
                (bounded,),
            ).fetchall()
        return [
            ChaosRecoveryReceipt.from_dict(
                {
                    "scenario": row["scenario"],
                    "seed": row["seed"],
                    "status": row["status"],
                    "invariants": _load_json(row["invariants_json"]),
                    "recovery": _load_json(row["recovery_json"]),
                    "receipt_sha256": row["receipt_sha256"],
                }
            )
            for row in rows
        ]


class ChaosRecoveryCatalog:
    """Run an injected scenario callback without allowing missing recovery proof to pass."""

    def __init__(self, *, seed: int = 105, clock: Callable[[], str] | None = None) -> None:
        if seed < 0:
            raise ValueError("chaos seed cannot be negative")
        self.seed = seed
        self.clock = clock or (lambda: "")
        self.receipts: list[ChaosRecoveryReceipt] = []

    def run(self, scenario: str, recovery: Mapping[str, Any] | None) -> ChaosRecoveryReceipt:
        if scenario not in CHAOS_SCENARIOS:
            raise ValueError("unknown chaos scenario")
        observed = dict(recovery or {})
        raw_invariants = observed.get("invariants")
        invariants = (
            {str(key): bool(value) for key, value in raw_invariants.items()}
            if isinstance(raw_invariants, Mapping)
            else {}
        )
        passed = bool(invariants) and all(invariants.values())
        payload = {
            "schema_version": "open_stock_ai.chaos_recovery_receipt.v1",
            "scenario": scenario,
            "seed": self.seed,
            "status": "recovered" if passed else "blocked",
            "invariants": invariants,
            "recovery": {"observed_at": str(self.clock()), **observed},
        }
        receipt = ChaosRecoveryReceipt(
            scenario=scenario,
            seed=self.seed,
            status=payload["status"],
            invariants=invariants,
            recovery=payload["recovery"],
            receipt_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
        )
        self.receipts.append(receipt)
        return receipt

    def run_catalog(self, recoveries: Mapping[str, Mapping[str, Any]]) -> tuple[ChaosRecoveryReceipt, ...]:
        return tuple(self.run(scenario, recoveries.get(scenario)) for scenario in CHAOS_SCENARIOS)

    def complete(self) -> bool:
        return len(self.receipts) == len(CHAOS_SCENARIOS) and all(
            receipt.scenario in CHAOS_SCENARIOS and receipt.status == "recovered" and receipt.verify()
            for receipt in self.receipts
        )


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load_json(value: str) -> dict[str, Any]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("chaos recovery receipt payload must be an object")
    return payload
