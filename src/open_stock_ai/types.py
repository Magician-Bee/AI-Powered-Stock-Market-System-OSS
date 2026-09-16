from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Literal


Action = Literal["buy", "sell", "hold", "reduce", "add"]
Market = Literal["TW", "US", "CRYPTO"]
Horizon = Literal["intraday", "swing", "weekly", "monthly"]
AdapterRole = Literal["intelligence", "research", "contract"]
UniverseSource = Literal[
    "user_watchlist",
    "portfolio_positions",
    "explicit_symbols",
    "workflow_parameters",
    "all_twse_active",
    "all_tpex_active",
    "top_by_volume",
    "top_by_market_cap",
    "sector_members",
    "screening_query",
    "tool_discovered",
]
SymbolSource = Literal[
    "user_explicit",
    "ui_selected",
    "workflow_parameter",
    "portfolio_position",
    "tool_discovered",
    "session_reference",
    "none",
]


class MissingSymbolError(ValueError):
    """Raised when a symbol-required operation receives no explicit symbol."""


@dataclass(frozen=True)
class SymbolContext:
    symbol: str | None = None
    source: SymbolSource = "none"
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()
    user_confirmed: bool = False
    schema_version: str = "open_stock_ai.symbol_context.v1"

    def __post_init__(self) -> None:
        normalized = str(self.symbol or "").strip().upper() or None
        object.__setattr__(self, "symbol", normalized)
        if normalized is None and self.source != "none":
            raise ValueError("A SymbolContext without a symbol must use source='none'")
        if normalized is not None and self.source == "none":
            raise ValueError("A SymbolContext with a symbol must identify its source")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("SymbolContext confidence must be between 0 and 1")

    def require_symbol(self) -> str:
        if self.symbol is None:
            raise MissingSymbolError("This operation requires an explicit symbol; no fallback is allowed")
        return self.symbol


@dataclass(frozen=True)
class UniverseRequest:
    source: UniverseSource
    symbols: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 100
    schema_version: str = "open_stock_ai.universe_request.v1"

    def __post_init__(self) -> None:
        normalized = tuple(
            dict.fromkeys(str(symbol).strip().upper() for symbol in self.symbols if str(symbol).strip())
        )
        object.__setattr__(self, "symbols", normalized)
        invalid = [
            symbol
            for symbol in normalized
            if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^_-]{0,31}", symbol)
        ]
        if invalid:
            raise ValueError(f"UniverseRequest contains invalid symbols: {', '.join(invalid)}")
        if not 1 <= self.limit <= 5000:
            raise ValueError("UniverseRequest limit must be between 1 and 5000")
        if self.source == "explicit_symbols" and not normalized:
            raise ValueError(f"{self.source} requires at least one symbol")


@dataclass(frozen=True)
class UniverseSnapshot:
    source: UniverseSource | Literal["none"]
    symbols: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: str = "open_stock_ai.universe_snapshot.v1"

    @property
    def count(self) -> int:
        return len(self.symbols)

    @classmethod
    def empty(cls) -> "UniverseSnapshot":
        return cls(source="none")


@dataclass
class StockRequest:
    symbol: str
    market: Market = "TW"
    horizon: Horizon = "swing"
    schema_version: str = "open_stock_ai.stock_request.v1"

    def __post_init__(self) -> None:
        normalized = str(self.symbol or "").strip().upper()
        if not normalized:
            raise MissingSymbolError("StockRequest requires an explicit symbol; no fallback is allowed")
        self.symbol = normalized


@dataclass
class MarketSnapshot:
    symbol: str
    market: Market
    price: float | None = None
    ohlcv: list[dict[str, Any]] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)
    financials: dict[str, Any] = field(default_factory=dict)
    chips: dict[str, Any] = field(default_factory=dict)
    announcements: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "open_stock_ai.market_snapshot.v1"


@dataclass
class AdapterResult:
    schema_version: str
    source_key: str
    source_name: str
    role: AdapterRole
    method: str
    status: str
    loaded: bool
    origin_verified: bool
    lock_verified: bool
    summary: str
    evidence_count: int
    evidence: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    capability_contract: dict[str, Any] = field(default_factory=dict)
    external_projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    source_lineage: dict[str, Any] = field(default_factory=dict)
    execution_boundary: str | None = None


