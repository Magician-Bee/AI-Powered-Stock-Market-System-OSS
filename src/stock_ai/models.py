from typing import Any, Literal

from pydantic import BaseModel, Field

from .data_quality_contracts import DecisionDataQualityReceipt


class Entity(BaseModel):
    entity_id: str
    symbol: str
    name: str
    entity_type: str
    market: str
    exchange: str
    currency: str
    sector: str | None = None
    industry: str | None = None
    is_active: bool = True
    lifecycle_status: str = "active"


class PricePoint(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float | None = None


class AdjustedPricePoint(PricePoint):
    price_basis: Literal[
        "unadjusted",
        "forward_adjusted",
        "backward_adjusted",
    ]
    adjustment_factor: float
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    forward_adjusted_open: float
    forward_adjusted_high: float
    forward_adjusted_low: float
    forward_adjusted_close: float
    backward_adjusted_open: float
    backward_adjusted_high: float
    backward_adjusted_low: float
    backward_adjusted_close: float
    factor_set_id: str
    factor_source_id: str


class FundamentalSnapshot(BaseModel):
    fiscal_period: str
    revenue_yoy: float
    eps: float
    gross_margin: float
    roe: float
    pe: float
    pb: float


class FlowSnapshot(BaseModel):
    foreign_net_buy: int
    investment_trust_net_buy: int
    dealer_net_buy: int
    margin_balance_change: int


class EventItem(BaseModel):
    event_id: str
    event_time: str
    related_symbols: list[str]
    event_type: str
    title: str
    summary: str
    sentiment: Literal["positive", "neutral", "negative"]
    estimated_impact_direction: Literal["positive", "neutral", "negative", "mixed"]
    confidence: float = Field(ge=0, le=1)
    confidence_type: Literal["ranking_score"] = "ranking_score"
    confidence_calibrated: bool = False
    source_url: str


class MarketSummary(BaseModel):
    entity: Entity
    latest_price: PricePoint
    change_percent: float
    trend: str
    fundamentals: FundamentalSnapshot | None = None
    flow: FlowSnapshot | None = None
    events: list[EventItem]
    linked_factors: list[str]
    data_source: str = "demo"
    data_timestamp: str | None = None
    freshness_note: str | None = None
    reliability_note: str | None = None
    provider_id: str | None = None
    connector_id: str | None = None
    quote_kind: str | None = None
    authorized: bool = False
    realtime: bool = False
    delayed: bool = False
    official_close: bool = False
    max_age_seconds: int = 86400
    limit_up: float | None = None
    limit_down: float | None = None
    trading_state: str = "unknown"
    buy_liquidity_confirmed: bool = False
    sell_liquidity_confirmed: bool = False
    source_trade: dict[str, Any] | None = None
    # Shared decision-quality evidence for legacy summary/detail consumers.
    # The receipt stays partial when no independent cross-source observation
    # exists; callers must not infer certification from the quote itself.
    data_quality_receipt: DecisionDataQualityReceipt | None = None


class LinkageExplanation(BaseModel):
    source: str
    target: str
    direction: Literal["positive", "negative", "mixed", "unknown"]
    confidence: float = Field(ge=0, le=1)
    confidence_type: Literal["rule_score"] = "rule_score"
    confidence_calibrated: bool = False
    mechanism: str
    path: list[str]
    evidence: list[str]
    affected_symbols: list[str]


class QueryRequest(BaseModel):
    question: str


class QueryResponse(BaseModel):
    route: str
    answer: str
    data: dict[str, Any]
    sources: list[str]


class ScreenerRequest(BaseModel):
    market: str = "taiwan"
    conditions: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    universe_source: Literal[
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
    ] = "explicit_symbols"
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=100, ge=1, le=5000)


class ScreenerItem(BaseModel):
    symbol: str
    name: str
    score: float
    reasons: list[str]
    metrics: dict[str, Any]
    data_quality_receipt: DecisionDataQualityReceipt | None = None


class SecurityMasterItem(BaseModel):
    entity_id: str | None = None
    symbol: str
    name: str
    market: str
    exchange: str
    listing_type: str
    entity_type: str = "stock"
    lifecycle_status: str = "unknown"
    listing_identifier_status: str = "unknown"
    # Legacy coarse entity_type is identity metadata, not product evidence.
    product_classification: dict[str, Any] = Field(default_factory=lambda: {
        "schema_version": "stock_ai.product_classification.v1", "status": "unknown",
        "product_type": "unknown", "reasons": ["product_classification_missing"],
    })
    industry: str | None = None
    trade_unit: int | None = None
    day_trade_eligible: bool | None = None
    margin_eligible: bool | None = None
    short_eligible: bool | None = None
    is_etf: bool = False
    is_warrant: bool = False
    list_date: str | None = None
    delist_date: str | None = None
    venue_history: list[dict[str, Any]] = Field(default_factory=list)
    trading_status: str = "active"
    source: str


