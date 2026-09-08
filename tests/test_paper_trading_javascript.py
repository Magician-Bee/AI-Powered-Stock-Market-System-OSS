from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_paper_trading_javascript_parses() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = ROOT / "src" / "stock_ai" / "ui" / "static" / "paper-training.js"
    result = subprocess.run(
        [node, "--check", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

def test_paper_navigation_has_no_special_label_box_and_reset_is_race_safe() -> None:
    script = (ROOT / "src" / "stock_ai" / "ui" / "static" / "paper-training.js").read_text(encoding="utf-8")
    assert "模擬交易" in script
    assert '<span class="paper-nav-label">模擬交易</span>' not in script
    assert "accountRequestVersion" in script
    assert "resetInFlight" in script
    assert "requestVersion !== accountRequestVersion" in script
    assert "總資產" in script
    assert "SQLite 驗證失敗" in script
    assert "成本證據" in script
    assert "本機紙上設定" in script
