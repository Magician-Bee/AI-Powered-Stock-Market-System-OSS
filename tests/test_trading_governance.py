from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from open_stock_ai.config.settings import load_settings
from open_stock_ai.governance import capability_status, render_readme_capability_status
from open_stock_ai.governance.promotion_ladder import (
    CapabilityPromotionLadder,
    SQLitePromotionReceiptStore,
    required_evidence,
)
from stock_ai.main import app, load_open_stock_ai_settings


ROOT = Path(__file__).parents[1]
client = TestClient(app)


def test_capability_status_declares_all_ordered_promotion_levels() -> None:
    status = capability_status()

    assert status["schema_version"] == "stock_ai.capability_status.v1"
    assert status["source_of_truth"] == "config/capability_status.yaml"
    assert status["master_requirement_ledger"] == "config/production_requirement_status.yaml"
    assert status["current_level"] == "PAPER"
    assert status["effective_execution_mode"] == "paper"
    assert status["live_order_submission_enabled"] is False
    assert status["model_direct_broker_submission_enabled"] is False
    assert status["authoritative_store_matrix"]["validated"] is True
    assert len(status["authoritative_store_matrix"]["matrix_sha256"]) == 64
    assert [item["name"] for item in status["levels"]] == [
        "RESEARCH",
        "PAPER",
        "SHADOW",
        "BROKER_SANDBOX",
        "RESTRICTED_LIVE",
        "PRODUCTION_LIVE",
    ]
    assert [item["ordinal"] for item in status["levels"]] == list(range(6))
    assert [item["name"] for item in status["levels"] if item["active"]] == ["PAPER"]


def test_environment_cannot_skip_current_governance_stage(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text("system:\n  mode: paper\n  live_trading_enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("OPEN_STOCK_AI_MODE", "restricted_live")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")

    settings = load_settings(config_path)

    assert settings.mode == "paper"
    assert settings.live_trading_enabled is False
    assert settings.capability_level == "PAPER"
    assert "requested_execution_mode_exceeds_current_approved_level" in settings.capability_activation_blockers
    assert "live_execution_requires_restricted_live_or_production_live_approval" in settings.capability_activation_blockers


def test_research_downgrade_is_allowed_but_never_enables_live_execution(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text("system:\n  mode: paper\n", encoding="utf-8")
    monkeypatch.setenv("OPEN_STOCK_AI_MODE", "research")
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)

    settings = load_settings(config_path)

    assert settings.mode == "research"
    assert settings.live_trading_enabled is False
    assert settings.capability_activation_blockers == ()


def test_capability_status_api_exposes_the_same_machine_readable_authority() -> None:
    response = client.get("/api/system/capability-status")

    assert response.status_code == 200
    assert response.json()["source_of_truth"] == "config/capability_status.yaml"
    assert response.json()["master_requirement_ledger"] == "config/production_requirement_status.yaml"
    assert response.json()["current_level"] == "PAPER"
    assert response.json()["live_order_submission_enabled"] is False
    assert response.json()["production_requirements"]["full_release_total"] == 124
    assert response.json()["authoritative_store_matrix"]["validated"] is True
    assert response.json()["authoritative_store_matrix"]["source_of_truth"] == "config/authoritative_store_matrix.yaml"
    runtime_governance = response.json()["runtime_governance"]
    assert runtime_governance["schema_version"] == "open_stock_ai.runtime_governance.v1"
    assert runtime_governance["durable"] is True
    assert runtime_governance["authorities"]["artifact_rollback"]["store_wired"] is True
    assert runtime_governance["authorities"]["change_management"]["store_wired"] is True
    assert runtime_governance["authorities"]["retention"]["store_wired"] is True
    assert runtime_governance["authorities"]["retention"]["maintenance"]["durable"] is True


def test_capability_status_api_initializes_file_backed_promotion_store_on_first_boot(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "first-boot" / "open_stock_ai.sqlite"
    configured = replace(load_open_stock_ai_settings(), sqlite_path=str(database_path))
    monkeypatch.setattr("stock_ai.main.load_open_stock_ai_settings", lambda: configured)

    response = client.get("/api/system/capability-status")

    assert response.status_code == 200
    promotion = response.json()["promotion"]
    assert promotion["store_configured"] is True
    assert promotion["durable"] is True
    assert database_path.is_file()
    assert SQLitePromotionReceiptStore(database_path).receipts() == []


def test_capability_status_exposes_durable_promotion_history(tmp_path) -> None:
    store = SQLitePromotionReceiptStore(tmp_path / "promotion.sqlite")
    receipt = CapabilityPromotionLadder(store=store).promote(
        "paper",
        evidence={key: f"verified-{key}" for key in required_evidence("paper")},
        approved_by="owner",
    )

    status = capability_status(promotion_store=store)

    assert receipt.verify() is True
    assert status["promotion"]["store_configured"] is True
    assert status["promotion"]["durable"] is True
    assert status["promotion"]["configured_level"] == "PAPER"
    assert status["promotion"]["persisted_level"] == "PAPER"
    assert status["promotion"]["effective_level"] == "PAPER"
    assert status["promotion"]["active_level_source"] == "durable_promotion_receipt"
    assert status["promotion"]["receipt_count"] == 1
    assert status["promotion"]["latest_receipt"]["receipt_sha256"] == receipt.receipt_sha256
    assert status["promotion"]["activation_blockers"] == []


def test_durable_promotion_receipt_controls_the_runtime_stage_after_restart(
    monkeypatch, tmp_path
) -> None:
    database_path = tmp_path / "promotion-runtime.sqlite"
    store = SQLitePromotionReceiptStore(database_path)
    CapabilityPromotionLadder(store=store).promote(
        "paper",
        evidence={key: f"verified-{key}" for key in required_evidence("paper")},
        approved_by="owner",
    )
    CapabilityPromotionLadder(store=store).promote(
        "shadow",
        evidence={key: f"verified-{key}" for key in required_evidence("shadow")},
        approved_by="owner",
    )
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text("system:\n  mode: paper\n", encoding="utf-8")
    monkeypatch.setenv("OPEN_STOCK_AI_SQLITE_PATH", str(database_path))
    monkeypatch.setenv("OPEN_STOCK_AI_MODE", "shadow")

    settings = load_settings(config_path)
    status = capability_status(promotion_store=SQLitePromotionReceiptStore(database_path))

    assert settings.capability_level == "SHADOW"
    assert settings.mode == "shadow"
    assert settings.capability_activation_blockers == ()
    assert status["current_level"] == "SHADOW"
    assert status["effective_execution_mode"] == "shadow"
    assert status["promotion"]["effective_level"] == "SHADOW"


def test_readme_capability_block_is_generated_from_the_authoritative_status() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("<!-- capability-status:start -->")
    end = readme.index("<!-- capability-status:end -->", start) + len("<!-- capability-status:end -->")

    assert readme[start:end] == render_readme_capability_status()
