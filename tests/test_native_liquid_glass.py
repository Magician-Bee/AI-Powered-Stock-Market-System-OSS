from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "macos" / "StockAILiquidGlass" / "main.swift"
BUILDER = ROOT / "build-macos-liquid-glass.sh"
ARCHIVE = ROOT / "macos" / "StockAILiquidGlass.app.zip"
WEB_STYLES = ROOT / "src" / "stock_ai" / "ui" / "static" / "css"


def web_styles() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(WEB_STYLES.rglob("*.css"))
    )


def test_native_shell_uses_documented_apple_liquid_glass_runtime_classes():
    source = SOURCE.read_text(encoding="utf-8")

    assert "import SwiftUI" not in source
    assert "private final class SidebarSelectionOverlayView: NSView" in source
    assert "style: .clear" in source
    assert "tintAlpha: 0" in source
    assert "glassView.tintColor = nil" in source
    assert "glassView.animator().frame = snapshot.frame" in source
    assert "GlassEffectContainer(" not in source
    assert ".glassEffect(" not in source
    assert ".glassEffectID(" not in source
    assert ".glassEffectTransition(" not in source
    assert "let glassView = NSGlassEffectView(frame: .zero)" in source
    assert "let container = NSGlassEffectContainerView(frame: .zero)" in source
    assert "glassView.contentView = content" in source
    assert "glassView.cornerRadius = cornerRadius" in source
    assert "glassView.tintColor = tintColor.withAlphaComponent(tintAlpha)" in source
    assert "glassView.style = style" in source
    assert "container.contentView = content" in source
    assert "container.spacing = spacing" in source
    assert "style: NSGlassEffectView.Style = .clear" in source
    assert "NSClassFromString" not in source
    assert "NSSelectorFromString" not in source
    assert "setValue(" not in source
    assert "AppleGlassMaterialVariant" not in source
    assert "applyPrivateVariant" not in source
    assert "backdrop-filter" in source


def test_native_shell_preserves_original_web_layout_and_tracks_it_with_native_glass():
    source = SOURCE.read_text(encoding="utf-8")

    assert "WKWebView" in source
    assert "glassLayout" in source
    assert "getBoundingClientRect()" in source
    assert "new ResizeObserver(scheduleLayout)" in source
    assert "window.addEventListener('resize'" in source
    assert ".sidebar,.topbar,.glass-navigation" in source
    assert "dataset.nativeLiquidGlass = 'appkit'" in source
    assert '.nav-menu.stock-nav-switcher::after' in source
    assert ".liquid-backdrop{background-color:rgba(1,4,7,.28)!important}" not in source
    assert ".main{margin-left:292px" not in source
    assert ".sidebar,.topbar,.liquid-native-canvas" not in source
    assert "window.minSize = NSSize(width: 680, height: 560)" in source
    assert "window.isOpaque = true" in source
    assert "window.backgroundColor = .black" in source
    assert "root.layer?.backgroundColor = NSColor.black.cgColor" in source
    assert "AdaptiveToolbarContentView" in source
    assert ".topbar-content{visibility:hidden!important}" in source
    assert "toolbarOverlay.addSubview(toolbarContent)" in source
    assert "ToolbarOverlayView" in source
    assert "interactiveFrame.contains(point)" in source
    assert "toolbarContent.autoresizingMask = [.width, .height]" in source
    assert "toolbarContent.frame = glassView.frame" in source
    assert "private var glassViewportSize: NSSize = .zero" in source
    assert 'let chartFocus = (body["chartFocus"] as? Bool) ?? false' in source
    assert "from: chartFocus ? nil : body[\"toolbar\"] as? [String: Any]" in source
    assert "toolbarContent?.isHidden = true" in source
    assert "toolbarContent.isHidden = false" in source
    assert "toolbarOverlay?.interactiveFrame = .zero" in source
    assert "chartFocus:document.documentElement.dataset.chartFocus === 'true'" in source
    assert "attributeFilter:['data-ui-theme','data-chart-focus']" in source


def test_native_shell_exposes_standard_edit_commands_to_the_webview_responder_chain():
    source = SOURCE.read_text(encoding="utf-8")

    assert "private func installMainMenu()" in source
    assert '#selector(NSText.copy(_:))' in source
    assert '#selector(NSText.paste(_:))' in source
    assert '#selector(NSText.selectAll(_:))' in source
    assert "item.target = nil" in source
    assert "installMainMenu()" in source


def test_original_responsive_breakpoints_remain_the_layout_authority():
    source = SOURCE.read_text(encoding="utf-8")
    styles = web_styles()

    assert "@media(max-width:1060px)" in styles
    assert "@media(max-width:760px)" in styles
    assert ".main{margin-left:0;padding:24px}" in styles
    assert "scroll-snap-type:x proximity" in styles
    assert "@media(max-width:1060px){.main" not in source


def test_layout_spacing_and_narrow_screener_rules_prevent_stuck_or_overflowing_cards():
    styles = web_styles()

    assert "margin-top:18px!important" in styles
    assert ".decision-plan-list{grid-template-columns:1fr" in styles
    assert "#screenerTable .row{grid-template-columns:1fr" in styles
    assert "#screenerTable .row.header{display:none}" in styles
    assert 'content:"可見指標規則"' in styles
    assert "Pale tinted glass" in styles
    assert ".summary-cards .mini-card:nth-child(4n+1)" in styles
    assert ".process-step.content-surface.warn" in styles
    assert "0 18px 40px rgba(0,0,0,.30)" in styles


