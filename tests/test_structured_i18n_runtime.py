import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "stock_ai" / "ui" / "static"
RUNTIME = STATIC / "js" / "core" / "dom-state.js"
EXPLORER = STATIC / "js" / "features" / "explorer.js"
EXACT = STATIC / "i18n" / "en-dynamic-10.json"
SEGMENTS = STATIC / "i18n" / "en-segments.json"


def runtime_source() -> str:
    return RUNTIME.read_text(encoding="utf-8") + EXPLORER.read_text(encoding="utf-8")


def test_generated_workspaces_use_structured_english_renderers():
    source = runtime_source()

    required = (
        "installStockAiStructuredLocalization",
        "runScreenerStructured",
        "renderNewsCenterStructured",
        "renderTradingWorkspaceStructured",
        "renderRiskWorkspaceStructured",
        "renderAssetWorkspaceStructured",
        "renderRequirementContractStructured",
        "renderSourcePolicyStructured",
        "renderUpdateRunnerStructured",
        "renderAssistantWorkspaceStructured",
        "renderFundamentalsBoxStructured",
        "renderMonitorSignalsStructured",
        "riskCopy",
        "safetyRuleEnglish",
        "stockAiFindMixedLanguageUi",
    )
    for marker in required:
        assert marker in source, marker


def test_screenshot_reported_mixed_language_failures_are_not_emitted():
    source = runtime_source()

    forbidden = (
        "列入Buy Candidate",
        "下一交易日Close後Reassess",
        "Research, Risk ControlPassed",
        "保持Watch, 不送出Order",
        "紙上交易Mode",
        "Available Cash不足",
        "單筆Position極限",
        "Until a brokerage API is connected不得視為可正式送單",
        "Scope: 全Market",
    )
    for phrase in forbidden:
        assert phrase not in source, phrase


def test_stock_names_and_raw_news_are_preserved_as_source_content():
    source = runtime_source()

    assert "data-i18n-skip>${stockAiEscape(item.title)}" in source
    assert "data-i18n-skip>${renderNewsSummaryLink" in source
    assert "${stockAiEscape(item.name)}" in source
    assert "${stockAiEscape(workspace.preview.name)}" in source


def test_screenshot_workspaces_have_complete_english_sentences():
    source = runtime_source()

    expected = (
        "Realtime-source change is",
        "Only fields present in the realtime payload are displayed",
        "Only trade preview, risk checks, and the portfolio framework are available",
        "This result comes from the central RiskEngine",
        "Automatic ordering is prohibited without a brokerage API",
        "The system may not place orders by simulating mouse input",
    )
    for phrase in expected:
        assert phrase in source, phrase


def test_residual_catalogs_are_valid_and_cover_settings_quant_and_learning_log():
    exact = json.loads(EXACT.read_text(encoding="utf-8"))
    segments = json.loads(SEGMENTS.read_text(encoding="utf-8"))

    exact_expectations = {
        "Codex App Server · 已在本機設定": "Codex App Server · configured locally",
        "OpenAPI · 公開資料連線，不需要 API key。": "OpenAPI · Public-data connection; no API key is required.",
        "回合摘要": "Episode Summary",
        "計算本回合績效": "Calculate Episode Performance",
    }
    for source, translation in exact_expectations.items():
        assert exact[source] == translation

    segment_expectations = {
        "橋接就緒": "Bridge ready",
        "累積績效": "Cumulative performance",
        "契約": "Contract",
        "本地 UI": "Local UI",
        "反思回放": "Reflection Replay",
        "投組歸因": "Portfolio Attribution",
        "執行期連接器": "Runtime Connectors",
        "模擬交易帳本": "Paper Trading Ledger",
        "訊號": "Signal",
        "風險": "Risk",
    }
    for source, translation in segment_expectations.items():
        assert segments[source] == translation


def test_linkage_analysis_builds_complete_english_instead_of_translating_backend_chinese():
    source = EXPLORER.read_text(encoding="utf-8")

    required = (
        "function linkageEnglishContent",
        "<strong>Direction:</strong> ",
        "<strong>Uncalibrated rule score:</strong> ",
        "<strong>Evidence:</strong> ",
        "The PHLX Semiconductor Index reflects global semiconductor risk appetite.",
        "Realtime quantitative validation is not yet complete",
        "U.S. Treasury yields affect discount rates",
    )
    for marker in required:
        assert marker in source, marker

    assert "<strong>Direction:</strong>${" not in source
    assert "<strong>Confidence:</strong>${" not in source
