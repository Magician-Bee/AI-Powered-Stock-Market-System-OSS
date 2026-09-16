from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..data_quality_contracts import DataQuality, DecisionDataQualityReceipt, utc_now


SnapshotStatus = Literal[
    "initializing",
    "ready",
    "refreshing",
    "partial",
    "stale",
    "model_failed",
    "data_degraded",
    "failed",
]

CandidateCategory = Literal[
    "actionable_now",
    "near_actionable",
    "wait_for_pullback",
    "wait_for_breakout",
    "avoid_now",
    "high_risk",
    "add",
    "hold",
    "reduce",
    "exit",
    "insufficient_data",
]

PortfolioAction = Literal["hold", "add", "reduce", "exit"]
MarketCategory = Literal["BUY_NOW", "WATCH", "FUTURE_BUY", "AVOID_NOW", "INSUFFICIENT_DATA"]

RANKING_CATEGORIES: tuple[CandidateCategory, ...] = (
    "actionable_now",
    "near_actionable",
    "wait_for_pullback",
    "wait_for_breakout",
    "avoid_now",
    "high_risk",
    "insufficient_data",
)

POSITION_CATEGORIES: tuple[CandidateCategory, ...] = ("add", "hold", "reduce", "exit")


class CandidateEvidence(BaseModel):
    evidence_id: str
    source: str
    statement: str
    observed_at: str | None = None
    acquired_at: str = Field(default_factory=utc_now)
    quality: Literal["valid", "partial", "invalid"] = "valid"
    fallback: bool = False


class CandidateDetail(BaseModel):
    symbol: str
    entity_id: str | None = None
    name: str
    exchange: str
    industry: str | None = None
    # Product classification is carried with every decision row so consumers
    # can enforce the same ordinary-stock universe rule as the scanner.
    is_etf: bool = False
    is_warrant: bool = False
    is_managed_stock: bool = False
    is_special_security: bool = False
    product_classification: dict[str, Any] = Field(default_factory=lambda: {
        "schema_version": "stock_ai.product_classification.v1", "status": "unknown",
        "product_type": "unknown", "reasons": ["product_classification_missing"],
    })
    lifecycle_status: str | None = None
    product_entry_assessment: dict[str, Any] = Field(default_factory=dict)
    category: CandidateCategory
    market_category: MarketCategory | None = None
    portfolio_action: PortfolioAction | None = None
    rank: int = Field(default=0, ge=0)
    previous_rank: int | None = Field(default=None, ge=0)
    rank_change: int | None = None
    score: float = Field(default=0.0, ge=0, le=100)
    latest_price: float | None = None
    change_percent: float | None = None
    volume: int | None = None
    trade_value: float | None = None
    range_position: float | None = Field(default=None, ge=0, le=1)
    decision_label: str
    primary_reason: str
    positive_reasons: list[str] = Field(default_factory=list)
    contrary_reasons: list[str] = Field(default_factory=list)
    trigger: str
    trigger_price: float | None = None
    trigger_event: str | None = None
    invalidation: str
    invalidation_price: float | None = None
    next_review: str
    distance_to_trigger_percent: float | None = None
    risk_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    liquidity_status: Literal["pass", "limited", "blocked", "unknown"] = "unknown"
    host_risk_status: Literal["passed", "blocked", "not_applicable", "unknown"] = "unknown"
    portfolio_fit: Literal["fit", "blocked", "not_held", "held", "unknown"] = "unknown"
    market_fit: Literal[
        "bullish", "neutral_bullish", "neutral", "neutral_weak", "bearish",
        "data_insufficient", "blocked", "unknown",
    ] = "unknown"
    is_position: bool = False
    position_weight_percent: float | None = None
    data_quality: DataQuality
    data_quality_receipt: DecisionDataQualityReceipt | None = None
    evidence: list[CandidateEvidence] = Field(default_factory=list)
    factor_scores: dict[str, dict[str, Any]] = Field(default_factory=dict)
    model_status: Literal["not_analyzed", "queued", "succeeded", "failed"] = "not_analyzed"
    model_receipt: dict[str, Any] | None = None
    model_overlay: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return str(value).strip().upper()

    @model_validator(mode="after")
    def derive_market_category(self) -> "CandidateDetail":
        if self.market_category is None:
            self.market_category = {
                "actionable_now": "BUY_NOW",
                "near_actionable": "WATCH",
                "wait_for_breakout": "WATCH",
                "wait_for_pullback": "FUTURE_BUY",
                "avoid_now": "AVOID_NOW",
                "high_risk": "AVOID_NOW",
                "insufficient_data": "INSUFFICIENT_DATA",
            }.get(self.category, "WATCH")
        return self


