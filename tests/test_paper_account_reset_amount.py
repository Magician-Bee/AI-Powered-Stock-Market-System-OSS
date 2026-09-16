from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.execution.paper_training import PaperTrainingLab
from open_stock_ai.storage.sqlite_store import SQLiteStore


def test_reset_persists_requested_initial_cash_and_cash_balance(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_ACCOUNT_ID", "reset-amount-test")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "1000000")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_COMMISSION_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SELL_TAX_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_LOT_SIZE", "1")

    store = SQLiteStore(db_path=tmp_path / "paper-reset.sqlite")
    oms = PaperOMS(store=store)
    lab = PaperTrainingLab(store=store, oms=oms)

    result = lab.reset_account(10_000_000)
    immediate = result["account"]
    verified = lab.account_summary()

    assert immediate["initial_cash"] == 10_000_000
    assert immediate["cash_balance"] == 10_000_000
    assert immediate["total_equity"] == 10_000_000
    assert verified["initial_cash"] == 10_000_000
    assert verified["cash_balance"] == 10_000_000
    assert verified["total_equity"] == 10_000_000

def test_fastapi_reset_returns_the_same_persisted_amount(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from open_stock_ai.runtime import clear_runtime_engine_cache
    from stock_ai.main import app

    monkeypatch.setenv("OPEN_STOCK_AI_SQLITE_PATH", str(tmp_path / "api-reset.sqlite"))
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_ACCOUNT_ID", "api-reset-test")
    monkeypatch.setenv("OPEN_STOCK_AI_PAPER_INITIAL_CASH", "1000000")
    clear_runtime_engine_cache()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/open-stock-ai/agent/paper-training/reset",
            json={"initial_cash": 12_345_678},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["verified"] is True
        assert payload["reset_token"].startswith("RESET-")
        assert payload["account"]["initial_cash"] == 12_345_678
        assert payload["account"]["cash_balance"] == 12_345_678
        assert payload["account"]["total_equity"] == 12_345_678

        persisted = client.get("/api/open-stock-ai/agent/paper-training/account").json()
        assert persisted["initial_cash"] == 12_345_678
        assert persisted["cash_balance"] == 12_345_678
        assert persisted["total_equity"] == 12_345_678
        assert persisted["account_updated_at"]
    finally:
        clear_runtime_engine_cache()
