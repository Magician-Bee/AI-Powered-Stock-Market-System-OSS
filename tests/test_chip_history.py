from pathlib import Path
from time import sleep

from stock_ai.chip_history import build_chip_history, query_chip_history


def _flow(day, foreign, trust, dealer):
    return {
        "trade_date": day, "symbol": "2330.TW", "name": "台積電",
        "foreign_net": foreign, "trust_net": trust, "dealer_net": dealer,
        "total_institutional_net": foreign + trust + dealer,
    }


def _margin(day, previous, current, short_previous, short, offsetting):
    return {
        "trade_date": day, "symbol": "2330.TW", "name": "台積電",
        "margin_previous_balance": previous, "margin_balance": current,
        "margin_limit": 100_000, "short_previous_balance": short_previous,
        "short_balance": short, "short_limit": 50_000, "offsetting": offsetting,
    }


def test_chip_history_keeps_investors_separate_and_calculates_trends():
    payload = build_chip_history(
        symbol="2330.TW",
        institutional_items=[
            _flow("2026-07-24", 100, -10, 5),
            _flow("2026-07-25", 200, 20, -5),
            _flow("2026-07-28", 300, 30, 10),
        ],
        margin_items=[
            _margin("2026-07-24", 1000, 1100, 100, 120, 2),
            _margin("2026-07-28", 1100, 1050, 120, 105, 3),
        ],
    )
    # Institutional and margin data alone remain analytically useful, but P0
    # PIT coverage requires borrowed-short and TDCC streams before a history
    # can be represented as complete production evidence.
    assert payload["status"] == "partial"
    assert payload["institutional"]["total_streak"] == {"direction": "buy", "days": 3}
    assert payload["institutional"]["items"][-1]["cumulative"]["foreign_net"] == 600
    latest = payload["margin"]["items"][-1]
    assert latest["margin_change"] == -50
    assert latest["short_change"] == -15
    assert latest["short_to_margin_percent"] == 10
    assert payload["truthfulness"]["calendar_gaps_not_filled_with_zero"] is True


def test_chip_query_uses_stored_history_without_refresh():
    payload = query_chip_history(
        "2330.TW",
        refresh=False,
        flow_loader=lambda **_kwargs: [_flow("2026-07-28", 100, 20, 5)],
        margin_loader=lambda **_kwargs: [_margin("2026-07-28", 1000, 1100, 100, 120, 2)],
    )
    assert payload["institutional"]["count"] == 1
    assert payload["margin"]["count"] == 1


def test_chip_query_includes_stored_borrowed_and_tdcc_companions_in_one_receipt():
    acquired_at = "2026-07-28T08:00:00+00:00"
    payload = query_chip_history(
        "2330.TW",
        refresh=False,
        flow_loader=lambda **_kwargs: [{
            **_flow("2026-07-28", 100, 20, 5),
            "source_id": "twse_openapi",
            "acquired_at": acquired_at,
        }],
        margin_loader=lambda **_kwargs: [{
            **_margin("2026-07-28", 1000, 1100, 100, 120, 2),
            "source_id": "twse_openapi",
            "acquired_at": acquired_at,
        }],
        borrowed_short_loader=lambda **_kwargs: [{
            "symbol": "2330.TW",
            "trade_date": "2026-07-28",
            "source_id": "twse_official_web",
            "acquired_at": acquired_at,
        }],
        tdcc_loader=lambda **_kwargs: [{
            "symbol": "2330.TW",
            "report_date": "2026-07-25",
            "source_id": "tdcc",
            "published_at": acquired_at,
            "acquired_at": acquired_at,
        }],
    )

    assert payload["status"] == "complete"
    assert payload["companion_streams"]["borrowed_short"]["count"] == 1
    assert payload["companion_streams"]["tdcc_holding_distribution"]["count"] == 1
    assert payload["pit_coverage"]["exact_replay_eligible"] is False
    tdcc_coverage = payload["pit_coverage"]["streams"]["tdcc_holding_distribution"]
    assert tdcc_coverage["source_retention_days"] == [365]
    assert tdcc_coverage["historical_pit_eligible"] is False


def test_chip_refresh_respects_budget_and_reports_missing_official_streams():
    def delayed_empty(**_kwargs):
        sleep(0.05)
        return []

    payload = query_chip_history(
        "2330.TW",
        days=1,
        refresh=True,
        refresh_budget_seconds=0,
        flow_loader=delayed_empty,
        margin_loader=delayed_empty,
    )

    assert payload["status"] == "partial"
    assert payload["refresh"]["budget_seconds"] == 0
    assert payload["refresh"]["pending_dates"]
    assert payload["refresh"]["zero_fill_used"] is False
    assert payload["pit_coverage"]["exact_replay_eligible"] is False


def test_chip_history_ui_exposes_official_history_controls():
    root = Path(__file__).resolve().parents[1] / "src/stock_ai"
    markup = (root / "ui/static/index.html").read_text(encoding="utf-8")
    script = (root / "ui/static/js/features/dashboard.js").read_text(encoding="utf-8")
    assert 'id="loadChipHistoryBtn"' in markup
    assert "連續買賣超" in markup
    assert "券資比" in markup
    assert "renderChipHistory" in script
    assert "PIT 覆蓋未認證" in script
    assert "正在先保存官方借券與 TDCC 快照" in script
    assert "companion_streams" in script
    assert "PIT 歷史未認證" in script
    assert "不會以空白範圍回補官方籌碼資料" in script
