from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec


class SkillToolProvider:
    """Select and inject real SKILL.md workflows into a run without bypassing host tools."""

    provider_id = "skills"

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        codex_home = Path(os.getenv("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
        self.roots = (
            ("project", self.project_root / "skills"),
            ("codex", codex_home / "skills"),
            ("ai-trader", self.project_root / "external" / "AI-Trader" / "skills"),
        )
        self._skills: dict[str, dict[str, Any]] = {}
        self._scan_error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        skills: dict[str, dict[str, Any]] = {}
        try:
            for source, root in self.roots:
                if not root.is_dir():
                    continue
                for path in sorted(root.rglob("SKILL.md")):
                    if any(part in {".git", ".venv", "node_modules"} for part in path.parts):
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                    metadata = _frontmatter(text)
                    base_name = str(metadata.get("name") or path.parent.name).strip()
                    skill_id = base_name if base_name not in skills else f"{source}:{base_name}"
                    skills[skill_id] = {
                        "id": skill_id,
                        "name": base_name,
                        "description": str(metadata.get("description") or "").strip(),
                        "source": source,
                        "path": path.resolve(),
                        "metadata": metadata,
                    }
        except OSError as exc:
            self._scan_error = str(exc)
        else:
            self._scan_error = None
            self._skills = skills

    async def prepare(self, context: AgentRunContext) -> None:
        del context
        self.refresh()

    def manifest(self) -> list[dict[str, Any]]:
        names = sorted(self._skills)
        spec = AgentToolSpec(
            name="skills.activate",
            description=(
                "Load one installed SKILL.md workflow into this Agent run. This injects instructions only; "
                "every referenced operation still requires a real host capability and its permission gate."
            ),
            category="skills",
            packages=("SKILL.md",),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "enum": names} if names else {"type": "string"},
                },
            },
        )
        return [spec.to_dict()]

    def has_tool(self, name: str) -> bool:
        return name == "skills.activate"

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        if name != "skills.activate":
            raise ValueError(f"Unknown Skill capability: {name}")
        skill_name = str(arguments.get("name") or "").strip()
        skill = self._skills.get(skill_name)
        if skill is None:
            raise ValueError(f"Installed Skill not found: {skill_name}")
        instructions = skill["path"].read_text(encoding="utf-8", errors="replace")
        active = context.state.setdefault("active_skills", [])
        if skill_name not in active:
            active.append(skill_name)
        return {
            "schema_version": "open_stock_ai.skill_activation.v1",
            "skill": skill_name,
            "source": skill["source"],
            "description": skill["description"],
            "instructions": instructions,
            "instruction_characters": len(instructions),
            "active_skills": list(active),
            "execution_boundary": "instructions_only_host_capability_permissions_still_apply",
        }

    def describe(self) -> dict[str, Any]:
        return {
            "configured": bool(self._skills),
            "runtime_ready": bool(self._skills),
            "health": "ready" if self._skills else "empty",
            "inventory_count": len(self._skills),
            "inventory": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "source": item["source"],
                    "description": item["description"],
                }
                for item in self._skills.values()
            ],
            "scan_error": self._scan_error,
            "runtime_binding": "selected_skill_content_is_injected_into_tool_results",
        }


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        payload = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    return payload if isinstance(payload, dict) else {}
