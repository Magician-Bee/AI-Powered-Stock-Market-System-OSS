from __future__ import annotations

import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from open_stock_ai.config.loader import load_yaml_config
from open_stock_ai.external_sources.registry import ExternalProjectRegistry, default_registry
from open_stock_ai.intelligence.news_intelligence import NewsIntelligence
from open_stock_ai.intelligence.sentiment_intelligence import SentimentIntelligence
from open_stock_ai.types import MarketSnapshot, StockRequest, adapter_result_to_dict


@dataclass
class FinGPTSource:
    registry: ExternalProjectRegistry | None = None
    sentiment: SentimentIntelligence | None = None
    news: NewsIntelligence | None = None

    def load_capability_contract(self) -> dict[str, Any]:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "fingpt",
            capability_terms=[
                "sentiment",
                "forecaster",
                "rag",
                "market_sentiment",
                "prompt",
            ],
        )
        project_path = Path(registry.root) / profile.path
        profile_dict = asdict(profile)
        runtime_capability = self._runtime_capability(Path(registry.root))
        adapter_result = adapter_result_to_dict(
            source_key="fingpt",
            source_name="FinGPT",
            role="contract",
            method="fingpt_read_only_contract",
            status="verified" if profile.origin_verified else "missing",
            summary="FinGPT sentiment, forecaster, RAG, and trading contracts parsed without loading a FinGPT model.",
            capability_contract={
                "loaded": profile.origin_verified,
                "runtime_capability": runtime_capability,
                "execution_boundary": "read_only_contract_no_fingpt_runtime_import",
            },
            external_projects={"fingpt": profile_dict},
        )
        return {
            "source": "FinGPT",
            "loaded": profile.origin_verified,
            "status": "verified" if profile.origin_verified else "missing",
            "method": "fingpt_read_only_contract",
            "adapter_result": adapter_result,
            "benchmark_contract": self._benchmark_contract(project_path),
            "forecaster_contract": self._forecaster_contract(project_path),
            "rag_contract": self._rag_contract(project_path),
            "trading_contract": self._trading_contract(project_path),
            "runtime_capability": runtime_capability,
            "execution_boundary": "read_only_contract_no_fingpt_runtime_import",
        }

    def analyze_news(self, request: StockRequest, market_snapshot: MarketSnapshot) -> dict:
        registry = self.registry or default_registry()
        profile = registry.profile(
            "fingpt",
            capability_terms=[
                "sentiment",
                "forecaster",
                "rag",
                "market_sentiment",
                "prompt",
            ],
        )
        capability_contract = self.load_capability_contract()
        runtime_capability = capability_contract["runtime_capability"]
        sentiment = (self.sentiment or SentimentIntelligence()).analyze(market_snapshot.news)
        news_summary = (self.news or NewsIntelligence()).summarize(market_snapshot.news)
        forecast_projection = self._forecast_projection(
            request=request,
            market_snapshot=market_snapshot,
            sentiment=sentiment,
            capability_contract=capability_contract,
        )
        news_count = len(market_snapshot.news)
        status = "disabled" if runtime_capability["enabled"] is False else "runtime_not_available"
        summary = (
            f"FinGPT runtime {status}: {news_count} news items processed by a local baseline as "
            f"{sentiment['sentiment_label']} ({sentiment['sentiment_score']}); "
            "this is not FinGPT model inference."
        )
        profile_dict = asdict(profile)
        # This adapter is deterministic evidence processing. No FinGPT model is
        # downloaded, loaded, or inferred; it must never be presented as model output.
        method = "local_baseline_sentiment_news_summary_not_fingpt_inference"
        model_provenance = {
            "schema_version": "open_stock_ai.model_output_provenance.v1",
            "provider": "FinGPT",
            "provenance_type": "local_baseline",
            "model_output": False,
            "runtime_status": runtime_capability["status"],
            "reason": runtime_capability["reason"],
        }
        evidence = sentiment["evidence"]
        metrics = {
            "sentiment_score": sentiment["sentiment_score"],
            "sentiment_label": sentiment["sentiment_label"],
            "news_count": news_count,
            "forecast_projection": forecast_projection,
            "model_provenance": model_provenance,
        }
        return {
            "summary": summary,
            "sentiment_score": sentiment["sentiment_score"],
            "sentiment_label": sentiment["sentiment_label"],
            "forecast_projection": forecast_projection,
            "model_provenance": model_provenance,
            "risks": sentiment["risks"],
            "evidence": [*evidence, forecast_projection],
            "news_summary": news_summary,
            "capability_contract": capability_contract,
            "method": method,
            "status": status,
            "external_project": profile_dict,
            "adapter_result": adapter_result_to_dict(
                source_key="fingpt",
                source_name="FinGPT",
                role="intelligence",
                method=method,
                status=status,
                summary=summary,
                evidence=[*evidence, forecast_projection],
                risks=sentiment["risks"],
                metrics=metrics,
                capability_contract=capability_contract,
                external_projects={"fingpt": profile_dict},
                loaded=False,
            ),
        }

    def _forecast_projection(
        self,
        *,
        request: StockRequest,
        market_snapshot: MarketSnapshot,
        sentiment: dict[str, Any],
        capability_contract: dict[str, Any],
    ) -> dict[str, Any]:
        closes = [float(row.get("close")) for row in market_snapshot.ohlcv if row.get("close") is not None]
        momentum_5d = 0.0
        if len(closes) >= 5 and closes[-5]:
            momentum_5d = (closes[-1] - closes[-5]) / closes[-5]
        revenue = market_snapshot.financials.get("revenue") if isinstance(market_snapshot.financials, dict) else {}
        revenue_yoy = self._number((revenue or {}).get("yoy_change_percent")) if isinstance(revenue, dict) else None
        sentiment_score = self._number(sentiment.get("sentiment_score")) or 0.0
        forecast_score = max(
            -1.0,
            min(
                1.0,
                sentiment_score * 0.45
                + momentum_5d * 4.0
                + ((revenue_yoy or 0.0) / 100.0 * 0.35),
            ),
        )
        if forecast_score >= 0.25:
            direction = "up"
            bin_label = "up by 1-2%" if forecast_score < 0.55 else "up by 2-3%"
        elif forecast_score <= -0.25:
            direction = "down"
            bin_label = "down by 1-2%" if forecast_score > -0.55 else "down by 2-3%"
        else:
            direction = "flat"
            bin_label = "0-1%"
        forecaster = capability_contract.get("forecaster_contract") if isinstance(capability_contract, dict) else {}
        prompt_functions = forecaster.get("prompt_functions") if isinstance(forecaster, dict) else []
        prompt_constants = forecaster.get("prompt_constants") if isinstance(forecaster, dict) else []
        return {
            "schema_version": "open_stock_ai.fingpt_forecast_projection.v1",
            "method": "deterministic_forecaster_contract_projection",
            "symbol": request.symbol,
            "horizon": request.horizon,
            "direction": direction,
            "bin_label": bin_label,
            "forecast_score": round(forecast_score, 4),
            "inputs": {
                "sentiment_score": sentiment_score,
                "sentiment_label": sentiment.get("sentiment_label"),
                "momentum_5d": round(momentum_5d, 4),
                "revenue_yoy_percent": revenue_yoy,
                "news_count": len(market_snapshot.news),
            },
            "prompt_contract": {
                "path": forecaster.get("prompt_path") if isinstance(forecaster, dict) else None,
                "has_get_all_prompts": "get_all_prompts" in prompt_functions,
                "has_prompt_end": "PROMPT_END" in prompt_constants,
                "prompt_functions": prompt_functions,
            },
            "execution_boundary": "read_only_contract_no_fingpt_runtime_import",
            "model_provenance": {
                "schema_version": "open_stock_ai.model_output_provenance.v1",
                "provider": "FinGPT",
                "provenance_type": "local_baseline",
                "model_output": False,
            },
        }

    @staticmethod
    def _runtime_capability(root: Path) -> dict[str, Any]:
        config = load_yaml_config(root / "config" / "agent_runtime.yaml")
        models = config.get("models") if isinstance(config, dict) else {}
        enabled = bool(models.get("fingpt_local_model_enabled")) if isinstance(models, dict) else False
        return {
            "schema_version": "open_stock_ai.fingpt_runtime_capability.v1",
            "enabled": enabled,
            "status": "disabled" if not enabled else "not_implemented",
            "reason": "fingpt_local_model_enabled_false" if not enabled else "fingpt_runtime_adapter_not_implemented",
            "provenance_required": True,
            "execution_authority": "none",
        }

    def _benchmark_contract(self, project_path: Path) -> dict[str, Any]:
        benchmark_root = project_path / "fingpt" / "FinGPT_Benchmark"
        benchmark_files = []
        benchmark_dir = benchmark_root / "benchmarks"
        if benchmark_dir.exists():
            benchmark_files = [
                self._relative(project_path, path)
                for path in sorted(benchmark_dir.glob("*.py"))
                if path.name != "__init__.py"
            ]
        template_path = benchmark_dir / "sentiment_templates.txt"
        templates = [
            line.strip()
            for line in self._read_text(template_path).splitlines()
            if line.strip()
        ]
        config = self._read_json(benchmark_root / "config.json")
        return {
            "path": self._relative(project_path, benchmark_root),
            "benchmark_count": len(benchmark_files),
            "benchmarks": benchmark_files,
            "sentiment_template_count": len(templates),
            "sentiment_templates": templates[:5],
            "training_config_keys": sorted(config.keys()) if config else [],
        }

    def _forecaster_contract(self, project_path: Path) -> dict[str, Any]:
        forecaster_root = project_path / "fingpt" / "FinGPT_Forecaster"
        prompt_path = forecaster_root / "prompt.py"
        prompt_symbols = self._python_symbols(prompt_path)
        config = self._read_json(forecaster_root / "config.json")
        readme = self._readme_summary(forecaster_root / "README.md")
        return {
            "path": self._relative(project_path, forecaster_root),
            "prompt_path": self._relative(project_path, prompt_path),
            "prompt_functions": prompt_symbols["functions"],
            "prompt_constants": prompt_symbols["constants"],
            "training_config_keys": sorted(config.keys()) if config else [],
            "readme_title": readme.get("title"),
            "readme_excerpt": readme.get("excerpt"),
        }

    def _rag_contract(self, project_path: Path) -> dict[str, Any]:
        paths = [
            project_path / "fingpt" / "FinGPT_RAG",
            project_path / "fingpt" / "FinGPT_MultiAgentsRAG",
            project_path / "fingpt" / "FinGPT_FinancialReportAnalysis" / "utils" / "rag.py",
        ]
        entries = []
        for path in paths:
            if not path.exists():
                continue
            if path.is_dir():
                readme = self._readme_summary(path / "README.md")
                files = [self._relative(project_path, item) for item in sorted(path.glob("*")) if item.is_file()]
                entries.append(
                    {
                        "path": self._relative(project_path, path),
                        "readme_title": readme.get("title"),
                        "readme_excerpt": readme.get("excerpt"),
                        "file_count": len(files),
                    }
                )
            else:
                symbols = self._python_symbols(path)
                entries.append(
                    {
                        "path": self._relative(project_path, path),
                        "functions": symbols["functions"],
                        "constants": symbols["constants"],
                    }
                )
        return {
            "component_count": len(entries),
            "components": entries,
        }

    def _trading_contract(self, project_path: Path) -> dict[str, Any]:
        trading_root = project_path / "fingpt" / "FinGPT_Others" / "FinGPT_Trading"
        readmes = []
        if trading_root.exists():
            for path in sorted(trading_root.rglob("README.md")):
                readme = self._readme_summary(path)
                readmes.append(
                    {
                        "path": self._relative(project_path, path),
                        "title": readme.get("title"),
                        "excerpt": readme.get("excerpt"),
                    }
                )
        return {
            "path": self._relative(project_path, trading_root),
            "strategy_readme_count": len(readmes),
            "strategy_readmes": readmes,
        }

    def _python_symbols(self, path: Path) -> dict[str, list[str]]:
        text = self._read_text(path)
        if not text:
            return {"functions": [], "constants": []}
        module = ast.parse(text)
        functions = [node.name for node in module.body if isinstance(node, ast.FunctionDef)]
        constants = [
            node.targets[0].id
            for node in module.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.isupper()
        ]
        return {"functions": functions, "constants": constants}

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _number(self, value: object) -> float | None:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _readme_summary(self, path: Path) -> dict[str, str | None]:
        text = self._read_text(path)
        if not text:
            return {"title": None, "excerpt": None}
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = None
        for line in lines:
            if line.startswith("#"):
                title = line.lstrip("#").strip()
                break
        body = " ".join(line for line in lines if not line.startswith(("#", "!", "[!", "<")))
        return {"title": title, "excerpt": body[:220] if body else None}

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def _relative(self, project_path: Path, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(project_path.resolve())).replace("\\", "/")
        except ValueError:
            return str(path)