def test_native_toolbar_and_tinted_glass_keep_the_requested_light_material():
    source = SOURCE.read_text(encoding="utf-8")

    assert "tintAlpha: 0.035" in source
    assert "toolbarGlass.alphaValue = 1.0" in source
    assert "private var appliedChromeTheme: String?" in source
    assert "updateChromeGlassTheme(theme: theme)" in source
    assert "guard let glass = toolbarGlass else { return }" in source
    assert "[toolbarGlass, sidebarGlass]" not in source
    assert "glass.alphaValue = 1.0" in source
    assert "glass.style = .clear" in source
    assert "glass.tintColor = nil" in source
    assert "let controlHeight: CGFloat = 44" in source
    assert "let searchWidth = min(600, max(340, availableSearchWidth))" in source
    assert "private var searchFieldGlass: NSGlassEffectView?" in source
    assert "private var searchButtonGlass: NSGlassEffectView?" in source
    assert "private var accountButtonGlass: NSGlassEffectView?" in source
    assert "private let searchFieldHost = SearchFieldHostView(frame: .zero)" in source
    assert "private let searchButtonHost = NSView(frame: .zero)" in source
    assert "private let accountButtonHost = NSView(frame: .zero)" in source
    assert "ToolbarSearchField" in source
    assert "SearchFieldHostView" in source
    assert 'NSImage(systemSymbolName: "sparkles"' in source
    assert 'searchButton = NSButton(title: "傳送"' in source
    assert "document.getElementById('globalAgentPrompt')" in source
    assert "document.getElementById('globalAgentSend')?.click()" in source
    assert 'self?.toolbarContent?.searchField.stringValue = ""' in source
    assert "guard error == nil, (result as? Bool) == true" in source
    assert "document.querySelector('.agent-runtime-panel')?.scrollIntoView" in source
    assert "searchField.isEditable = true" in source
    assert "window?.makeFirstResponder(searchField)" in source
    assert "let textHeight: CGFloat = 28" in source
    assert "y: bounds.midY - textHeight / 2 - 1" in source
    assert "layoutControl(control, in: host.bounds" in source
    assert "configureControlGlass(searchFieldGlass, depth: .recessed)" in source
    assert "configureControlGlass(searchButtonGlass, depth: .raised)" in source
    assert 'materialLayer.name = "control-glass-depth"' not in source
    assert "CAGradientLayer" not in source
    assert 'let light = theme == "pearl" || theme == "daylight"' in source
    assert "const theme = document.documentElement.dataset.uiTheme || 'exchange'" in source
    assert "attributeFilter:['data-ui-theme','data-chart-focus']" in source
    assert 'let theme = (body["theme"] as? String) ?? "exchange"' in source
    assert 'operatorLabel:document.getElementById(\'accountLabel\')' in source
    assert 'toolbarContent?.applyOperatorLabel' in source
    assert "toolbarContent?.applyInterfaceTheme(theme)" in source
    assert "y: controlsY" in source
    assert "searchButton.isBordered = false" in source
    assert "accountButton.isBordered = false" in source
    assert "tintAlpha: 0.32" in source
    assert "tintAlpha: 0.36" in source
    assert "tintAlpha: 0.34" in source
    assert 'searchButton.keyEquivalent = "\\r"' not in source
    assert ".buy-lane{" not in source
    assert ".summary-cards .mini-card:nth-child(4n+1)" not in source
    assert ".process-step.content-surface.warn" not in source
    assert ".nav-btn:nth-of-type(5n+2).active" not in source
    assert "freefrontend-liquid-glass.css" in (ROOT / "src/stock_ai/ui/static/index.html").read_text(encoding="utf-8")
    assert "const usesTransparentLightChrome = theme === 'pearl' || theme === 'daylight'" in source
    assert "topbar.style.setProperty('background', 'transparent', 'important')" in source
    assert "topbar.style.setProperty('box-shadow', 'none', 'important')" in source


def test_builder_creates_a_real_macos_application_bundle():
    builder = BUILDER.read_text(encoding="utf-8")

    assert "/Library/Developer/CommandLineTools/usr/bin/swiftc" in builder
    assert "/Library/Developer/CommandLineTools/SDKs/MacOSX26.0.sdk" in builder
    assert "arm64-apple-macos26.0" in builder
    assert "x86_64-apple-macos26.0" in builder
    assert "/usr/bin/lipo -create" in builder
    assert "-framework AppKit" in builder
    assert "-framework SwiftUI" not in builder
    assert "-framework WebKit" in builder
    assert "CFBundlePackageType -string APPL" in builder
    assert "LSMinimumSystemVersion -string 26.0" in builder
    assert "codesign --force --deep --sign -" in builder
    assert ARCHIVE.is_file()
    with zipfile.ZipFile(ARCHIVE) as archive:
        names = archive.namelist()
        assert "StockAILiquidGlass.app/Contents/Info.plist" in names
        assert "StockAILiquidGlass.app/Contents/MacOS/StockAILiquidGlass" in names
        assert not any(
            part == "__MACOSX" or part.startswith("._")
            for name in names
            for part in Path(name).parts
        )
