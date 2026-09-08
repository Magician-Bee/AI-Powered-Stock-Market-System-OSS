from pathlib import Path
import re

from stock_ai.data_platform.ui_api import audit_ui_data_access

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "stock_ai" / "ui" / "static"


def test_every_market_data_page_uses_the_unified_ui_data_api() -> None:
    audit = audit_ui_data_access(STATIC)
    assert audit["status"] == "passed"
    assert audit["violation_count"] == 0
    assert audit["scanned_file_count"] >= 20

    codex_js = (STATIC / "js/features/codex.js").read_text(encoding="utf-8")
    dashboard_js = (STATIC / "js/features/dashboard.js").read_text(encoding="utf-8")
    market_chart_js = (STATIC / "js/features/market-chart.js").read_text(
        encoding="utf-8"
    )
    explorer_js = (STATIC / "js/features/explorer.js").read_text(encoding="utf-8")
    assert "const UI_DATA_API_PREFIX = '/api/data/ui/v1';" in codex_js
    assert "uiDataApi('/securities/master" in dashboard_js
    assert "uiDataApi('/realtime/status')" in market_chart_js
    assert "uiDataApi('/catalog')" in explorer_js


def test_screener_controls_are_backed_by_typed_conditions_and_a_real_universe() -> None:
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    explorer_js = (STATIC / "js/features/explorer.js").read_text(encoding="utf-8")

    assert 'data-screener-condition="realtime == true"' in index_html
    assert 'data-screener-condition="change_percent > 0"' in index_html
    assert 'id="screenerCustomConditions"' in index_html
    assert "revenue_yoy &gt; 15" in index_html
    assert "close &gt; sma_60" in index_html
    assert "function selectedScreenerConditions()" in explorer_js
    assert "universe_source: 'top_by_volume'" in explorer_js
    assert "condition_receipt" in explorer_js
    assert "function screenerFieldReceiptText(receipt)" in explorer_js


def frontend_script() -> str:
    """Read the modular application scripts as one source corpus for contract tests."""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((STATIC / "js").rglob("*.js"))
    )


def frontend_styles() -> str:
    """Read the modular application styles as one source corpus for contract tests."""
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((STATIC / "css").rglob("*.css"))
    )


def test_obsolete_home_radar_pipeline_is_removed_instead_of_hidden():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    codex_js = (STATIC / "js/features/codex.js").read_text(encoding="utf-8")
    dom_state_js = (STATIC / "js/core/dom-state.js").read_text(encoding="utf-8")
    source = "\n".join((index_html, codex_js, dom_state_js))

    for obsolete in (
        "renderHomeRadar",
        "renderDecisionRows",
        "scheduleHomeRadarRefresh",
        "selectedHomeUniverseRequest",
        "loadHomeUniverseOptions",
        "loadCodexHome",
        "homeUniverseSource",
        "homeBuyNow",
        "homeSellNow",
        "homeWait",
        "state.homeRadar",
    ):
        assert obsolete not in source, obsolete
    assert "HomeMarketWorkspace.load" in dom_state_js


def test_home_search_resolves_the_security_master_before_using_agent_commands():
    source = (
        STATIC / "js/features/market-intelligence/workspace-home.js"
    ).read_text(encoding="utf-8")

    assert "async function searchOrAskAgent(query)" in source
    assert "uiDataApi(`/entities/search?q=${encodeURIComponent(query)}`)" in source
    assert "searchedEntities.set" in source
    assert "await selectInstrument(entity.symbol" in source
    assert "未誤送成 Agent 問題" in source
    assert source.index("await api(uiDataApi(`/entities/search") < source.index(
        "window.AgentDockController?.open()",
        source.index("async function searchOrAskAgent(query)"),
    )


def test_workspace_bootstrap_primes_the_default_chart_and_navigation_lists():
    workspace = (
        STATIC / "js/features/market-intelligence/workspace-home.js"
    ).read_text(encoding="utf-8")
    navigation = (
        STATIC / "js/features/market-intelligence/market-navigation.js"
    ).read_text(encoding="utf-8")

    assert "defaultChart.payload?.points" in workspace
    assert "StockWorkspaceCache.set(`chart:index:" in workspace
    for category in (
        "navigation.watchlist",
        "navigation.volume",
        "navigation.movers",
        "navigation.indices",
        "navigation.industries",
        "navigation.institutional",
        "navigation.anomalies",
        "navigation.alerts",
    ):
        assert category in navigation
    assert "TX=F" not in navigation
    # A delayed home bootstrap must use the route visible after hydration, not
    # the stale route that was visible before the user clicked a workspace tab.
    assert "Hydration itself is asynchronous." in workspace
    assert "await WorkspaceContextStore.hydrate(payload.agent_context);\n    preserveExplicitTaskSelection(liveNavigation);" in workspace
    assert "document.documentElement.dataset.workspace\n      || window.__activeWorkspace" in workspace
    # A strict data-quality gate can intentionally make BUY_NOW empty. The
    # home list must then expose the next populated decision surface instead
    # of presenting a blank market workspace.
    assert "function preferredListAfterHydration()" in navigation
    assert "decisionListPriority.find(name => itemsFor(name).length)" in navigation


def test_decision_cards_show_trigger_invalidation_review_and_real_supplemental_data():
    decision = (
        STATIC / "js/features/market-intelligence/decision-board.js"
    ).read_text(encoding="utf-8")
    workspace = (
        STATIC / "js/features/market-intelligence/workspace-home.js"
    ).read_text(encoding="utf-8")

    for label in ("觸發事件", "失效條件", "距離觸發", "下次重估"):
        assert label in decision
    for tab in ("chips:", "financials:", "valuation:", "news:", "events:", "risk:", "evidence:"):
        assert tab in decision
    assert "不以規則分數或空值冒充" in decision
    assert "DQ RECEIPT" in decision
    assert "決策品質收據" in decision
    assert "{ summary: state.detailSummary }" in workspace


def test_security_master_selector_does_not_select_first_stock_implicitly():
    dashboard_js = (STATIC / "js/features/dashboard.js").read_text(encoding="utf-8")
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert '<select id="symbolSelect"><option value="">請先搜尋並選擇股票</option></select>' in index_html
    assert """select.innerHTML = '<option value="">請先搜尋並選擇股票</option>'""" in dashboard_js
    assert "select.value = seen.has(current) ? current : '';" in dashboard_js
    assert "first.symbol" not in dashboard_js


def test_tdcc_history_ui_requires_explicit_symbol_and_uses_unified_route():
    dashboard_js = (STATIC / "js/features/dashboard.js").read_text(encoding="utf-8")
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'id="tdccHistorySymbol"' in index_html
    assert 'id="tdccHistorySymbol" value=' not in index_html
    assert 'id="loadTdccHistoryBtn"' in index_html
    assert "/flow/chip/tdcc-history?symbol=" in dashboard_js
    assert "散戶（1–10,000 股）" in dashboard_js
    assert "原始列雜湊" in dashboard_js
    assert "TDCC OpenAPI 只提供最新快照" in dashboard_js


def test_stock_view_skips_null_requests_and_preserves_explicit_symbol():
    navigation_js = (STATIC / "js/shell/navigation.js").read_text(encoding="utf-8")
    bootstrap_js = (STATIC / "js/bootstrap.js").read_text(encoding="utf-8")
    dashboard_js = (STATIC / "js/features/dashboard.js").read_text(encoding="utf-8")
    market_chart_js = (STATIC / "js/features/market-chart.js").read_text(encoding="utf-8")

    assert "HomeMarketWorkspace?.load?.({ selectDefault: false })" in navigation_js
    assert "currentViewId() === 'instrument' && String(state.symbol || '').trim()" in bootstrap_js
    assert "const symbol = String(e.target.value || '').trim();" in dashboard_js
    assert "if (symbol) loadSummary(symbol);" in dashboard_js
    assert "const requestedSymbol = String(symbol || '').trim().toUpperCase();" in market_chart_js
    assert "if (!requestedSymbol)" in market_chart_js
    assert "const entitySymbol = String(entity.symbol || symbol).trim().toUpperCase();" in market_chart_js


def test_ownership_tab_replaces_stale_overview_failure_with_an_equity_prerequisite():
    navigation_js = (STATIC / "js/shell" / "navigation.js").read_text(encoding="utf-8")

    assert "function showOwnershipSelectEquityState()" in navigation_js
    assert "renderEmptyBlock('請先選擇個股', message)" in navigation_js
    assert "showOwnershipSelectEquityState();" in navigation_js


