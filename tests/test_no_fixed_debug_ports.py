from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_sources_do_not_post_to_fixed_debug_ports():
    paths = [
        ROOT / "src/stock_ai/services.py",
        ROOT / "src/stock_ai/mvp_features.py",
        ROOT / "src/stock_ai/phase1_data.py",
        *sorted((ROOT / "src/stock_ai/ui/static/js").rglob("*.js")),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "127.0.0.1:7777" not in combined
    assert "127.0.0.1:7778" not in combined
    assert "fetch(DEBUG_SERVER_URL" not in combined
    assert "fetch(MARKET_SUMMARY_DEBUG_SERVER_URL" not in combined
    assert "Request(DEBUG_SERVER_URL" not in combined
    assert "Request(WATCHLIST_DEBUG_SERVER_URL" not in combined
    assert "Request(DAILY_DEBUG_SERVER_URL" not in combined
