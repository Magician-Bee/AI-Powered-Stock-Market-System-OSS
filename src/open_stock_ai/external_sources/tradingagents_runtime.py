from __future__ import annotations

"""Bounded, research-only runner for the vendored TradingAgents graph.

The adapter does not translate a language-model opinion into a trade.  It
executes the upstream graph in an isolated child process only when an explicit
runtime configuration is supplied, validates a compact receipt, and otherwise
returns an honest disabled/failed receipt.  This makes the source tree useful
as actual research evidence without allowing it to become an execution path.
"""

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from open_stock_ai.types import MarketSnapshot, StockRequest


RuntimeRunner = Callable[[dict[str, Any]], dict[str, Any]]
_SCHEMA = "open_stock_ai.tradingagents_runtime_receipt.v1"
_ALLOWED_ANALYSTS = {"market", "social", "news", "fundamentals"}


@dataclass(slots=True)
class TradingAgentsRuntimeAdapter:
    project_root: Path
    runner: RuntimeRunner | None = None
    timeout_seconds: int = 180

    def run(self, request: StockRequest, snapshot: MarketSnapshot) -> dict[str, Any]:
        configuration = self._configuration(snapshot)
        if configuration is None:
            return self._receipt("disabled", reason="tradingagents_runtime_not_enabled")
        missing = [key for key in ("base_url", "model") if not configuration.get(key)]
        if missing:
            return self._receipt("disabled", reason=f"tradingagents_runtime_missing:{','.join(missing)}")
        project = self.project_root / "external" / "TradingAgents"
        if not (project / "tradingagents" / "graph" / "trading_graph.py").is_file():
            return self._receipt("disabled", reason="tradingagents_source_runtime_missing")
        payload = {
            "project_root": str(project),
            "symbol": request.symbol,
            "trade_date": self._trade_date(snapshot),
            "base_url": configuration["base_url"],
            "model": configuration["model"],
            "selected_analysts": configuration["selected_analysts"],
            "max_debate_rounds": configuration["max_debate_rounds"],
            "max_risk_discuss_rounds": configuration["max_risk_discuss_rounds"],
        }
        try:
            result = (self.runner or self._run_subprocess)(payload)
            validated = self._validate_result(result)
        except Exception as exc:
            return self._receipt("failed", reason=f"tradingagents_runtime_failed:{type(exc).__name__}")
        return {
            **self._receipt("executed"),
            "provider": {"kind": "openai_compatible", "base_url": payload["base_url"], "model": payload["model"]},
            "request": {"symbol": request.symbol, "trade_date": payload["trade_date"], "analysts": payload["selected_analysts"]},
            "result": validated,
            "result_sha256": self._hash(validated),
            "model_output": True,
            "execution_authority": "none",
            "execution_boundary": "research_evidence_only_no_order_authority",
        }

    def _configuration(self, snapshot: MarketSnapshot) -> dict[str, Any] | None:
        raw = snapshot.raw if isinstance(snapshot.raw, dict) else {}
        value = raw.get("tradingagents_runtime")
        if not isinstance(value, dict) or value.get("enabled") is not True:
            return None
        analysts = value.get("selected_analysts") or ["market", "news", "fundamentals"]
        analysts = [str(item) for item in analysts]
        if not analysts or any(item not in _ALLOWED_ANALYSTS for item in analysts):
            return {"base_url": "", "model": ""}
        return {
            "base_url": str(value.get("base_url") or "").rstrip("/"),
            "model": str(value.get("model") or ""),
            "selected_analysts": analysts,
            "max_debate_rounds": max(0, min(2, int(value.get("max_debate_rounds", 1)))),
            "max_risk_discuss_rounds": max(0, min(2, int(value.get("max_risk_discuss_rounds", 1)))),
        }

    def _run_subprocess(self, payload: dict[str, Any]) -> dict[str, Any]:
        script = """
import json, sys
config = json.loads(sys.stdin.read())
sys.path.insert(0, config.pop('project_root'))
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
graph_config = DEFAULT_CONFIG.copy()
graph_config.update({
  'llm_provider': 'openai', 'deep_think_llm': config['model'], 'quick_think_llm': config['model'],
  'backend_url': config['base_url'], 'selected_analysts': config['selected_analysts'],
  'max_debate_rounds': config['max_debate_rounds'], 'max_risk_discuss_rounds': config['max_risk_discuss_rounds'],
  'checkpoint_enabled': False,
})
graph = TradingAgentsGraph(selected_analysts=tuple(config['selected_analysts']), debug=False, config=graph_config)
state, decision = graph.propagate(config['symbol'], config['trade_date'])
print(json.dumps({'decision': str(decision), 'state_keys': sorted(str(key) for key in state.keys())}, ensure_ascii=False))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script], input=json.dumps(payload), text=True,
            capture_output=True, cwd=payload["project_root"], timeout=self.timeout_seconds, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("tradingagents_child_failed")
        return json.loads(completed.stdout)

    @staticmethod
    def _validate_result(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ValueError("tradingagents_result_not_object")
        decision = str(result.get("decision") or "").strip()
        state_keys = result.get("state_keys")
        if not decision or not isinstance(state_keys, list) or not all(isinstance(item, str) for item in state_keys):
            raise ValueError("tradingagents_result_schema_invalid")
        return {"decision": decision, "state_keys": sorted(set(state_keys))}

    @staticmethod
    def _trade_date(snapshot: MarketSnapshot) -> str:
        rows = snapshot.ohlcv if isinstance(snapshot.ohlcv, list) else []
        latest = rows[-1] if rows and isinstance(rows[-1], dict) else {}
        return str(latest.get("timestamp") or latest.get("date") or datetime.now(timezone.utc).date().isoformat())[:10]

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _receipt(status: str, *, reason: str | None = None) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA,
            "status": status,
            "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model_output": False,
            "execution_authority": "none",
            "execution_boundary": "research_evidence_only_no_order_authority",
        }
