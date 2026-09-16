#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from open_stock_ai.research.autonomous_oos_audit import audit_research_cycle


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Audit one retained autonomous OOS research cycle without modifying it.")
    result.add_argument("--database", required=True, type=Path)
    result.add_argument("--cycle-id")
    result.add_argument("--account-id", default="autonomous-paper-v1")
    result.add_argument("--output", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    audit = audit_research_cycle(args.database, cycle_id=args.cycle_id, account_id=args.account_id)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        key: audit[key] for key in (
            "schema_version", "cycle_id", "deep_result_count", "qualification_receipt_count",
            "integrity_passed", "research_paper_candidate_eligible_count", "positive_ev_qualified_count",
            "phases", "conclusion", "receipt_sha256",
        )
    }, ensure_ascii=False, indent=2))
    return 0 if audit["integrity_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
