from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from open_stock_ai.external_sources.registry import ExternalProjectRegistry, default_registry
from open_stock_ai.types import adapter_result_to_dict, source_lineage_from_projects


@dataclass
class AITraderSource:
    registry: ExternalProjectRegistry | None = None

    def load_skill_schema(self) -> dict:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "ai_trader",
            capability_terms=["skill", "market-intel", "signal", "agent", "routes", "research"],
        )
        project_path = Path(registry.root) / profile.path
        schema_contract = self._load_schema_contract(project_path)
        skill_contract = self._load_skill_contract(project_path)
        profile_dict = asdict(profile)
        method = "local_agent_schema_and_skill_contract"
        note = "AI-Trader skills and service schemas are parsed as local reference contracts only."
        return {
            "source": "AI-Trader",
            "loaded": profile.origin_verified,
            "status": "verified" if profile.origin_verified else "missing",
            "note": note,
            "method": method,
            "schema_contract": schema_contract,
            "skill_contract": skill_contract,
            "external_project": profile_dict,
            "adapter_result": adapter_result_to_dict(
                source_key="ai_trader",
                source_name="AI-Trader",
                role="contract",
                method=method,
                status="verified" if profile.origin_verified else "missing",
                summary=note,
                evidence=[],
                risks=[],
                metrics={
                    "schema_count": schema_contract.get("schema_count", 0),
                    "skill_count": skill_contract.get("skill_count", 0),
                },
                capability_contract={
                    "loaded": profile.origin_verified,
                    "execution_boundary": "read_only_contract_no_remote_ai_trader_publish",
                },
                external_projects={"ai_trader": profile_dict},
            ),
        }

    def validate_paper_order(self, order: dict[str, Any]) -> dict[str, Any]:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "ai_trader",
            capability_terms=["skill", "market-intel", "signal", "agent", "routes", "research"],
        )
        project_path = Path(registry.root) / profile.path
        schema_path = project_path / "research" / "schemas" / "trades.schema.json"
        schema = self._read_json(schema_path)
        row = self._paper_order_to_trade_row(order, schema)
        errors = self._validate_json_schema_row(row, schema, schema_name="trades")
        return {
            "schema_version": "open_stock_ai.ai_trader_trade_validation.v1",
            "source": "AI-Trader",
            "method": "local_trades_schema_validation",
            "loaded": profile.origin_verified and bool(schema),
            "valid": profile.origin_verified and bool(schema) and not errors,
            "errors": errors,
            "schema_path": self._relative(project_path, schema_path),
            "schema_id": schema.get("$id"),
            "schema_title": schema.get("title"),
            "schema_export_version": schema.get("x-export-version"),
            "publishing_boundary": "validated_locally_no_remote_publish",
            "row": row,
        }

    def validate_signal(self, signal: dict[str, Any], created_at: str | None = None) -> dict[str, Any]:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "ai_trader",
            capability_terms=["skill", "market-intel", "signal", "agent", "routes", "research"],
        )
        project_path = Path(registry.root) / profile.path
        schema_path = project_path / "research" / "schemas" / "signals.schema.json"
        schema = self._read_json(schema_path)
        row = self._signal_to_signal_row(signal, schema, created_at=created_at)
        errors = self._validate_json_schema_row(row, schema, schema_name="signals")
        return {
            "schema_version": "open_stock_ai.ai_trader_signal_validation.v1",
            "source": "AI-Trader",
            "method": "local_signals_schema_validation",
            "loaded": profile.origin_verified and bool(schema),
            "valid": profile.origin_verified and bool(schema) and not errors,
            "errors": errors,
            "schema_path": self._relative(project_path, schema_path),
            "schema_id": schema.get("$id"),
            "schema_title": schema.get("title"),
            "schema_export_version": schema.get("x-export-version"),
            "publishing_boundary": "validated_locally_no_remote_publish",
            "row": row,
        }

    def build_signal_interop_projection(self, validation: dict[str, Any]) -> dict[str, Any]:
        contract = self.load_skill_schema()
        schema_contract = contract.get("schema_contract") if isinstance(contract, dict) else {}
        skill_contract = contract.get("skill_contract") if isinstance(contract, dict) else {}
        lineage = self._lineage_from_contract(contract)
        signals_schema = (
            schema_contract.get("schemas", {}).get("signals", {})
            if isinstance(schema_contract.get("schemas"), dict)
            else {}
        )
        row = validation.get("row") if isinstance(validation.get("row"), dict) else {}
        return {
            "schema_version": "open_stock_ai.ai_trader_interop_projection.v1",
            "method": "local_signal_schema_interop_projection",
            "artifact_type": "signal",
            "valid": validation.get("valid") is True,
            "validation_schema_version": validation.get("schema_version"),
            "schema_path": validation.get("schema_path"),
            "schema_title": validation.get("schema_title"),
            "schema_export_version": validation.get("schema_export_version"),
            "row_field_count": len(row),
            "required_field_count": len(signals_schema.get("required", [])) if isinstance(signals_schema, dict) else 0,
            "agent_hash": row.get("agent_hash"),
            "message_type": row.get("message_type"),
            "side": row.get("side"),
            "symbol": row.get("symbol"),
            "skill_count": skill_contract.get("skill_count", 0) if isinstance(skill_contract, dict) else 0,
            "source_lineage": lineage,
            "publishing_boundary": validation.get("publishing_boundary"),
            "execution_boundary": "validated_locally_no_remote_ai_trader_publish",
        }

    def build_trade_interop_projection(self, validation: dict[str, Any]) -> dict[str, Any]:
        contract = self.load_skill_schema()
        schema_contract = contract.get("schema_contract") if isinstance(contract, dict) else {}
        skill_contract = contract.get("skill_contract") if isinstance(contract, dict) else {}
        lineage = self._lineage_from_contract(contract)
        trades_schema = (
            schema_contract.get("schemas", {}).get("trades", {})
            if isinstance(schema_contract.get("schemas"), dict)
            else {}
        )
        row = validation.get("row") if isinstance(validation.get("row"), dict) else {}
        return {
            "schema_version": "open_stock_ai.ai_trader_interop_projection.v1",
            "method": "local_trade_schema_interop_projection",
            "artifact_type": "paper_order",
            "valid": validation.get("valid") is True,
            "validation_schema_version": validation.get("schema_version"),
            "schema_path": validation.get("schema_path"),
            "schema_title": validation.get("schema_title"),
            "schema_export_version": validation.get("schema_export_version"),
            "row_field_count": len(row),
            "required_field_count": len(trades_schema.get("required", [])) if isinstance(trades_schema, dict) else 0,
            "agent_hash": row.get("agent_hash"),
            "outcome": row.get("outcome"),
            "side": row.get("side"),
            "symbol": row.get("symbol"),
            "skill_count": skill_contract.get("skill_count", 0) if isinstance(skill_contract, dict) else 0,
            "source_lineage": lineage,
            "publishing_boundary": validation.get("publishing_boundary"),
            "execution_boundary": "validated_locally_no_remote_ai_trader_publish",
        }

    def build_skill_route_projection(
        self,
        signal: dict[str, Any],
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validation = validation or {}
        contract = self.load_skill_schema()
        schema_contract = contract.get("schema_contract") if isinstance(contract, dict) else {}
        skill_contract = contract.get("skill_contract") if isinstance(contract, dict) else {}
        lineage = self._lineage_from_contract(contract)
        skills = skill_contract.get("skills") if isinstance(skill_contract, dict) else []
        if not isinstance(skills, list):
            skills = []
        row = validation.get("row") if isinstance(validation.get("row"), dict) else {}
        source_modules = signal.get("source_modules") if isinstance(signal.get("source_modules"), list) else []
        selected = self._select_signal_route_skills(
            skills=skills,
            signal={**signal, **{key: value for key, value in row.items() if value is not None}},
            source_modules=[str(item) for item in source_modules],
        )
        signals_schema = (
            schema_contract.get("schemas", {}).get("signals", {})
            if isinstance(schema_contract.get("schemas"), dict)
            else {}
        )
        selected_names = [item["name"] for item in selected]
        return {
            "schema_version": "open_stock_ai.ai_trader_skill_route.v1",
            "method": "local_skill_frontmatter_route_projection",
            "source": "AI-Trader",
            "loaded": contract.get("loaded") is True,
            "valid": validation.get("valid") is True,
            "signal_schema_path": validation.get("schema_path") or signals_schema.get("path"),
            "validation_schema_version": validation.get("schema_version"),
            "agent_hash": row.get("agent_hash") or "open_stock_ai",
            "symbol": row.get("symbol") or signal.get("symbol"),
            "side": row.get("side") or self._signal_side(signal.get("action")),
            "source_modules": [str(item) for item in source_modules],
            "skill_count": len(skills),
            "selected_skill_count": len(selected),
            "selected_skill_names": selected_names,
            "selected_skills": selected,
            "route_ready": validation.get("valid") is True and "ai-trader" in selected_names,
            "source_lineage": lineage,
            "disabled_remote_actions": [
                "publish_signal",
                "sync_trade",
                "copy_trade",
                "heartbeat_polling",
            ],
            "publishing_boundary": validation.get("publishing_boundary") or "validated_locally_no_remote_publish",
            "execution_boundary": "read_only_skill_route_no_remote_ai_trader_publish",
        }

    def _lineage_from_contract(self, contract: dict[str, Any]) -> dict[str, Any]:
        adapter_result = contract.get("adapter_result") if isinstance(contract.get("adapter_result"), dict) else {}
        lineage = adapter_result.get("source_lineage") if isinstance(adapter_result.get("source_lineage"), dict) else {}
        if lineage:
            return lineage
        project = contract.get("external_project") if isinstance(contract.get("external_project"), dict) else {}
        return source_lineage_from_projects({"ai_trader": project} if project else {})

    def _paper_order_to_trade_row(self, order: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        required = schema.get("required") if isinstance(schema, dict) else []
        fields = list(required) if isinstance(required, list) else []
        row = {field: None for field in fields}
        action = str(order.get("action") or "").lower()
        row.update(
            {
                "agent_hash": "open_stock_ai",
                "market": order.get("market"),
                "symbol": order.get("symbol"),
                "outcome": "paper_open",
                "side": "sell" if action in {"sell", "reduce"} else "buy" if action in {"buy", "add"} else action or None,
                "entry_price": order.get("entry_price"),
                "exit_price": None,
                "quantity": order.get("position_size_pct"),
                "pnl": None,
                "executed_at": order.get("created_at"),
                "created_at": order.get("created_at"),
                "content": order.get("order_id"),
                "experiment_key": "open_stock_ai",
                "variant_key": order.get("horizon"),
            }
        )
        return row

    def _select_signal_route_skills(
        self,
        *,
        skills: list[dict[str, Any]],
        signal: dict[str, Any],
        source_modules: list[str],
    ) -> list[dict[str, Any]]:
        action = str(signal.get("action") or signal.get("signal_type") or "").lower()
        text = " ".join(
            str(item).lower()
            for item in [
                action,
                signal.get("reason"),
                signal.get("content"),
                signal.get("message_type"),
                " ".join(source_modules),
            ]
            if item
        )
        selected: list[dict[str, Any]] = []

        def add_skill(skill: dict[str, Any], reason: str, mode: str) -> None:
            name = str(skill.get("name") or "")
            if not name or any(item.get("name") == name for item in selected):
                return
            selected.append(
                {
                    "name": name,
                    "path": skill.get("path"),
                    "reason": reason,
                    "mode": mode,
                    "description": skill.get("description"),
                }
            )

        for skill in skills:
            name = str(skill.get("name") or "").lower()
            description = str(skill.get("description") or "").lower()
            haystack = f"{name} {description}"
            if name == "ai-trader":
                add_skill(skill, "bootstrap AI-Trader routing and signal contract", "read_only_bootstrap")
            elif "market-intel" in name or "market-intel" in haystack:
                if any(term in text for term in ["news", "market", "sentiment", "fingpt", "finrobot", "tradingagents"]):
                    add_skill(skill, "read-only market context before signal publication", "read_only_context")
            elif "tradesync" in name or "trade sync" in haystack:
                if action in {"buy", "sell", "add", "reduce"}:
                    add_skill(skill, "remote publishing route identified but disabled by Open Stock AI boundary", "publish_disabled")
            elif "heartbeat" in name:
                add_skill(skill, "normal remote-operation health route identified but disabled locally", "publish_disabled")
        if not selected and skills:
            add_skill(skills[0], "fallback local skill route from available AI-Trader contract", "read_only_contract")
        return selected

    def _signal_side(self, action: Any) -> str | None:
        normalized = str(action or "").lower()
        if normalized in {"sell", "reduce"}:
            return "sell"
        if normalized in {"buy", "add"}:
            return "buy"
        return normalized or None

    def _signal_to_signal_row(
        self,
        signal: dict[str, Any],
        schema: dict[str, Any],
        created_at: str | None = None,
    ) -> dict[str, Any]:
        required = schema.get("required") if isinstance(schema, dict) else []
        fields = list(required) if isinstance(required, list) else []
        row = {field: None for field in fields}
        action = str(signal.get("action") or "").lower()
        side = "sell" if action in {"sell", "reduce"} else "buy" if action in {"buy", "add"} else action or None
        symbol = signal.get("symbol")
        horizon = signal.get("horizon")
        source_modules = signal.get("source_modules") or []
        if not isinstance(source_modules, list):
            source_modules = [str(source_modules)]
        row.update(
            {
                "agent_hash": "open_stock_ai",
                "message_type": "trading_signal",
                "market": signal.get("market"),
                "signal_type": action or None,
                "symbol": symbol,
                "outcome": "proposed",
                "symbols": symbol,
                "side": side,
                "entry_price": signal.get("entry_price"),
                "exit_price": signal.get("target_price"),
                "quantity": signal.get("position_size_pct"),
                "pnl": None,
                "title": f"Open Stock AI {action or 'signal'} {symbol or ''}".strip(),
                "content": signal.get("reason"),
                "tags": ",".join(str(item) for item in source_modules),
                "timestamp": None,
                "created_at": created_at,
                "executed_at": None,
                "experiment_key": "open_stock_ai",
                "variant_key": horizon,
            }
        )
        return row

    def _validate_json_schema_row(self, row: dict[str, Any], schema: dict[str, Any], schema_name: str) -> list[str]:
        if not schema:
            return [f"AI-Trader {schema_name} schema is missing or unreadable."]
        errors: list[str] = []
        required = schema.get("required") if isinstance(schema, dict) else []
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        if isinstance(required, list):
            for field in required:
                if field not in row:
                    errors.append(f"Missing required field: {field}")
        if schema.get("additionalProperties") is False:
            allowed = set(properties.keys()) if isinstance(properties, dict) else set()
            for field in row:
                if field not in allowed:
                    errors.append(f"Additional property is not allowed: {field}")
        if isinstance(properties, dict):
            for field, value in row.items():
                spec = properties.get(field)
                if not isinstance(spec, dict) or "type" not in spec:
                    continue
                allowed_types = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
                if not self._value_matches_json_type(value, allowed_types):
                    errors.append(f"Field {field} has invalid type {type(value).__name__}; expected {allowed_types}")
        return errors

    def _value_matches_json_type(self, value: Any, allowed_types: list[str]) -> bool:
        if value is None:
            return "null" in allowed_types
        if isinstance(value, bool):
            return "boolean" in allowed_types
        if isinstance(value, int) and not isinstance(value, bool):
            return "integer" in allowed_types or "number" in allowed_types
        if isinstance(value, float):
            return "number" in allowed_types
        if isinstance(value, str):
            return "string" in allowed_types
        if isinstance(value, dict):
            return "object" in allowed_types
        if isinstance(value, list):
            return "array" in allowed_types
        return False

    def _load_schema_contract(self, project_path: Path) -> dict[str, Any]:
        schema_root = project_path / "research" / "schemas"
        selected = {
            "agents": schema_root / "agents.schema.json",
            "signals": schema_root / "signals.schema.json",
            "trades": schema_root / "trades.schema.json",
        }
        schemas: dict[str, Any] = {}
        for key, path in selected.items():
            schema = self._read_json(path)
            properties = schema.get("properties") if isinstance(schema, dict) else {}
            required = schema.get("required") if isinstance(schema, dict) else []
            schemas[key] = {
                "path": self._relative(project_path, path),
                "title": schema.get("title") if isinstance(schema, dict) else None,
                "field_count": len(properties) if isinstance(properties, dict) else 0,
                "required": list(required) if isinstance(required, list) else [],
                "core_fields": [
                    field
                    for field in ["agent_id", "agent_hash", "message_type", "market", "symbol", "side", "entry_price", "quantity", "pnl"]
                    if isinstance(properties, dict) and field in properties
                ],
            }
        return {
            "schema_count": len(schemas),
            "schemas": schemas,
            "publishing_boundary": "read_only_contract_no_remote_publish",
        }

    def _load_skill_contract(self, project_path: Path) -> dict[str, Any]:
        skill_root = project_path / "skills"
        skills: list[dict[str, Any]] = []
        if skill_root.exists():
            for path in sorted(skill_root.glob("*/SKILL.md")):
                frontmatter = self._read_frontmatter(path)
                skills.append(
                    {
                        "name": frontmatter.get("name") or path.parent.name,
                        "description": frontmatter.get("description"),
                        "path": self._relative(project_path, path),
                    }
                )
        guide_path = project_path / "docs" / "README_AGENT.md"
        return {
            "skill_count": len(skills),
            "skills": skills,
            "agent_guide_path": self._relative(project_path, guide_path) if guide_path.exists() else None,
            "execution_boundary": "OpenStockAI never publishes to AI-Trader without an explicit future connector.",
        }

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _read_frontmatter(self, path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        values: dict[str, str] = {}
        for line in lines[1:]:
            stripped = line.strip()
            if stripped == "---":
                break
            if ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            values[key.strip()] = value.strip()
        return values

    def _relative(self, project_path: Path, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(project_path.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)
