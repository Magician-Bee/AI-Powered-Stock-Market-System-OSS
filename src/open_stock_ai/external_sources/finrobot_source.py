from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from open_stock_ai.external_sources.registry import ExternalProjectRegistry, default_registry
from open_stock_ai.types import MarketSnapshot, StockRequest, adapter_result_to_dict


@dataclass
class FinRobotSource:
    registry: ExternalProjectRegistry | None = None

    def load_capability_contract(self) -> dict[str, Any]:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "finrobot",
            capability_terms=[
                "annual_report",
                "report",
                "rag",
                "agent",
                "fundamental",
                "analyzer",
            ],
        )
        project_path = Path(registry.root) / profile.path
        profile_dict = asdict(profile)
        runtime_capability = self._runtime_capability()
        adapter_result = adapter_result_to_dict(
            source_key="finrobot",
            source_name="FinRobot",
            role="contract",
            method="local_report_agent_contract",
            status="verified" if profile.origin_verified else "missing",
            summary="FinRobot report-agent roles, analysis tools, and sample report contracts parsed locally.",
            capability_contract={
                "loaded": profile.origin_verified,
                "runtime_capability": runtime_capability,
                "execution_boundary": "read_only_contract_no_finrobot_runtime_import",
            },
            external_projects={"finrobot": profile_dict},
        )
        return {
            "source": "FinRobot",
            "loaded": profile.origin_verified,
            "status": "verified" if profile.origin_verified else "missing",
            "method": "local_report_agent_contract",
            "adapter_result": adapter_result,
            "agent_roles": self._agent_roles(project_path),
            "report_analysis_tools": self._report_analysis_tools(project_path),
            "equity_report_modules": self._equity_report_modules(project_path),
            "report_structure_contract": self._report_structure_contract(project_path),
            "sample_reports": self._sample_reports(project_path),
            "runtime_capability": runtime_capability,
            "execution_boundary": "read_only_contract_no_finrobot_runtime_import",
        }

    def analyze_fundamentals(self, request: StockRequest, market_snapshot: MarketSnapshot) -> dict:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "finrobot",
            capability_terms=[
                "annual_report",
                "report",
                "rag",
                "agent",
                "fundamental",
                "analyzer",
            ],
        )
        capability_contract = self.load_capability_contract()
        runtime_capability = capability_contract["runtime_capability"]
        revenue = market_snapshot.financials.get("revenue") or {}
        has_revenue = bool(revenue)
        yoy = self._number(revenue.get("yoy_change_percent"))
        mom = self._number(revenue.get("mom_change_percent"))
        if yoy is not None and yoy >= 10:
            view = "positive"
        elif yoy is not None and yoy <= -10:
            view = "negative"
        else:
            view = "neutral"
        status = runtime_capability["status"]
        profile_dict = asdict(profile)
        method = "local_baseline_revenue_snapshot_not_finrobot_runtime"
        model_provenance = {
            "schema_version": "open_stock_ai.model_output_provenance.v1",
            "provider": "FinRobot",
            "provenance_type": "local_baseline",
            "model_output": False,
            "runtime_status": runtime_capability["status"],
            "reason": runtime_capability["reason"],
        }
        report_projection = self._report_projection(
            request=request,
            market_snapshot=market_snapshot,
            capability_contract=capability_contract,
            fundamental_view=view,
            revenue_yoy=yoy,
            revenue_mom=mom,
        )
        evidence = ([revenue] if has_revenue else []) + [report_projection]
        risks = [] if has_revenue else ["No revenue snapshot available."]
        summary = (
            f"FinRobot runtime {status}: fundamentals loaded={has_revenue}, view={view}; "
            "the local baseline is not FinRobot runtime output."
        )
        return {
            "summary": summary,
            "fundamental_view": view,
            "financial_metrics": {"revenue_yoy_percent": yoy, "revenue_mom_percent": mom},
            "report_projection": report_projection,
            "model_provenance": model_provenance,
            "capability_contract": capability_contract,
            "risks": risks,
            "evidence": evidence,
            "method": method,
            "status": status,
            "external_project": profile_dict,
            "adapter_result": adapter_result_to_dict(
                source_key="finrobot",
                source_name="FinRobot",
                role="intelligence",
                method=method,
                status=status,
                summary=summary,
                evidence=evidence,
                risks=risks,
                metrics={
                    "fundamental_view": view,
                    "revenue_yoy_percent": yoy,
                    "revenue_mom_percent": mom,
                    "report_projection": report_projection,
                    "model_provenance": model_provenance,
                },
                capability_contract=capability_contract,
                external_projects={"finrobot": profile_dict},
                loaded=False,
            ),
        }

    def _report_projection(
        self,
        *,
        request: StockRequest,
        market_snapshot: MarketSnapshot,
        capability_contract: dict[str, Any],
        fundamental_view: str,
        revenue_yoy: float | None,
        revenue_mom: float | None,
    ) -> dict[str, Any]:
        contract_roles = capability_contract.get("agent_roles") if isinstance(capability_contract, dict) else {}
        roles = contract_roles.get("roles") if isinstance(contract_roles, dict) else []
        expert_role = next((role for role in roles if role.get("name") == "Expert_Investor"), {}) if isinstance(roles, list) else {}
        tools_contract = capability_contract.get("report_analysis_tools") if isinstance(capability_contract, dict) else {}
        tools = tools_contract.get("tools") if isinstance(tools_contract, dict) else []
        tool_names = [tool.get("name") for tool in tools if isinstance(tool, dict) and tool.get("name")]
        structure = capability_contract.get("report_structure_contract") if isinstance(capability_contract, dict) else {}
        sections = structure.get("standard_sections") if isinstance(structure, dict) else []
        risk_keyword_count = self._risk_keyword_count(market_snapshot.news)
        risk_view = self._risk_view(revenue_yoy=revenue_yoy, revenue_mom=revenue_mom, risk_keyword_count=risk_keyword_count)
        valuation = self._valuation_snapshot(market_snapshot.price, revenue_yoy)
        return {
            "schema_version": "open_stock_ai.finrobot_report_projection.v1",
            "method": "local_equity_report_contract_projection",
            "symbol": request.symbol,
            "horizon": request.horizon,
            "report_view": fundamental_view,
            "risk_view": risk_view,
            "valuation_view": valuation["valuation_view"],
            "financial_metrics": {
                "revenue_yoy_percent": revenue_yoy,
                "revenue_mom_percent": revenue_mom,
                "risk_keyword_count": risk_keyword_count,
            },
            "valuation_snapshot": valuation,
            "report_contract": {
                "section_count": len(sections) if isinstance(sections, list) else 0,
                "sections": sections,
                "required_sections": structure.get("required_sections") if isinstance(structure, dict) else [],
            },
            "agent_contract": {
                "primary_role": expert_role.get("name"),
                "role_count": contract_roles.get("count") if isinstance(contract_roles, dict) else 0,
                "tool_count": len(tool_names),
                "required_tools_present": {
                    "analyze_income_stmt": "analyze_income_stmt" in tool_names,
                    "get_risk_assessment": "get_risk_assessment" in tool_names,
                    "get_competitors_analysis": "get_competitors_analysis" in tool_names,
                },
            },
            "execution_boundary": "read_only_contract_no_finrobot_runtime_import",
            "model_provenance": {
                "schema_version": "open_stock_ai.model_output_provenance.v1",
                "provider": "FinRobot",
                "provenance_type": "local_baseline",
                "model_output": False,
            },
        }

    @staticmethod
    def _runtime_capability() -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.finrobot_runtime_capability.v1",
            "enabled": False,
            "status": "disabled",
            "reason": "finrobot_runtime_not_configured",
            "provenance_required": True,
            "execution_authority": "none",
        }

    def _risk_keyword_count(self, news: list[dict[str, Any]]) -> int:
        keywords = ("risk", "warning", "pressure", "decline", "lawsuit", "regulatory", "cut", "weak")
        count = 0
        for item in news:
            text = " ".join(str(value) for value in item.values()).lower() if isinstance(item, dict) else str(item).lower()
            if any(keyword in text for keyword in keywords):
                count += 1
        return count

    def _risk_view(
        self,
        *,
        revenue_yoy: float | None,
        revenue_mom: float | None,
        risk_keyword_count: int,
    ) -> str:
        if (revenue_yoy is not None and revenue_yoy <= -10) or (revenue_mom is not None and revenue_mom <= -15):
            return "high"
        if risk_keyword_count >= 2 or (revenue_yoy is not None and revenue_yoy < 0):
            return "medium"
        return "low"

    def _valuation_snapshot(self, price: float | None, revenue_yoy: float | None) -> dict[str, Any]:
        if price is None or price <= 0:
            return {
                "current_price": price,
                "target_price": None,
                "low_estimate": None,
                "high_estimate": None,
                "valuation_view": "unavailable",
                "method": "local_revenue_growth_valuation_band",
            }
        growth = max(-0.3, min(0.3, (revenue_yoy or 0.0) / 100.0))
        target_price = price * (1 + growth * 0.45)
        low_estimate = target_price * 0.92
        high_estimate = target_price * 1.08
        if target_price >= price * 1.08:
            valuation_view = "upside"
        elif target_price <= price * 0.92:
            valuation_view = "downside"
        else:
            valuation_view = "fair"
        return {
            "current_price": round(price, 4),
            "target_price": round(target_price, 4),
            "low_estimate": round(low_estimate, 4),
            "high_estimate": round(high_estimate, 4),
            "valuation_view": valuation_view,
            "method": "local_revenue_growth_valuation_band",
        }

    def _number(self, value: object) -> float | None:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _agent_roles(self, project_path: Path) -> dict[str, Any]:
        path = project_path / "finrobot" / "agents" / "agent_library.py"
        text = self._read_text(path)
        roles: list[dict[str, Any]] = []
        if not text:
            return {
                "path": self._relative(project_path, path),
                "count": 0,
                "roles": roles,
            }
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Dict):
                continue
            values = {
                key.value: value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            name_node = values.get("name")
            if not isinstance(name_node, ast.Constant) or not isinstance(name_node.value, str):
                continue
            profile_node = values.get("profile")
            roles.append(
                {
                    "name": name_node.value,
                    "profile_excerpt": self._literal_excerpt(profile_node),
                    "toolkits": self._toolkit_names(values.get("toolkits")),
                }
            )
        return {
            "path": self._relative(project_path, path),
            "count": len(roles),
            "roles": roles,
        }

    def _report_analysis_tools(self, project_path: Path) -> dict[str, Any]:
        path = project_path / "finrobot" / "functional" / "analyzer.py"
        text = self._read_text(path)
        tools: list[dict[str, Any]] = []
        if text:
            module = ast.parse(text)
            for node in module.body:
                if not isinstance(node, ast.ClassDef) or node.name != "ReportAnalysisUtils":
                    continue
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        doc = ast.get_docstring(item) or ""
                        tools.append(
                            {
                                "name": item.name,
                                "parameters": [arg.arg for arg in item.args.args],
                                "doc_excerpt": self._excerpt(doc),
                            }
                        )
        return {
            "path": self._relative(project_path, path),
            "count": len(tools),
            "tools": tools,
        }

    def _equity_report_modules(self, project_path: Path) -> dict[str, Any]:
        module_root = project_path / "finrobot_equity" / "core" / "src" / "modules"
        files = []
        if module_root.exists():
            files = [
                self._relative(project_path, path)
                for path in sorted(module_root.rglob("*.py"))
                if "__pycache__" not in path.parts
            ]
        return {
            "path": self._relative(project_path, module_root),
            "count": len(files),
            "files": files[:20],
        }

    def _sample_reports(self, project_path: Path) -> dict[str, Any]:
        report_roots = [
            project_path / "report",
            project_path / "finrobot_equity" / "core" / "output",
        ]
        reports: list[str] = []
        for root in report_roots:
            if not root.exists():
                continue
            for path in sorted(root.glob("*")):
                if path.is_file() and path.suffix.lower() in {".pdf", ".html", ".htm"}:
                    reports.append(self._relative(project_path, path))
        return {
            "count": len(reports),
            "files": reports[:10],
        }

    def _report_structure_contract(self, project_path: Path) -> dict[str, Any]:
        path = project_path / "finrobot_equity" / "core" / "src" / "modules" / "report_structure.py"
        text = self._read_text(path)
        standard_sections: list[dict[str, Any]] = []
        required_sections: list[str] = []
        if text:
            module = ast.parse(text)
            for node in ast.walk(module):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "required_sections":
                            value = self._literal_value(node.value)
                            if isinstance(value, list):
                                required_sections = [str(item) for item in value]
                        if isinstance(target, ast.Name) and target.id == "STANDARD_SECTIONS":
                            value = self._literal_value(node.value)
                            if isinstance(value, list):
                                standard_sections = [
                                    {"id": item[0], "title": item[1], "order": item[2]}
                                    for item in value
                                    if isinstance(item, tuple) and len(item) == 3
                                ]
                elif isinstance(node, ast.ClassDef) and node.name == "ReportStructureManager":
                    for item in node.body:
                        if isinstance(item, ast.Assign):
                            for target in item.targets:
                                if isinstance(target, ast.Name) and target.id == "STANDARD_SECTIONS":
                                    value = self._literal_value(item.value)
                                    if isinstance(value, list):
                                        standard_sections = [
                                            {"id": section[0], "title": section[1], "order": section[2]}
                                            for section in value
                                            if isinstance(section, tuple) and len(section) == 3
                                        ]
        return {
            "path": self._relative(project_path, path),
            "standard_section_count": len(standard_sections),
            "standard_sections": standard_sections,
            "required_sections": required_sections or [
                "executive_summary",
                "company_overview",
                "financial_analysis",
                "valuation_analysis",
                "risk_factors",
                "investment_recommendation",
            ],
        }

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def _literal_excerpt(self, node: ast.AST | None) -> str | None:
        if node is None:
            return None
        try:
            value = ast.literal_eval(node)
        except (ValueError, TypeError):
            return None
        return self._excerpt(value) if isinstance(value, str) else None

    def _toolkit_names(self, node: ast.AST | None) -> list[str]:
        if not isinstance(node, ast.List):
            return []
        names: list[str] = []
        for item in node.elts:
            if isinstance(item, ast.Name):
                names.append(item.id)
            elif isinstance(item, ast.Attribute):
                names.append(self._attribute_name(item))
        return names

    def _attribute_name(self, node: ast.Attribute) -> str:
        parts = [node.attr]
        value = node.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))

    def _literal_value(self, node: ast.AST) -> Any:
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            return None

    def _excerpt(self, text: str, limit: int = 180) -> str:
        normalized = " ".join(text.split())
        return normalized[:limit] if normalized else ""

    def _relative(self, project_path: Path, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(project_path.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)
