from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from open_stock_ai.external_sources.registry import ExternalProjectRegistry, default_registry
from open_stock_ai.research.backtest_research import BacktestResearch
from open_stock_ai.types import MarketSnapshot, StockRequest, TradingSignal, adapter_result_to_dict


@dataclass
class FinRLSource:
    registry: ExternalProjectRegistry | None = None
    backtest: BacktestResearch | None = None

    def load_capability_contract(self) -> dict[str, Any]:
        registry = self.registry or default_registry()
        finrl_profile = registry.profile(
            "finrl",
            capability_terms=["train", "trade", "backtest", "portfolio", "papertrading", "env"],
        )
        finrl_trading_profile = registry.profile(
            "finrl_trading",
            capability_terms=["backtest", "strategy", "adaptive_rotation", "paper_trading", "execution"],
        )
        finrl_path = Path(registry.root) / finrl_profile.path
        finrl_trading_path = Path(registry.root) / finrl_trading_profile.path
        projects = {
            "finrl": asdict(finrl_profile),
            "finrl_trading": asdict(finrl_trading_profile),
        }
        source_verified = finrl_profile.origin_verified and finrl_trading_profile.origin_verified
        adapter_result = adapter_result_to_dict(
            source_key="finrl",
            source_name="FinRL / FinRL-Trading",
            role="contract",
            method="local_backtest_paper_trading_contract",
            status="verified" if source_verified else "missing",
            summary=(
                "FinRL and FinRL-Trading source contracts were parsed locally. "
                "This confirms source lineage and interfaces only; no FinRL runtime, agent or policy was executed."
            ),
            capability_contract={
                "loaded": source_verified,
                "runtime_connected": False,
                "model_executed": False,
                "execution_evidence_eligible": False,
                "execution_boundary": "read_only_contract_no_finrl_runtime_import",
            },
            external_projects=projects,
        )
        return {
            "source": "FinRL / FinRL-Trading",
            "loaded": source_verified,
            "status": "verified" if source_verified else "missing",
            "runtime_status": "not_connected",
            "model_executed": False,
            "execution_evidence_eligible": False,
            "method": "local_backtest_paper_trading_contract",
            "adapter_result": adapter_result,
            "finrl_contract": self._finrl_contract(finrl_path),
            "finrl_trading_contract": self._finrl_trading_contract(finrl_trading_path),
            "execution_boundary": "read_only_contract_no_finrl_runtime_import",
        }

    def backtest_signal(self, request: StockRequest, signal: TradingSignal, market_snapshot: MarketSnapshot) -> dict:
        registry = self.registry or default_registry()
        finrl_profile = registry.profile(
            "finrl",
            capability_terms=["train", "trade", "backtest", "portfolio", "papertrading", "env"],
        )
        finrl_trading_profile = registry.profile(
            "finrl_trading",
            capability_terms=["backtest", "strategy", "adaptive_rotation", "paper_trading", "execution"],
        )
        source_verified = finrl_profile.origin_verified and finrl_trading_profile.origin_verified
        status = "verified" if source_verified else "missing"
        result = (self.backtest or BacktestResearch()).evaluate(request, signal, market_snapshot)
        capability_contract = self.load_capability_contract()
        method = "local_ohlcv_backtest_plus_contract"
        projects = {
            "finrl": asdict(finrl_profile),
            "finrl_trading": asdict(finrl_trading_profile),
        }
        note = (
            "FinRL and FinRL-Trading source trees are mapped for research contracts, but their runtimes are not "
            f"imported into the main process. {result.get('note', '')}"
        )
        metrics = {
            "backtest_id": result.get("backtest_id"),
            "validation_kind": result.get("validation_kind"),
            "sharpe": result.get("sharpe"),
            "max_drawdown_pct": result.get("max_drawdown_pct"),
            "win_rate": result.get("win_rate"),
            "strategy_return_pct": result.get("strategy_return_pct"),
            "benchmark_return_pct": result.get("benchmark_return_pct"),
            "excess_return_pct": result.get("excess_return_pct"),
            "trade_count": result.get("trade_count"),
            "sample_size": result.get("sample_size"),
            "lookahead_safe": result.get("lookahead_safe"),
            "transaction_costs_included": result.get("transaction_costs_included"),
            "strategy_replay_exact": result.get("strategy_replay_exact"),
            "empirical_valid": result.get("empirical_valid"),
            "execution_evidence_eligible": result.get("execution_evidence_eligible"),
        }
        backtest_projection = self._backtest_projection(
            request=request,
            signal=signal,
            result=result,
            capability_contract=capability_contract,
        )
        metrics["backtest_projection"] = backtest_projection
        return {
            **result,
            "status": status,
            "runtime_status": "not_connected",
            "model_executed": False,
            "note": note,
            "method": method,
            "backtest_projection": backtest_projection,
            "capability_contract": capability_contract,
            "external_projects": projects,
            "adapter_result": adapter_result_to_dict(
                source_key="finrl",
                source_name="FinRL / FinRL-Trading",
                role="research",
                method=method,
                status=status,
                summary=note,
                evidence=[],
                risks=list(result.get("approval_blockers") or []),
                metrics=metrics,
                capability_contract=capability_contract,
                external_projects=projects,
            ),
        }

    def _backtest_projection(
        self,
        *,
        request: StockRequest,
        signal: TradingSignal,
        result: dict[str, Any],
        capability_contract: dict[str, Any],
    ) -> dict[str, Any]:
        finrl_contract = capability_contract.get("finrl_contract") if isinstance(capability_contract, dict) else {}
        trading_contract = (
            capability_contract.get("finrl_trading_contract")
            if isinstance(capability_contract, dict)
            else {}
        )
        backtest_engine = (
            trading_contract.get("backtest_engine")
            if isinstance(trading_contract, dict) and isinstance(trading_contract.get("backtest_engine"), dict)
            else {}
        )
        sharpe = self._number(result.get("sharpe"))
        max_drawdown = self._number(result.get("max_drawdown_pct"))
        win_rate = self._number(result.get("win_rate"))
        empirical_valid = result.get("empirical_valid") is True
        exact_replay = result.get("strategy_replay_exact") is True
        lookahead_safe = result.get("lookahead_safe") is True
        costs_included = result.get("transaction_costs_included") is True
        execution_evidence_eligible = result.get("execution_evidence_eligible") is True
        passed = bool(
            result.get("passed")
            and empirical_valid
            and exact_replay
            and lookahead_safe
            and costs_included
            and execution_evidence_eligible
            and sharpe is not None
            and sharpe >= 1.0
            and (max_drawdown is None or max_drawdown <= 15.0)
        )
        blockers = list(result.get("approval_blockers") or [])
        if not empirical_valid and "backtest_not_empirically_valid" not in blockers:
            blockers.append("backtest_not_empirically_valid")
        if not exact_replay and not blockers:
            blockers.append("exact_strategy_replay_not_verified")
        return {
            "schema_version": "open_stock_ai.finrl_backtest_projection.v1",
            "method": "local_finrl_backtest_contract_projection",
            "symbol": request.symbol,
            "horizon": request.horizon,
            "signal_action": signal.action,
            "backtest_id": result.get("backtest_id"),
            "validation_kind": result.get("validation_kind"),
            "baseline_passed": result.get("baseline_passed") is True,
            "passed": passed,
            "runtime_connected": False,
            "model_executed": False,
            "empirical_valid": empirical_valid,
            "strategy_replay_exact": exact_replay,
            "lookahead_safe": lookahead_safe,
            "transaction_costs_included": costs_included,
            "execution_evidence_eligible": execution_evidence_eligible and passed,
            "approval_blockers": blockers,
            "metrics": {
                "sharpe": sharpe,
                "max_drawdown_pct": max_drawdown,
                "win_rate": win_rate,
                "strategy_return_pct": result.get("strategy_return_pct"),
                "benchmark_return_pct": result.get("benchmark_return_pct"),
                "excess_return_pct": result.get("excess_return_pct"),
                "trade_count": result.get("trade_count"),
                "sample_size": result.get("sample_size"),
                "transaction_cost_bps": result.get("transaction_cost_bps"),
            },
            "thresholds": {
                "min_sharpe": 1.0,
                "max_drawdown_pct": 15.0,
                "requires_exact_strategy_replay": True,
                "requires_empirical_validation": True,
            },
            "contract": {
                "finrl_paper_example_count": finrl_contract.get("paper_example_count", 0)
                if isinstance(finrl_contract, dict)
                else 0,
                "finrl_environment_count": finrl_contract.get("environment_count", 0)
                if isinstance(finrl_contract, dict)
                else 0,
                "finrl_trading_strategy_file_count": trading_contract.get("strategy_file_count", 0)
                if isinstance(trading_contract, dict)
                else 0,
                "adaptive_rotation_file_count": trading_contract.get("adaptive_rotation_file_count", 0)
                if isinstance(trading_contract, dict)
                else 0,
                "backtest_engine_path": backtest_engine.get("path"),
                "backtest_engine_classes": backtest_engine.get("classes", []),
                "has_backtest_engine": "BacktestEngine" in (backtest_engine.get("classes") or []),
            },
            "execution_boundary": "read_only_contract_no_finrl_runtime_import",
        }

    def _finrl_contract(self, project_path: Path) -> dict[str, Any]:
        examples_root = project_path / "examples"
        paper_examples = self._files_matching(examples_root, ["*PaperTrading*"])
        stock_examples = self._files_matching(examples_root, ["*StockTrading*"])
        env_root = project_path / "finrl" / "meta"
        env_files = self._files_matching(env_root, ["env_*.py", "*papertrading*.py"])
        core_modules = [
            project_path / "finrl" / "train.py",
            project_path / "finrl" / "trade.py",
            project_path / "finrl" / "config.py",
            project_path / "finrl" / "agents" / "portfolio_optimization" / "models.py",
            project_path / "finrl" / "agents" / "portfolio_optimization" / "algorithms.py",
        ]
        return {
            "paper_example_count": len(paper_examples),
            "paper_examples": [self._relative(project_path, path) for path in paper_examples[:10]],
            "stock_example_count": len(stock_examples),
            "stock_examples": [self._relative(project_path, path) for path in stock_examples[:10]],
            "environment_count": len(env_files),
            "environment_files": [self._relative(project_path, path) for path in env_files[:20]],
            "core_modules": [
                {
                    "path": self._relative(project_path, path),
                    **self._python_symbols(path),
                }
                for path in core_modules
                if path.exists()
            ],
        }

    def _finrl_trading_contract(self, project_path: Path) -> dict[str, Any]:
        backtest_path = project_path / "src" / "backtest" / "backtest_engine.py"
        strategy_root = project_path / "src" / "strategies"
        adaptive_root = strategy_root / "adaptive_rotation"
        strategy_files = self._files_matching(strategy_root, ["*.py"])
        adaptive_files = self._files_matching(adaptive_root, ["*.py"])
        figures = self._files_matching(project_path / "figs", ["*.png", "*.jpg"])
        return {
            "backtest_engine": {
                "path": self._relative(project_path, backtest_path),
                **self._python_symbols(backtest_path),
            },
            "strategy_file_count": len(strategy_files),
            "strategy_files": [self._relative(project_path, path) for path in strategy_files[:20]],
            "adaptive_rotation_file_count": len(adaptive_files),
            "adaptive_rotation_files": [self._relative(project_path, path) for path in adaptive_files[:20]],
            "artifact_count": len(figures),
            "artifacts": [self._relative(project_path, path) for path in figures[:10]],
        }

    def _files_matching(self, root: Path, patterns: list[str]) -> list[Path]:
        if not root.exists():
            return []
        found: list[Path] = []
        for pattern in patterns:
            found.extend(path for path in sorted(root.rglob(pattern)) if path.is_file())
        deduped: list[Path] = []
        for path in found:
            if path not in deduped and "__pycache__" not in path.parts:
                deduped.append(path)
        return deduped

    def _python_symbols(self, path: Path) -> dict[str, list[str]]:
        if not path.exists() or path.suffix != ".py":
            return {"classes": [], "functions": []}
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            module = ast.parse(text)
        except SyntaxError:
            return {"classes": [], "functions": []}
        return {
            "classes": [node.name for node in module.body if isinstance(node, ast.ClassDef)],
            "functions": [node.name for node in module.body if isinstance(node, ast.FunctionDef)],
        }

    def _number(self, value: object) -> float | None:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _relative(self, project_path: Path, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(project_path.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)