class InstitutionalFlowItem(BaseModel):
    trade_date: str
    symbol: str
    name: str
    foreign_buy: int
    foreign_sell: int
    foreign_net: int
    foreign_dealer_buy: int
    foreign_dealer_sell: int
    foreign_dealer_net: int
    trust_buy: int
    trust_sell: int
    trust_net: int
    dealer_buy: int
    dealer_sell: int
    dealer_net: int
    dealer_hedge_net: int
    total_institutional_net: int
    source: str


class MarginTradingItem(BaseModel):
    trade_date: str
    symbol: str
    name: str
    margin_buy: int
    margin_sell: int
    margin_cash_redemption: int
    margin_previous_balance: int
    margin_balance: int
    margin_limit: int
    short_buy: int
    short_sell: int
    short_cash_redemption: int
    short_previous_balance: int
    short_balance: int
    short_limit: int
    offsetting: int
    note: str | None = None
    source: str


class RevenueItem(BaseModel):
    report_date: str | None = None
    report_date_semantics: str | None = None
    period: str
    symbol: str
    name: str
    industry: str | None = None
    current_revenue: float | None
    previous_revenue: float | None
    last_year_revenue: float | None
    mom_change_percent: float | None
    yoy_change_percent: float | None
    ytd_revenue: float | None
    last_ytd_revenue: float | None
    ytd_change_percent: float | None
    note: str | None = None
    source: str
    source_id: str | None = None
    source_url: str | None = None
    source_market: str | None = None
    unit: str = "thousand_twd"
    acquired_at: str | None = None
    published_at: str | None = None
    available_at: str | None = None
    publication_time_status: str | None = None
    growth_source: str = "official_disclosed"


class IncomeStatementItem(BaseModel):
    period: str
    fiscal_year: int
    quarter: int = Field(ge=1, le=4)
    symbol: str
    name: str | None = None
    statement_scope: str
    statement_semantics: str = "year_to_date_cumulative"
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    net_income_basis: str | None = None
    eps: float | None = None
    current_quarter_revenue: float | None = None
    current_quarter_gross_profit: float | None = None
    current_quarter_operating_income: float | None = None
    current_quarter_net_income: float | None = None
    current_quarter_eps: float | None = None
    current_quarter_status: str
    unit: str = "thousand_twd"
    eps_unit: str = "twd_per_share"
    source: str
    source_id: str
    source_url: str
    source_method: str = "POST"
    source_market: str
    source_request_parameters: dict[str, str] = Field(default_factory=dict)
    raw_field_labels: dict[str, str | None] = Field(default_factory=dict)
    acquired_at: str
    published_at: str | None = None
    available_at: str
    publication_time_status: str = "not_provided_by_archive"


class BalanceSheetItem(BaseModel):
    period: str
    fiscal_year: int
    quarter: int = Field(ge=1, le=4)
    symbol: str
    name: str | None = None
    statement_scope: str
    statement_kind: str = "balance_sheet"
    cash_and_cash_equivalents: float | None = None
    total_assets: float | None = None
    total_liabilities: float | None = None
    total_equity: float | None = None
    inventory: float | None = None
    accounts_receivable: float | None = None
    current_assets: float | None = None
    current_liabilities: float | None = None
    interest_bearing_debt: float | None = None
    official_comparison_dates: list[str] = Field(default_factory=list)
    unit: str = "thousand_twd"
    source: str
    source_id: str
    source_url: str
    source_method: str = "POST"
    source_market: str
    source_request_parameters: dict[str, str] = Field(default_factory=dict)
    raw_field_labels: dict[str, str | None] = Field(default_factory=dict)
    acquired_at: str
    published_at: str | None = None
    available_at: str
    publication_time_status: str = "not_provided_by_archive"