def test_frontend_scripts_are_partitioned_and_loaded_in_dependency_order():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    sources = re.findall(r'<script src="(/static/js/[^"]+)', index_html)
    expected = [
        "/static/js/core/dom-state.js",
        "/static/js/core/cache-store.js",
        "/static/js/core/workspace-context-store.js",
        "/static/js/core/preferences.js",
        "/static/js/shell/glass.js",
        "/static/js/shell/freefrontend-liquid-glass.js",
        "/static/js/features/codex.js",
        "/static/js/core/presentation.js",
        "/static/js/features/agent-model-discovery.js",
        "/static/js/features/agent-runtime.js",
        "/static/js/features/agent/agent-formatters.js",
        "/static/js/features/agent/agent-event-reducer.js",
        "/static/js/features/agent/agent-dock-store.js",
        "/static/js/features/agent/agent-stream-controller.js",
        "/static/js/features/agent/agent-session-controller.js",
        "/static/js/features/agent/agent-tool-view.js",
        "/static/js/features/agent/agent-skill-view.js",
        "/static/js/features/agent/agent-approval-view.js",
        "/static/js/features/agent/agent-artifact-selection.js",
        "/static/js/features/agent/agent-composer-context.js",
        "/static/js/features/agent/agent-artifact-view.js",
        "/static/js/features/agent/agent-plan-view.js",
        "/static/js/features/agent/agent-task-tree-view.js",
        "/static/js/features/agent/agent-task-forest.js",
        "/static/js/features/agent/agent-runtime-timeline.js",
        "/static/js/features/agent/agent-decision-card.js",
        "/static/js/features/agent/agent-automation-view.js",
        "/static/js/features/agent/agent-observability-dashboard.js",
        "/static/js/features/agent/agent-evidence-graph.js",
        "/static/js/features/agent/agent-diff-view.js",
        "/static/js/features/agent/agent-schema-visualization.js",
        "/static/js/features/agent/agent-artifact-canvas.js",
        "/static/js/features/agent/agent-conversation-view.js",
        "/static/js/features/agent/agent-context-bar.js",
        "/static/js/features/agent/agent-control-bar.js",
        "/static/js/features/agent/agent-composer.js",
        "/static/js/features/agent/agent-dock-controller.js",
        "/static/js/features/broker-gateway.js",
        "/static/js/features/agent-ui-bridge.js",
        "/static/js/features/trading-workspaces.js",
        "/static/js/features/quant-research.js",
        "/static/js/shell/navigation.js",
        "/static/js/features/market-chart.js",
        "/static/js/features/market-chart-interactions.js",
        "/static/js/features/market-intelligence/snapshot-service.js",
        "/static/js/features/market-intelligence/decision-board.js",
        "/static/js/features/market-intelligence/market-navigation.js",
        "/static/js/features/market-intelligence/workspace-home.js",
        "/static/js/features/explorer.js",
        "/static/js/features/system-status.js",
        "/static/js/features/dashboard.js",
        "/static/js/bootstrap.js",
    ]
    assert [source.split("?", 1)[0] for source in sources] == expected
    assert not (STATIC / "app.js").exists()
    for source in expected:
        assert (STATIC / source.removeprefix("/static/")).is_file()


def test_agent_answers_and_activity_are_unified_in_the_right_dock():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    agent_js = frontend_script()
    runtime_adapter_js = (STATIC / "js/features/agent-runtime.js").read_text(encoding="utf-8")
    model_discovery_js = (STATIC / "js/features/agent-model-discovery.js").read_text(encoding="utf-8")
    dashboard_js = (STATIC / "js/features/dashboard.js").read_text(encoding="utf-8")

    for agent_control in (
        'id="globalAgentPrompt"',
        'id="globalAgentSend"',
        'id="agentAutonomySelect"',
        'id="agentDock"',
        'id="agentChatFeed"',
        'id="agentTasksTree"',
        'id="agentArtifactsList"',
        'id="homeActionFeedback"',
        ):
            assert agent_control in index_html
    for removed_surface in (
        'id="codexPrompt"',
        'id="codexComputerUse"',
        'id="runCodexPrompt"',
        'id="codexAnswer"',
        'id="globalAgentPopover"',
        'id="agentActivityTimeline"',
        'id="agentRuntimeAnswer"',
        "AI Agent 工作台",
    ):
        assert removed_surface not in index_html
    for provider_control in (
        'id="agentDefaultDriver"',
        'id="agentOpenAIBaseUrl"',
        'id="agentOpenAIModel"',
        'id="discoverAgentModels"',
        'id="agentDiscoveredModels"',
        'id="agentDiscoveredModelsField"',
        'id="agentExternalEndpoint"',
        'id="saveAgentRuntimeSettings"',
        'id="testAgentProvider"',
    ):
        assert provider_control in index_html
    assert "health.reachable === false" in runtime_adapter_js
    assert "health.authenticated === false" in runtime_adapter_js
    assert "請確認服務位址、模型與驗證設定" in runtime_adapter_js
    assert "Agent 設定已儲存" in runtime_adapter_js
    assert "state.agentSettingsFormDirty" in runtime_adapter_js
    assert "pickerField.hidden = false" in model_discovery_js
    assert "從下方清單選擇後會帶入模型名稱" in model_discovery_js
    assert "agentControlApi('/api/agents/providers/openai-compatible/models')" in model_discovery_js
    assert "url.hostname = 'localhost'" in model_discovery_js
    assert "state.agentSettingsFormDirty = true" in dashboard_js
    assert "agent-discovered-model-field" in frontend_styles()
    assert "AI 正在規劃下一項任務" in agent_js
    assert 'id="continueWithoutCodex"' in index_html
    assert 'id="settingsLoginCodex"' in index_html
    assert 'data-codex-auth="checking"' in index_html
    assert 'id="codex-auth-first-paint-guard"' in index_html
    assert "body > :not(#authGate):not(script)" in index_html
    assert '[data-codex-auth="checking"]' in frontend_styles()
    assert '[data-codex-auth="required"]' in frontend_styles()
    assert "body > .app-shell" in frontend_styles()
    assert "Stock AI Agent Runtime 保留相同工具、權限與稽核層" in index_html
    assert 'id="agentDriverSelect"' not in index_html
    assert 'id="globalSearch"' not in index_html
    assert '加強模式' not in index_html
    assert index_html.index('id="settingsAgentRuntime"') > index_html.index('id="system"')
    assert "/api/codex/run" not in agent_js
    assert "/api/agents/runs" in agent_js
    assert "active_run_id" in agent_js
    assert "after_sequence" in agent_js
    assert "/api/agents/settings" in agent_js
    assert "runAutonomousAgent" in agent_js
    assert "EXPLICIT_PAPER_ORDER" in agent_js
    assert "autoPaperExecution" in agent_js
    assert "window.confirm" not in runtime_adapter_js
    assert "globalAgentSend" not in dashboard_js
    ui_bridge_js = (STATIC / "js/features/agent-ui-bridge.js").read_text(encoding="utf-8")
    assert "/api/agents/ui/state" in ui_bridge_js
    assert "/api/agents/ui/commands" in ui_bridge_js
    assert "http://127.0.0.1:8000" not in ui_bridge_js
    assert "paperTrainingSubmitOrder" not in ui_bridge_js


def test_plain_language_paper_request_enters_the_local_paper_lane_without_model_category_gate():
    controller_js = (
        STATIC / "js/features/agent/agent-dock-controller.js"
    ).read_text(encoding="utf-8")

    assert "&& EXPLICIT_PAPER_ORDER.test(objective);" in controller_js
    assert "&& intent.category === 'market_decision'\n      && EXPLICIT_PAPER_ORDER.test(objective);" not in controller_js


def test_frontend_styles_are_partitioned_and_loaded_in_cascade_order():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    sources = re.findall(r'<link rel="stylesheet" href="(/static/css/[^"]+)', index_html)
    expected = [
        "/static/css/core/base.css",
        "/static/css/features/workspaces.css",
        "/static/css/features/market-workspace.css",
        "/static/css/shell/material.css",
        "/static/css/features/preferences.css",
        "/static/css/features/codex-home.css",
        "/static/css/features/agent-dock.css",
        "/static/css/shell/responsive.css",
        "/static/css/features/themes.css",
        "/static/css/shell/adaptive-glass.css",
        "/static/css/shell/freefrontend-liquid-glass.css",
    ]
    assert [source.split("?", 1)[0] for source in sources] == expected
    assert not (STATIC / "styles.css").exists()
    for source in expected:
        assert (STATIC / source.removeprefix("/static/")).is_file()


