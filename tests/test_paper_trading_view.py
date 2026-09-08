from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "stock_ai" / "ui" / "static"


def test_paper_trading_is_embedded_in_the_portfolio_workspace() -> None:
    script = (STATIC / "paper-training.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "const PAPER_VIEW_ID = 'portfolio'" in script
    assert "paper-trading-view" in script
    assert "模擬交易" in script
    assert 'id="assets" data-workspace-parent="portfolio" data-workspace-tab="overview positions simulation"' in index
    assert 'data-ui-id="portfolio.simulation.root"' in index
    assert "window.setWorkspaceTab?.('portfolio', 'simulated');" in script
    assert "assetsView.insertAdjacentElement('afterend', view)" not in script
    assert "data-paper-trading-nav" not in index
    assert "REAL MARKET DATA · VIRTUAL CAPITAL" not in script
    assert "禁止假股票" not in script


def test_paper_trading_has_complete_buy_sell_order_ticket() -> None:
    script = (STATIC / "paper-training.js").read_text(encoding="utf-8")
    required_controls = {
        'id="paperTrainingBuySide"',
        'id="paperTrainingSellSide"',
        'id="paperTrainingShortSellSide"',
        'id="paperTrainingCoverSide"',
        'id="paperTrainingBorrowReceipt"',
        'id="paperTrainingSymbol"',
        'id="paperTrainingOrderType"',
        'id="paperTrainingLotType"',
        'id="paperTrainingQuantity"',
        'id="paperTrainingTimeInForce"',
        'id="paperTrainingSession"',
        'id="paperTrainingLimitPrice"',
        'id="paperTrainingStopPrice"',
        'id="paperTrainingSubmitOrder"',
        'id="paperTrainingOpenOrders"',
        'id="paperTrainingFills"',
        'id="paperTrainingRollbackScope"',
        'id="paperTrainingRollbackArtifact"',
    }
    assert all(control in script for control in required_controls)
    assert "market" in script
    assert "limit" in script
    assert "stop_limit" in script
    assert "rod" in script
    assert "ioc" in script
    assert "fok" in script
    assert "cancelOrder" in script
    assert "syncOfficialTradingControls" in script
    assert "借券 locate" in script
    assert "borrow_receipt" in script
    assert "loadArtifactGovernance" in script
    assert "rollbackArtifact" in script


def test_agent_trading_starts_without_a_default_stock_or_question() -> None:
    script = (STATIC / "agent-trading-workspace.js").read_text(encoding="utf-8")
    stylesheet = (STATIC / "agent-trading-workspace.css").read_text(encoding="utf-8")
    paper_script = (STATIC / "paper-training.js").read_text(encoding="utf-8")

    assert 'id="agentTradingHomeSymbol" value=""' in script
    assert 'id="agentTradingHomePrompt" value=""' in script
    assert 'id="agentTradingPaperSymbol" value=""' in script
    assert 'id="paperTrainingSymbol" value=""' in paper_script
    assert "|| '2330.TW'" not in script
    assert "|| byId('paperTrainingSymbol')?.value" not in script
    assert "const officialSymbol = byId('tradingSymbol')?.value" not in paper_script
    assert paper_script.count("if (hasOrderSymbol())") >= 3
    assert "請先選擇或輸入股票代號。" in script
    assert "display:grid!important" in stylesheet
    assert "gap:24px!important" in stylesheet
    assert "margin:0 0 8px!important" in stylesheet


def test_paper_trading_view_loads_uncached_broker_styles() -> None:
    script = (STATIC / "paper-training.js").read_text(encoding="utf-8")
    base_stylesheet = (STATIC / "paper-trading-view.css").read_text(encoding="utf-8")
    broker_styles = (STATIC / "paper-trading-broker-v4.css").read_text(encoding="utf-8")
    assert "/static/paper-trading-view.css" in script
    assert "/static/paper-trading-broker-v4.css?v=20260718-moving-switcher-v7" in script
    assert ".paper-trading-view" in base_stylesheet
    assert ".paper-broker-layout" in broker_styles
    assert ".paper-side-switch" in broker_styles
    assert ".paper-ticket-grid" in broker_styles
    assert ".paper-order-table" in broker_styles
    assert ".paper-fill-table" in broker_styles


def test_display_settings_are_a_balanced_rounded_liquid_glass_group() -> None:
    stylesheet = (STATIC / "paper-trading-view.css").read_text(encoding="utf-8")
    assert ".settings-rows" in stylesheet
    assert "border-radius:28px!important" in stylesheet
    assert "overflow:hidden!important" in stylesheet
    assert "padding:10px!important" in stylesheet
    assert ".settings-rows::after" in stylesheet
    assert "grid-template-columns:minmax(260px,1fr) minmax(300px,520px)!important" in stylesheet
    assert "padding:22px 24px!important" in stylesheet
    assert "width:min(100%,520px)!important" in stylesheet


def test_navigation_uses_one_source_switcher_material_instead_of_per_button_glass() -> None:
    stylesheet = (STATIC / "css/shell/freefrontend-liquid-glass.css").read_text(encoding="utf-8")
    broker_styles = (STATIC / "paper-trading-broker-v4.css").read_text(encoding="utf-8")
    script = (STATIC / "paper-training.js").read_text(encoding="utf-8")

    assert ".nav-menu.stock-nav-switcher::after" in stylesheet
    assert ".nav-menu.stock-nav-switcher .switcher__input" in stylesheet
    assert ":has(input[c-option=\"21\"]:checked)::after" in stylesheet
    assert "nav-active-lens" not in stylesheet
    assert "translate 400ms cubic-bezier(1, 0, .4, 1)" in stylesheet
    assert "Navigation glass is owned by the shared KwpRaGr switcher." in broker_styles
    assert "body .sidebar .nav-btn > .glass-control-lens" not in broker_styles
    assert "rgba(44,53,62,.94)" not in broker_styles

    assert "decorateNavigationButton" in script
    assert "syncNavigationMaterial" in script
    assert "const selected = Boolean(activeView) && button.dataset.view === activeView" in script
    assert "button.dataset.selected = 'true'" in script
    assert "button.setAttribute('aria-current', 'page')" in script


def test_paper_account_controls_never_collapse_into_vertical_buttons() -> None:
    stylesheet = (STATIC / "paper-trading-broker-v4.css").read_text(encoding="utf-8")
    assert ".paper-account-grid" in stylesheet
    assert "grid-template-columns:minmax(0,1fr) minmax(156px,186px)!important" in stylesheet
    assert "#paperTrainingReset" in stylesheet
    assert "#paperTrainingStartEpisode" in stylesheet
    assert "white-space:nowrap!important" in stylesheet
    assert "word-break:keep-all!important" in stylesheet


def test_reset_uses_post_result_then_uncached_account_verification() -> None:
    script = (STATIC / "paper-training.js").read_text(encoding="utf-8")
    assert "cache: 'no-store'" in script
    assert "_paper_ui_ts=${Date.now()}" in script
    assert "renderAccount(returnedAccount, { forceInitialCashInput: true })" in script
    assert "const verifiedAccount = await request('/api/open-stock-ai/agent/paper-training/account')" in script
    assert "verifiedAccount.initial_cash" in script
    assert "verifiedAccount.cash_balance" in script
    assert "總資產與可用現金已同步" in script


def test_ui_keeps_market_validation_in_backend_without_prompt_copy() -> None:
    script = (STATIC / "paper-training.js").read_text(encoding="utf-8")
    api = (ROOT / "src" / "stock_ai" / "paper_training_api.py").read_text(encoding="utf-8")
    assert "/api/open-stock-ai/agent/paper-training/order" in script
    assert "/api/open-stock-ai/agent/paper-training/preview" in script
    assert "payload.price" not in script
    assert "payload.entry_price" not in script
    assert "get_execution_price_summary" in api
    assert "A last trade or exchange close is required" in api


def test_realtime_stream_uses_authenticated_fetch_instead_of_native_event_source() -> None:
    script = (STATIC / "js" / "features" / "market-chart.js").read_text(encoding="utf-8")
    assert "openSecuredEventStream" in script
    assert "headers: { Accept: 'text/event-stream' }" in script
    assert "new EventSource" not in script



def test_paper_navigation_is_static_before_glass_bootstrap() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "paper-training.js").read_text(encoding="utf-8")
    assert 'id="assets" data-workspace-parent="portfolio" data-workspace-tab="overview positions simulation"' in index
    assert 'data-view="paper-trading"' not in index
    assert 'data-paper-trading-nav' not in script
    assert "button.querySelectorAll(':scope > .glass-control-lens').forEach((lens) => lens.remove())" in script
    decorate = script.split("function decorateNavigationButton", 1)[1].split("function syncNavigationMaterial", 1)[0]
    assert "document.createElement('span')" not in decorate
    assert "paperBound" in script


def test_trade_and_risk_workspaces_do_not_treat_market_indices_as_stocks() -> None:
    script = (STATIC / "js" / "features" / "trading-workspaces.js").read_text(encoding="utf-8")

    assert "candidate => /\\.(TW|TWO)$/i.test(candidate) && !candidate.startsWith('^')" in script
    assert "return symbol || '';" in script
