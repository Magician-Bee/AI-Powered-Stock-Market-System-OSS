from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from stock_ai.main import app
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.storage.sqlite_store import SQLiteStore
from open_stock_ai.storage.trade_store import TradeStore


OFFICIAL_URL = "https://www.twse.com.tw/zh/announcement/index.html"


def _buy(oms: PaperOMS, symbol: str) -> None:
    result = oms.submit_and_fill(
        {
            "order_id": f"BUY-{symbol}",
            "symbol": symbol,
            "market": "TW",
            "action": "buy",
            "entry_price": 100,
            "position_size_pct": 1,
            "risk_approved": True,
        }
    )
    assert result["filled"] is True


def _action(symbol: str, action_type: str, **terms) -> dict:
    return {
        "source_event_id": f"{symbol}-{action_type}",
        "symbol": symbol,
        "market": "TW",
        "effective_date": "2026-01-15",
        "action_type": action_type,
        "status": "confirmed",
        "official_verified": True,
        "source_id": "twse_official",
        "source_url": OFFICIAL_URL,
        "acquired_at": "2026-01-10T08:00:00+00:00",
        "source_payload": {"fixture": "official-contract-shape"},
        **terms,
    }


def test_corporate_actions_adjust_every_holder_effect_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "1000000")
    store = SQLiteStore(db_path=tmp_path / "corporate-actions.sqlite")
    oms = PaperOMS(store=store)
    for symbol in ("CASH.TW", "STOCK.TW", "REDUCE.TW", "RIGHTS.TW", "SPLIT.TW", "REVERSE.TW", "MERGE.TW"):
        _buy(oms, symbol)

    payloads = [
        _action(
            "CASH.TW",
            "cash_dividend",
            cash_per_share=2,
            reference_price_before=100,
            reference_price_after=98,
        ),
        _action(
            "STOCK.TW",
            "stock_dividend",
            share_multiplier=1.1,
            reference_price_before=100,
            reference_price_after=90.90909091,
        ),
        _action(
            "REDUCE.TW",
            "capital_reduction",
            share_multiplier=0.8,
            cash_per_share=0.5,
            reference_price_before=100,
            reference_price_after=124.375,
        ),
        _action(
            "RIGHTS.TW",
            "capital_increase",
            subscription_ratio=0.2,
            subscription_price=50,
            reference_price_before=100,
            reference_price_after=90,
        ),
        _action(
            "SPLIT.TW",
            "split",
            share_multiplier=2,
            reference_price_before=100,
            reference_price_after=50,
        ),
        _action(
            "REVERSE.TW",
            "reverse_split",
            share_multiplier=0.5,
            reference_price_before=100,
            reference_price_after=200,
        ),
        _action(
            "MERGE.TW",
            "merger",
            successor_symbol="SUCCESSOR.TW",
            successor_market="TW",
            exchange_ratio=0.5,
            price_multiplier=2,
            cash_boot_per_share=1,
        ),
        _action("CASH.TW", "treasury_stock"),
    ]
    imported = oms.import_corporate_actions(payloads)
    assert imported["created_count"] == 8
    assert all(item["terms_complete"] for item in imported["items"])

    first = oms.sync_corporate_actions(as_of="2026-12-31")
    assert first["applied_count"] == 8
    assert first["skipped_count"] == 0
    outcomes = {item["action_type"]: item["outcome"] for item in first["items"]}
    assert outcomes == {
        "cash_dividend": "cash_credited_and_price_adjusted",
        "stock_dividend": "position_and_price_adjusted",
        "capital_reduction": "position_and_price_adjusted",
        "capital_increase": "rights_recorded_no_automatic_subscription",
        "split": "position_and_price_adjusted",
        "reverse_split": "position_and_price_adjusted",
        "merger": "position_transferred_to_successor",
        "treasury_stock": "holder_position_unchanged",
    }
    portfolio = oms.portfolio_summary()
    positions = {item["symbol"]: item for item in portfolio["positions"]}
    assert positions["CASH.TW"]["quantity"] == 100
    assert positions["CASH.TW"]["last_price"] == pytest.approx(98)
    assert positions["STOCK.TW"]["quantity"] == pytest.approx(110)
    assert positions["STOCK.TW"]["average_cost"] == pytest.approx(100 / 1.1)
    assert positions["REDUCE.TW"]["quantity"] == pytest.approx(80)
    assert positions["REDUCE.TW"]["average_cost"] == pytest.approx(
        (100 * 100 - 100 * 0.5) / 80
    )
    assert positions["SPLIT.TW"]["quantity"] == pytest.approx(200)
    assert positions["REVERSE.TW"]["quantity"] == pytest.approx(50)
    assert positions["RIGHTS.TW"]["quantity"] == pytest.approx(100)
    assert "MERGE.TW" not in positions
    assert positions["SUCCESSOR.TW"]["quantity"] == pytest.approx(50)
    assert portfolio["corporate_action_count"] == 8
    assert portfolio["pending_entitlement_count"] == 1

    second = oms.sync_corporate_actions(as_of="2026-12-31")
    assert second["applied_count"] == 0
    assert second["idempotent_count"] == 8
    assert oms.portfolio_summary() == portfolio

    with store._connect() as conn:
        cash_entries = conn.execute(
            "select count(*) from cash_ledger where entry_type='corporate_action'"
        ).fetchone()[0]
        assert cash_entries == 3
        right = conn.execute(
            """
            select quantity, status from paper_corporate_action_entitlements
             where entitlement_type='subscription_right'
            """
        ).fetchone()
        assert right[0] == pytest.approx(20)
        assert right[1] == "pending_manual_exercise"