class MarketRegime(BaseModel):
    label: Literal[
        "bullish",
        "neutral_bullish",
        "neutral",
        "neutral_weak",
        "bearish",
        "data_insufficient",
    ]
    summary: str
    risk_level: Literal["low", "medium", "high", "unknown"]
    breadth_percent: float | None = None
    advancers: int = 0
    decliners: int = 0
    unchanged: int = 0
    leading_industries: list[str] = Field(default_factory=list)
    weak_industries: list[str] = Field(default_factory=list)
    source: str = "TWSE_ALL_QUOTES + TPEX_DAILY_QUOTES"


class UniverseSummary(BaseModel):
    source: Literal["all_taiwan_active"] = "all_taiwan_active"
    resolved_count: int = Field(ge=0)
    valid_data_count: int = Field(ge=0)
    partial_data_count: int = Field(default=0, ge=0)
    insufficient_data_count: int = Field(default=0, ge=0)
    exchanges: dict[str, int] = Field(default_factory=dict)
    security_master_count: int = Field(default=0, ge=0)
    ordinary_stock_count: int = Field(default=0, ge=0)
    etf_count: int = Field(default=0, ge=0)
    excluded_product_count: int = Field(default=0, ge=0)
    investable_count: int = Field(default=0, ge=0)
    classified_count: int = Field(default=0, ge=0)
    product_type_counts: dict[str, int] = Field(default_factory=dict)
    product_classification_status_counts: dict[str, int] = Field(default_factory=dict)
    product_classification_reason_counts: dict[str, int] = Field(default_factory=dict)
    product_classification_scope: str = "unknown_legacy_snapshot"


class ModelOverlay(BaseModel):
    status: Literal["not_run", "running", "succeeded", "failed"] = "not_run"
    generated_at: str | None = None
    provider: str | None = None
    model_id: str | None = None
    analyzed_symbols: list[str] = Field(default_factory=list, max_length=50)
    summaries: dict[str, dict[str, Any]] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    previous_success_generated_at: str | None = None


class MarketIntelligenceSnapshot(BaseModel):
    schema_version: Literal["stock_ai.market_intelligence_snapshot.v1"] = (
        "stock_ai.market_intelligence_snapshot.v1"
    )
    snapshot_id: str
    market: Literal["TW"] = "TW"
    generated_at: str = Field(default_factory=utc_now)
    data_as_of: str
    status: SnapshotStatus = "ready"
    market_regime: MarketRegime
    universe: UniverseSummary
    rankings: dict[CandidateCategory, list[str]]
    portfolio_actions: dict[PortfolioAction, list[str]]
    candidate_details: dict[str, CandidateDetail]
    data_quality: dict[str, Any] = Field(default_factory=dict)
    model_overlay: ModelOverlay = Field(default_factory=ModelOverlay)
    risk_overlay: dict[str, Any] = Field(default_factory=dict)
    model_receipts: list[dict[str, Any]] = Field(default_factory=list)
    next_refresh_conditions: list[str] = Field(default_factory=list)
    scan_statistics: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_snapshot_membership(self) -> "MarketIntelligenceSnapshot":
        details = self.candidate_details
        if len(details) != self.universe.resolved_count:
            raise ValueError(
                "candidate_details must contain every resolved Universe instrument exactly once"
            )
        normalized_details = {symbol.upper(): item for symbol, item in details.items()}
        if set(normalized_details) != {item.symbol for item in details.values()}:
            raise ValueError("candidate_details keys must match normalized symbols")

        ranked_symbols: list[str] = []
        for category, symbols in self.rankings.items():
            for symbol in symbols:
                if symbol not in details:
                    raise ValueError(f"ranking references unknown symbol: {symbol}")
                if details[symbol].category != category:
                    raise ValueError(
                        f"{symbol} category {details[symbol].category} does not match {category}"
                    )
                ranked_symbols.append(symbol)
        non_position_symbols = {
            symbol
            for symbol, item in details.items()
            if item.category not in POSITION_CATEGORIES
        }
        if set(ranked_symbols) != non_position_symbols or len(ranked_symbols) != len(
            set(ranked_symbols)
        ):
            raise ValueError("every non-position symbol must have one primary ranking category")

        position_symbols = {
            symbol for symbol, item in details.items() if item.is_position
        }
        categorized_positions: set[str] = set()
        for action, symbols in self.portfolio_actions.items():
            for symbol in symbols:
                if symbol not in position_symbols:
                    raise ValueError(
                        f"portfolio action {action} contains a symbol that is not a real position: {symbol}"
                    )
                categorized_positions.add(symbol)
        if categorized_positions != position_symbols:
            raise ValueError("every real position must have one portfolio action")
        return self


