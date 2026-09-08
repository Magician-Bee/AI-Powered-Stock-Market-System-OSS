from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from typing import Any

from .config.settings import OpenStockAISettings, load_settings
from .engine import OpenStockAIEngine
from .main import build_engine


_MAX_RUNTIME_ENGINES = 8
_ENGINE_CACHE: OrderedDict[tuple[Any, ...], OpenStockAIEngine] = OrderedDict()
_ENGINE_CACHE_LOCK = RLock()


def _settings_key(settings: OpenStockAISettings) -> tuple[Any, ...]:
    external_projects = tuple(sorted((settings.external_projects or {}).items()))
    watchlist = tuple(
        (str(item.get("symbol") or ""), str(item.get("market") or ""))
        for item in settings.watchlist
    )
    return (
        settings.name,
        settings.mode,
        settings.live_trading_enabled,
        settings.require_human_confirm,
        settings.external_runtime_connectors_enabled,
        settings.broker_account_imports_enabled,
        settings.external_credentials_enabled,
        settings.model_provider,
        settings.min_rule_score_threshold,
        settings.max_position_size_pct,
        settings.max_intraday_loss_pct,
        settings.max_daily_loss_pct,
        settings.max_weekly_loss_pct,
        settings.max_monthly_loss_pct,
        settings.max_consecutive_losses,
        settings.max_total_drawdown_pct,
        settings.max_symbol_exposure_pct,
        settings.max_total_paper_exposure_pct,
        settings.max_industry_exposure_pct,
        settings.require_backtest_passed,
        settings.sqlite_path,
        external_projects,
        watchlist,
    )


def get_runtime_engine(settings: OpenStockAISettings | None = None) -> OpenStockAIEngine:
    """Return one reusable engine per effective configuration.

    The cache is deliberately small and configuration-aware, so tests and alternate
    paper ledgers do not leak into one another while normal Agent/API requests reuse
    the Registry, stores and pipeline instead of rebuilding them on every call.
    """
    resolved = settings or load_settings()
    key = _settings_key(resolved)
    with _ENGINE_CACHE_LOCK:
        cached = _ENGINE_CACHE.get(key)
        if cached is not None:
            _ENGINE_CACHE.move_to_end(key)
            return cached
        engine = build_engine(settings=resolved)
        _ENGINE_CACHE[key] = engine
        _ENGINE_CACHE.move_to_end(key)
        while len(_ENGINE_CACHE) > _MAX_RUNTIME_ENGINES:
            _ENGINE_CACHE.popitem(last=False)
        return engine


def clear_runtime_engine_cache() -> None:
    with _ENGINE_CACHE_LOCK:
        _ENGINE_CACHE.clear()


def runtime_engine_cache_info() -> dict[str, int]:
    with _ENGINE_CACHE_LOCK:
        return {"size": len(_ENGINE_CACHE), "max_size": _MAX_RUNTIME_ENGINES}
