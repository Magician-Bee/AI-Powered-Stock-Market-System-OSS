from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from open_stock_ai.external_sources.registry import ExternalProjectRegistry, default_registry
from open_stock_ai.external_sources.tradingagents_runtime import TradingAgentsRuntimeAdapter
from open_stock_ai.intelligence.technical_intelligence import TechnicalIntelligence
from open_stock_ai.types import MarketSnapshot, StockRequest, adapter_result_to_dict


@dataclass
class TradingAgentsSource:
    registry: ExternalProjectRegistry | None = None
    technical: TechnicalIntelligence | None = None
    runtime: TradingAgentsRuntimeAdapter | None = None

    def load_capability_contract(self) -> dict[str, Any]:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "tradingagents",
            capability_terms=[
                "trading_graph",
                "analyst",
                "signal_processing",
                "reflection",
                "risk",
                "agents",
            ],
        )
        project_path = Path(registry.root) / profile.path
        profile_dict = asdict(profile)
        adapter_result = adapter_result_to_dict(
            source_key="tradingagents",
            source_name="TradingAgents",
            role="contract",
            method="local_multi_agent_schema_graph_contract",
            status="verified" if profile.origin_verified else "missing",
            summary="TradingAgents typed schema, graph, rating, and risk-memory contracts parsed locally.",
            capability_contract={
                "loaded": profile.origin_verified,
                "execution_boundary": "read_only_contract_no_tradingagents_runtime_import",
            },
            external_projects={"tradingagents": profile_dict},
        )
        return {
            "source": "TradingAgents",
            "loaded": profile.origin_verified,
            "status": "verified" if profile.origin_verified else "missing",
            "method": "local_multi_agent_schema_graph_contract",
            "adapter_result": adapter_result,
            "schema_contract": self._schema_contract(project_path),
            "graph_contract": self._graph_contract(project_path),
            "risk_memory_contract": self._risk_memory_contract(project_path),
            "execution_boundary": "read_only_contract_no_tradingagents_runtime_import",
        }

    def analyze_context(self, request: StockRequest, market_snapshot: MarketSnapshot) -> dict:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "tradingagents",
            capability_terms=[
                "trading_graph",
                "analyst",
                "signal_processing",
                "reflection",
                "risk",
                "agents",
            ],
        )
        technical = (self.technical or TechnicalIntelligence()).analyze(market_snapshot.ohlcv)
        capability_contract = self.load_capability_contract()
        runtime = self.runtime or TradingAgentsRuntimeAdapter(project_root=Path(registry.root))
        runtime_receipt = runtime.run(request, market_snapshot)
        risk_debate_summary = self._risk_debate_summary(
            capability_contract=capability_contract,
            technical=technical,
            request=request,
        )
        technical_view = technical["technical_view"]
        runtime_executed = runtime_receipt.get("status") == "executed"
        status = "executed" if runtime_executed else ("verified" if profile.origin_verified else "missing")
        method = (
            "tradingagents_graph_runtime_research_evidence"
            if runtime_executed else "local_structured_analyst_view_plus_contract"
        )
        profile_dict = asdict(profile)
        analyst_views = {
            "market_analyst": technical_view,
            "risk_analyst": "high_volatility" if technical["volatility_percent"] > 3 else "normal_volatility",
            "portfolio_manager": "hold_until_risk_gate_approves",
        }
        runtime_summary = (
            f"runtime decision: {runtime_receipt['result']['decision']}"
            if runtime_executed else f"runtime {runtime_receipt.get('status')}: {runtime_receipt.get('reason')}"
        )
        summary = (
            f"TradingAgents source {status}: technical view is {technical_view}; {runtime_summary}. "
            "Multi-agent graph output is research evidence only and cannot bypass OpenStockAIEngine."
        )
        model_provenance = {
            "schema_version": "open_stock_ai.model_output_provenance.v1",
            "provider": "TradingAgents",
            "provenance_type": "external_runtime" if runtime_executed else "local_projection",
            "model_output": runtime_executed,
            "runtime_status": runtime_receipt.get("status"),
            "runtime_receipt": runtime_receipt,
            "execution_authority": "none",
        }
        return {
            "summary": summary,
            "technical_view": technical_view,
            "technical_metrics": technical,
            "capability_contract": capability_contract,
            "analyst_views": analyst_views,
            "risk_debate_summary": risk_debate_summary,
            "risks": technical["risks"],
            "evidence": technical["evidence"],
            "runtime_receipt": runtime_receipt,
            "model_provenance": model_provenance,
            "method": method,
            "status": status,
            "external_project": profile_dict,
            "adapter_result": adapter_result_to_dict(
                source_key="tradingagents",
                source_name="TradingAgents",
                role="intelligence",
                method=method,
                status=status,
                summary=summary,
                evidence=technical["evidence"],
                risks=technical["risks"],
                metrics={
                    "technical_metrics": technical,
                    "analyst_views": analyst_views,
                    "risk_debate_summary": risk_debate_summary,
                    "model_provenance": model_provenance,
                },
                capability_contract=capability_contract,
                external_projects={"tradingagents": profile_dict},
            ),
        }

    def _risk_debate_summary(
        self,
        *,
        capability_contract: dict[str, Any],
        technical: dict[str, Any],
        request: StockRequest,
    ) -> dict[str, Any]:
        risk_contract = capability_contract.get("risk_memory_contract") or {}
        rating_scale = risk_contract.get("rating_scale") if isinstance(risk_contract, dict) else []
        debator_count = risk_contract.get("risk_debator_count", 0) if isinstance(risk_contract, dict) else 0
        volatility = self._number(technical.get("volatility_percent"))
        technical_view = str(technical.get("technical_view") or "neutral")
        if volatility is not None and volatility > 3:
            stance = "conservative"
        elif technical_view == "uptrend":
            stance = "aggressive"
        elif technical_view == "downtrend":
            stance = "conservative"
        else:
            stance = "neutral"
        return {
            "schema_version": "open_stock_ai.tradingagents_risk_debate.v1",
            "method": "local_tradingagents_risk_debate_projection",
            "symbol": request.symbol,
            "horizon": request.horizon,
            "stance": stance,
            "risk_debator_count": debator_count,
            "rating_scale": rating_scale if isinstance(rating_scale, list) else [],
            "technical_view": technical_view,
            "volatility_percent": volatility,
            "execution_boundary": "read_only_contract_no_tradingagents_runtime_import",
        }

    def _schema_contract(self, project_path: Path) -> dict[str, Any]:
        path = project_path / "tradingagents" / "agents" / "schemas.py"
        module = self._parse(path)
        classes: list[dict[str, Any]] = []
        render_functions: list[str] = []
        if module:
            for node in module.body:
                if isinstance(node, ast.ClassDef):
                    classes.append(
                        {
                            "name": node.name,
                            "bases": [self._name(base) for base in node.bases],
                            "fields": self._annotated_fields(node),
                            "enum_values": self._enum_values(node),
                            "doc_excerpt": self._excerpt(ast.get_docstring(node) or ""),
                        }
                    )
                elif isinstance(node, ast.FunctionDef) and node.name.startswith("render_"):
                    render_functions.append(node.name)
        return {
            "path": self._relative(project_path, path),
            "class_count": len(classes),
            "classes": classes,
            "render_functions": render_functions,
        }

    def _graph_contract(self, project_path: Path) -> dict[str, Any]:
        path = project_path / "tradingagents" / "graph" / "setup.py"
        module = self._parse(path)
        nodes: list[str] = []
        edges: list[dict[str, str | None]] = []
        conditional_edges: list[str] = []
        selected_analysts: list[str] = []
        if module:
            for node in ast.walk(module):
                if isinstance(node, ast.FunctionDef) and node.name == "setup_graph":
                    selected_analysts = self._selected_analysts(node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "add_node" and node.args:
                        node_name = self._literal_string(node.args[0])
                        if node_name:
                            nodes.append(node_name)
                    elif node.func.attr == "add_edge" and len(node.args) >= 2:
                        edges.append(
                            {
                                "source": self._literal_or_name(node.args[0]),
                                "target": self._literal_or_name(node.args[1]),
                            }
                        )
                    elif node.func.attr == "add_conditional_edges" and node.args:
                        source = self._literal_or_name(node.args[0])
                        if source:
                            conditional_edges.append(source)
        return {
            "path": self._relative(project_path, path),
            "selected_analysts": selected_analysts,
            "node_count": len(set(nodes)),
            "nodes": sorted(set(nodes)),
            "edge_count": len(edges),
            "edges": edges,
            "conditional_edge_sources": sorted(set(conditional_edges)),
        }

    def _risk_memory_contract(self, project_path: Path) -> dict[str, Any]:
        files = [
            project_path / "tradingagents" / "agents" / "risk_mgmt" / "aggressive_debator.py",
            project_path / "tradingagents" / "agents" / "risk_mgmt" / "conservative_debator.py",
            project_path / "tradingagents" / "agents" / "risk_mgmt" / "neutral_debator.py",
            project_path / "tradingagents" / "agents" / "utils" / "memory.py",
            project_path / "tradingagents" / "agents" / "utils" / "rating.py",
            project_path / "tradingagents" / "graph" / "signal_processing.py",
            project_path / "tradingagents" / "graph" / "reflection.py",
        ]
        parsed = []
        for path in files:
            parsed.append(
                {
                    "path": self._relative(project_path, path),
                    "exists": path.exists(),
                    **self._python_symbols(path),
                }
            )
        rating_path = project_path / "tradingagents" / "agents" / "utils" / "rating.py"
        return {
            "files": parsed,
            "rating_scale": self._rating_scale(rating_path),
            "risk_debator_count": sum(1 for item in parsed if "risk_mgmt" in item["path"] and item["exists"]),
        }

    def _number(self, value: object) -> float | None:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _annotated_fields(self, node: ast.ClassDef) -> list[str]:
        return [
            item.target.id
            for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        ]

    def _enum_values(self, node: ast.ClassDef) -> list[str]:
        values: list[str] = []
        for item in node.body:
            if not isinstance(item, ast.Assign):
                continue
            if len(item.targets) != 1 or not isinstance(item.targets[0], ast.Name):
                continue
            if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                values.append(item.value.value)
        return values

    def _selected_analysts(self, node: ast.FunctionDef) -> list[str]:
        for default in node.args.defaults:
            if isinstance(default, ast.Tuple):
                return [elt.value for elt in default.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
        return []

    def _rating_scale(self, path: Path) -> list[str]:
        module = self._parse(path)
        if not module:
            return []
        for node in module.body:
            value: ast.AST | None = None
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "RATINGS_5_TIER"
                for target in node.targets
            ):
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "RATINGS_5_TIER":
                value = node.value
            if isinstance(value, ast.Tuple):
                return [elt.value for elt in value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
        return []

    def _python_symbols(self, path: Path) -> dict[str, list[str]]:
        module = self._parse(path)
        if not module:
            return {"classes": [], "functions": []}
        return {
            "classes": [node.name for node in module.body if isinstance(node, ast.ClassDef)],
            "functions": [node.name for node in module.body if isinstance(node, ast.FunctionDef)],
        }

    def _parse(self, path: Path) -> ast.Module | None:
        if not path.exists():
            return None
        try:
            return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            return None

    def _literal_string(self, node: ast.AST) -> str | None:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def _literal_or_name(self, node: ast.AST) -> str | None:
        literal = self._literal_string(node)
        if literal:
            return literal
        if isinstance(node, ast.Name):
            return node.id
        return None

    def _name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{self._name(node.value)}.{node.attr}"
        return type(node).__name__

    def _excerpt(self, text: str, limit: int = 180) -> str:
        normalized = " ".join(text.split())
        return normalized[:limit] if normalized else ""

    def _relative(self, project_path: Path, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(project_path.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)