class WorkspaceRoute(BaseModel):
    workspace: Literal["home", "market", "instrument", "portfolio", "research", "system"] = "home"
    tab: str = "overview"
    params: dict[str, str] = Field(default_factory=dict)


class WorkspaceSelection(BaseModel):
    entity_id: str | None = None
    symbol: str | None = None
    entity_kind: str | None = None
    candidate_id: str | None = None
    universe_id: str = "all_taiwan_active"
    explicit_intent_symbols: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().upper()
        return normalized or None

    @field_validator("explicit_intent_symbols")
    @classmethod
    def normalize_explicit_symbols(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(symbol).strip().upper() for symbol in value if str(symbol).strip()))[:6]


class WorkspaceChart(BaseModel):
    type: str = "candles"
    timeframe: str = "1d"
    range: str = "1y"
    price_basis: str = "unadjusted"
    indicators: list[str] = Field(
        default_factory=lambda: ["MA5", "MA20", "MA60", "VOLUME", "MACD"]
    )
    focus_mode: bool = False
    viewport: dict[str, Any] | None = None


class WorkspaceComparison(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(symbol).strip().upper() for symbol in value if str(symbol).strip()))[:6]


class WorkspacePortfolio(BaseModel):
    account_id: str = "paper-default"
    mode: Literal["paper", "broker-read-only", "sandbox", "live"] = "paper"
    selected_position_id: str | None = None
    order_draft_id: str | None = None


class WorkspaceResearch(BaseModel):
    strategy_id: str | None = None
    report_id: str | None = None


class WorkspaceAgent(BaseModel):
    session_id: str | None = None
    run_id: str | None = None
    dock_open: bool = True
    dock_tab: Literal["chat", "tasks", "artifacts"] = "chat"
    dock_width: int = Field(default=360, ge=320, le=520)
    maximized: bool = False


class WorkspaceData(BaseModel):
    market_snapshot_id: str | None = None
    as_of: str | None = None
    freshness: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] | None = None
    market_session: str | None = None
    latest_quote: dict[str, Any] | None = None


class WorkspaceLayout(BaseModel):
    sidebar_collapsed: bool = False
    density: str = "comfortable"


