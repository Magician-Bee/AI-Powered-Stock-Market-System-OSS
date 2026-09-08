from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from types import ModuleType
from typing import Any


ROOT = Path(os.environ.get("STOCK_AI_PROJECT_ROOT") or Path(__file__).resolve().parents[2]).resolve()


def _load(relative_path: str, name: str):
    path = ROOT / relative_path
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load external module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _package(name: str, relative_path: str) -> ModuleType:
    """Register a package shell so one external module can use local imports.

    The worker deliberately avoids importing an external project's top-level
    package because several snapshots eagerly import every optional service.
    The target source module and its direct dependencies are still executed.
    """

    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    module = ModuleType(name)
    module.__path__ = [str(ROOT / relative_path)]  # type: ignore[attr-defined]
    module.__package__ = name
    sys.modules[name] = module
    return module


def _module_stub(name: str, **values: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _run(project: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if (project, action) == ("tradingagents", "model_capabilities"):
        relative = "external/TradingAgents/tradingagents/llm_clients/capabilities.py"
        module = _load(relative, "open_stock_ai_external_tradingagents_capabilities")
        function = "get_capabilities"
        result = asdict(module.get_capabilities(str(payload["model"])))
    elif (project, action) == ("tradingagents", "investment_decision"):
        relative = "external/TradingAgents/tradingagents/agents/utils/rating.py"
        module = _load(relative, "open_stock_ai_external_tradingagents_rating")
        function = "parse_rating"
        rating = module.parse_rating(
            str(payload["analysis"]),
            default=str(payload.get("default") or "Hold"),
        )
        result = {
            "rating": rating,
            "scale": list(module.RATINGS_5_TIER),
            "decision_found": rating.lower() in str(payload["analysis"]).lower(),
        }
    elif (project, action) == ("fingpt", "sentiment_consensus"):
        relative = "external/FinGPT/fingpt/FinGPT_Forecaster/market_sentiment.py"
        module = _load(relative, "open_stock_ai_external_fingpt_sentiment")
        function = "summarize_market_sentiment"
        signals = [
            {
                "source": str(item["source"]),
                "available": bool(item.get("available", True)),
                "average_sentiment_score": float(item["average_sentiment_score"]),
                "total_mentions": int(item["total_mentions"]),
            }
            for item in payload["signals"]
        ]
        result = module.summarize_market_sentiment(signals)
    elif (project, action) == ("finrobot", "report_quality"):
        relative = "external/FinRobot/finrobot/functional/text.py"
        module = _load(relative, "open_stock_ai_external_finrobot_text")
        function = "TextUtils.check_text_length"
        text = str(payload["text"])
        min_words = int(payload.get("min_words", 0))
        max_words = int(payload.get("max_words", 100000))
        result = {
            "word_count": len(text.split()),
            "assessment": module.TextUtils.check_text_length(text, min_words, max_words),
            "within_range": min_words <= len(text.split()) <= max_words,
        }
    elif (project, action) == ("finrobot", "market_data"):
        _package("finrobot", "external/FinRobot/finrobot")
        _package("finrobot.data_source", "external/FinRobot/finrobot/data_source")
        _load("external/FinRobot/finrobot/utils.py", "finrobot.utils")
        relative = "external/FinRobot/finrobot/data_source/yfinance_utils.py"
        module = _load(relative, "finrobot.data_source.yfinance_utils")
        function = "YFinanceUtils.get_stock_data"
        frame = module.YFinanceUtils.get_stock_data(
            str(payload["symbol"]),
            str(payload["start_date"]),
            str(payload["end_date"]),
        )
        if frame.empty:
            raise RuntimeError("FinRobot returned no market rows for the requested symbol and date range")
        limit = max(1, min(int(payload.get("limit", 60)), 250))
        rows = []
        for index, row in frame.tail(limit).iterrows():
            rows.append(
                {
                    "date": index.isoformat() if hasattr(index, "isoformat") else str(index),
                    **{
                        str(key).lower().replace(" ", "_"): _finite_or_none(value)
                        for key, value in row.items()
                        if _is_number(value)
                    },
                }
            )
        closes = frame["Close"].dropna() if "Close" in frame else None
        result = {
            "symbol": str(payload["symbol"]),
            "start_date": str(payload["start_date"]),
            "end_date": str(payload["end_date"]),
            "row_count": int(len(frame)),
            "rows": rows,
            "summary": {
                "first_close": _finite_or_none(closes.iloc[0]) if closes is not None and len(closes) else None,
                "last_close": _finite_or_none(closes.iloc[-1]) if closes is not None and len(closes) else None,
                "return_pct": (
                    _finite_or_none((closes.iloc[-1] / closes.iloc[0] - 1.0) * 100.0)
                    if closes is not None and len(closes) >= 2 and float(closes.iloc[0]) != 0
                    else None
                ),
            },
        }
    elif (project, action) == ("finrl", "walk_forward_windows"):
        relative = "external/FinRL/finrl/meta/data_processors/func.py"
        module = _load(relative, "open_stock_ai_external_finrl_windows")
        function = "calc_train_trade_starts_ends_if_rolling"
        values = module.calc_train_trade_starts_ends_if_rolling(
            [str(value) for value in payload["train_dates"]],
            [str(value) for value in payload["trade_dates"]],
            int(payload["rolling_window_length"]),
        )
        result = dict(zip(("train_starts", "train_ends", "trade_starts", "trade_ends"), values))
    elif (project, action) == ("finrl", "simulate_environment"):
        relative = "external/FinRL/finrl/meta/env_stock_trading/env_stocktrading_np.py"
        module = _load(relative, "open_stock_ai_external_finrl_environment")
        function = "StockTradingEnv.reset/step"
        numpy = __import__("numpy")
        prices = numpy.asarray(payload["prices"], dtype=numpy.float32)
        actions = numpy.asarray(payload["actions"], dtype=numpy.float32)
        if prices.ndim != 2 or actions.ndim != 2:
            raise ValueError("prices and actions must be two-dimensional arrays")
        if prices.shape[0] < 2 or prices.shape[1] < 1:
            raise ValueError("prices must contain at least two rows and one asset")
        if actions.shape[1] != prices.shape[1]:
            raise ValueError("each action row must match the price asset count")
        if actions.shape[0] > prices.shape[0] - 1:
            raise ValueError("actions cannot exceed the available environment steps")
        technical = payload.get("technical_features")
        tech_array = (
            numpy.asarray(technical, dtype=numpy.float32)
            if technical is not None
            else numpy.zeros((prices.shape[0], prices.shape[1]), dtype=numpy.float32)
        )
        if tech_array.ndim != 2 or tech_array.shape[0] != prices.shape[0]:
            raise ValueError("technical_features must have one row per price row")
        turbulence = numpy.asarray(
            payload.get("turbulence", [0.0] * prices.shape[0]),
            dtype=numpy.float32,
        )
        if turbulence.shape != (prices.shape[0],):
            raise ValueError("turbulence must have one value per price row")
        initial_capital = float(payload.get("initial_capital", 1_000_000.0))
        environment = module.StockTradingEnv(
            {
                "price_array": prices,
                "tech_array": tech_array,
                "turbulence_array": turbulence,
                "if_train": False,
            },
            initial_capital=initial_capital,
            max_stock=float(payload.get("max_stock", 100.0)),
            buy_cost_pct=float(payload.get("buy_cost_pct", 0.001)),
            sell_cost_pct=float(payload.get("sell_cost_pct", 0.001)),
        )
        state, _ = environment.reset(seed=0)
        steps = []
        terminated = False
        for action_values in actions:
            state, reward, terminated, truncated, _ = environment.step(action_values)
            steps.append(
                {
                    "day": int(environment.day),
                    "reward": _finite_or_none(reward),
                    "total_asset": _finite_or_none(environment.total_asset),
                    "cash": _finite_or_none(environment.amount),
                    "stocks": [_finite_or_none(value) for value in environment.stocks.tolist()],
                    "terminated": bool(terminated),
                    "truncated": bool(truncated),
                }
            )
            if terminated or truncated:
                break
        result = {
            "asset_count": int(prices.shape[1]),
            "price_rows": int(prices.shape[0]),
            "steps_executed": len(steps),
            "terminated": bool(terminated),
            "initial_total_asset": _finite_or_none(environment.initial_total_asset),
            "final_total_asset": _finite_or_none(environment.total_asset),
            "return_pct": _finite_or_none(
                (environment.total_asset / environment.initial_total_asset - 1.0) * 100.0
            ),
            "final_state_size": int(len(state)),
            "steps": steps,
        }
    elif (project, action) == ("finrl_trading", "information_ratio"):
        relative = "external/FinRL-Trading/src/strategies/adaptive_rotation/utils/robust_stats.py"
        module = _load(relative, "open_stock_ai_external_finrl_trading_stats")
        function = "compute_information_ratio"
        pandas = __import__("pandas")
        returns = pandas.Series([float(value) for value in payload["returns"]])
        benchmark = pandas.Series([float(value) for value in payload["benchmark_returns"]])
        lookback = min(int(payload.get("lookback", len(returns))), len(returns), len(benchmark))
        value = module.compute_information_ratio(
            returns,
            benchmark,
            lookback=lookback,
            robust=bool(payload.get("robust", True)),
            annualization_factor=float(payload.get("annualization_factor", 1.0)),
            min_periods=lookback,
        )
        result = {"information_ratio": _finite_or_none(value), "lookback": lookback}
    elif (project, action) == ("finrl_trading", "regime_signals"):
        base = "external/FinRL-Trading/src/strategies/adaptive_rotation"
        _package("adaptive_rotation", base)
        _package("adaptive_rotation.utils", f"{base}/utils")
        _load(f"{base}/utils/robust_stats.py", "adaptive_rotation.utils.robust_stats")
        _module_stub("adaptive_rotation.config_loader", AdaptiveRotationConfig=object)
        relative = f"{base}/market_regime.py"
        module = _load(relative, "adaptive_rotation.market_regime")
        function = "compute_slow_regime_signals"
        pandas = __import__("pandas")
        dates = pandas.to_datetime([str(value) for value in payload["dates"]], utc=True)
        spx_values = [float(value) for value in payload["spx_prices"]]
        vix_values = [float(value) for value in payload["vix_prices"]]
        if len(dates) != len(spx_values) or len(dates) != len(vix_values):
            raise ValueError("dates, spx_prices, and vix_prices must have equal lengths")
        spx = pandas.Series(spx_values, index=dates, dtype=float)
        vix = pandas.Series(vix_values, index=dates, dtype=float)
        signals = module.compute_slow_regime_signals(
            spx_prices=spx,
            vix_prices=vix,
            as_of_date=dates[-1],
            trend_ma_weeks=int(payload.get("trend_ma_weeks", 26)),
            drawdown_weeks=int(payload.get("drawdown_weeks", 13)),
            drawdown_threshold=float(payload.get("drawdown_threshold", 0.10)),
            vix_lookback_years=int(payload.get("vix_lookback_years", 3)),
            vix_z_threshold=float(payload.get("vix_z_threshold", 3.0)),
        )
        result = asdict(signals)
        result["as_of_date"] = dates[-1].isoformat()
        result["regime"] = "risk_on" if signals.risk_score == 0 else ("neutral" if signals.risk_score == 1 else "risk_off")
    elif (project, action) == ("qlib", "align_signals"):
        relative = "external/qlib/qlib/utils/index_data.py"
        module = _load(relative, "open_stock_ai_external_qlib_index_data")
        function = "SingleData.add"
        left = module.SingleData({str(key): float(value) for key, value in payload["left"].items()})
        right = module.SingleData({str(key): float(value) for key, value in payload["right"].items()})
        aligned = left.add(right, fill_value=float(payload.get("fill_value", 0.0)))
        result = {"aligned": aligned.to_dict(), "count": len(aligned)}
    elif (project, action) == ("qlib", "factor_dataset"):
        relative = "external/qlib/qlib/contrib/data/utils/sepdf.py"
        module = _load(relative, "open_stock_ai_external_qlib_sepdf")
        function = "SepDataFrame"
        pandas = __import__("pandas")
        feature_frame = pandas.DataFrame.from_dict(
            {str(key): float(value) for key, value in payload["features"].items()},
            orient="index",
            columns=["factor"],
        )
        label_frame = pandas.DataFrame.from_dict(
            {str(key): float(value) for key, value in payload["labels"].items()},
            orient="index",
            columns=["label"],
        )
        dataset = module.SepDataFrame(
            {"feature": feature_frame, "label": label_frame},
            join="feature",
        )
        aligned = pandas.concat([dataset["feature"], dataset["label"]], axis=1)
        valid = aligned.dropna()
        information_coefficient = valid["factor"].corr(valid["label"]) if len(valid) >= 2 else None
        result = {
            "join": dataset.join,
            "row_count": len(dataset),
            "valid_pair_count": int(len(valid)),
            "information_coefficient": _finite_or_none(information_coefficient) if information_coefficient is not None else None,
            "aligned": [
                {
                    "index": str(index),
                    "factor": _finite_or_none(row["factor"]),
                    "label": _finite_or_none(row["label"]) if not pandas.isna(row["label"]) else None,
                }
                for index, row in aligned.iterrows()
            ],
        }
    elif (project, action) == ("ai_trader", "variant_metrics"):
        relative = "external/AI-Trader/research/scripts/research_common.py"
        module = _load(relative, "open_stock_ai_external_ai_trader_research")
        function = "variant_summary"
        metric = str(payload["metric"])
        rows = [dict(item) for item in payload["rows"]]
        result = {"metric": metric, "variants": module.variant_summary(rows, metric)}
    elif (project, action) == ("ai_trader", "score_signal"):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE agents (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY, signal_id INTEGER UNIQUE NOT NULL,
                agent_id INTEGER NOT NULL, message_type TEXT NOT NULL,
                market TEXT NOT NULL, symbol TEXT, symbols TEXT, title TEXT,
                content TEXT, tags TEXT, timestamp INTEGER NOT NULL,
                created_at TEXT NOT NULL, accepted_reply_id INTEGER
            );
            CREATE TABLE signal_predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL, market TEXT, symbol TEXT, direction TEXT,
                target_price REAL, target_probability REAL, confidence REAL,
                horizon_start_at TEXT, horizon_end_at TEXT, invalid_if TEXT,
                evidence_json TEXT, extracted_by TEXT, created_at TEXT
            );
            CREATE TABLE signal_quality_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL, verifiability_score REAL,
                evidence_score REAL, specificity_score REAL, novelty_score REAL,
                review_score REAL, overall_score REAL, model_version TEXT,
                metadata_json TEXT, created_at TEXT
            );
            """
        )
        _module_stub(
            "database",
            begin_write_transaction=lambda active_cursor: active_cursor.execute("BEGIN"),
            get_db_connection=lambda: connection,
        )
        _module_stub("routes_shared", utc_now_iso_z=lambda: datetime.now(timezone.utc).isoformat())
        relative = "external/AI-Trader/service/server/signal_quality.py"
        module = _load(relative, "open_stock_ai_external_ai_trader_signal_quality")
        function = "score_signal_quality"
        created_at = datetime.now(timezone.utc).isoformat()
        signal = {
            "signal_id": 1,
            "agent_id": 1,
            "message_type": "strategy",
            "market": str(payload.get("market") or "stock"),
            "symbol": str(payload["symbol"]),
            "symbols": None,
            "title": str(payload.get("title") or "Agent market signal"),
            "content": str(payload["content"]),
            "tags": json.dumps(payload.get("tags") or [], ensure_ascii=False),
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "created_at": created_at,
            "accepted_reply_id": None,
        }
        cursor.execute("INSERT INTO agents (id, name) VALUES (1, 'Open Stock AI Agent')")
        cursor.execute(
            """
            INSERT INTO signals
            (id, signal_id, agent_id, message_type, market, symbol, symbols,
             title, content, tags, timestamp, created_at, accepted_reply_id)
            VALUES (1, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal["message_type"], signal["market"], signal["symbol"], signal["symbols"],
                signal["title"], signal["content"], signal["tags"], signal["timestamp"],
                signal["created_at"], signal["accepted_reply_id"],
            ),
        )
        quality = module.score_signal_quality(signal, cursor=cursor)
        cursor.execute("SELECT * FROM signal_predictions WHERE signal_id = 1 ORDER BY id DESC LIMIT 1")
        prediction = dict(cursor.fetchone())
        for item in (quality, prediction):
            for key in ("metadata_json", "evidence_json"):
                if item.get(key) and isinstance(item[key], str):
                    item[key] = json.loads(item[key])
        result = {"prediction": prediction, "quality": quality}
        connection.close()
    else:
        raise ValueError(f"Unsupported external action: {project}.{action}")
    return {
        "executed_module": relative,
        "executed_function": function,
        "result": _jsonable(result),
    }


def _finite_or_none(value: Any) -> float | None:
    number = float(value)
    return number if math.isfinite(number) else None


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        return _jsonable(value.item())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: external_runtime_worker PROJECT ACTION")
    payload = json.loads(sys.stdin.read() or "{}")
    result = _run(sys.argv[1], sys.argv[2], payload)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