class CashFlowStatementItem(BaseModel):
    period: str
    fiscal_year: int
    quarter: int = Field(ge=1, le=4)
    symbol: str
    name: str | None = None
    statement_scope: str
    statement_kind: str = "cash_flow_statement"
    statement_semantics: str = "year_to_date_cumulative"
    operating_cash_flow: float | None = None
    investing_cash_flow: float | None = None
    financing_cash_flow: float | None = None
    capital_expenditure: float | None = None
    free_cash_flow: float | None = None
    depreciation_and_amortization: float | None = None
    free_cash_flow_formula: str = "operating_cash_flow - abs(capital_expenditure)"
    unit: str = "thousand_twd"
    source: str
    source_id: str
    source_url: str
    source_method: str = "POST"
    source_market: str
    source_request_parameters: dict[str, str] = Field(default_factory=dict)
    raw_field_labels: dict[str, str | None] = Field(default_factory=dict)
    acquired_at: str
    published_at: str | None = None
    available_at: str
    publication_time_status: str = "not_provided_by_archive"


class DailySelectionItem(BaseModel):
    symbol: str
    name: str
    score: float
    signal: Literal["buy", "watch", "avoid"]
    reasons: list[str]
    risk_factors: list[str]
    data_sources: list[str]
    event_count: int


class DailyReport(BaseModel):
    generated_at: str
    title: str
    summary: str
    picks: list[DailySelectionItem]
    source_snapshot: list[str]
    universe: dict[str, Any] = Field(default_factory=dict)


class NewsItem(BaseModel):
    news_id: str
    title: str
    source: str
    published_at: str
    related_symbols: list[str]
    category: Literal["company", "industry", "macro", "international", "policy", "analyst"]
    sentiment: Literal["positive", "neutral", "negative"]
    impact: Literal["positive", "neutral", "negative", "mixed"]
    credibility: float = Field(ge=0, le=1)
    official_verified: bool = False
    summary: str
    source_url: str


class WatchlistOverviewItem(BaseModel):
    symbol: str
    name: str
    exchange: str
    industry: str | None = None
    latest_price: float | None = None
    change_percent: float | None = None
    total_volume_lots: int | None = None
    institutional_net: int | None = None
    margin_balance: int | None = None
    revenue_yoy: float | None = None
    latest_news_title: str | None = None
    latest_news_url: str | None = None
    alert_flags: list[str]
    data_sources: list[str]


class NotificationChannelStatus(BaseModel):
    channel: Literal["telegram", "line"]
    configured: bool
    enabled: bool
    mode: Literal["ready", "preview_only", "disabled"]
    target_hint: str | None = None
    note: str


class NotificationPreview(BaseModel):
    category: Literal["daily_report", "watchlist_alert", "market_news"]
    title: str
    body: str
    channels: list[Literal["telegram", "line"]]
    related_symbols: list[str]
    dry_run: bool = True


class NotificationSendRequest(BaseModel):
    title: str
    body: str
    channels: list[Literal["telegram", "line"]]
    related_symbols: list[str] = Field(default_factory=list)
    dry_run: bool = True


class NotificationDeliveryResult(BaseModel):
    channel: Literal["telegram", "line"]
    configured: bool
    attempted: bool
    sent: bool
    mode: Literal["dry_run", "ready", "not_configured", "error"]
    target_hint: str | None = None
    status_code: int | None = None
    detail: str


class ReadonlyWorkspaceMeta(BaseModel):
    status: Literal["preview_only", "read_only"] = "preview_only"
    broker_api_connected: bool = False
    order_submission_enabled: bool = False
    generated_at: str
    data_sources: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    fallback: dict[str, Any] = Field(default_factory=dict)


class BrokerConnectionStatus(BaseModel):
    provider_name: str
    connected: bool = False
    can_submit_orders: bool = False
    mode: Literal["preview_only", "not_connected"] = "preview_only"
    note: str


class OrderCostEstimate(BaseModel):
    gross_amount: float
    estimated_fee: float
    estimated_tax: float
    estimated_total: float
    currency: str = "TWD"


class PositionDetail(BaseModel):
    symbol: str
    name: str
    side: Literal["long"] = "long"
    industry: str | None = None
    quantity_shares: float
    average_cost: float
    latest_price: float | None = None
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_percent: float
    weight_percent: float
    data_sources: list[str] = Field(default_factory=list)


class AssetSummary(BaseModel):
    total_assets: float
    cash_available: float
    holdings_market_value: float
    today_pnl: float
    unrealized_pnl: float
    realized_pnl: float
    dividend_income: float
    note: str | None = None


class AssetWorkspace(BaseModel):
    meta: ReadonlyWorkspaceMeta
    summary: AssetSummary
    positions: list[PositionDetail]


class RiskAlert(BaseModel):
    code: str
    level: Literal["info", "warning", "block"]
    title: str
    message: str
    triggered: bool = True
    metric_value: float | None = None
    threshold_value: float | None = None


