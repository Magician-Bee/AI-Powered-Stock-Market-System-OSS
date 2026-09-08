from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


OPTIONAL_EXTERNAL_SOURCES = {
    "freqtrade": {
        "display_name": "Freqtrade",
        "repository": "https://github.com/freqtrade/freqtrade",
        "documentation": "https://www.freqtrade.io/en/stable/",
        "candidate_roles": ["dry_run", "backtesting", "web_ui", "strategy_plugin"],
        "local_path": "external/freqtrade",
        "status": "not_approved_not_cloned",
        "reason": "Mentioned in the integration brief as optional; not included in the approved seven-repo source lock.",
    },
}


@dataclass
class OptionalExternalSourceRegistry:
    root: str | Path = "."

    def build(self, *, approved_keys: list[str]) -> dict[str, Any]:
        root = Path(self.root).resolve()
        entries = []
        for key, spec in OPTIONAL_EXTERNAL_SOURCES.items():
            local_path = root / spec["local_path"]
            approved = key in approved_keys
            exists = local_path.exists()
            entries.append(
                {
                    "key": key,
                    "display_name": spec["display_name"],
                    "repository": spec["repository"],
                    "documentation": spec["documentation"],
                    "candidate_roles": list(spec["candidate_roles"]),
                    "local_path": spec["local_path"],
                    "exists": exists,
                    "approved": approved,
                    "source_lock_member": approved,
                    "status": "approved_unexpected_clone" if approved else spec["status"],
                    "reason": spec["reason"],
                    "allowed_boundary": "not_loaded_not_imported_not_in_source_lock",
                }
            )
        excluded_count = sum(1 for item in entries if not item["approved"] and item["status"] == "not_approved_not_cloned")
        unexpected_clone_count = sum(1 for item in entries if item["exists"] and not item["approved"])
        return {
            "schema_version": "open_stock_ai.optional_external_source_registry.v1",
            "method": "integration_brief_optional_source_policy",
            "approved_source_count": len(approved_keys),
            "optional_count": len(entries),
            "excluded_count": excluded_count,
            "unexpected_clone_count": unexpected_clone_count,
            "source_lock_membership_required": False,
            "execution_boundary": "optional_sources_excluded_until_explicit_approval",
            "available": len(entries) == 1 and excluded_count == len(entries) and unexpected_clone_count == 0,
            "entries": entries,
        }
