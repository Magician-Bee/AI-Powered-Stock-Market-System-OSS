from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "macos" / "StockAILiquidGlass" / "main.swift"
LOCALIZATION_PATCHER = ROOT / "macos" / "StockAILiquidGlass" / "patch-localization.py"
BUILD_SCRIPT = ROOT / "build-macos-liquid-glass.sh"


def apply_localization_patch(tmp_path: Path) -> str:
    localized_output = tmp_path / "main.localized.swift"
    subprocess.run(
        [sys.executable, str(LOCALIZATION_PATCHER), str(SOURCE), str(localized_output)],
        check=True,
        capture_output=True,
        text=True,
    )
    return localized_output.read_text(encoding="utf-8")


def test_native_toolbar_localization_patch(tmp_path: Path):
    patched = apply_localization_patch(tmp_path)

    assert "func applyInterfaceStrings(" in patched
    assert "string: toolbarPlaceholderText" in patched
    assert "string: toolbarSendText" in patched
    assert 'body["searchPlaceholder"]' in patched
    assert 'body["searchButton"]' in patched
    assert "searchAccessibility" in patched
    assert "accountAccessibility" in patched
    assert "document.documentElement.lang || 'zh-Hant'" in patched
    assert "attributeFilter:['data-ui-theme','data-chart-focus','lang']" in patched


def test_native_sidebar_selection_uses_one_official_appkit_glass_view():
    source = SOURCE.read_text(encoding="utf-8")

    required = (
        "private final class SidebarSelectionOverlayView: NSView",
        "private let glassView: NSGlassEffectView",
        "private let selectionContent = SidebarSelectionContentView(frame: .zero)",
        "contentView: selectionContent",
        "style: .clear",
        "tintAlpha: 0",
        "glassView.tintColor = nil",
        "glassView.animator().frame = snapshot.frame",
        "context.duration = 0.42",
        "context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)",
        "let sidebarSelectionOverlay = SidebarSelectionOverlayView(frame: .zero)",
        'body["kind"] as? String) == "navigationSelection"',
        "func updateSidebarSelection(",
        "scheduleNavigationSelection",
        "MutationObserver(scheduleNavigationSelection)",
    )
    for marker in required:
        assert marker in source, marker

    assert "import SwiftUI" not in source
    assert "GlassEffectContainer(" not in source
    assert "transitionTask" not in source
    assert "stepCount" not in source
    assert "spacing: max(proxy.size.width, proxy.size.height)" not in source
    assert ".glassEffect(.regular.interactive(), in: .capsule)" not in source
    assert ".glassEffectID(" not in source
    assert ".glassEffectTransition(" not in source
    assert ".id(selection.viewID)" not in source
    assert "sidebarSelectionTrailGlass" not in source
    assert "launchFrame" not in source


def test_native_sidebar_keeps_active_content_in_one_glass_layer():
    source = SOURCE.read_text(encoding="utf-8")

    assert "private let codeLabel = NSTextField(labelWithString:" in source
    assert "private let titleLabel = NSTextField(labelWithString:" in source
    assert "selectionContent.apply(snapshot)" in source
    assert 'html[data-native-liquid-glass="appkit"] .nav-btn,' in source
    assert "background:transparent!important" in source
    assert "backdrop-filter:none!important" in source
    assert "color:transparent!important" in source
    assert "opacity:0!important" in source
    assert "body .sidebar .nav-menu.stock-nav-switcher::after" in source
    assert "content:none!important" in source
    assert "animation:none!important" in source


def test_sidebar_reporter_escapes_javascript_regex_for_swift():
    source = SOURCE.read_text(encoding="utf-8")

    assert r".replace(/\\s+/g,' ')" in source
    assert r".replace(/\s+/g,' ')" not in source


def test_native_build_uses_only_localization_patch_before_compilation():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "official-appkit-single-glass-v8" in script
    assert "patch-localization.py" in script
    assert "patch-sidebar-selection" not in script
    assert 'PATCHED_SOURCE="${BUILD_DIR}/main.localized.swift"' in script
    assert 'python3 "$LOCALIZATION_PATCHER" "$SOURCE" "$PATCHED_SOURCE"' in script
    assert '"$PATCHED_SOURCE"' in script
