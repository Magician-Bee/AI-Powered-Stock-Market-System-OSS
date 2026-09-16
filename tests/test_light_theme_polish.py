from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "src" / "stock_ai" / "ui" / "static" / "ui-fixes-v2.css"
INDEX = ROOT / "src" / "stock_ai" / "ui" / "static" / "index.html"
AGENT_DOCK_CSS = ROOT / "src" / "stock_ai" / "ui" / "static" / "css" / "features" / "agent-dock.css"
MARKET_WORKSPACE_CSS = ROOT / "src" / "stock_ai" / "ui" / "static" / "css" / "features" / "market-workspace.css"


def css_source() -> str:
    return CSS.read_text(encoding="utf-8")


def test_light_theme_has_coherent_surface_tokens():
    source = css_source()

    required = (
        "Light-theme coherence pass v3",
        "--light-surface-panel",
        "--light-surface-nested",
        "--light-surface-control",
        "--light-surface-border",
        "--light-shadow",
        "--surface:rgba(247,251,255,.92)",
        "--chart-canvas-surface",
        ".workspace-tabs",
        ".home-chart-canvas-wrap",
        ".home-chart-decision-panel",
        ".agent-runtime-panel",
        ".agent-settings-toolbar",
        ".agent-task-pane",
        ".agent-activity-pane",
        ".agent-trading-home-panel",
        ".paper-trading-view .paper-training-panel",
        ".paper-trading-view .paper-broker-card",
        ".view#openstock>.panel",
        ".view#catalog>.panel",
    )
    for marker in required:
        assert marker in source, marker


def test_home_chart_uses_shared_theme_instead_of_hard_coded_dark_palette():
    source = (ROOT / "src/stock_ai/ui/static/js/features/market-intelligence/workspace-home.js").read_text(encoding="utf-8")
    preferences = (ROOT / "src/stock_ai/ui/static/js/core/preferences.js").read_text(encoding="utf-8")
    workspaces = (ROOT / "src/stock_ai/ui/static/css/features/workspaces.css").read_text(encoding="utf-8")

    assert "syncChartThemeFromDom(target)" in source
    assert "background: CHART_THEME.background" in source
    assert "panel: CHART_THEME.panel" in source
    assert "attributeFilter: ['data-ui-theme']" in source
    assert "window.HomeMarketWorkspace?.syncChartPreview?.()" in preferences
    assert "background: '#08111f'" not in source
    assert "#homePriceChart{height:390px;border:0;border-radius:14px;background:var(--chart-canvas-surface" in workspaces


def test_light_theme_controls_do_not_leave_graphite_islands():
    source = css_source()

    assert ".technical-details>summary" in source
    assert ".panel-head>.chip" in source
    assert ".paper-broker-head>.chip" in source
    assert "#codexRuntimeChip" in source
    assert "#agentRuntimeChip" in source
    assert "#agentTradingHomeCodex" in source
    assert "Light controls remain part of the same workspace" in source
    assert "color:var(--light-ink-900)!important" in source
    assert "background:linear-gradient(145deg,rgba(255,255,255,.66),rgba(217,235,246,.38))!important" in source


def test_stock_chart_keeps_the_original_financial_palette():
    source = css_source()
    chart_theme = (ROOT / "src/stock_ai/ui/static/js/core/dom-state.js").read_text(encoding="utf-8")
    chart = (ROOT / "src/stock_ai/ui/static/js/features/market-chart.js").read_text(encoding="utf-8")
    interactions = (ROOT / "src/stock_ai/ui/static/js/features/market-chart-interactions.js").read_text(encoding="utf-8")

    assert "canvas,#priceChart,#realtimeChart" not in source
    assert "background: 'rgba(255,255,255,0.025)'" in chart_theme
    assert "['成交/中價','#fff']" in chart
    assert "const lightTheme = ['pearl', 'daylight']" not in interactions
    assert "ctx.fillStyle = '#f6f8fb'" in interactions


