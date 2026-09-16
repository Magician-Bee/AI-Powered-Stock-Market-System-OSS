from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class ReportGenerator:
    report_dir: str | Path = "output/reports"
    backtest_dir: str | Path = "output/backtests"

    def render_preview(self, payload: dict[str, Any]) -> str:
        request = payload.get("request") or {}
        signal = payload.get("signal") or {}
        research = payload.get("research") or {}
        risk = payload.get("risk") or {}
        qlib_projection = payload.get("qlib_factor_projection") or {}
        qlib_lines: list[str] = []
        if isinstance(qlib_projection, dict) and qlib_projection:
            qlib_lines = [
                "",
                "## Qlib Factor Projection",
                "",
                f"- Schema: {qlib_projection.get('schema_version', '-')}",
                f"- Selected model: {qlib_projection.get('selected_model', '-')}",
                f"- Model score: {qlib_projection.get('model_score', '-')}",
                f"- Rank IC proxy: {qlib_projection.get('rank_ic_proxy', '-')}",
                f"- Workflow count: {qlib_projection.get('workflow_count', '-')}",
            ]
        return "\n".join(
            [
                "# Open Stock AI Research Report",
                "",
                f"- Symbol: {request.get('symbol', '-')}",
                f"- Market: {request.get('market', '-')}",
                f"- Horizon: {request.get('horizon', '-')}",
                f"- Signal: {signal.get('action', '-')} ({signal.get('confidence', '-')})",
                f"- Research passed: {research.get('passed', '-')}",
                f"- Risk approved: {risk.get('approved', '-')}",
                "",
                "## Summary",
                "",
                str(research.get("summary") or signal.get("reason") or "No summary."),
                *qlib_lines,
            ]
        )

    def save_research_artifacts(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = payload.get("request") or {}
        symbol = self._safe_name(str(request.get("symbol") or "unknown"))
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = Path(self.report_dir) / f"{timestamp}-{symbol}-research.md"
        backtest_path = Path(self.backtest_dir) / f"{timestamp}-{symbol}-backtest.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        backtest_path.parent.mkdir(parents=True, exist_ok=True)

        report_text = self.render_preview(payload)
        report_path.write_text(report_text, encoding="utf-8")
        backtest_payload = {
            "request": request,
            "signal": payload.get("signal") or {},
            "research": payload.get("research") or {},
            "risk": payload.get("risk") or {},
            "adapter_results": payload.get("adapter_results") or [],
            "qlib_factor_projection": payload.get("qlib_factor_projection") or {},
            "generated_at": timestamp,
        }
        backtest_path.write_text(json.dumps(backtest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        report = str(report_path).replace("\\", "/")
        backtest = str(backtest_path).replace("\\", "/")
        return {
            "schema_version": "open_stock_ai.research_artifacts.v1",
            "method": "local_markdown_report_and_backtest_json",
            "generated_at": timestamp,
            "report_path": report,
            "backtest_path": backtest,
            "artifacts": [
                {
                    "kind": "research_report",
                    "path": report,
                    "media_type": "text/markdown",
                    "source": "open_stock_ai.research.report_generator",
                    "role": "human_review",
                    "exists": report_path.exists(),
                },
                {
                    "kind": "backtest_payload",
                    "path": backtest,
                    "media_type": "application/json",
                    "source": "open_stock_ai.research.report_generator",
                    "role": "machine_replay",
                    "exists": backtest_path.exists(),
                },
            ],
        }

    def _safe_name(self, value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "unknown"