def test_corporate_action_revisions_are_idempotent_immutable_and_correctable(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "revisions.sqlite")
    oms = PaperOMS(store=store)
    payload = _action(
        "2330.TW",
        "cash_dividend",
        cash_per_share=3,
        reference_price_before=100,
        reference_price_after=97,
    )
    first = oms.import_corporate_actions([payload])
    same = oms.import_corporate_actions([payload])
    corrected = oms.import_corporate_actions([{**payload, "cash_per_share": 3.5}])
    assert first["created_count"] == 1
    assert same["idempotent_count"] == 1
    assert corrected["items"][0]["revision"] == 2
    assert corrected["items"][0]["supersedes_revision_id"] == first["items"][0]["revision_id"]
    assert oms.corporate_actions(symbol="2330.TW")["count"] == 1
    assert oms.corporate_actions(symbol="2330.TW")["items"][0]["terms"]["cash_per_share"] == 3.5

    with store._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "update corporate_action_revisions set status='cancelled' where revision_id=?",
                (first["items"][0]["revision_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "delete from corporate_action_revisions where revision_id=?",
                (first["items"][0]["revision_id"],),
            )


def test_sync_skips_unverified_incomplete_and_future_actions(tmp_path):
    store = SQLiteStore(db_path=tmp_path / "skips.sqlite")
    oms = PaperOMS(store=store)
    _buy(oms, "2330.TW")
    oms.import_corporate_actions(
        [
            {
                **_action("2330.TW", "cash_dividend", cash_per_share=2),
                "official_verified": False,
            },
            _action("2330.TW", "capital_increase", subscription_ratio=0.1),
            {
                **_action(
                    "2330.TW",
                    "split",
                    share_multiplier=2,
                    reference_price_before=100,
                    reference_price_after=50,
                ),
                "source_event_id": "future-split",
                "effective_date": "2027-01-01",
            },
        ]
    )
    result = oms.sync_corporate_actions(as_of="2026-12-31")
    assert result["applied_count"] == 0
    assert {item["reason"] for item in result["skipped"]} == {
        "not_officially_verified",
        "incomplete_terms",
    }


def test_corporate_action_api_import_list_and_sync(tmp_path, monkeypatch):
    trade_store = TradeStore(store=SQLiteStore(db_path=tmp_path / "api.sqlite"))
    _buy(trade_store.paper_oms, "2330.TW")
    engine = SimpleNamespace(
        pipeline=SimpleNamespace(trade_store=trade_store),
    )
    monkeypatch.setattr("stock_ai.main.get_runtime_engine", lambda: engine)
    monkeypatch.setattr("open_stock_ai.api.get_runtime_engine", lambda: engine)
    client = TestClient(app)
    payload = _action(
        "2330.TW",
        "cash_dividend",
        cash_per_share=2,
        reference_price_before=100,
        reference_price_after=98,
    )

    imported = client.post("/api/official/corporate-actions/import", json={"items": [payload]})
    assert imported.status_code == 200
    assert imported.json()["created_count"] == 1
    listed = client.get("/api/data/ui/v1/market/2330.TW/corporate-actions")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["official_verified"] is True
    synced = client.post(
        "/api/open-stock-ai/paper-account/corporate-actions/sync",
        json={"symbol": "2330.TW", "as_of": "2026-12-31"},
    )
    assert synced.status_code == 200
    assert synced.json()["applied_count"] == 1
    compatibility = client.get("/api/market/2330.TW/corporate-actions")
    assert compatibility.json()["items"][0]["application"]["outcome"] == "cash_credited_and_price_adjusted"


@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "symbol is required"),
        (_action("2330.TW", "unknown"), "unsupported corporate action type"),
        (
            {**_action("2330.TW", "split"), "source_url": ""},
            "source_id and source_url are required",
        ),
        (
            {
                **_action(
                    "2330.TW",
                    "cash_dividend",
                    cash_per_share=2,
                    reference_price_before=100,
                    reference_price_after=98,
                ),
                "source_url": "https://example.com/unverified",
            },
            "official_verified requires an official",
        ),
        (
            {
                **_action(
                    "2330.TW",
                    "cash_dividend",
                    cash_per_share=2,
                    reference_price_before=100,
                    reference_price_after=98,
                ),
                "source_payload": {},
            },
            "official_verified requires the captured official source payload",
        ),
    ],
)
def test_corporate_action_import_rejects_missing_provenance_or_type(tmp_path, payload, message):
    oms = PaperOMS(store=SQLiteStore(db_path=tmp_path / "invalid.sqlite"))
    with pytest.raises(ValueError, match=message):
        oms.import_corporate_actions([payload])
