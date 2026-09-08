from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from open_stock_ai.agent_runtime.contracts import AgentRunContext, AgentToolSpec
from open_stock_ai.agent_workspace import (
    build_agent_tool_manifest,
    build_agent_watchlist,
    build_agent_workspace,
)

from .paper_training_api import (
    PaperOrderRequest,
    paper_training_account,
    paper_training_learning,
    paper_training_mark_to_market,
    paper_training_order,
    paper_training_preview,
    research_pack,
)
from .agent_general_tools import GeneralAgentToolProvider
from .broker_tools import BrokerAgentToolProvider
from .capability_registry import BoundCapabilityProvider, CapabilityRegistry
from .external_project_tools import ExternalProjectToolProvider
from .official_derivatives import fetch_taifex_foreign_futures_open_interest
from .phase1_data import (
    TWSE_T86_URL,
    list_institutional_flows,
    list_monthly_revenues,
    list_securities_master,
    securities_master_status,
)
from .tool_providers import (
    BrowserToolProvider,
    AgentRuntimeToolProvider,
    GitToolProvider,
    MCPToolProvider,
    NotificationToolProvider,
    ScheduleToolProvider,
    SkillToolProvider,
    UIToolProvider,
)


_MARKET = {"type": "string", "enum": ["TW", "US", "CRYPTO"]}
_HORIZON = {"type": "string", "enum": ["intraday", "swing", "weekly", "monthly"]}
_SECURITY_MASTER_AGENT_TIMEOUT_SECONDS = 12.0
_ORDER_PROPERTIES: dict[str, Any] = {
    "symbol": {"type": "string"},
    "side": {"type": "string", "enum": ["buy", "sell", "add", "reduce"]},
    "order_type": {"type": "string", "enum": ["market", "limit", "stop", "stop_limit"]},
    "time_in_force": {"type": "string", "enum": ["rod", "ioc", "fok"]},
    "lot_type": {"type": "string", "enum": ["board_lot", "odd_lot"]},
    "session": {"type": "string", "enum": ["regular", "after_hours"]},
    "quantity_lots": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}]},
    "quantity_shares": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}]},
    "cash_amount": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}]},
    "position_size_pct": {
        "anyOf": [{"type": "number", "exclusiveMinimum": 0, "maximum": 100}, {"type": "null"}]
    },
    "limit_price": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}]},
    "stop_price": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}]},
    "episode_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "rationale": {"type": "string"},
}
_ORDER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["symbol", "side"],
    "properties": _ORDER_PROPERTIES,
}
_SYMBOL_PLACEHOLDER = re.compile(r"^<?(?:SYMBOL\d*|TARGET|UNKNOWN|N/?A|NONE)>?$", re.IGNORECASE)