def test_freefrontend_liquid_glass_preserves_the_global_agent_composer_and_syncs_operator_identity():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    material_js = (STATIC / "js/shell/freefrontend-liquid-glass.js").read_text(encoding="utf-8")
    material_css = (STATIC / "css/shell/freefrontend-liquid-glass.css").read_text(encoding="utf-8")
    codex_js = (STATIC / "js/features/codex.js").read_text(encoding="utf-8")
    agent_js = (STATIC / "js/features/agent-runtime.js").read_text(encoding="utf-8")

    assert 'class="searchbar agent-global-composer' in index_html
    assert 'data-liquid-preserve' in index_html
    assert "isFreeFrontendPreserved" in material_js
    assert "searchButtonLiquid" not in material_js
    for feature in (
        "glass-distortion",
        "btn-glass",
        "mini-liquid-lens",
        "liquidGlass-effect",
        "liquidGlass-tint",
        "liquidGlass-shine",
        "slider-thumb-glass",
    ):
        assert feature in material_js or feature in material_css
    assert "baseFrequency=\"0.01 0.01\"" in material_js
    assert 'scale="150"' in material_js
    assert "baseFrequency=\"0.008 0.008\"" in material_js
    assert 'scale="77"' in material_js
    assert 'scale="-252"' in material_js
    assert "prefers-reduced-motion" in material_css
    assert "forced-colors: active" in material_css
    assert "ensureFooonticSwitcherFilter" in material_js
    assert "KwpRaGr-fullpage.html" in material_js
    assert "initFooonticNavigationSwitcher" in material_js
    assert "KwpRaGr-radio-state-machine" in material_js
    assert "const trackPrevious = (el) =>" in material_js
    assert "radio.getAttribute(\"c-option\")" in material_js
    assert "offsetLeft" not in material_js
    assert "offsetTop" not in material_js
    assert ".nav-menu.stock-nav-switcher" in material_css
    assert ":has(input[c-option=\"1\"]:checked)::after" in material_css
    assert "blur(8px) url(#stock-ai-nav-switcher) saturate(var(--saturation))" in material_css
    assert "inset 1.8px 3px 0 -2px" in material_css
    assert "--ui-glass-material-opacity" in material_css
    assert "--ui-glass-material-rgb" in material_css
    assert "white veil to text, charts, or status colors" in material_css
    assert "JavaScript does not measure or" in material_css
    pens = STATIC / "vendor/freefrontend-liquid-glass/pens"
    assert len(list(pens.glob("*-fullpage.html"))) == 16
    assert all(path.stat().st_size > 7_000 for path in pens.glob("*-fullpage.html"))
    assert "account?.email || (plan" not in codex_js
    assert "'openai-compatible': 'Stock AI Agent · OpenAI Compatible'" in agent_js
    assert "'external-agent': 'Stock AI Agent · 外部 Agent'" in agent_js


def test_agent_model_discovery_reads_the_backend_items_contract():
    agent_js = (STATIC / "js/features/agent-model-discovery.js").read_text(encoding="utf-8")
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "Array.isArray(result.items)" in agent_js
    assert "models.forEach((model) =>" in agent_js
    assert "pickerField.hidden = false" in agent_js
    assert "從下方清單選擇後會帶入模型名稱" in agent_js
    assert "agentDiscoveredModelsField" in index_html
    assert "agent-model-discovery.js?v=20260801-visible-picker-v1" in index_html
    liquid_system = (STATIC / "liquid-glass-system.css").read_text(encoding="utf-8")
    assert "html,body{user-select:text;-webkit-user-select:text}" in liquid_system


def test_liquid_glass_uses_designed_theme_wallpapers_instead_of_a_forced_floral_image():
    base_css = (STATIC / "css/core/base.css").read_text(encoding="utf-8")
    bridge_css = (STATIC / "css/shell/freefrontend-liquid-glass.css").read_text(encoding="utf-8")
    material_css = (STATIC / "css/shell/material.css").read_text(encoding="utf-8")
    theme_css = (STATIC / "css/features/themes.css").read_text(encoding="utf-8")
    preferences_css = (STATIC / "css/features/preferences.css").read_text(encoding="utf-8")

    dark_wallpaper = STATIC / "assets/market-intelligence-dark.jpg"
    light_wallpaper = STATIC / "assets/market-intelligence-light.jpg"
    assert dark_wallpaper.stat().st_size > 250_000
    assert light_wallpaper.stat().st_size > 200_000
    assert 'url("../../assets/market-intelligence-dark.jpg")' in base_css
    assert ".market-tile-field{\n  display:block" in base_css
    assert ".market-tile{display:none}" in base_css
    assert "liquid-glass-floral-background.png" not in base_css
    assert "html .liquid-backdrop" not in bridge_css
    assert "liquid-glass-floral-background.png" not in bridge_css
    assert ".liquid-backdrop{background:#010407}" not in material_css
    assert 'html[data-ui-backdrop="constellation"] .market-tile-field' in theme_css
    assert 'html[data-ui-backdrop="tiles"] .market-tile-field{opacity:1' in theme_css
    assert 'html[data-ui-theme="graphite"] .liquid-backdrop{' in preferences_css
    assert 'html[data-ui-theme="clarity"] .liquid-backdrop{' in preferences_css
    assert 'html[data-ui-theme="terminal"] .liquid-backdrop{' in preferences_css
    assert preferences_css.count('url("../../assets/market-intelligence-dark.jpg")') >= 7
    assert preferences_css.count('url("../../assets/market-intelligence-light.jpg")') >= 2
    assert 'html[data-ui-backdrop="grid"] .market-tile-field' in theme_css
    assert "conic-gradient" in theme_css
    assert "repeating-linear-gradient" not in theme_css
    assert "repeating-radial-gradient" not in theme_css
    assert "background-size:72px 72px" not in theme_css
    assert 'html[data-ui-theme="aurora"] .liquid-backdrop' in theme_css
    assert 'html[data-ui-theme="pearl"] .liquid-backdrop' in theme_css
    assert 'html[data-ui-theme="daylight"] .liquid-backdrop' in theme_css
    assert theme_css.count('url("../../assets/market-intelligence-dark.jpg")') >= 1
    assert theme_css.count('url("../../assets/market-intelligence-light.jpg")') >= 2


def test_market_intelligence_home_workspace_is_bound_without_a_required_symbol_input():
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    workspace_js = (STATIC / "js/features/market-intelligence/workspace-home.js").read_text(encoding="utf-8")
    snapshot_js = (STATIC / "js/features/market-intelligence/snapshot-service.js").read_text(encoding="utf-8")

    for item in [
        'data-view="home"',
        'id="home"',
        'id="homeChartHost"',
        'id="marketNavigationList"',
        'id="actionableDecisionList"',
        'id="avoidDecisionList"',
        'id="watchDecisionList"',
        'id="futureDecisionList"',
        'id="portfolioDecisionList"',
        'id="workspaceDecisionDetail"',
        'market-workspace.css?v=20260813-responsive-density-v1',
        'workspace-home.js?v=20260813-light-theme-v1',
    ]:
        assert item in index_html

    for item in [
        "renderInstrumentWorkbench",
        "WorkspaceContextStore.set",
        "MarketDecisionBoard.render",
        "MarketWorkspaceNavigation.hydrate",
        "await loadSummary(normalized, { navigate: false })",
        "目前卡片與圖表保留",
        "drawHomeChartPreview",
        "data-home-chart-type",
        "data-home-chart-range",
        "homeChartHoverCard",
        "homeChartDecisionPanel",
        "renderHomeChartDecision",
        "homeFactorEvidence",
        "不會自動建立 Agent Run",
        "目前決策",
        "觸發條件",
        "失效條件",
        "下一次重估",
    ]:
        assert item in workspace_js
    assert "context.drawImage(source" not in workspace_js
    assert "/api/workspace/bootstrap" in snapshot_js
    assert "/api/workspace/stream" in snapshot_js
    assert 'id="homeUniverseSource"' not in index_html
    assert "function preserveExplicitTaskSelection(liveContext)" in workspace_js
    assert "reason: 'bootstrap:preserve-explicit-task-selection'" in workspace_js
    assert "preserveExplicitTaskSelection(liveNavigation);" in workspace_js
    assert "它不在本次全市場候選清單中" in workspace_js


def test_chart_ui_has_axes_and_technical_indicators():
    app_js = frontend_script()
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    styles = frontend_styles()

    required_labels = [
        "K線圖模式",
        "最新K：",
        "價格",
        "時間",
        "成交量",
        "MA5",
        "MA10",
        "MA20",
        "MA60",
        "BOLL(20,2)",
        "MACD(12,26,9)",
        "盤中即時行情",
        "交易狀態",
        "最佳委買 / 委賣",
        "委買五檔",
        "委賣五檔",
        "單筆",
        "累計成交量 / 更新",
    ]
    for label in required_labels:
        assert label in app_js

    assert "realtimePanel" in index_html
    assert "時間橫軸" in index_html
    assert "價格縱軸" in index_html
    assert "#priceChart" in styles
    assert "620px" in styles
    assert "renderChartMessage" in app_js
    assert "normalizeChartPoints" in app_js
    assert "stock_ai.realtime_quote.v1" in app_js
    assert "realtimeTradingStatusLabel" in app_js
    assert "realtimeFreshnessLabel" in app_js
    assert "data.best_bid || data.bids?.[0]" in app_js
    assert "<th>價格</th><th>張數</th>" in app_js
    assert "20260728-intraday-candles-v2" in index_html
    assert "return data.previous_close ?? null" not in app_js
    assert "background: '#07111f'" not in app_js
    assert "background: '#08080a'" not in app_js
    assert "background:transparent!important" in styles
    assert ".chart-canvas-glass::before" in styles
    assert 'class="chart-canvas-layout"' in index_html
    assert 'class="chart-drawing-tools" role="toolbar"' in index_html
    assert "chart-drawing-settings-popover" in index_html
    assert ".chart-canvas-layout{position:relative;display:grid;grid-template-columns:48px minmax(0,1fr)" in styles


