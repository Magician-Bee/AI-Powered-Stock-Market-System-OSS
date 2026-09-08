from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _luminance(value: str) -> float:
    rgb = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    channels = [item / 12.92 if item <= 0.04045 else ((item + 0.055) / 1.055) ** 2.4 for item in rgb]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_light_themes_use_soft_blue_gray_readability_tokens():
    css = (ROOT / "src/stock_ai/ui/static/liquid-glass-system.css").read_text(encoding="utf-8")

    assert "Light theme readability layer v2" in css
    assert '--light-text-strong:#233747' in css
    assert '--light-text-body:#3f5566' in css
    assert '--light-text-muted:#53697a' in css
    assert '#000' not in css.split("/* Light theme readability layer v2 */", 1)[1]
    assert _contrast("#233747", "#e6edf2") >= 7.0
    assert _contrast("#3f5566", "#e6edf2") >= 4.5
    assert _contrast("#53697a", "#e6edf2") >= 4.5


def test_light_theme_overrides_cover_dense_secondary_text():
    css = (ROOT / "src/stock_ai/ui/static/liquid-glass-system.css").read_text(encoding="utf-8")

    for selector in [
        ".panel-copy",
        ".settings-status-row small",
        ".process-step small",
        ".nav-section-label",
        "input,textarea",
        ".tag.positive",
        ".tag.negative",
    ]:
        assert selector in css
