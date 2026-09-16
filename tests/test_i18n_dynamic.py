import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "stock_ai" / "ui" / "static"
I18N = STATIC / "i18n"


def load_json(name: str):
    return json.loads((I18N / name).read_text(encoding="utf-8"))


def test_dynamic_english_catalog_covers_all_workspaces():
    catalogs = [load_json(f"en-dynamic-{index:02d}.json") for index in range(1, 11)]
    merged = {key: value for catalog in catalogs for key, value in catalog.items()}

    assert len(merged) >= 720
    for label in (
        "市場判斷已完成",
        "每日市場研究報告",
        "委託預覽流程",
        "外部策略流程",
        "Agent 執行狀態",
        "MACD(12,26,9) 即時樣本",
        "模擬帳戶",
        "Stock AI Agent 控制台",
        "即時行情源尚未設定。",
    ):
        assert label in merged
        assert merged[label]


def test_dynamic_english_runtime_handles_non_dom_ui():
    bootstrap = (STATIC / "js" / "bootstrap.js").read_text(encoding="utf-8")
    segments = load_json("en-segments.json")
    extra_segments = load_json("en-segments-extra.json")
    controls = load_json("en-controls.json")

    assert len(segments) >= 100
    assert len(extra_segments) >= 200
    assert "paperTrainingObjective" in controls
    assert "agentTradingHomePrompt" in controls
    assert "CanvasRenderingContext2D.prototype.fillText" in bootstrap
    assert "window.confirm =" in bootstrap
    assert "translateUiTree = function stockAiTranslateUiTree" in bootstrap
    assert "translateProtectedUiChrome" in bootstrap
    assert "startDynamicChromeObserver" in bootstrap
    assert "stockAiFindUntranslatedUi" in bootstrap
    assert "#newsCenterBox,#overviewNewsBox,#eventList" in bootstrap
    assert "cache: 'no-store'" in bootstrap
    assert "length: 10" in bootstrap
    assert "en-segments-extra.json" in bootstrap
    assert "en-dynamic-" in bootstrap
