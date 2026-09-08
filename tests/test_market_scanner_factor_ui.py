from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_market_decision_cards_disclose_scanner_factor_values_and_pit_status() -> None:
    script = (
        ROOT / "src" / "stock_ai" / "ui" / "static" / "js" / "features" / "market-intelligence" / "decision-board.js"
    ).read_text(encoding="utf-8")
    styles = (
        ROOT / "src" / "stock_ai" / "ui" / "static" / "css" / "features" / "market-workspace.css"
    ).read_text(encoding="utf-8")

    assert "function factorEvidence(item)" in script
    assert "historical_pit_eligible === true ? 'PIT'" in script
    assert "decision-card-factors" in script
    assert "decision-factor" in styles