class StockAgentToolRegistry:
    """The executable Stock AI weapon system exposed to replaceable Agents."""

    def __init__(self) -> None:
        self.general_tools = GeneralAgentToolProvider()
        self.runtime_tools = AgentRuntimeToolProvider()
        self.git_tools = GitToolProvider(self.general_tools.project_root)
        self.external_tools = ExternalProjectToolProvider()
        self.skill_tools = SkillToolProvider(self.general_tools.project_root)
        self.mcp_tools = MCPToolProvider()
        self.ui_tools = UIToolProvider()
        self.browser_tools = BrowserToolProvider(self.general_tools.project_root)
        self.notification_tools = NotificationToolProvider()
        self.schedule_tools = ScheduleToolProvider()
        self.broker_tools = BrokerAgentToolProvider()
        self._specs = {
            spec.name: spec
            for spec in (
                AgentToolSpec(
                    name="market.scan_watchlist",
                    description="Run the real OpenStockAIEngine over the configured watchlist and rank candidates.",
                    category="market_research",
                    skills=("stock-ai-market-analyst",),
                    packages=("OpenStockAIEngine",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "horizon": _HORIZON,
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                    },
                ),
                AgentToolSpec(
                    name="market.analyze_symbol",
                    description="Run market data, intelligence, strategy, research and RiskEngine for one symbol.",
                    category="market_research",
                    skills=("stock-ai-market-analyst",),
                    packages=("OpenStockAIEngine", "RiskEngine"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol"],
                        "properties": {"symbol": {"type": "string"}, "market": _MARKET, "horizon": _HORIZON},
                    },
                ),
                AgentToolSpec(
                    name="market.analyze_universe",
                    description=(
                        "Analyze every symbol in one explicitly resolved Market Radar Universe and return "
                        "separate observations, rule output and Host risk output for each symbol."
                    ),
                    category="market_research",
                    skills=("stock-ai-market-analyst",),
                    packages=("OpenStockAIEngine", "RiskEngine"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbols"],
                        "properties": {
                            "symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 20,
                            },
                            "horizon": _HORIZON,
                        },
                    },
                ),
                AgentToolSpec(
                    name="market.research_pack",
                    description="Read verified price, technical features, events, pipeline decision and symbol memory.",
                    category="market_research",
                    skills=("stock-ai-market-analyst", "market-intel"),
                    packages=("MarketDataHub", "IntelligenceHub"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol"],
                        "properties": {"symbol": {"type": "string"}, "horizon": _HORIZON},
                    },
                ),
                AgentToolSpec(
                    name="market.institutional_flow",
                    description=(
                        "Read official TWSE institutional buy/sell flow for one Taiwan symbol, separated into "
                        "foreign, investment-trust and dealer net values with trade date and source."
                    ),
                    category="market_research",
                    skills=("stock-ai-market-analyst", "institutional-flow"),
                    packages=("TWSE T86", "MarketDataHub"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol"],
                        "properties": {
                            "symbol": {"type": "string"},
                            "date": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                        },
                    },
                ),
                AgentToolSpec(
                    name="market.monthly_revenue",
                    description=(
                        "Read official TWSE monthly revenue evidence for one Taiwan symbol, including period, "
                        "month-over-month/year-over-year changes, publication metadata and source."
                    ),
                    category="market_research",
                    skills=("stock-ai-market-analyst", "fundamental-analysis"),
                    packages=("TWSE OpenAPI", "FundamentalDataHub"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["symbol"],
                        "properties": {
                            "symbol": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 24},
                        },
                    },
                ),
                AgentToolSpec(
                    name="market.taifex_foreign_open_interest",
                    description=(
                        "Read the latest officially published TAIFEX foreign institutional futures open-interest "
                        "position directly from TAIFEX OpenAPI. Use this instead of web search for Taiwan index "
                        "futures foreign net-long/net-short questions."
                    ),
                    category="market_research",
                    skills=("taifex-official-data",),
                    packages=("TAIFEX OpenAPI", "httpx"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "contract": {
                                "type": "string",
                                "enum": ["臺股期貨", "小型臺指期貨", "微型臺指期貨"],
                            }
                        },
                    },
                ),
                AgentToolSpec(
                    name="market.search_taiwan_securities",
                    description=(
                        "Search the complete auto-refreshed TWSE and TPEx security master, including newly listed "
                        "stocks and ETFs. Returns official listing metadata and universe synchronization status."
                    ),
                    category="market_research",
                    skills=("stock-ai-market-analyst",),
                    packages=("TWSE OpenAPI", "TPEx OpenAPI"),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "query": {"type": "string"},
                            "exchange": {"type": "string", "enum": ["all", "twse", "tpex"]},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                            "recent_first": {"type": "boolean"},
                            "refresh": {"type": "boolean"},
                        },
                    },
                ),
                AgentToolSpec(
                    name="portfolio.snapshot",
                    description="Read the shared SQLite paper account, cash, positions, open orders and fills.",
                    category="portfolio",
                    packages=("SQLite Paper Account",),
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                ),
                AgentToolSpec(
                    name="memory.learning",
                    description="Read evaluated paper episodes, rewards and saved reflections.",
                    category="memory",
                    skills=("heartbeat",),
                    packages=("SQLite learning store",),
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                    },
                ),
                AgentToolSpec(
                    name="paper.preview_order",
                    description=(
                        "Resolve a verified server-side price and preview an exact local paper-training order. "
                        "Risk evidence is recorded as advisory; the Paper Broker still enforces price, cash, "
                        "owned-position and exchange-rule validation."
                    ),
                    category="paper_execution",
                    packages=("PaperBroker", "RiskEngine"),
                    input_schema=_ORDER_SCHEMA,
                    # One bounded re-check covers a connector that briefly
                    # reports a research-only quote while it refreshes to an
                    # execution-eligible last trade.  Submission is still a
                    # separate, exact-preview-gated operation.
                    retry_policy={"max_attempts": 2, "backoff": "bounded_exponential"},
                ),
                AgentToolSpec(
                    name="paper.submit_order",
                    description=(
                        "Submit an exact previously previewed local paper-training order and return its fill. "
                        "This never submits a live brokerage order."
                    ),
                    category="paper_execution",
                    input_schema=_ORDER_SCHEMA,
                    mutating=True,
                    requires_paper_execution=True,
                    idempotency="arguments",
                    packages=("PaperBroker", "RiskEngine"),
                ),
                AgentToolSpec(
                    name="paper.mark_to_market",
                    description="Refresh prices for positions and open paper orders, then return account state.",
                    category="paper_execution",
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                    mutating=True,
                    requires_paper_execution=True,
                    packages=("PaperBroker", "MarketDataHub"),
                ),
                AgentToolSpec(
                    name="system.capabilities",
                    description="Read the authoritative Agent tool manifest and immutable execution boundaries.",
                    category="system",
                    packages=("AgentToolRegistry",),
                    input_schema={"type": "object", "additionalProperties": False, "properties": {}},
                ),
            )
        }
        self.capability_registry = CapabilityRegistry(
            (
                BoundCapabilityProvider(
                    provider_id="stock_core",
                    specs=self._specs,
                    executor=self._execute_core,
                    status=lambda: {
                        "configured": True,
                        "runtime_ready": True,
                        "health": "ready",
                        "live_trading": False,
                        "paper_execution": True,
                    },
                ),
                self.general_tools,
                self.runtime_tools,
                self.git_tools,
                self.external_tools,
                self.skill_tools,
                self.mcp_tools,
                self.ui_tools,
                self.browser_tools,
                self.notification_tools,
                self.schedule_tools,
                self.broker_tools,
            )
        )

    def manifest(self) -> list[dict[str, Any]]:
        return self.capability_registry.manifest()

    async def prepare(self, context: AgentRunContext) -> None:
        await self.capability_registry.prepare(context)

    async def close_run(self, run_id: str) -> None:
        await self.browser_tools.close_run(run_id)

    def describe_capabilities(self) -> dict[str, Any]:
        return self.capability_registry.describe()

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        return await self.capability_registry.execute(name, arguments, context)

    async def _execute_core(
        self,
        name: str,
        arguments: dict[str, Any],
        context: AgentRunContext,
    ) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"Unknown Stock AI tool: {name}")
        if spec.requires_paper_execution and not context.allow_paper_orders:
            raise PermissionError(f"{name} requires autonomy=paper_execute")

        if name == "market.scan_watchlist":
            result = await asyncio.to_thread(
                build_agent_watchlist,
                horizon=str(arguments.get("horizon") or "swing"),
                limit=max(1, min(int(arguments.get("limit") or 20), 100)),
            )
            return _compact_watchlist(result)
        if name == "market.analyze_symbol":
            symbol = _symbol(arguments, context)
            result = await asyncio.to_thread(
                build_agent_workspace,
                symbol=symbol,
                market=str(arguments.get("market") or _infer_market(symbol)),
                horizon=str(arguments.get("horizon") or "swing"),
            )
            return _compact_workspace(result)
        if name == "market.analyze_universe":
            requested = [
                _require_concrete_symbol(value)
                for value in arguments.get("symbols") or []
                if str(value).strip()
            ]
            symbols = list(dict.fromkeys(requested))
            if not symbols:
                raise ValueError("market.analyze_universe requires at least one explicit symbol")
            if len(symbols) > 20:
                raise ValueError("market.analyze_universe accepts at most 20 symbols per run")
            horizon = str(arguments.get("horizon") or "swing")

            async def analyze(symbol: str) -> dict[str, Any]:
                try:
                    result = await asyncio.to_thread(
                        build_agent_workspace,
                        symbol=symbol,
                        market=_infer_market(symbol),
                        horizon=horizon,
                    )
                except Exception as exc:
                    return {
                        "symbol": symbol,
                        "ok": False,
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                return {
                    "symbol": symbol,
                    "ok": True,
                    "analysis": _compact_workspace(result),
                }

            items = await asyncio.gather(*(analyze(symbol) for symbol in symbols))
            return {
                "schema_version": "stock_ai.market_universe_observation.v1",
                "symbols": symbols,
                "count": len(items),
                "success_count": sum(1 for item in items if item.get("ok") is True),
                "error_count": sum(1 for item in items if item.get("ok") is not True),
                "items": items,
            }
        if name == "market.research_pack":
            symbol = _symbol(arguments, context)
            result = await asyncio.to_thread(
                research_pack,
                symbol=symbol,
                horizon=str(arguments.get("horizon") or "swing"),
            )
            return _compact_research_pack(result)
        if name == "market.institutional_flow":
            symbol = _symbol(arguments, context)
            requested_date = str(arguments.get("date") or "") or None
            items = await asyncio.to_thread(
                list_institutional_flows,
                symbol=symbol,
                date=requested_date,
                limit=max(1, min(int(arguments.get("limit") or 10), 30)),
            )
            return {
                "schema_version": "stock_ai.institutional_flow_evidence.v1",
                "symbol": symbol,
                "count": len(items),
                "data_status": "available" if items else "no_data",
                "items": [item.model_dump() for item in items],
                "provenance": {
                    "source_id": "twse_openapi",
                    "source": "TWSE official T86 daily institutional flow",
                    "source_url": TWSE_T86_URL.format(
                        date=(requested_date or datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")).replace("-", "")
                    ),
                    "published_at": items[0].trade_date if items else None,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        if name == "market.monthly_revenue":
            symbol = _symbol(arguments, context)
            items = await asyncio.to_thread(
                list_monthly_revenues,
                symbol=symbol,
                limit=max(1, min(int(arguments.get("limit") or 12), 24)),
            )
            return {
                "schema_version": "stock_ai.monthly_revenue_evidence.v1",
                "symbol": symbol,
                "count": len(items),
                "data_status": "available" if items else "no_data",
                "items": [item.model_dump() for item in items],
            }
        if name == "market.taifex_foreign_open_interest":
            return await asyncio.to_thread(
                fetch_taifex_foreign_futures_open_interest,
                str(arguments.get("contract") or "臺股期貨"),
            )
        if name == "market.search_taiwan_securities":
            refresh = arguments.get("refresh") is True
            # Official master refreshes can wait on both TWSE and TPEx.  An
            # Agent Run must surface that failure and revise its plan instead
            # of leaving the Dock permanently at an executing tool step.
            try:
                status = await asyncio.wait_for(
                    asyncio.to_thread(securities_master_status, refresh=refresh),
                    timeout=_SECURITY_MASTER_AGENT_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    "台股證券主檔同步逾時；Host 未使用過期或猜測資料，請稍後重試。"
                ) from exc
            result_limit = max(1, min(int(arguments.get("limit") or 100), 500))
            recent_first = arguments.get("recent_first") is True
            try:
                rows = await asyncio.wait_for(
                    asyncio.to_thread(
                        list_securities_master,
                        q=str(arguments.get("query") or ""),
                        market=str(arguments.get("exchange") or "all"),
                        limit=5000 if recent_first else result_limit,
                    ),
                    timeout=_SECURITY_MASTER_AGENT_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    "台股證券主檔查詢逾時；Host 未回填過期或猜測資料，請稍後重試。"
                ) from exc
            if recent_first:
                rows.sort(key=lambda item: item.list_date or "", reverse=True)
                rows = rows[:result_limit]
            return {
                "count": len(rows),
                "items": [item.model_dump() for item in rows],
                "universe_sync": status,
            }
        if name == "portfolio.snapshot":
            return await asyncio.to_thread(paper_training_account, refresh_prices=False)
        if name == "memory.learning":
            return await asyncio.to_thread(
                paper_training_learning,
                limit=max(1, min(int(arguments.get("limit") or 20), 100)),
            )
        if name == "paper.preview_order":
            request = _paper_request(arguments)
            if context.allow_paper_orders:
                request = request.model_copy(update={"training_fill_at_latest_mark": True})
            result = await asyncio.to_thread(paper_training_preview, request)
            signature = _order_signature(request)
            context.previewed_orders.add(signature)
            context.state.setdefault("paper_previews", {})[signature] = {
                "created_monotonic": time.monotonic(),
                "price": (result.get("market") or {}).get("price"),
                "source_signature": ((result.get("market") or {}).get("source_envelope") or {}).get("signature"),
                "broker_can_submit": result.get("can_submit") is True,
            }
            return result
        if name == "paper.submit_order":
            request = _paper_request(arguments)
            if context.allow_paper_orders:
                request = request.model_copy(update={"training_fill_at_latest_mark": True})
            signature = _order_signature(request)
            has_local_preview = signature in context.previewed_orders
            if not has_local_preview and context.state.get("explicit_paper_order_authorized") is not True:
                raise PermissionError("The exact paper order must be previewed in this Agent run before submission")
            receipt = (context.state.get("paper_previews") or {}).get(signature) or {}
            if has_local_preview:
                age_seconds = time.monotonic() - float(receipt.get("created_monotonic") or 0.0)
                if age_seconds > 30.0:
                    raise PermissionError("The paper-order preview expired; preview the exact order again")
            # The supervised in-process worker may be recreated between the
            # preview and submit Plan nodes.  For an explicit local paper
            # experiment, re-resolve the exact server-side preview instead of
            # treating worker-local memory loss as if the user skipped it.
            current_preview = await asyncio.to_thread(paper_training_preview, request)
            current_market = current_preview.get("market") or {}
            if has_local_preview and (
                current_market.get("price") != receipt.get("price")
                or (current_market.get("source_envelope") or {}).get("signature") != receipt.get("source_signature")
            ):
                raise PermissionError("The verified market price changed; preview the exact order again")
            if current_preview.get("can_submit") is not True:
                raise PermissionError("Paper Broker validation rejected the Agent paper order")
            return await asyncio.to_thread(paper_training_order, request)
        if name == "paper.mark_to_market":
            return await asyncio.to_thread(paper_training_mark_to_market)
        if name == "system.capabilities":
            return {
                "schema_version": "open_stock_ai.agent_capabilities.v1",
                "host_tool_manifest": build_agent_tool_manifest(),
                "runtime_tools": self.manifest(),
                "capability_registry": self.describe_capabilities(),
                "paper_execution_enabled_for_run": context.allow_paper_orders,
                "project_execution_enabled_for_run": context.allow_project_actions,
                "external_execution_enabled_for_run": context.allow_external_actions,
                "live_trading_enabled": False,
                "inventory_is_not_execution": True,
                "execution_boundary": "local_paper_broker_only_no_live_submission",
            }
        raise RuntimeError(f"Stock AI tool is registered but has no executor: {name}")


def _paper_request(arguments: dict[str, Any]) -> PaperOrderRequest:
    payload = {key: value for key, value in arguments.items() if key in _ORDER_PROPERTIES}
    payload["actor"] = "agent"
    request = PaperOrderRequest.model_validate(payload)
    _require_concrete_symbol(request.symbol)
    return request


def _order_signature(request: PaperOrderRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"actor"}, exclude_none=False)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _symbol(arguments: dict[str, Any], context: AgentRunContext) -> str:
    return _require_concrete_symbol(arguments.get("symbol") or context.symbols[0])


def _require_concrete_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or _SYMBOL_PLACEHOLDER.fullmatch(symbol):
        raise ValueError("A concrete stock symbol is required; placeholders such as <SYMBOL1> cannot be executed")
    return symbol


def _infer_market(symbol: str) -> str:
    if symbol.endswith((".TW", ".TWO")) or symbol.replace(".", "").isdigit():
        return "TW"
    if symbol.endswith(("-USD", "/USD")):
        return "CRYPTO"
    return "US"


def _compact_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "schema_version",
        "symbol",
        "market",
        "horizon",
        "recommendation_bucket",
        "execution_permission",
        "analysis_only",
        "ranking",
        "portfolio_status",
        "data_status",
        "research_status",
        "signal_summary",
        "analysis_snapshot",
        "risk_summary",
        "blockers",
        "agent_next_actions",
        "execution_boundary",
    )
    return {key: workspace.get(key) for key in keys}


def _compact_watchlist(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": result.get("schema_version"),
        "horizon": result.get("horizon"),
        "count": result.get("count"),
        "bucket_counts": result.get("bucket_counts"),
        "portfolio": result.get("portfolio"),
        "items": [_compact_workspace(item) for item in result.get("items") or []],
        "execution_boundary": result.get("execution_boundary"),
    }


def _compact_research_pack(result: dict[str, Any]) -> dict[str, Any]:
    workspace = result.get("pipeline_workspace") or {}
    return {
        "schema_version": result.get("schema_version"),
        "generated_at": result.get("generated_at"),
        "symbol": result.get("symbol"),
        "market_price": result.get("market_price"),
        "technical_features": result.get("technical_features"),
        "recent_history": (result.get("history") or [])[-60:],
        "recent_events": (result.get("events") or [])[:25],
        "pipeline_workspace": _compact_workspace(workspace),
        "paper_position": result.get("paper_position"),
        "paper_account_summary": result.get("paper_account_summary"),
        "learning_for_symbol": (result.get("learning_for_symbol") or [])[:30],
    }
