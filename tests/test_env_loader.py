from __future__ import annotations

from open_stock_ai.config.env_loader import load_project_env
from open_stock_ai.config.settings import load_settings
from open_stock_ai.execution.paper_oms import PaperOMS
from open_stock_ai.storage.sqlite_store import SQLiteStore


def test_project_env_reaches_settings_and_paper_oms(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    sqlite_path = tmp_path / "configured.sqlite"
    env_path.write_text(
        f'''MODEL_PROVIDER="ignored-local-override"
MAX_POSITION_SIZE_PCT=7.5
MAX_INDUSTRY_EXPOSURE_PCT=38
REQUIRE_BACKTEST_PASSED=false
OPEN_STOCK_AI_REQUIRE_CHANGE_BINDING=true
OPEN_STOCK_AI_ACTIVE_CHANGE_ID=owner-approved-change-1
OPEN_STOCK_AI_SQLITE_PATH={sqlite_path.as_posix()}
OPEN_STOCK_AI_PAPER_INITIAL_CASH=123456
OPEN_STOCK_AI_PAPER_COMMISSION_BPS=12.5
''',
        encoding="utf-8",
    )
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text(
        """
system:
  mode: paper
risk:
  max_position_size_pct: 10
  max_industry_exposure_pct: 45
  require_backtest_passed: true
storage:
  sqlite_path: output/fallback.sqlite
""",
        encoding="utf-8",
    )
    for key in [
        "MODEL_PROVIDER",
        "MAX_POSITION_SIZE_PCT",
        "MAX_INDUSTRY_EXPOSURE_PCT",
        "REQUIRE_BACKTEST_PASSED",
        "OPEN_STOCK_AI_REQUIRE_CHANGE_BINDING",
        "OPEN_STOCK_AI_ACTIVE_CHANGE_ID",
        "OPEN_STOCK_AI_SQLITE_PATH",
        "OPEN_STOCK_AI_PAPER_INITIAL_CASH",
        "OPEN_STOCK_AI_PAPER_COMMISSION_BPS",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPEN_STOCK_AI_ENV_FILE", str(env_path))

    settings = load_settings(config_path)
    oms = PaperOMS(store=SQLiteStore(db_path=settings.sqlite_path))
    account = oms.portfolio_summary()

    assert settings.model_provider == "codex"
    assert settings.max_position_size_pct == 7.5
    assert settings.max_industry_exposure_pct == 38.0
    assert settings.require_backtest_passed is False
    assert settings.require_change_binding is True
    assert settings.active_change_id == "owner-approved-change-1"
    assert settings.sqlite_path == str(sqlite_path)
    assert account["initial_cash"] == 123456.0
    assert account["assumptions"]["commission_bps"] == 12.5


def test_existing_process_environment_has_priority(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("MODEL_PROVIDER=file-model\nMAX_DAILY_LOSS_PCT=8\n", encoding="utf-8")
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text("risk:\n  max_daily_loss_pct: 3\n", encoding="utf-8")
    monkeypatch.setenv("OPEN_STOCK_AI_ENV_FILE", str(env_path))
    monkeypatch.setenv("MODEL_PROVIDER", "shell-model")
    monkeypatch.setenv("MAX_DAILY_LOSS_PCT", "2.5")

    settings = load_settings(config_path)

    assert settings.model_provider == "codex"
    assert settings.max_daily_loss_pct == 2.5


def test_settings_preserve_configured_openai_compatible_provider(tmp_path, monkeypatch):
    config_path = tmp_path / "open_stock_ai.yaml"
    config_path.write_text("llm:\n  provider: openai-compatible\n", encoding="utf-8")
    monkeypatch.setenv("OPEN_STOCK_AI_ENV_FILE", str(tmp_path / "missing.env"))

    settings = load_settings(config_path)

    assert settings.model_provider == "openai-compatible"


def test_env_parser_supports_export_quotes_and_inline_comments(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        """
# comment
export TEST_STOCK_AI_QUOTED="hello world"
TEST_STOCK_AI_URL=https://example.test/path#fragment
TEST_STOCK_AI_INLINE=value # local note
INVALID-KEY=ignored
""",
        encoding="utf-8",
    )
    for key in ["TEST_STOCK_AI_QUOTED", "TEST_STOCK_AI_URL", "TEST_STOCK_AI_INLINE"]:
        monkeypatch.delenv(key, raising=False)

    loaded = load_project_env(env_path)

    assert loaded["TEST_STOCK_AI_QUOTED"] == "hello world"
    assert loaded["TEST_STOCK_AI_URL"] == "https://example.test/path#fragment"
    assert loaded["TEST_STOCK_AI_INLINE"] == "value"
    assert "INVALID-KEY" not in loaded