def test_home_index_chart_uses_index_only_path_and_workspace_stream_is_authenticated():
    chart_js = (STATIC / "js/features/market-chart.js").read_text(encoding="utf-8")
    snapshot_js = (STATIC / "js/features/market-intelligence/snapshot-service.js").read_text(encoding="utf-8")

    assert "async function loadMarketIndexSummary" in chart_js
    assert "return loadMarketIndexSummary(requestedSymbol, options);" in chart_js
    assert "/api/instruments/${encodeURIComponent(requestedSymbol)}/chart" in chart_js
    assert "不會送出個股即時五檔、財報、籌碼或估值請求" in chart_js
    assert "new EventSource" not in snapshot_js
    assert "openSecuredEventStream(" in snapshot_js
    assert "new Set(['snapshot.updated'])" in snapshot_js


def test_chart_workbench_supports_crosshair_view_modes_zoom_pan_and_persistent_annotations():
    app_js = frontend_script()
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    styles = frontend_styles()

    for control in (
        'id="chartInteractionSurface"',
        'id="priceChartOverlay"',
        'id="chartHoverCard"',
        'id="chartAnnotationLabel"',
        'id="chartUndoAnnotation"',
        'id="chartClearAnnotations"',
        'id="chartResetViewport"',
        'id="chartInteractionStatus"',
        'data-chart-type="candles"',
        'data-chart-type="bars"',
        'data-chart-type="line"',
        'data-chart-type="area"',
        'data-chart-type="heikin"',
        'data-chart-tool="cursor"',
        'data-chart-tool="trend"',
        'data-chart-tool="horizontal"',
        'data-chart-tool="rectangle"',
        'data-chart-tool="text"',
        'data-chart-tool="marker"',
        'data-chart-range="1"',
        'data-chart-range="5"',
        'data-chart-range="750"',
        'data-chart-range="1250"',
        'data-chart-range="all"',
    ):
        assert control in index_html

    for interaction_contract in (
        "stock-ai.chart-workbench.v1",
        "stock-ai.chart-annotations.v1",
        "resolveViewport",
        "setRenderModel",
        "handlePointerMove",
        "handlePointerDown",
        "handleWheel",
        "setPointerCapture",
        "renderOverlay",
        "createAnnotationFromPoint",
        "heikinAshiSeries",
        "document.querySelectorAll('button[data-chart-tool]')",
        "window.StockChartInteractions?.init()",
    ):
        assert interaction_contract in app_js

    assert ".price-chart-overlay" in styles
    assert "touch-action:none" in styles
    assert "cursor:crosshair" in styles
    assert 'market-chart-interactions.js?v=20260813-chart-restore-v1' in index_html
    assert 'id="chartFocusMode"' in index_html


def test_chart_indicator_controls_are_independent_and_touch_zoom_can_recover_from_minimum():
    chart_js = (STATIC / "js/features/market-chart.js").read_text(encoding="utf-8")
    interactions = (STATIC / "js/features/market-chart-interactions.js").read_text(encoding="utf-8")
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")

    for key in ("ma5", "ma10", "ma20", "ma60", "boll", "volume", "macd"):
        assert f'data-chart-indicator="{key}"' in index_html
    assert 'aria-label="技術線與資料顯示"' in index_html
    assert 'id="chartIndicatorToggle"' not in index_html
    assert "INDICATOR_KEYS" in interactions
    assert "function setIndicator(key)" in interactions
    assert "getIndicators: () => ({ ...indicators })" in interactions
    assert "WHEEL_ZOOM_THRESHOLD" in interactions
    assert "Math.max(nextSpan + 1, Math.ceil(nextSpan * 1.16))" in interactions
    assert "function applyZoomToSpan(rawSpan, anchorPoint = null)" in interactions
    assert "function resetGestureState()" in interactions
    assert "function finishWheelSession()" in interactions
    assert "if (!pinchGesture && activeTouchPointers.size >= 2) beginPinchGesture();" in interactions
    assert "wheelZoomEndTimer = window.setTimeout(finishWheelSession, 180);" in interactions
    assert "overlay.addEventListener('pointerenter'" in interactions
    assert "lostpointercapture" in interactions
    assert "beginPinchGesture" in interactions
    assert "activeTouchPointers" in interactions
    assert "const showVolume = indicators.volume !== false;" in chart_js
    assert "const showMacd = indicators.macd !== false;" in chart_js
    assert "if (indicators.ma5 !== false)" in chart_js
    assert "if (indicators.boll !== false)" in chart_js


def test_glass_sampling_clone_cannot_duplicate_workspace_or_action_contracts():
    glass = (STATIC / "js/shell/glass.js").read_text(encoding="utf-8")

    for attribute in (
        "data-ui-id", "data-ui-action", "data-workspace-parent",
        "data-workspace-tab", "data-route-content", "data-view",
    ):
        assert attribute in glass
    assert "data-liquid-sample-clone" in glass
    assert "appClone.setAttribute('aria-hidden', 'true')" in glass
    assert "appClone.setAttribute('inert', '')" in glass
    navigation = (STATIC / "js/shell/navigation.js").read_text(encoding="utf-8")
    assert "function isApplicationPanel(panel)" in navigation
    assert "!panel.closest('[data-liquid-sample-clone]')" in navigation
    assert "function selectedEquitySymbol()" in navigation
    assert "市場指數不會代替個股送出資料請求" in navigation


def test_system_tabs_are_single_responsibility_without_a_second_jumpbar():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    navigation_js = (STATIC / "js/shell/navigation.js").read_text(encoding="utf-8")
    preferences_css = (STATIC / "css/features/preferences.css").read_text(encoding="utf-8")

    assert 'settings-jumpbar' not in index_html
    for tab in ('securities', 'data-platform', 'brokers', 'agent-models', 'tools', 'interface'):
        assert f"['{tab}'," in navigation_js
    assert 'loadEntities?.()' in navigation_js
    assert 'loadCatalog?.()' in navigation_js
    assert 'loadBrokerConnections?.()' in navigation_js
    assert 'data-system-section="agent-models"' in index_html
    assert 'data-system-section="brokers"' in index_html
    assert 'data-system-section="interface"' in index_html
    assert 'data-workspace-tab="tools"' in preferences_css
    assert "registryChip.textContent = '暫時無法取得'" in navigation_js
    assert "platformChip.textContent = '暫時無法取得'" in navigation_js
    agent_runtime = (STATIC / "js/features/agent-runtime.js").read_text(encoding="utf-8")
    assert "$('settingsAgentBadge').textContent = `${agentDriverLabel(identity)} 已選用`" in agent_runtime
    assert "$('settingsAgentBadge').textContent = '載入失敗'" in agent_runtime
    codex_js = (STATIC / "js/features/codex.js").read_text(encoding="utf-8")
    assert "function boundedSystemStatus(promise, milliseconds = 6000)" in codex_js
    assert "boundedSystemStatus(loadCodexAccount(refresh))" in codex_js


def test_visible_market_ui_is_realtime_only():
    app_js = frontend_script()
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")

    assert "歷史日K + 盤中即時最新一根" in app_js
    assert "所有可見行情與圖表參數只用即時 payload" in app_js
    assert "mergedChartSeries" in app_js
    assert "realtimeCandles" in app_js
    assert "/history" in app_js
    assert "renderCurrentChart()" in app_js
    assert "previous_close" not in app_js
    assert "metrics.close" not in app_js
    assert "metrics.date" not in app_js
    assert "chartTitle" in index_html


