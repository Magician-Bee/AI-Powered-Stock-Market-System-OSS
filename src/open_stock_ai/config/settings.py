from __future__ import annotations

from dataclasses import dataclass
from os import getenv
from pathlib import Path
from typing import Any

from open_stock_ai.config.env_loader import load_project_env
from open_stock_ai.config.loader import load_yaml_config
from open_stock_ai.governance import resolve_execution_stage
from open_stock_ai.governance.promotion_ladder import SQLitePromotionReceiptStore


@dataclass(frozen=True)
class OpenStockAISettings:
    name: str = "Open Stock AI System"
    mode: str = "paper"
    live_trading_enabled: bool = False
    capability_level: str = "PAPER"
    requested_execution_mode: str = "paper"
    requested_live_trading_enabled: bool = False
    capability_activation_blockers: tuple[str, ...] = ()
    require_human_confirm: bool = True
    external_runtime_connectors_enabled: bool = False
    broker_account_imports_enabled: bool = False
    external_credentials_enabled: bool = False
    # This is a descriptive runtime setting. Agent driver selection and its
    # credentials remain separately governed by stock_ai.agent_drivers.
    model_provider: str = "codex"
    min_rule_score_threshold: float = 0.70
    max_position_size_pct: float = 10.0
    max_intraday_loss_pct: float = 1.5
    max_daily_loss_pct: float = 3.0
    max_weekly_loss_pct: float = 6.0
    max_monthly_loss_pct: float = 10.0
    max_consecutive_losses: int = 3
    max_total_drawdown_pct: float = 15.0
    max_symbol_exposure_pct: float = 20.0
    max_total_paper_exposure_pct: float = 100.0
    max_industry_exposure_pct: float = 45.0
    require_backtest_passed: bool = True
    require_change_binding: bool = False
    active_change_id: str | None = None
    retention_maintenance_interval_seconds: int = 3600
    sqlite_path: str = "output/open_stock_ai.sqlite"
    external_projects: dict[str, str] | None = None
    watchlist: tuple[dict[str, str], ...] = ()


