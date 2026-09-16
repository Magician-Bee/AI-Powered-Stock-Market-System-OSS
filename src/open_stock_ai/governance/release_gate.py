"""Machine-readable release gate backed by the authoritative P0–P105 ledger."""

from __future__ import annotations

import argparse
import json
from typing import Any

from open_stock_ai.governance.production_requirements import production_requirement_status


SCHEMA_VERSION = "stock_ai.release_gate.v1"


class ReleaseGateFailure(RuntimeError):
    """Raised when a requested release gate is not satisfied."""


def evaluate_release_gate(gate_name: str = "full_release") -> dict[str, Any]:
    """Return a stable JSON decision without weakening the authoritative ledger."""
    status = production_requirement_status()
    gate = (status.get("gates") or {}).get(gate_name)
    if not isinstance(gate, dict):
        return {
            "schema_version": SCHEMA_VERSION,
            "source_of_truth": status["source_of_truth"],
            "ledger_sha256": status["ledger_sha256"],
            "gate": gate_name,
            "passed": False,
            "blocking_requirement_ids": ["unknown_gate"],
            "counts": {"complete": 0, "partial": 0, "unverified": 0, "total": 0},
        }
    requirements = [*(status.get("requirements") or []), *(status.get("release_requirements") or [])]
    counts = {
        "complete": sum(item.get("status") == "complete" for item in requirements),
        "partial": sum(item.get("status") == "partial" for item in requirements),
        "unverified": sum(item.get("status") == "unverified" for item in requirements),
        "total": len(requirements),
    }
    blocking_ids = [str(item) for item in gate.get("incomplete_requirement_ids") or []]
    return {
        "schema_version": SCHEMA_VERSION,
        "source_of_truth": status["source_of_truth"],
        "ledger_sha256": status["ledger_sha256"],
        "gate": gate_name,
        "passed": bool(gate.get("passed")) and not blocking_ids,
        "blocking_requirement_ids": blocking_ids,
        "counts": counts,
    }


def assert_release_gate(gate_name: str = "full_release") -> dict[str, Any]:
    report = evaluate_release_gate(gate_name)
    if not report["passed"]:
        blocked = ",".join(report["blocking_requirement_ids"]) or "unknown_gate"
        raise ReleaseGateFailure(f"release_gate_failed:{gate_name}:{blocked}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", default="full_release")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    report = evaluate_release_gate(args.gate)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        decision = "PASS" if report["passed"] else "BLOCKED"
        print(f"{decision} {report['gate']}: {len(report['blocking_requirement_ids'])} blocking requirements")
        if report["blocking_requirement_ids"]:
            print("blocking_requirement_ids=" + ",".join(report["blocking_requirement_ids"]))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