class RiskSummary(BaseModel):
    single_trade_risk_percent: float
    daily_risk_percent: float
    position_concentration_percent: float
    industry_exposure_percent: float
    max_single_trade_risk_percent: float
    max_daily_risk_percent: float
    max_position_concentration_percent: float
    max_industry_exposure_percent: float
    order_allowed: bool
    alerts: list[RiskAlert] = Field(default_factory=list)


class TradingPreview(BaseModel):
    symbol: str
    name: str
    side: Literal["buy", "sell"]
    order_type: Literal["limit_preview"] = "limit_preview"
    quantity_lots: int
    quantity_shares: int
    reference_price: float
    estimated_fill_price: float
    available_cash_before: float
    available_cash_after: float
    estimated_costs: OrderCostEstimate
    limitation_note: str
    fallback_fields: dict[str, Any] = Field(default_factory=dict)


class TradingWorkspace(BaseModel):
    meta: ReadonlyWorkspaceMeta
    broker_status: BrokerConnectionStatus
    preview: TradingPreview
    risk_summary: RiskSummary
    asset_summary: AssetSummary
    positions: list[PositionDetail]


class RiskWorkspace(BaseModel):
    meta: ReadonlyWorkspaceMeta
    summary: RiskSummary
    preview_symbol: str
    preview_side: Literal["buy", "sell"]
    position_snapshot: list[PositionDetail]


class AITradePlanCard(BaseModel):
    session: Literal["pre_market", "intraday", "post_market"]
    symbol: str
    name: str
    action_bias: Literal["buy", "watch", "hold", "reduce", "avoid"]
    origin: Literal["rule_strategy"] = "rule_strategy"
    rule_score: float = Field(ge=-1, le=1)
    score_min: float = -1.0
    score_max: float = 1.0
    calibrated: bool = False
    rule_set_id: str = "stock_ai.quant_trading_assistant.v2"
    technical_reasons: list[str] = Field(default_factory=list)
    flow_reasons: list[str] = Field(default_factory=list)
    fundamental_reasons: list[str] = Field(default_factory=list)
    event_reasons: list[str] = Field(default_factory=list)
    risk_reasons: list[str] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    as_of: str
    conflict_note: str | None = None


class AITradingAssistantWorkspace(BaseModel):
    meta: ReadonlyWorkspaceMeta
    focus_symbol: str
    report_title: str
    cards: list[AITradePlanCard]
    notification_previews: list[NotificationPreview] = Field(default_factory=list)
    watchlist_items: list[dict[str, Any]] = Field(default_factory=list)
    market_snapshot: dict[str, Any] = Field(default_factory=dict)


class OrderBookLevel(BaseModel):
    price: float | None = None
    volume: int | None = None
    size: int | None = None


class RealTimeQuoteRecord(BaseModel):
    schema_version: Literal["stock_ai.realtime_quote.v1"] = (
        "stock_ai.realtime_quote.v1"
    )
    symbol: str
    time: str
    name: str | None = None
    exchange: str | None = None
    exchange_timestamp: str | None = None
    last_price: float | None = None
    last_trade_size_lots: int | None = None
    change: float | None = None
    change_percent: float | None = None
    volume: int | None = None
    total_volume_lots: int | None = None
    turnover: float | None = None
    inner_volume: int | None = None
    outer_volume: int | None = None
    bids: list[OrderBookLevel] = Field(default_factory=list, max_length=5)
    asks: list[OrderBookLevel] = Field(default_factory=list, max_length=5)
    high: float | None = None
    low: float | None = None
    open: float | None = None
    previous_close: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    best_bid: OrderBookLevel | None = None
    best_ask: OrderBookLevel | None = None
    trading_status: str = "unknown"
    trading_status_source: str = "unavailable"
    is_market_open: bool = False
    freshness: str = "unknown"
    quote_age_ms: int | None = None
    sequence: int | None = None
    source: str


class OHLCVRecord(BaseModel):
    symbol: str
    period: Literal["tick", "1s", "5s", "1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo"]
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover: float | None = None
    vwap: float | None = None
    source: str


class IntradayCandleRecord(BaseModel):
    schema_version: Literal["stock_ai.intraday_candle.v1"] = (
        "stock_ai.intraday_candle.v1"
    )
    symbol: str
    trading_date: str
    timeframe_minutes: Literal[1, 5, 15, 30, 60]
    bucket_start: str
    bucket_end: str
    open: float
    high: float
    low: float
    close: float
    volume_lots: float = Field(ge=0)
    turnover: float | None = None
    average: float | None = None
    one_minute_count: int = Field(ge=1)
    expected_one_minute_count: int = Field(ge=1)
    is_final: bool
    source_ids: list[str] = Field(min_length=1)
    authorized: bool
    revision_ids: list[str] = Field(min_length=1)