def adapter_result_to_dict(
    *,
    source_key: str,
    source_name: str,
    role: AdapterRole,
    method: str,
    status: str,
    summary: str,
    evidence: list[dict[str, Any]] | None = None,
    risks: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
    capability_contract: dict[str, Any] | None = None,
    external_projects: dict[str, dict[str, Any]] | None = None,
    loaded: bool | None = None,
) -> dict[str, Any]:
    evidence_items = evidence or []
    contract = _without_nested_adapter_result(capability_contract or {})
    projects = external_projects or {}
    origin_verified = bool(projects) and all(project.get("origin_verified") is True for project in projects.values())
    lock_verified = bool(projects) and all(project.get("lock_verified") is True for project in projects.values())
    contract_loaded = contract.get("loaded") is True if contract else True
    return {
        "schema_version": "open_stock_ai.adapter_result.v1",
        "source_key": source_key,
        "source_name": source_name,
        "role": role,
        "method": method,
        "status": status,
        "loaded": origin_verified and contract_loaded if loaded is None else loaded,
        "origin_verified": origin_verified,
        "lock_verified": lock_verified,
        "summary": summary,
        "evidence_count": len(evidence_items),
        "evidence": evidence_items,
        "risks": risks or [],
        "metrics": metrics or {},
        "capability_contract": contract,
        "external_projects": projects,
        "source_lineage": source_lineage_from_projects(projects),
        "execution_boundary": contract.get("execution_boundary"),
    }


def source_lineage_from_projects(projects: dict[str, dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for key, project in sorted(projects.items()):
        entries.append(
            {
                "source_key": key,
                "display_name": project.get("display_name") or key,
                "expected_origin": project.get("expected_origin"),
                "origin": project.get("origin"),
                "expected_branch": project.get("expected_branch"),
                "branch": project.get("branch"),
                "expected_head": project.get("expected_head"),
                "head": project.get("head"),
                "origin_verified": project.get("origin_verified") is True,
                "branch_verified": project.get("branch_verified") is True,
                "head_verified": project.get("head_verified") is True,
                "lock_verified": project.get("lock_verified") is True,
                "license_status": project.get("license_status"),
                "license_evidence_path": project.get("license_evidence_path") or project.get("license_path"),
            }
        )
    return {
        "schema_version": "open_stock_ai.external_evidence_lineage.v1",
        "method": "adapter_external_project_commit_lineage",
        "source_count": len(entries),
        "source_keys": [item["source_key"] for item in entries],
        "all_origin_verified": bool(entries) and all(item["origin_verified"] for item in entries),
        "all_lock_verified": bool(entries) and all(item["lock_verified"] for item in entries),
        "entries": entries,
    }


def _without_nested_adapter_result(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "adapter_result"}


@dataclass
class IntelligenceResult:
    symbol: str
    market: Market
    summary: str
    sentiment_score: float | None = None
    sentiment_label: str | None = None
    fundamental_view: str | None = None
    technical_view: str | None = None
    news_view: str | None = None
    risks: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    adapter_results: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "open_stock_ai.intelligence_result.v1"


@dataclass
class TradingSignal:
    symbol: str
    market: Market
    action: Action
    confidence: float | None
    horizon: Horizon
    reason: str
    entry_price: float | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    position_size_pct: float | None = None
    rule_score: float | None = None
    confidence_type: Literal["rule_score", "calibrated_probability", "model_self_reported", "none"] = "rule_score"
    confidence_calibrated: bool = False
    rule_set_id: str | None = None
    price_method_id: str | None = None
    decision_status: Literal["ready", "insufficient_data"] = "ready"
    evidence: list[dict[str, Any]] = field(default_factory=list)
    source_modules: list[str] = field(default_factory=list)
    decision_schema: dict[str, Any] = field(default_factory=dict)
    external_risk_evidence: dict[str, Any] = field(default_factory=dict)
    external_report_evidence: dict[str, Any] = field(default_factory=dict)
    ai_trader_validation: dict[str, Any] = field(default_factory=dict)
    ai_trader_signal_row: dict[str, Any] = field(default_factory=dict)
    ai_trader_interop_projection: dict[str, Any] = field(default_factory=dict)
    ai_trader_skill_route: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "open_stock_ai.trading_signal.v1"


@dataclass
class ResearchResult:
    passed: bool
    summary: str
    backtest_id: str | None = None
    sharpe: float | None = None
    max_drawdown_pct: float | None = None
    win_rate: float | None = None
    report_path: str | None = None
    adapter_results: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "open_stock_ai.research_result.v1"


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    max_position_size_pct: float
    adjusted_position_size_pct: float | None = None
    risk_notes: list[str] = field(default_factory=list)
    schema_version: str = "open_stock_ai.risk_decision.v1"
    gate_checks: list[dict[str, Any]] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    executed: bool
    mode: Literal["paper", "paper_pending_oms", "live_disabled"]
    order_id: str | None = None
    change_id: str | None = None
    reason: str = ""
    ledger: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "open_stock_ai.execution_result.v1"


@dataclass
class StockDecision:
    request: StockRequest
    market_snapshot: MarketSnapshot
    intelligence: IntelligenceResult
    signal: TradingSignal
    research: ResearchResult
    risk: RiskDecision
    execution: ExecutionResult
    schema_version: str = "open_stock_ai.stock_decision.v1"