def test_stock_chart_line_geometry_matches_the_original_view():
    chart_theme = (ROOT / "src/stock_ai/ui/static/js/core/dom-state.js").read_text(encoding="utf-8")

    for marker in (
        "grid: 'rgba(34,50,61,0.23)'",
        "text: '#101923'",
        "up: '#f01844'",
        "down: '#00a95f'",
        "ma10: '#007ec7'",
        "boll: '#009486'",
    ):
        assert marker in chart_theme, marker

    chart = (ROOT / "src/stock_ai/ui/static/js/features/market-chart.js").read_text(encoding="utf-8")
    assert "CHART_THEME.ma5, 1.7" in chart
    assert "CHART_THEME.ma60, 1.4" in chart
    assert "ctx.lineWidth = 1.2" in chart


def test_home_candidate_cards_use_readable_full_width_metadata_rows():
    source = MARKET_WORKSPACE_CSS.read_text(encoding="utf-8")

    assert "grid-template-columns:minmax(250px,280px) minmax(0,1fr)" in source
    assert "grid-template-columns:minmax(112px,1fr) minmax(72px,auto)" in source
    assert ".market-navigation-row strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px" in source
    assert ".market-navigation-row>span:first-child small{display:-webkit-box;min-height:2.7em" in source
    assert ".market-navigation-row>.market-navigation-meta{grid-column:1/-1;display:flex" in source
    assert ".market-navigation-row>small{grid-column:1/-1;text-align:left}" in source


def test_home_market_navigation_bounds_initial_dom_and_discloses_progress():
    script = (ROOT / "src/stock_ai/ui/static/js/features/market-intelligence/market-navigation.js").read_text(encoding="utf-8")
    page = (ROOT / "src/stock_ai/ui/static/index.html").read_text(encoding="utf-8")

    assert "const PAGE_SIZE = 40" in script
    assert "items.slice(0, visibleLimit)" in script
    assert "visibleLimit += PAGE_SIZE" in script
    assert "顯示 ${visibleCount.toLocaleString()} / ${totalCount.toLocaleString()} 檔" in script
    assert 'id="marketNavigationProgress"' in page
    assert 'id="marketNavigationMore"' in page


def test_agent_dock_header_and_topbar_adapt_to_dock_width():
    source = AGENT_DOCK_CSS.read_text(encoding="utf-8")

    assert ".agent-dock-header-actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))" in source
    assert "html.agent-dock-open :is(.topbar,.liquid-topbar-lens){right:calc(var(--agent-dock-width) + 48px)}" in source
    assert "html.agent-dock-open .topbar-content{display:grid;grid-template-columns:minmax(0,1fr) auto" in source
    assert "html.agent-dock-open .topbar .title-stack{display:none}" in source


def test_light_agent_artifacts_do_not_use_dark_radar_or_code_blocks():
    source = css_source()

    for marker in (
        ".agent-operation-disclosure pre",
        ".agent-automation-technical pre",
        ".agent-schema-visualization",
        ".agent-schema-radar-shape",
        "rgba(247,251,255,.96)",
    ):
        assert marker in source


def test_news_controls_have_consistent_geometry_and_visible_counts():
    source = css_source()

    required = (
        "body :is(#newsScopeBar,#newsFilterBar) .filter-chip",
        "min-height:48px!important",
        "border-radius:999px!important",
        "body #newsFilterBar .filter-chip>span",
        "min-width:24px!important",
        "color:#245f87!important",
        "background:rgba(77,155,208,.14)!important",
        "body #newsMetaBar .meta-pill",
    )
    for marker in required:
        assert marker in source, marker


def test_light_theme_fix_stylesheet_is_last_project_css_layer():
    source = INDEX.read_text(encoding="utf-8")
    links = [line.strip() for line in source.splitlines() if '<link rel="stylesheet"' in line]

    assert links
    assert "/static/ui-fixes-v2.css" in links[-1]


def test_light_agent_provider_panels_keep_readable_labels_and_legends():
    source = css_source()

    assert ".agent-provider-panel legend" in source
    assert ".agent-provider-panel :is(label,small,.agent-inline-check)" in source
    assert ".agent-provider-panel input::placeholder" in source
    assert "color:var(--light-ink-950)!important" in source