class MarketIndexRecord(BaseModel):
    symbol: str
    name: str
    category: Literal["broad", "sector", "industry", "theme", "etf", "futures"]
    timestamp: str
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    turnover: float | None = None
    source: str


class ChipDataRecord(BaseModel):
    trade_date: str
    symbol: str
    margin_balance: int | None = None
    short_balance: int | None = None
    margin_short_ratio: float | None = None
    borrowed_sell: int | None = None
    borrowed_balance: int | None = None
    day_trade_ratio: float | None = None
    broker_branch_net: int | None = None
    major_holder_1000_lot_ratio: float | None = None
    holder_400_lot_ratio: float | None = None
    small_shareholder_count: int | None = None
    source: str


class TDCCHoldingDistributionRecord(BaseModel):
    report_date: str
    symbol: str
    name: str | None = None
    total_holders: int | None = None
    total_shares: int | None = None
    major_holder_1000_lot_ratio: float | None = None
    holder_400_lot_ratio: float | None = None
    small_shareholder_count: int | None = None
    concentration_score: float | None = Field(default=None, ge=0, le=100)
    source: str
    official_public_data: bool = True


class TaifexFuturesInstitutionalRecord(BaseModel):
    trade_date: str
    contract: str
    product_name: str
    foreign_long: int | None = None
    foreign_short: int | None = None
    investment_trust_long: int | None = None
    investment_trust_short: int | None = None
    dealer_long: int | None = None
    dealer_short: int | None = None
    open_interest: int | None = None
    source: str
    official_public_data: bool = True


class TaifexPutCallRatioRecord(BaseModel):
    trade_date: str
    put_volume: int | None = None
    call_volume: int | None = None
    put_call_ratio: float | None = None
    put_open_interest: int | None = None
    call_open_interest: int | None = None
    open_interest_put_call_ratio: float | None = None
    source: str
    official_public_data: bool = True


class FundamentalsRecord(BaseModel):
    period: str
    symbol: str
    monthly_revenue: float | None = None
    revenue_yoy: float | None = None
    revenue_mom: float | None = None
    accumulated_revenue_yoy: float | None = None
    eps: float | None = None
    roe: float | None = None
    roa: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    cash_flow: float | None = None
    debt_ratio: float | None = None
    inventory: float | None = None
    accounts_receivable: float | None = None
    book_value_per_share: float | None = None
    pe: float | None = None
    pb: float | None = None
    dividend_yield: float | None = None
    source: str


class CompanyEventRecord(BaseModel):
    event_id: str
    symbol: str
    event_time: str
    event_type: Literal[
        "material_event",
        "earnings_call",
        "shareholder_meeting",
        "ex_dividend",
        "cash_dividend",
        "stock_dividend",
        "capital_reduction",
        "capital_increase",
        "private_placement",
        "merger",
        "treasury_stock",
        "attention_stock",
        "disposition_stock",
        "resume_trading",
        "halt_trading",
    ]
    title: str
    summary: str
    official_verified: bool = False
    source: str


class PortfolioRecord(BaseModel):
    symbol: str
    holding_shares: int
    cost: float
    latest_price: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    return_percent: float | None = None
    position_weight_percent: float | None = None
    risk_exposure_percent: float | None = None
    today_pnl: float | None = None
    total_assets: float | None = None
    available_cash: float | None = None
    mode: Literal["preview", "paper", "broker"] = "preview"


class OrderExecutionRecord(BaseModel):
    order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    order_price: float
    order_quantity: int
    order_condition: str | None = None
    time_in_force: Literal["ROD", "IOC", "FOK"] = "ROD"
    order_status: str
    fill_price: float | None = None
    fill_quantity: int | None = None
    fee: float | None = None
    tax: float | None = None
    executed_at: str | None = None
    broker_api_required: bool = True


class TradingSignalRecord(BaseModel):
    symbol: str
    signal_time: str
    signal_type: str
    action: Literal["buy", "sell", "watch"]
    confidence: float = Field(ge=0, le=1)
    confidence_type: Literal["rule_score"] = "rule_score"
    confidence_calibrated: bool = False
    technical_reason: str
    chip_reason: str
    fundamental_reason: str
    news_reason: str
    risk: str
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    invalid_condition: str
    data_sources: list[str] = Field(default_factory=list)
    data_timestamp: str
    conflict_note: str | None = None
