from __future__ import annotations

import sqlite3

import pytest

from open_stock_ai.agent_runtime.final_runtime import FinalAgentRuntime
from open_stock_ai.agent_runtime.automation.store import AutomationStore


def _authentication(*, nonce: str = "a") -> dict[str, object]:
    return {
        "source": "n8n",
        "authenticated": True,
        "callback_timestamp": 1_780_000_000,
        "verified_at": 1_780_000_001,
        "body_sha256": "b" * 64,
        "nonce_sha256": nonce * 64,
        "signature_sha256": "c" * 64,
        "token_sha256": "d" * 64,
    }


def test_verified_callback_receipt_is_secret_free_immutable_and_queryable(tmp_path):
    store = AutomationStore(tmp_path / "automation.sqlite")
    payload = {
        "automation_id": "AUT-proof",
        "automation_version": 2,
        "submission_id": "AUS-proof",
        "source_event_id": "n8n:execution:42",
        "source": "n8n",
        "compiler_digest": "e" * 64,
        "ignored_secret": "must-not-persist",
    }
    receipt = store.record_verified_callback(
        authentication=_authentication(),
        event_type="automation.n8n.trigger",
        payload=payload,
        outcomes=[
            {"automation_id": "AUT-proof", "status": "completed", "error": {"message": "omit"}},
        ],
    )

    assert receipt["authenticated"] is True
    assert receipt["payload"] == {key: value for key, value in payload.items() if key != "ignored_secret"}
    assert receipt["outcomes"] == [{"automation_id": "AUT-proof", "status": "completed"}]
    assert len(receipt["receipt_sha256"]) == 64
    assert "must-not-persist" not in repr(receipt)
    assert store.list_callback_receipts(automation_id="AUT-proof") == [receipt]
    # A process retry cannot create a contradictory second receipt for one nonce.
    assert store.record_verified_callback(
        authentication=_authentication(),
        event_type="automation.n8n.trigger",
        payload=payload,
        outcomes=[],
    ) == receipt
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "update agent_automation_callback_receipts set event_type='forged' where receipt_id=?",
                (receipt["receipt_id"],),
            )
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "delete from agent_automation_callback_receipts where receipt_id=?",
                (receipt["receipt_id"],),
            )


def test_automation_activation_creates_a_durable_session_when_api_omits_one(tmp_path):
    runtime = FinalAgentRuntime(tmp_path / "runtime.sqlite")
    response = runtime.activate_automation(
        {
            "goal": "每週確認研究資料是否需要更新",
            "user_id": "stock-ai-local",
            "kind": "event_watch",
            "trigger": {"type": "event", "event_type": "market.close"},
            "actions": [{"type": "in_app", "label": "保存結果"}],
        },
        confirmed=True,
    )

    session_id = response["automation"]["session_id"]
    assert session_id.startswith("AS-")
    assert runtime.sessions.get(session_id)["metadata"]["created_by"] == "automation_activation"