def load_settings(config_path: str | Path | None = None) -> OpenStockAISettings:
    # The portable launchers create .env from .env.example. Load it before reading
    # OPEN_STOCK_AI_CONFIG or any Paper OMS/RiskEngine environment override.
    load_project_env()
    config = load_yaml_config(config_path or getenv("OPEN_STOCK_AI_CONFIG", "config/open_stock_ai.yaml"))
    system = _section(config, "system")
    llm = _section(config, "llm")
    risk = _section(config, "risk")
    storage = _section(config, "storage")
    external_projects = _section(config, "external_projects")
    watchlist = _watchlist(config.get("watchlist"))
    requested_mode = str(getenv("OPEN_STOCK_AI_MODE", system.get("mode", "paper")))
    requested_live_trading = _env_bool(
        "LIVE_TRADING_ENABLED", system.get("live_trading_enabled", False)
    )
    sqlite_path = str(
        getenv("OPEN_STOCK_AI_SQLITE_PATH", storage.get("sqlite_path", "output/open_stock_ai.sqlite"))
    )
    promotion_store = (
        None
        if sqlite_path == ":memory:"
        else SQLitePromotionReceiptStore(sqlite_path)
    )
    stage = resolve_execution_stage(
        requested_mode,
        requested_live_trading,
        promotion_store=promotion_store,
    )
    return OpenStockAISettings(
        name=str(system.get("name", "Open Stock AI System")),
        mode=stage.mode,
        live_trading_enabled=stage.live_trading_enabled,
        capability_level=stage.active_level,
        requested_execution_mode=stage.requested_mode,
        requested_live_trading_enabled=stage.requested_live_trading_enabled,
        capability_activation_blockers=stage.activation_blockers,
        require_human_confirm=_env_bool("REQUIRE_HUMAN_CONFIRM", system.get("require_human_confirm", True)),
        external_runtime_connectors_enabled=_env_bool(
            "EXTERNAL_RUNTIME_CONNECTORS_ENABLED",
            system.get("external_runtime_connectors_enabled", False),
        ),
        broker_account_imports_enabled=_env_bool(
            "BROKER_ACCOUNT_IMPORTS_ENABLED",
            system.get("broker_account_imports_enabled", False),
        ),
        external_credentials_enabled=_env_bool(
            "EXTERNAL_CREDENTIALS_ENABLED",
            system.get("external_credentials_enabled", False),
        ),
        model_provider=_model_provider(llm.get("provider", "codex")),
        min_rule_score_threshold=_env_float(
            "MIN_RULE_SCORE_THRESHOLD",
            risk.get("min_rule_score_threshold", risk.get("min_confidence", 0.70)),
            0.70,
        ),
        max_position_size_pct=_env_float("MAX_POSITION_SIZE_PCT", risk.get("max_position_size_pct", 10.0), 10.0),
        max_intraday_loss_pct=_env_float(
            "MAX_INTRADAY_LOSS_PCT", risk.get("max_intraday_loss_pct", 1.5), 1.5
        ),
        max_daily_loss_pct=_env_float("MAX_DAILY_LOSS_PCT", risk.get("max_daily_loss_pct", 3.0), 3.0),
        max_weekly_loss_pct=_env_float(
            "MAX_WEEKLY_LOSS_PCT", risk.get("max_weekly_loss_pct", 6.0), 6.0
        ),
        max_monthly_loss_pct=_env_float(
            "MAX_MONTHLY_LOSS_PCT", risk.get("max_monthly_loss_pct", 10.0), 10.0
        ),
        max_consecutive_losses=_env_int(
            "MAX_CONSECUTIVE_LOSSES", risk.get("max_consecutive_losses", 3), 3
        ),
        max_total_drawdown_pct=_env_float("MAX_TOTAL_DRAWDOWN_PCT", risk.get("max_total_drawdown_pct", 15.0), 15.0),
        max_symbol_exposure_pct=_env_float("MAX_SYMBOL_EXPOSURE_PCT", risk.get("max_symbol_exposure_pct", 20.0), 20.0),
        max_total_paper_exposure_pct=_env_float(
            "MAX_TOTAL_PAPER_EXPOSURE_PCT",
            risk.get("max_total_paper_exposure_pct", 100.0),
            100.0,
        ),
        max_industry_exposure_pct=_env_float(
            "MAX_INDUSTRY_EXPOSURE_PCT",
            risk.get("max_industry_exposure_pct", 45.0),
            45.0,
        ),
        require_backtest_passed=_env_bool(
            "REQUIRE_BACKTEST_PASSED",
            risk.get("require_backtest_passed", True),
        ),
        require_change_binding=_env_bool(
            "OPEN_STOCK_AI_REQUIRE_CHANGE_BINDING",
            system.get("require_change_binding", False),
        ),
        active_change_id=(
            str(getenv("OPEN_STOCK_AI_ACTIVE_CHANGE_ID", system.get("active_change_id", ""))).strip()
            or None
        ),
        retention_maintenance_interval_seconds=max(
            1,
            _env_int(
                "RETENTION_MAINTENANCE_INTERVAL_SECONDS",
                system.get("retention_maintenance_interval_seconds", 3600),
                3600,
            ),
        ),
        sqlite_path=sqlite_path,
        external_projects={str(key): str(value) for key, value in external_projects.items()},
        watchlist=watchlist,
    )


def _section(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key) if isinstance(config, dict) else {}
    return value if isinstance(value, dict) else {}


def _model_provider(value: Any) -> str:
    provider = str(value or "codex").strip().lower()
    return provider if provider in {"codex", "openai-compatible", "external-agent"} else "codex"


def _env_bool(name: str, default: Any) -> bool:
    value = getenv(name)
    if value is None:
        return _bool(default)
    return _bool(value)


def _env_float(name: str, default: Any, fallback: float) -> float:
    return _float(getenv(name, default), fallback)


def _env_int(name: str, default: Any, fallback: int) -> int:
    try:
        return int(getenv(name, default))
    except (TypeError, ValueError):
        return fallback


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _watchlist(value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        return ()
    items: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        market = str(item.get("market") or "TW").strip().upper()
        if symbol and market in {"TW", "US", "CRYPTO"}:
            items.append({"symbol": symbol, "market": market})
    return tuple(items)