class WorkspaceContext(BaseModel):
    schema_version: Literal["stock_ai.workspace_context.v2"] = "stock_ai.workspace_context.v2"
    revision: int = Field(default=1, ge=1)
    route: WorkspaceRoute = Field(default_factory=WorkspaceRoute)
    selection: WorkspaceSelection = Field(default_factory=WorkspaceSelection)
    chart: WorkspaceChart = Field(default_factory=WorkspaceChart)
    comparison: WorkspaceComparison = Field(default_factory=WorkspaceComparison)
    portfolio: WorkspacePortfolio = Field(default_factory=WorkspacePortfolio)
    research: WorkspaceResearch = Field(default_factory=WorkspaceResearch)
    agent: WorkspaceAgent = Field(default_factory=WorkspaceAgent)
    data: WorkspaceData = Field(default_factory=WorkspaceData)
    layout: WorkspaceLayout = Field(default_factory=WorkspaceLayout)
    updated_at: str = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def migrate_v1_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        legacy_keys = {
            "selectedEntityId", "selectedSymbol", "selectedUniverse", "selectedCandidateId",
            "marketSnapshotId", "timeframe", "dateRange", "priceBasis", "indicators",
            "comparisonSymbols", "activePortfolio", "activeStrategy", "marketSession",
            "latestQuote", "dataFreshness", "agentSessionId", "agentRunId",
            "currentWorkspace", "activeWorkspaceTab", "activeDetailTab", "layoutState",
        }
        if payload.get("schema_version") == "stock_ai.workspace_context.v1" or legacy_keys.intersection(payload):
            route = dict(payload.get("route") or {})
            selection = dict(payload.get("selection") or {})
            chart = dict(payload.get("chart") or {})
            comparison = dict(payload.get("comparison") or {})
            portfolio = dict(payload.get("portfolio") or {})
            research = dict(payload.get("research") or {})
            agent = dict(payload.get("agent") or {})
            data = dict(payload.get("data") or {})
            layout = dict(payload.get("layout") or {})
            route["workspace"] = payload.get("currentWorkspace", route.get("workspace", "home"))
            route["tab"] = payload.get("activeWorkspaceTab", route.get("tab", "overview"))
            selection["entity_id"] = payload.get("selectedEntityId", selection.get("entity_id"))
            selection["symbol"] = payload.get("selectedSymbol", selection.get("symbol"))
            selection["universe_id"] = payload.get("selectedUniverse", selection.get("universe_id", "all_taiwan_active"))
            selection["candidate_id"] = payload.get("selectedCandidateId", selection.get("candidate_id"))
            chart["timeframe"] = payload.get("timeframe", chart.get("timeframe", "1d"))
            chart["range"] = payload.get("dateRange", chart.get("range", "1y"))
            chart["price_basis"] = payload.get("priceBasis", chart.get("price_basis", "unadjusted"))
            chart["indicators"] = payload.get("indicators", chart.get("indicators", ["MA5", "MA20", "MA60", "VOLUME", "MACD"]))
            comparison["symbols"] = payload.get("comparisonSymbols", comparison.get("symbols", []))
            portfolio["account_id"] = payload.get("activePortfolio", portfolio.get("account_id", "paper-default"))
            research["strategy_id"] = payload.get("activeStrategy", research.get("strategy_id"))
            agent["session_id"] = payload.get("agentSessionId", agent.get("session_id"))
            agent["run_id"] = payload.get("agentRunId", agent.get("run_id"))
            data["market_snapshot_id"] = payload.get("marketSnapshotId", data.get("market_snapshot_id"))
            data["market_session"] = payload.get("marketSession", data.get("market_session"))
            data["latest_quote"] = payload.get("latestQuote", data.get("latest_quote"))
            data["freshness"] = payload.get("dataFreshness", data.get("freshness", {}))
            legacy_layout = payload.get("layoutState") or {}
            if isinstance(legacy_layout, dict):
                layout.update({key: item for key, item in legacy_layout.items() if key in WorkspaceLayout.model_fields})
            for key in legacy_keys:
                payload.pop(key, None)
            payload.update({
                "schema_version": "stock_ai.workspace_context.v2",
                "route": route,
                "selection": selection,
                "chart": chart,
                "comparison": comparison,
                "portfolio": portfolio,
                "research": research,
                "agent": agent,
                "data": data,
                "layout": layout,
            })
        return payload

    @property
    def selectedSymbol(self) -> str:
        return self.selection.symbol or "^TWII"

    @property
    def marketSnapshotId(self) -> str | None:
        return self.data.market_snapshot_id

    @property
    def timeframe(self) -> str:
        return self.chart.timeframe

    @property
    def dateRange(self) -> str:
        return self.chart.range

    @property
    def priceBasis(self) -> str:
        return self.chart.price_basis

    @property
    def layoutState(self) -> dict[str, Any]:
        return self.layout.model_dump(mode="json")