def test_intraday_candle_ui_selects_date_and_all_required_timeframes():
    app_js = frontend_script()
    index_html = (
        ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    styles = frontend_styles()

    for control in (
        'id="intradayTimeframe"',
        'id="intradayTradeDate"',
        'id="loadIntradayCandles"',
        'id="intradayCandleStatus"',
    ):
        assert control in index_html
    for option in (
        '<option value="1">1 分 K</option>',
        '<option value="5">5 分 K</option>',
        '<option value="15">15 分 K</option>',
        '<option value="30">30 分 K</option>',
        '<option value="60">60 分 K</option>',
    ):
        assert option in index_html
    assert "/intraday/candles/" in app_js
    assert "stock_ai.intraday_candle.v1" in app_js
    assert "指定交易日重建" in app_js
    assert "由 ${payload.source_one_minute_count} 根已保存 1 分 K 重建" in app_js
    assert ".intraday-candle-status" in styles


def test_trading_workspace_ui_emphasizes_preview_and_no_live_order_submission():
    app_js = frontend_script()
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")

    required_html = [
        "I. 交易功能",
        "僅供預覽",
        "更新交易預覽",
        "模型研究",
        "K. 風控功能",
        "L. 帳務與資產",
    ]
    for item in required_html:
        assert item in index_html

    required_preview_copy = [
        "目前僅提供唯讀預覽。",
        "未接券商 API 前，僅可檢視預覽與成本估算。",
        "委託流程目前固定為預覽模式",
        "本機預覽資產摘要",
        "部位、已實現損益與股利收入目前為預覽或推估值",
    ]
    for item in required_preview_copy:
        assert item in app_js

    assert "更新 AI 建議" not in index_html
    assert "Refresh AI Advice" not in app_js
    for missing_symbol_copy in (
        "未指定標的時不會建立交易預覽。",
        "本輪不會執行規則或模型分析。",
        "未指定標的時不會產生風控判定。",
    ):
        assert missing_symbol_copy in app_js


def test_resize_keeps_existing_realtime_kline_instead_of_reloading_summary():
    app_js = frontend_script()

    assert "function renderCurrentChart()" in app_js
    assert "state.realtimeCandles[key] || []" in app_js
    assert "window.addEventListener('resize', () => { renderCurrentChart(); });" in app_js
    assert "window.addEventListener('resize', () => { if (state.symbol) loadSummary(state.symbol).catch(() => {}); });" not in app_js


def test_initial_summary_load_does_not_override_active_workspace():
    app_js = frontend_script()

    assert "async function loadSummary(symbol = state.symbol, options = {})" in app_js
    assert "if (options.navigate !== false) setView('instrument');" in app_js
    assert "loadSummary(state.symbol, { navigate: false })" in app_js


def test_slow_sidebar_loader_cannot_restore_an_old_workspace_after_navigation():
    market_chart_js = (STATIC / "js/features/market-chart.js").read_text(encoding="utf-8")
    navigation_js = (STATIC / "js/shell/navigation.js").read_text(encoding="utf-8")

    assert "const requestedTab = document.documentElement.dataset.workspaceTab || '';" in market_chart_js
    assert "window.__activeWorkspace === view\n      && document.documentElement.dataset.workspaceTab === requestedTab" in market_chart_js
    assert "const previousWorkspace = window.__activeWorkspace || '';" in navigation_js
    assert "previousWorkspace === workspace && context.route?.workspace === workspace" in navigation_js


def test_trading_workspaces_i_j_k_l_are_present_and_bound():
    app_js = frontend_script()
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    styles = frontend_styles()

    required_navs = [
        'data-view="home"',
        'data-view="market"',
        'data-view="instrument"',
        'data-view="portfolio"',
        'data-view="research"',
        'data-view="system"',
    ]
    for item in required_navs:
        assert item in index_html

    required_sections = [
        'id="portfolio"',
        'id="research"',
        'id="risk"',
        'id="assets"',
        'id="refreshTradingBtn"',
        'id="refreshAssistantBtn"',
        'id="refreshRiskBtn"',
        'id="refreshAssetsBtn"',
        'id="tradingLinkedSummary"',
        'id="assistantCardsBox"',
        'id="riskAlertsBox"',
        'id="assetPositionsTable"',
    ]
    for item in required_sections:
        assert item in index_html

    required_bindings = [
        "async function loadTradingView()",
        "async function loadAssistantView()",
        "async function loadRiskView()",
        "async function loadAssetsView()",
        "if (workspace === 'portfolio' && tab === 'orders') return loadTradingView?.();",
            "if (workspace === 'portfolio' && tab === 'overview') return loadAssetsView?.();",
            "if (workspace === 'portfolio' && tab === 'positions') return loadAssetsView?.();",
            "if (workspace === 'portfolio' && tab === 'simulation') return window.__paperTradingUI?.loadAccount?.({ refreshPrices: false });",
        "if (workspace === 'research' && tab === 'deep-research') return loadAssistantView?.();",
        "HomeMarketWorkspace?.load?.({ selectDefault: false })",
        "const WORKSPACES = Object.freeze({",
        "portfolio: { title: '投資組合'",
        "research: { title: '研究'",
    ]
    for item in required_bindings:
        assert item in app_js

    assert ".trade-grid" in styles
    assert ".workspace-alert.block" in styles


def test_workspace_manifest_has_exactly_six_top_level_entries_and_embeds_paper_trading():
    app_js = frontend_script()
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    nav_views = re.findall(r'<button[^>]+class="nav-btn[^>]*"[^>]+data-view="([^"]+)"', index_html)
    assert nav_views == ["home", "market", "instrument", "portfolio", "research", "system"]
    assert 'data-paper-trading-nav' not in index_html
    assert "data-paper-trading-nav" not in app_js
    assert "const WORKSPACES = Object.freeze({" in app_js
    for workspace in ("home", "market", "instrument", "portfolio", "research", "system"):
        assert f"{workspace}: {{" in app_js


def test_workspace_routes_expose_second_level_navigation_loaders_and_shared_agent_context():
    navigation = (STATIC / "js/shell/navigation.js").read_text(encoding="utf-8")
    context = (STATIC / "js/core/workspace-context-store.js").read_text(encoding="utf-8")
    agent_context = (STATIC / "js/features/agent/agent-context-bar.js").read_text(encoding="utf-8")

    for label in (
        "['overview', '總覽']", "['rankings', '排行']", "['watchlists', '自選與提醒']",
        "['screener', '條件選股']", "['monitor', '即時監控']", "['events', '新聞與事件']",
        "['chart', '圖表']", "['technical', '技術']", "['ownership', '籌碼']",
        "['financials', '財務']", "['valuation', '估值']", "['events', '新聞與事件']",
        "['evidence', '風險與證據']", "['positions', '持倉與建議']",
        "['simulation', '模擬帳戶']", "['performance', '績效']",
        "['factor-lab', '因子研究']", "['tools', '工具與整合']",
    ):
        assert label in navigation

    for loader in (
        "async function loadWorkspaceTabData(workspace, tab)",
        "if (workspace === 'market' && tab === 'overview') return loadDashboardOverview?.();",
        "if (workspace === 'portfolio' && tab === 'orders') return loadTradingView?.();",
        "if (workspace === 'research' && tab === 'deep-research') return loadAssistantView?.();",
            "routeTimeout(loadWorkspaceTabData(workspace, activeTab), loadBudget)",
            "workspace === 'system' && activeTab === 'data-platform' ? 15_000 : 6_000",
        "function renderWorkspaceFailure(workspace, tab, error)",
        "route: { workspace, tab: activeTab, params: routeParams }",
    ):
        assert loader in navigation

    assert "route: { workspace: 'home', tab: 'overview', params: {} }" in context
    assert "selection: {" in context
    assert "workspaceContext.route?.workspace" in agent_context
    assert "workspaceContext.route?.tab" in agent_context
    assert "workspaceContext.selection?.symbol" in agent_context


def test_instrument_events_route_does_not_block_news_on_full_symbol_detail_fanout():
    navigation = (STATIC / "js/shell/navigation.js").read_text(encoding="utf-8")

    route_start = navigation.index("if (workspace === 'instrument' && tab === 'events')")
    route_end = navigation.index("if (workspace === 'instrument' && tab === 'evidence')", route_start)
    route = navigation[route_start:route_end]
    assert "const payload = await api(uiDataApi(`/news/center?symbol=${encodeURIComponent(symbol)}&limit=8`));" in route
    assert "await loadDashboardSymbolDetails?.(symbol);" not in route
    assert "loadDashboardSymbolDetails?.(symbol).catch?.(() => {});" in route


def test_home_overview_is_a_native_workspace_route_not_a_loading_fallback():
    navigation = (STATIC / "js/shell/navigation.js").read_text(encoding="utf-8")
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'data-ui-id="home.root"' in index_html
    assert "workspace === 'home' || nativeTabs.includes(activeTab)" in navigation
    workspace_home = (STATIC / "js/features/market-intelligence/workspace-home.js").read_text(encoding="utf-8")
    assert "if (workspaceToPreserve)" in workspace_home
    assert "workspaceToPreserve !== 'home'" not in workspace_home
    assert "navigation.js?v=20260803-ui-contract-v5" in index_html
    assert "view.classList.toggle('workspace-route-hidden', !showNativeView);" in navigation
    assert "routePanel(" not in navigation


def test_workspace_context_ignores_late_persist_responses_from_an_older_navigation():
    context = (STATIC / "js/core/workspace-context-store.js").read_text(encoding="utf-8")

    assert "let localRevision = 0;" in context
    assert "const requestRevision = localRevision;" in context
    assert "if (requestRevision !== localRevision) return;" in context
    assert "localRevision += 1;\n    value = normalize(merge(value, next || {}));" in context
    assert "value = normalize(response || payload);" in context


def test_research_comparison_is_a_native_verified_workspace_not_a_summary_fallback():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    navigation = (STATIC / "js/shell/navigation.js").read_text(encoding="utf-8")

    assert 'id="research" data-workspace="research" data-default-tab="compare"' in index_html
    for item in (
        'id="researchComparison"',
        'data-workspace-parent="research" data-workspace-tab="compare"',
        'id="researchComparisonTarget"',
        'id="researchComparisonPeers"',
        'id="researchComparisonTable"',
        'id="researchComparisonForm"',
    ):
        assert item in index_html

    for item in (
            "async function loadResearchComparison",
            "function renderResearchComparison",
            "RESEARCH_COMPARISON_METRICS",
            "function comparisonApi(path)",
            "function comparisonApiOptions()",
            "comparisonApi(`/fundamentals/valuation/peers?${params.toString()}`)",
        "comparison: { symbols: [target, ...peers] }",
        "請輸入至少一檔同業股票後按「開始比較」",
            "comparisonApiOptions(),",
        "if (value === null || value === undefined || value === '') return '-';",
        "selection: { symbol: target }",
        "research: { title: '研究', subtitle: '股票比較、AI 深度研究、策略回測、因子、關聯與研究報告', defaultTab: 'compare'",
        "loadResearchComparison().catch(error => console.warn('Research comparison is unavailable', error));",
        "if (workspace === 'research' && tab === 'compare') return loadResearchComparison();",
    ):
        assert item in navigation
    assert "比較清單已同步" not in navigation


def test_system_tabs_do_not_mix_agent_skills_and_connection_cards():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    preferences_css = (STATIC / "css/features/preferences.css").read_text(encoding="utf-8")
    navigation_js = (STATIC / "js/shell/navigation.js").read_text(encoding="utf-8")

    assert 'data-system-section="agent skills connections"' not in index_html
    assert 'data-system-card=' not in index_html
    assert 'data-system-section="agent-models"' in index_html
    assert 'data-system-section="tools"' in index_html
    assert 'data-system-section="brokers"' in index_html
    assert 'system-agent-connection' in index_html
    assert 'system-skills' in index_html
    assert 'system-api-connections' in index_html
    assert '模型與執行狀態' in navigation_js
    assert "dataset?.workspaceTab === 'interface'" in (STATIC / 'js/core/preferences.js').read_text(encoding='utf-8')
    assert '.settings-connections [data-system-card=' not in preferences_css


def test_system_securities_page_has_a_searchable_native_empty_state():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    explorer_js = (STATIC / "js/features/explorer.js").read_text(encoding="utf-8")

    assert 'id="entitySearchForm"' in index_html
    assert 'id="entitySearchInput"' in index_html
    assert 'id="entityPageStatus"' in index_html
    assert 'id="entitySearchHint"' in index_html
    assert "function bindEntitySearch()" in explorer_js
    assert "form.dataset.entitySearchBound === 'true'" in explorer_js
    assert "document.addEventListener('DOMContentLoaded', bindEntitySearch" in explorer_js
    assert "features/explorer.js?v=20260907-operator-alert-delivery-v1" in index_html
    assert "new AbortController()" in explorer_js
    assert "controller.abort(), 12_000" in explorer_js
    assert "證券來源回應逾時" in explorer_js
    assert "證券主檔暫時無法讀取" in explorer_js


def test_open_stock_ai_workspace_is_present_and_bound():
    app_js = frontend_script()
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    styles = frontend_styles()

    required_html = [
        'data-workspace-parent="research"',
        'data-workspace-tab="strategy-lab"',
        'id="openstock"',
        'id="openStockSymbol"',
        'id="openStockMarket"',
        'id="openStockHorizon"',
        'id="runOpenStockAI"',
        'id="openStockSession"',
        'id="runOpenStockSession"',
        'id="openStockDecisionCards"',
        'id="openStockSessionBox"',
        'id="openStockFlowBox"',
        'id="openStockSourcesBox"',
        'id="openStockAdapterBox"',
        "量化策略研究",
        "執行研究",
        "執行批次",
        "研究來源與證據",
        "資料轉接器",
    ]
    for item in required_html:
        assert item in index_html

    required_bindings = [
        "async function loadOpenStockAIView()",
        "async function loadOpenStockAISession()",
        "function renderOpenStockSession(payload)",
        "const sharedRiskContext = payload?.portfolio_risk_context || {};",
        "資料待補：",
        "尚未設定批次觀察清單",
        "function renderOpenStockDecision(decision)",
        "function renderOpenStockFlow(decision)",
        "const performanceMetrics = research.performance_metrics || research.raw?.finrl?.performance_metrics || {};",
        "const benchmarkMetrics = research.benchmark_metrics || research.raw?.finrl?.benchmark_metrics || {};",
        "const monteCarloTailRisk = research.monte_carlo_tail_risk || research.raw?.finrl?.monte_carlo_tail_risk || {};",
        "const regimeRobustness = research.regime_robustness || research.raw?.finrl?.regime_robustness || {};",
        "const historicalUniverse = research.historical_universe || research.raw?.finrl?.historical_universe || {};",
        "const modelProvenance = decisionSchema.model_provenance || {};",
        "const scoreCalibration = decisionSchema.score_calibration || {};",
        "const modelRegistry = researchRaw.model_registry || {};",
        "'模型版本'",
        "const adapterModel = adapterResult.metrics?.model_provenance || {};",
        "模型輸出 ${adapterModel.model_output ? '已驗證 runtime' : '非模型輸出'}",
        "推廣 ${escapeHtml(modelProvenance.promotion_status || 'research_only_unpromoted')}",
        "部署 ${modelProvenance.deployment_mode || 'none'}",
        "Sortino ${performanceMetrics.sortino ?? '-'}",
        "Alpha ${benchmarkMetrics.alpha_annualized_pct ?? '-'}%",
        "CVaR ${monteCarloTailRisk.conditional_value_at_risk_loss_pct ?? '-'}%",
        "Regime ${regimeRobustness.passed ? '通過' : '需檢視'}",
        "Universe ${historicalUniverse.passed ? '通過' : '需檢視'}",
        "CAGR ${(research.performance_metrics || researchRaw.finrl?.performance_metrics)?.cagr_pct ?? '-'}%",
        "const supplementary = await Promise.allSettled([",
        "renderOpenStockDecision(decision, {});",
        "Open Stock AI core research render failed",
        "function renderOpenStockSources(payload, storage = null)",
        "if (workspace === 'research' && tab === 'deep-research') return loadAssistantView?.();",
        "/api/open-stock-ai/analyze",
        "/api/open-stock-ai/session/",
        "/api/open-stock-ai/external-sources",
        "/api/open-stock-ai/optional-external-sources",
        "/api/open-stock-ai/broker-import-governance",
        "/api/open-stock-ai/storage",
        "/api/open-stock-ai/integration-audit",
        "/api/open-stock-ai/signals",
        "/api/open-stock-ai/paper-orders",
        "/api/open-stock-ai/paper-exposure",
        "整合稽核",
        "需求矩陣",
        "模擬交易帳本",
        "模擬曝險",
        "執行期連接器",
        "券商匯入",
        "外部貢獻",
        "研究產物",
        "決策結構",
        "來源鎖定",
        "選用來源",
        "外部訊號",
        "證據溯源",
        "資料來源",
        "外部訊號結構",
        "訊號結構",
        "contractLabel",
        "risk.gate_checks",
        "行情資料中樞",
        "風控閘門",
        "模擬執行",
        "external_project",
        "adapter_results",
        "adapter_result",
        "capability_files",
        "unifiedAdapterResults",
        "鎖定 ${adapterResult.lock_verified ? '已驗證' : '需檢視'}",
        "來源已鎖定並可追溯",
        "sourceDisplayName",
        "neutralResearchText",
        "runOpenStockAI",
        "runOpenStockSession",
        "投組建構",
        "結果歸因",
        "openStockSymbol",
        "請先輸入台股代號",
        "策略研究需要明確的 .TW 或 .TWO 股票代號。",
        "btn.dataset.title || textLabel || btn.textContent.trim()",
    ]
    for item in required_bindings:
        assert item in app_js

    assert ".view#openstock .two-col" in styles
    assert ".process-flow" in styles
    assert ".process-step" in styles


def test_system_requirement_contract_is_visible_in_catalog_ui():
    app_js = frontend_script()
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")

    for item in [
        "系統規格覆蓋",
        "requirementContractBox",
        "scheduleGuardBox",
        "sourcePolicyBox",
        "officialEventsBox",
        "officialDerivativesBox",
        "updateRunnerBox",
        "requirementSafetyBox",
    ]:
        assert item in index_html
    for item in [
        "/api/system/requirements",
        "/api/system/schedule",
        "/api/system/source-policy",
        "/api/system/official-events",
        "/api/system/official-derivatives",
        "/api/system/update-plan",
        "renderRequirementContract",
        "renderScheduleGuard",
        "renderSourcePolicy",
        "renderOfficialEvents",
        "renderOfficialDerivatives",
        "renderUpdateRunner",
        "資料來源分層",
        "輔助行情限制",
        "更新任務乾跑",
        "TDCC 集保",
        "TAIFEX 期權",
        "JSON/CSV 匯入",
        "MOPS 重大訊息",
        "交易邊界",
        "目前排程",
    ]:
        assert item in app_js


def test_data_platform_exposes_news_pit_coverage_without_certifying_provider_history():
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    explorer_js = (STATIC / "js/features/explorer.js").read_text(encoding="utf-8")

    assert "新聞歷史回放覆蓋證據" in index_html
    assert 'id="newsHistoryCoverage"' in index_html
    assert "uiDataApi('/news/history-coverage')" in explorer_js
    assert "新聞中心看得到的內容不等於 PIT 證據" in explorer_js
    assert "Provider 全歷史：未認證" in explorer_js
    assert "reviewed_provider_wide_historical_coverage_receipt_missing" in explorer_js


def test_static_ui_uses_traditional_chinese_colorful_market_theme():
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    styles = frontend_styles()
    app_js = frontend_script()
    vendor_theme = (ROOT / "src" / "stock_ai" / "ui" / "static" / "vendor" / "glinui-theme.css").read_text(encoding="utf-8")
    vendor_liquid = (ROOT / "src" / "stock_ai" / "ui" / "static" / "vendor" / "liquidGL.js").read_text(encoding="utf-8")
    glass_system = (ROOT / "src" / "stock_ai" / "ui" / "static" / "liquid-glass-system.css").read_text(encoding="utf-8")

    for item in [
        'lang="zh-Hant"',
        "市場操作台",
        "市場資料",
        "即時行情監控",
        "量化策略研究",
        "sendNotificationDryRunBtn",
        "notificationSendResultBox",
        "發送 dry-run 檢查",
    ]:
        assert item in index_html

    for item in [
        "--bg:#050506",
        "--page-bg:#000",
        "--glass:rgba(255,255,255,.075)",
        "--glass-3-surface:rgba(255,255,255,.078)",
        "--glass-refraction-top:rgba(255,255,255,.32)",
        "--liquid-gl-edge:rgba(255,255,255,.18)",
        "background:var(--page-bg)",
        "backdrop-filter:saturate(165%) blur(var(--glass-blur-lg))",
        "background:rgba(255,255,255,.085)",
        ".ambient-grid{display:none}",
        ".technical-details",
        ".process-flow",
        ".status-dot",
    ]:
        assert item in styles
    assert 'data-ui-backdrop="constellation"' in styles
    assert "conic-gradient" in styles
    assert "repeating-radial-gradient" not in styles
    assert "dataset.liquidCanvasPresentation = 'background-webgl-canvas'" in app_js
    assert "dataset.glinSpotlightPresentation = 'token-bridge'" in app_js
    assert "context.createRadialGradient" not in app_js
    assert "dataset.liquidSnapshotVariance" in vendor_liquid
    assert 'element.hasAttribute("data-liquid-sample-source-root")' in vendor_liquid
    assert "liquid-native-canvas" in app_js
    assert "liquid-lens-proxy" not in app_js
    assert "renderer.canvas.style.setProperty('z-index', '0', 'important')" in app_js
    assert "appShell.style.setProperty('z-index', '1')" in app_js
    assert "Pale tinted glass" in styles
    assert "linear-gradient(145deg,rgba(96,165,250,.08)" in styles
    assert ".nav-btn::after{display:none!important}" in styles
    assert ".nav-btn:nth-of-type(5n+3).active" not in styles
    assert ".nav-btn:nth-of-type(5n).active" not in styles
    for item in [
        'id="settingsAgentConnectionTitle"',
        'id="settingsSkillsBox"',
        'id="settingsMcpBox"',
        'id="settingsApiBox"',
        'id="refreshAgentStatus"',
        'id="refreshSkillsStatus"',
        'id="refreshApiStatus"',
    ]:
        assert item in index_html
    for item in [
        "/api/system/settings/skills",
        "/api/system/settings/connections",
        "/api/codex/capabilities",
        "loadSystemAgentPage",
        "loadSystemSkillsPage",
        "loadSystemConnectionsPage",
        "renderSkillsSettings",
        "renderConnectionSettings",
    ]:
        assert item in app_js
    assert ".settings-integration-grid" in styles
    assert ".settings-overview-strip" in styles
    assert "@keyframes shimmer" not in styles
    assert "@keyframes spotlight" not in styles
    assert "@keyframes agent-scan" not in styles
    assert "@keyframes pulse" not in styles
    assert ".agent-panel" not in styles
    assert ".agent-flow" not in styles
    for item in ["CHART_THEME", "#ff4d5e", "#24c875", "#ffd166", "#5ac8fa"]:
        assert item in app_js
    for item in [
        "/api/notifications/send",
        "renderNotificationSendResult",
        "sendNotificationDryRun",
        "state.notificationPreviews",
    ]:
        assert item in app_js

    for item in [
        "/static/vendor/glinui-theme.css",
        "/static/vendor/html2canvas.min.js",
        "/static/vendor/liquidGL.js",
        "/static/liquid-glass-system.css",
    ]:
        assert item in index_html
    assert 'class="design-strip"' not in index_html
    assert 'class="sidebar liquidGL"' not in index_html
    assert 'class="topbar liquidGL"' not in index_html
    assert "/static/vendor/glinui-liquid-glass.js" not in index_html
    assert "window.GlinUILiquidGlass?.attach" not in app_js
    assert 'id="liquidChromeLens"' in index_html
    assert 'id="liquidSidebarLens"' in index_html
    assert 'id="liquidChromeLens" class="liquid-webgl-lens liquid-topbar-lens"' in index_html
    assert 'id="liquidSidebarLens" class="liquid-webgl-lens liquid-sidebar-lens"' in index_html
    assert 'class="liquid-content topbar-content"' in index_html
    assert 'class="liquid-content sidebar-content"' in index_html
    assert "'.liquid-webgl-lens,.glass-control-lens'" in app_js
    assert "glass-control-lens" in app_js
    assert "data-glass-optical-background" in index_html
    assert "target: liquidTargetSelector" in app_js
    assert "snapshot: '.glass-sample-layer'" in app_js
    assert 'class="liquid-backdrop"' in index_html
    assert 'id="glassSampleLayer"' in index_html
    assert "data-liquid-sample-source-root" in index_html
    assert "tilt: false" in app_js
    assert "function bindLiquidPointerTracking(instances, preferences)" in app_js
    assert "dataset.liquidPointerTracking = 'shader-uniforms'" in app_js
    assert "function presentLiquidCanvasInLenses(instances)" in app_js
    assert "dataset.liquidCanvasPresentation = 'background-webgl-canvas'" in app_js
    assert "frost: 0" in app_js
    assert "document.documentElement.dataset.liquidGl = 'webgl-ready'" in app_js
    assert ".side-card,.sidebar,.searchbar" not in app_js
    assert "el.classList.add('content-surface')" in app_js
    assert "el.classList.add('glass-button')" in app_js
    assert ".glin-liquid-button:not(.nav-btn)" in vendor_theme
    for item in [".glass-surface", ".glass-button", ".glass-group", ".glass-navigation", "--glass-aberration-strength", "--glass-dynamic-tilt", "--glass-press-scale", "perspective(620px)", "prefers-reduced-motion", "prefers-reduced-transparency", "forced-colors"]:
        assert item in glass_system
    assert "body > canvas[data-liquid-ignore]{pointer-events:none!important}" in styles
    assert ".app-shell{position:relative;min-height:100vh" in styles
    assert "z-index:40" in styles
    assert "z-index:50" in styles
    assert "transform:translateZ(0)" in styles
    assert ".nav-btn{width:auto;min-width:132px;flex:0 0 auto}" in styles
    assert "window.scrollTo({ top: 0, left: 0, behavior: 'auto' })" in app_js
    for item in [
        "function initOpenSourceGlassUI()",
        "document.documentElement.dataset.glinuiGlass = 'token-bridge'",
        "function configureGlassPreferences()",
        "function initDynamicGlassInteractions(preferences)",
        "function initLiveGlassSampling()",
        "dataset.glassSampling = 'live-document-uv-scroll'",
        "dataset.glassControlRefraction = 'webgl-realtime-uv-sampling'",
        "dataset.glassSampleScrollY",
        "dataset.glassScrollFrame",
        "dataset.glassCanvasSync",
        "data-liquid-canvas-snapshot",
        "body > .app-shell .view.active",
        "glass-sample-app",
        "dataset.glassLensCount",
        "dataset.glassInteractions = 'pointer-elastic'",
        "dataset.glassLifecycle = 'managed'",
        "window.liquidGL({",
        "window.__liquidGLNoWebGL__ = false",
        "document.documentElement.dataset.liquidGl = 'webgl-initializing'",
        "document.documentElement.dataset.liquidGl = 'webgl-ready'",
    ]:
        assert item in app_js
    assert "u_fooonticLens" in vendor_liquid
    assert "const float SAMPLE_RANGE = 4.0" in vendor_liquid
    assert "const float SAMPLE_OFFSET = 0.5" in vendor_liquid
    assert "const float LIGHTING_INTENSITY = 0.3" in vendor_liquid
    assert "market-band" not in index_html
    assert "market-node" not in index_html
    assert "data-market-tile-field" in index_html
    assert "data-liquid-sample-document" in index_html
    assert ".market-tile-field" in styles
    assert ".market-tile" in styles
    assert "updateCanvasRegion" in vendor_liquid
    assert "texSubImage2D" in vendor_liquid
    assert "u_chromaticAberration" in vendor_liquid
    assert "precision highp float" in vendor_liquid
    assert "chromaticAberration: 0.0036" in app_js
    assert "dataset.glassQuality = quality === 1 ? 'low' : quality === 2 ? 'high' : 'ultra'" in app_js
    assert "window.__glassSamplerController?.scheduleCapture?.(20)" in app_js
    assert 'data-view="system"' in index_html
    assert 'id="themePicker"' in index_html
    assert 'id="backdropPicker"' in index_html
    assert 'id="uiLanguage"' in index_html
    assert 'id="uiGlassTransparency"' in index_html
    assert 'id="uiGlassTransparencyValue"' in index_html
    assert 'id="uiFontScale" type="range" min="90" max="115" step="0.1"' in index_html
    assert 'id="uiGlassTransparency" type="range" min="0" max="95" step="0.1"' in index_html
    assert "requestAnimationFrame" in app_js
    assert "--ui-glass-material-opacity" in app_js
    assert "--ui-glass-tint-opacity" not in app_js
    assert 'id="stockEventToggle"' in index_html
    assert 'id="stockEventPanel"' in index_html
    assert 'id="stockEventClose"' not in index_html
    assert '>開啟個股事件</span>' in index_html
    assert 'data-events-open="false"' in index_html
    assert "setStockEventsOpen" in app_js
    assert "關閉個股事件" in app_js
    assert 'id="eventList" class="event-list" data-liquid-preserve tabindex="0" aria-label="個股事件清單"' in index_html
    assert 'id="corporateActionSync"' in index_html
    assert 'id="corporateActionSyncStatus"' in index_html
    assert "function renderCorporateActionCards(targetId, payload)" in app_js
    assert "公司行動台帳" in app_js
    assert "不從價格跳動推測" in app_js
    assert "/api/open-stock-ai/paper-account/corporate-actions/sync" in app_js
    assert "不自動扣款" in app_js
    assert ".corporate-action-card" in styles
    assert ".stock-primary-layout > .stock-event-panel{\n  position:absolute" in styles
    assert "display:flex!important" in styles
    assert "width:clamp(360px,34%,560px)" in styles
    assert "transform:translate3d(54px,-4px,0) scale(.94,.975)" in styles
    assert "0 36px 92px rgba(0,0,0,.58)" in styles
    assert "flex:1 1 0" in styles
    assert "overflow-y:auto" in styles
    assert "-webkit-overflow-scrolling:touch" in styles
    assert "scroll-padding-bottom:max(40px,env(safe-area-inset-bottom))" in styles
    assert "grid-auto-rows:max-content" in styles
    assert ".stock-event-panel > .event-list > .event-list-end" in styles
    assert "height:max(40px,env(safe-area-inset-bottom))" in styles
    assert "height:calc(100% - 126px)" in styles
    assert "max-height:var(--stock-event-panel-viewport-height,calc(100dvh - 250px))" in styles
    assert "function ensureStockEventListEnd(list)" in app_js
    assert "end.className = 'event-list-end'" in app_js
    assert "new MutationObserver(() => ensureStockEventListEnd(list)).observe(list, { childList: true })" in app_js
    assert "function syncStockEventPanelViewport(panel)" in app_js
    assert "window.innerHeight - panelTop - viewportInset" in app_js
    assert "--stock-event-panel-viewport-height" in app_js
    assert "panel.addEventListener('wheel'" in app_js
    assert "event.preventDefault()" in app_js
    assert "event.stopPropagation()" in app_js
    assert "list.scrollTop += delta" in app_js
    assert "{ passive: false }" in app_js
    stock_toggle_js = app_js.split("function setStockEventsOpen(open)", 1)[1].split("function initStockEventPanel", 1)[0]
    assert "renderCurrentChart" not in stock_toggle_js
    assert "scheduleCapture" not in stock_toggle_js
    assert "target?.closest('#stockEventToggle')" in app_js
    for removed_control in ["uiGlassQuality", "uiGlassOpacity", "uiRefraction", "uiMotion", "uiBackdropPattern"]:
        assert f'id="{removed_control}"' not in index_html
    assert "stock-ai-ui-settings-v1" in app_js
    assert "localStorage.setItem(UI_SETTINGS_KEY" in app_js
    assert 'data-ui-theme="graphite"' in styles
    assert 'data-ui-theme="clarity"' in styles
    assert 'data-ui-theme="terminal"' in styles
    assert 'data-ui-theme="aurora"' in styles
    assert 'data-ui-theme="pearl"' in styles
    assert 'data-ui-theme="daylight"' in styles
    assert 'html[data-ui-theme="pearl"] .searchbar' in styles
    assert 'html[data-ui-theme="daylight"] .searchbar button' in styles
    for backdrop in ["constellation", "grid", "waves", "bands", "tiles", "none"]:
        assert f'value="{backdrop}"' in index_html

    for item in ["openStockStatusLabel", "交易訊號", "風控決策", "尚無轉接器結果"]:
        assert item in app_js


def test_static_ui_avoids_ai_agent_branding_in_visible_shell():
    index_html = (ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = frontend_script()

    for item in ["Open Stock AI", ">TradingAgents<", ">AI-Trader<", "智能", "智慧", "Agent 工作區", "開源策略引擎"]:
        assert item not in index_html
    for item in ["智能分析", "'TradingAgents'", "'FinGPT 預測'", "'FinRobot 報告'", "MarketDataHub 資料中樞", "RiskEngine 風控閘門", "PaperExecutor 模擬執行", "策略引擎載入失敗"]:
        assert item not in app_js
    for item in ["市場研判", "外部策略流程", "情緒預測", "研究報告", "技術細節"]:
        assert item in index_html or item in app_js



def test_ui_assets_and_health_identity_are_never_stale():
    main = (ROOT / "src/stock_ai/main.py").read_text(encoding="utf-8")
    index = (ROOT / "src/stock_ai/ui/static/index.html").read_text(encoding="utf-8")
    launcher = (ROOT / "open-stock-ai.sh").read_text(encoding="utf-8")
    assert 'disable_ui_asset_cache' in main
    assert 'no-store, no-cache, must-revalidate, max-age=0' in main
    assert 'build_commit' in main and 'instance_id' in main and 'project_root' in main
    assert 'paper-training.js?v=20260820-t-plus-2-settlement-v1' in index
    assert 'build_commit' in launcher and 'GIT_COMMIT' in launcher
    assert 'instance_id' in launcher and 'PROJECT_INSTANCE_ID' in launcher


def test_data_catalog_exposes_unified_platform_health_quality_and_lineage():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    explorer = (STATIC / "js/features/explorer.js").read_text(encoding="utf-8")

    for element_id in (
        "dataPlatformSummary",
        "dataPlatformDatasets",
        "dataPlatformSources",
        "dataSourceObservability",
        "notifySourceObservability",
        "sourceObservabilityActionResult",
        "dataPlatformLineage",
        "refreshDataPlatform",
        "entityIdentifierInput",
        "resolveEntityIdentifier",
        "entityResolutionResult",
        "entityRegistryStatusChip",
    ):
        assert f'id="{element_id}"' in index
    for endpoint in (
        "/api/data/status",
        "/api/data/sources",
        "/api/data/query",
        "/api/data/revisions/",
        "/api/data/lineage/",
        "/api/data/quality/",
        "/api/data/entity-registry/resolve",
    ):
        assert endpoint in explorer
    assert "sourceObservability" in explorer
    assert "Source Observability" in explorer
    assert "/api/data/observability/notify" in explorer
    assert "評估並通知" in index
    assert "notification_status_counts" in explorer
    assert "已投遞 ${delivered}" in explorer
    assert "每筆結果均保存不可變收據" in explorer
    assert "available_at" in explorer
    assert "acquired_at" in explorer
    assert "payload_hash" in explorer
    assert "Market Warehouse" in explorer
    assert "standardWarehouse" in explorer
    assert "new Set(" in explorer
    assert "完整資料血緣" in index
    assert "原始來源 → revision → 指標 → 結論" in explorer
    assert "缺少原始資料即拒絕寫入" in explorer
