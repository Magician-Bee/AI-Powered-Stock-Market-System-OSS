#!/usr/bin/env python3
"""Fail closed when CodeQL SARIF contains high or critical security findings."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _security_severity(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def inspect_sarif(paths: list[Path], *, commit_sha: str, threshold: float = 7.0) -> dict[str, Any]:
    sarif_files = sorted(
        {candidate.resolve() for path in paths for candidate in ([path] if path.is_file() else path.rglob("*.sarif"))}
    )
    if not sarif_files:
        raise ValueError("no CodeQL SARIF files found")

    findings: list[dict[str, Any]] = []
    file_receipts: list[dict[str, Any]] = []
    total_results = 0
    for sarif_file in sarif_files:
        raw = sarif_file.read_bytes()
        payload = json.loads(raw)
        file_receipts.append(
            {
                "path": str(sarif_file),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        for run in payload.get("runs") or []:
            rules = ((run.get("tool") or {}).get("driver") or {}).get("rules") or []
            rules_by_id = {str(rule.get("id")): rule for rule in rules if rule.get("id")}
            for result in run.get("results") or []:
                total_results += 1
                rule_id = str(result.get("ruleId") or "")
                rule = rules_by_id.get(rule_id, {})
                properties = rule.get("properties") or {}
                severity = _security_severity(properties.get("security-severity"))
                if severity is None:
                    severity = _security_severity((result.get("properties") or {}).get("security-severity"))
                if severity is not None and severity >= threshold:
                    message = (result.get("message") or {}).get("text") or ""
                    locations = result.get("locations") or []
                    uri = ""
                    if locations:
                        uri = (
                            (((locations[0].get("physicalLocation") or {}).get("artifactLocation") or {}).get("uri"))
                            or ""
                        )
                    findings.append(
                        {
                            "rule_id": rule_id,
                            "security_severity": severity,
                            "message": message,
                            "location": uri,
                        }
                    )

    receipt_core = {
        "schema_version": "codeql-security-gate-receipt-v1",
        "commit_sha": commit_sha,
        "threshold": threshold,
        "sarif_files": file_receipts,
        "total_results": total_results,
        "high_critical_count": len(findings),
        "findings": findings,
        "passed": not findings,
    }
    receipt = dict(receipt_core)
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt_core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--threshold", type=float, default=7.0)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    try:
        receipt = inspect_sarif(args.paths, commit_sha=args.commit, threshold=args.threshold)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
