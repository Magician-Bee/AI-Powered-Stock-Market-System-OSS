from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_PATH = _ROOT / "config" / "production_requirement_status.yaml"
_VALID_STATUSES = {"complete", "partial", "unverified"}
_P0_REQUIREMENTS = {
    *(f"Q-{index:03d}" for index in range(1, 10)),
    *(f"D-{index:03d}" for index in range(1, 5)),
    "M-001",
    "M-002",
    "M-006",
    "S-001",
    "S-002",
    "S-003",
    *(f"E-{index:03d}" for index in range(1, 12)),
    *(f"A-{index:03d}" for index in range(1, 5)),
    "N-001",
    "N-002",
    "N-007",
    "SEC-001",
    "SEC-002",
    "R-001",
    "T-001",
    "T-003",
    "T-004",
    "T-005",
    "GOV-001",
    "GOV-002",
}
_P1_P2_REQUIREMENTS = {
    *(f"Q-{index:03d}" for index in range(10, 17)),
    *(f"D-{index:03d}" for index in range(5, 13)),
    "M-003", "M-004", "M-005", *(f"M-{index:03d}" for index in range(7, 13)),
    *(f"S-{index:03d}" for index in range(4, 13)),
    *(f"E-{index:03d}" for index in range(12, 19)),
    *(f"A-{index:03d}" for index in range(5, 12)),
    *(f"N-{index:03d}" for index in range(3, 7)),
    *(f"SEC-{index:03d}" for index in range(3, 11)),
    *(f"R-{index:03d}" for index in range(2, 12)),
    "T-002", *(f"T-{index:03d}" for index in range(6, 10)),
    *(f"GOV-{index:03d}" for index in range(3, 7)),
}


@lru_cache(maxsize=1)
def _definition() -> tuple[dict[str, Any], str]:
    raw = _CONFIG_PATH.read_bytes()
    payload = yaml.safe_load(raw) or {}
    if not isinstance(payload, dict) or not isinstance(payload.get("requirements"), list):
        raise ValueError("production requirement status requires a requirements list")
    requirements = payload["requirements"]
    identifiers = [str(item.get("id")) for item in requirements if isinstance(item, dict)]
    if len(identifiers) != len(requirements) or len(set(identifiers)) != len(identifiers):
        raise ValueError("production requirement IDs must be mappings and unique")
    if set(identifiers) != _P0_REQUIREMENTS:
        raise ValueError("production requirement status must contain the complete P0 quant/sizing/execution set")
    for item in requirements:
        status = item.get("status")
        blockers = item.get("blockers")
        evidence = item.get("evidence")
        if status not in _VALID_STATUSES or not isinstance(blockers, list) or not isinstance(evidence, list):
            raise ValueError(f"invalid production requirement record:{item.get('id')}")
        if status == "complete" and (blockers or not evidence):
            raise ValueError(f"complete production requirement has blockers:{item.get('id')}")
        if status == "partial" and not evidence:
            raise ValueError(f"partial production requirement requires direct evidence:{item.get('id')}")
        for reference in evidence:
            path = str(reference).split("::", 1)[0]
            if not (_ROOT / path).exists():
                raise ValueError(f"production requirement evidence does not exist:{item.get('id')}:{path}")
    release_requirements = _release_requirements(payload)
    release_ids = [item["id"] for item in release_requirements]
    if len(release_ids) != len(set(release_ids)) or set(release_ids) != _P1_P2_REQUIREMENTS:
        raise ValueError("release requirement groups must contain every P1/P2 final-plan ID exactly once")
    for item in release_requirements:
        for reference in item["evidence"]:
            path = str(reference).split("::", 1)[0]
            if not (_ROOT / path).exists():
                raise ValueError(f"release requirement evidence does not exist:{item['id']}:{path}")
    return payload, hashlib.sha256(raw).hexdigest()


def production_requirement_status() -> dict[str, Any]:
    definition, ledger_hash = _definition()
    requirements = [dict(item) for item in definition["requirements"]]
    release_requirements = _release_requirements(definition)
    by_id = {item["id"]: item for item in requirements}
    gates: dict[str, Any] = {}
    for name, gate in dict(definition.get("gates") or {}).items():
        required = list(gate.get("requirement_ids") or [])
        missing = [item for item in required if item not in by_id]
        incomplete = [item for item in required if item in by_id and by_id[item]["status"] != "complete"]
        gates[name] = {
            "requirement_ids": required,
            "passed": not missing and not incomplete,
            "missing_requirement_ids": missing,
            "incomplete_requirement_ids": incomplete,
        }
    all_release_requirements = [*requirements, *release_requirements]
    incomplete_release = [
        item["id"] for item in all_release_requirements if item["status"] != "complete"
    ]
    gates["full_release"] = {
        "requirement_ids": [item["id"] for item in all_release_requirements],
        "passed": not incomplete_release,
        "missing_requirement_ids": [],
        "incomplete_requirement_ids": incomplete_release,
    }
    counts = {status: sum(item["status"] == status for item in requirements) for status in sorted(_VALID_STATUSES)}
    all_gates_passed = bool(gates) and all(gate["passed"] for gate in gates.values())
    return {
        "schema_version": definition["schema_version"],
        "source_of_truth": definition["source_of_truth"],
        "ledger_sha256": ledger_hash,
        "counts": counts,
        "gates": gates,
        "master_p0_complete_count": sum(item["status"] == "complete" for item in requirements),
        "master_p0_total": len(requirements),
        "release_requirements": release_requirements,
        "full_release_complete_count": sum(
            item["status"] == "complete" for item in all_release_requirements
        ),
        "full_release_total": len(all_release_requirements),
        "research_execution_evidence_enabled": all_gates_passed,
        "requirements": requirements,
    }


def _release_requirements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand the P1/P2 release checklist without pretending it is complete.

    P0 remains a detailed acceptance ledger above.  P1/P2 begin as explicit
    release records in that same YAML and can be individually promoted through
    ``release_requirement_overrides`` only when direct evidence is available.
    """

    groups = payload.get("release_requirement_groups")
    overrides = payload.get("release_requirement_overrides") or {}
    if not isinstance(groups, list) or not isinstance(overrides, dict):
        raise ValueError("release requirement groups and overrides must be mappings/lists")
    records: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("release requirement group must be a mapping")
        priority = group.get("priority")
        domain = group.get("domain")
        identifiers = group.get("ids")
        if priority not in {"P1", "P2"} or not isinstance(domain, str) or not isinstance(identifiers, list):
            raise ValueError("release requirement group has invalid priority/domain/ids")
        for identifier in identifiers:
            item_id = str(identifier)
            override = overrides.get(item_id) or {}
            if not isinstance(override, dict):
                raise ValueError(f"release requirement override must be a mapping:{item_id}")
            status = override.get("status", "unverified")
            evidence = list(override.get("evidence") or [])
            blockers = (
                list(override["blockers"])
                if "blockers" in override
                else ["direct_release_evidence_not_recorded"]
            )
            if status not in _VALID_STATUSES:
                raise ValueError(f"invalid release requirement status:{item_id}")
            if status == "complete" and (not evidence or blockers):
                raise ValueError(f"complete release requirement has blockers:{item_id}")
            if status == "partial" and not evidence:
                raise ValueError(f"partial release requirement requires evidence:{item_id}")
            records.append(
                {
                    "id": item_id,
                    "priority": priority,
                    "domain": domain,
                    "status": status,
                    "acceptance": str(
                        override.get("acceptance")
                        or "Final P0–P105 master-plan release requirement."
                    ),
                    "evidence": evidence,
                    "blockers": blockers,
                }
            )
    return records
