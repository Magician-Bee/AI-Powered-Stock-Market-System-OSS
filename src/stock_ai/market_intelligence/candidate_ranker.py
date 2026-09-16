from __future__ import annotations

from math import log10
from typing import Any

from .contracts import CandidateDetail, DataQuality
from .portfolio_fit import portfolio_action
from .risk_overlay import deterministic_risk
from .product_projection import product_assessment


def _market_category(category: str) -> str:
    if category == "actionable_now":
        return "BUY_NOW"
    if category in {"near_actionable", "wait_for_breakout"}:
        return "WATCH"
    if category == "wait_for_pullback":
        return "FUTURE_BUY"
    if category == "insufficient_data":
        return "INSUFFICIENT_DATA"
    return "AVOID_NOW"


def _factor_scores(feature: dict[str, Any], score: float, market_fit: str) -> dict[str, dict[str, Any]]:
    change = float(feature.get("change_percent") or 0)
    range_position = float(feature.get("range_position") or 0.5)
    trade_value = float(feature.get("trade_value") or 0)
    liquidity = min(100.0, max(0.0, (log10(max(1.0, trade_value)) - 6) * 25))
    available = {"value": None, "status": "unavailable", "source": "not_provided"}
    scores = {key: dict(available) for key in (
        "trend_score", "momentum_score", "liquidity_score", "chip_score", "fundamental_score",
        "valuation_score", "event_score", "relative_strength_score", "market_fit_score",
        "data_quality_score", "risk_penalty",
    )}
    scores.update({
        "trend_score": {"value": round(max(0.0, min(100.0, 50 + change * 4)), 2), "status": "partial", "source": "official_daily_quote"},
        "momentum_score": {"value": round(max(0.0, min(100.0, 50 + change * 6)), 2), "status": "partial", "source": "official_daily_quote"},
        "liquidity_score": {"value": round(liquidity, 2), "status": "ready", "source": "official_daily_quote"},
        "market_fit_score": {"value": {"bullish": 85, "neutral_bullish": 70, "neutral": 55, "neutral_weak": 35, "bearish": 20}.get(market_fit, None), "status": "ready", "source": "market_regime"},
        "data_quality_score": {"value": round(float((feature.get("data_quality") or {}).get("score") or 0) * 100, 2), "status": "ready", "source": "data_quality_contract"},
        "risk_penalty": {"value": round(max(0.0, min(100.0, abs(change) * 8 + (1 - range_position) * 20)), 2), "status": "partial", "source": "deterministic_risk"},
    })
    for name, payload in dict(feature.get("scanner_factor_inputs") or {}).items():
        key = f"{name}_score"
        if key not in scores or not isinstance(payload, dict):
            continue
        value = payload.get("value")
        scores[key] = {
            "value": round(float(value), 2) if value is not None else None,
            "status": str(payload.get("status") or "unavailable"),
            "source": str(payload.get("source") or "not_provided"),
            "revision_id": payload.get("revision_id"),
            "available_at": payload.get("available_at"),
            "historical_pit_eligible": payload.get("historical_pit_eligible") is True,
            "reason": payload.get("reason"),
        }
    return scores


def _round_price(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 4 if value < 100 else 2)


def _score(feature: dict[str, Any]) -> float:
    change = float(feature.get("change_percent") or 0)
    range_position = float(feature.get("range_position") or 0.5)
    trade_value = max(1.0, float(feature.get("trade_value") or 0))
    liquidity = min(25.0, max(0.0, (log10(trade_value) - 6) * 10))
    momentum = min(30.0, max(0.0, 15 + change * 3))
    close_strength = min(25.0, max(0.0, range_position * 25))
    quality = float((feature.get("data_quality") or {}).get("score") or 0) * 20
    quote_score = min(100.0, liquidity + momentum + close_strength + quality)
    enriched = [
        float(item["value"])
        for item in dict(feature.get("scanner_factor_inputs") or {}).values()
        if isinstance(item, dict)
        and item.get("status") in {"ready", "partial"}
        and item.get("value") is not None
    ]
    # Optional warehouse factors are disclosed operational inputs, not a
    # certified strategy. Quote-only ranking remains unchanged when there are
    # no eligible rows, and no individual partial domain can dominate ranking.
    return round(quote_score if not enriched else quote_score * 0.7 + sum(enriched) / len(enriched) * 0.3, 3)


def _market_candidate(
    feature: dict[str, Any],
    *,
    score: float,
    liquidity_cutoff: float,
    market_regime: str = "neutral",
) -> tuple[str, dict[str, Any]]:
    quality = feature.get("data_quality") or {}
    close = feature.get("close")
    change = float(feature.get("change_percent") or 0)
    range_position = float(feature.get("range_position") or 0.5)
    trade_value = float(feature.get("trade_value") or 0)
    risk = deterministic_risk(feature)
    if quality.get("status") == "insufficient" or close is None:
        return "insufficient_data", {
            "label": "資料不足",
            "reason": "缺少可驗證的官方價格或成交量，不進入可執行候選。",
            "trigger": "官方報價補齊價格、成交量與資料日期後重新掃描",
            "invalidation": "資料未補齊前維持不可執行",
        }
    if quality.get("status") == "conflict":
        return "high_risk", {
            "label": "資料衝突",
            "reason": "獨立來源的同日觀測與主要報價衝突，不可列為可執行候選。",
            "trigger": "來源衝突釐清並產生新的品質收據後重新掃描",
            "invalidation": "品質收據仍為 conflict 時維持不可執行",
        }
    if trade_value < 10_000_000 or abs(change) >= 8:
        return "high_risk", {
            "label": "高風險",
            "reason": "流動性或單日波動觸發 Host 高風險門檻。",
            "trigger": "流動性與波動回到 Host 允許範圍後重新評估",
            "invalidation": "風控阻擋未解除前不可列為可執行",
        }
    if change <= -3 or range_position <= 0.18:
        return "avoid_now", {
            "label": "暫不進場",
            "reason": "當日趨勢轉弱或收盤位置接近日內低檔。",
            "trigger": "價格重新站回日內區間中值且跌勢停止",
            "invalidation": "續破當日低點時維持排除",
        }
    if change >= 5:
        return "wait_for_pullback", {
            "label": "等待回檔",
            "reason": "短線漲幅已偏離合理追價區間，避免把追高寫成買點。",
            "trigger": "回測突破區或短線漲幅收斂後量價仍健康",
            "invalidation": "回檔跌破當日低點或量價結構轉弱",
        }
    if quality.get("status") != "ready":
        observation = dict(quality.get("source_observation") or {})
        observation_reason = str(
            observation.get("reason") or "cross_source_observation_not_available"
        )
        return "near_actionable", {
            "label": "待交叉驗證",
            "reason": (
                "主要來源的價格與流動性可供研究，但尚未取得獨立同日行情觀測；"
                "不得將單一來源掃描列為立即可執行。"
            ),
            "trigger": f"取得一致的獨立同日報價後重新驗證（{observation_reason}）",
            "invalidation": "品質收據未升為 ready 前維持觀察，不建立新的部位",
        }
    regime_floor = {"bearish": 86, "neutral_weak": 82, "neutral": 78, "neutral_bullish": 75, "bullish": 72}.get(market_regime, 82)
    if (
        score >= regime_floor
        and 0.5 <= change <= 4.5
        and range_position >= (0.8 if market_regime in {"bearish", "neutral_weak"} else 0.72)
        and trade_value >= liquidity_cutoff
        and risk["status"] == "passed"
    ):
        return "actionable_now", {
            "label": "現在可執行",
            "reason": "價格動能、收盤位置、流動性、資料品質與 Host 風控均通過。",
            "trigger": "維持在突破區上方且成交量未明顯衰退",
            "invalidation": "跌破當日低點或 Host 風控狀態改為 blocked",
        }
    if score >= 67 and change > 0 and range_position >= 0.58:
        return "near_actionable", {
            "label": "接近條件",
            "reason": "量價與流動性接近門檻，但尚未同時通過完整進場條件。",
            "trigger": "放量突破當日高點並維持風控通過",
            "invalidation": "跌回日內區間下半部",
        }
    return "wait_for_breakout", {
        "label": "等待突破",
        "reason": "目前未達立即進場門檻，等待價格與量能共同確認。",
        "trigger": "突破當日高點並取得足夠成交金額",
        "invalidation": "跌破當日低點或資料品質下降",
    }


def rank_candidates(
    features: list[dict[str, Any]],
    *,
    positions: dict[str, dict[str, Any]] | None = None,
    previous_ranks: dict[str, int] | None = None,
    market_regime: str = "neutral",
) -> tuple[dict[str, CandidateDetail], dict[str, list[str]], dict[str, list[str]], dict[str, Any]]:
    positions = positions or {}
    previous_ranks = previous_ranks or {}
    trade_values = sorted(
        float(item.get("trade_value") or 0)
        for item in features
        if (item.get("data_quality") or {}).get("status") == "ready"
        and product_assessment(item)["allowed"]
    )
    liquidity_cutoff = (
        trade_values[int((len(trade_values) - 1) * 0.70)]
        if trade_values
        else 50_000_000
    )
    provisional: list[tuple[float, dict[str, Any], str, dict[str, Any], dict[str, Any]]] = []
    for feature in features:
        score = _score(feature)
        symbol = str(feature["symbol"]).upper()
        risk = deterministic_risk(feature)
        product = product_assessment(feature)
        if not product["allowed"]:
            risk = {**risk, "status": "blocked", "flags": [*risk["flags"], *product["reasons"]]}
        if symbol in positions:
            action, label, reason = portfolio_action(feature, positions[symbol])
            # Reducing an existing position remains a defensive action, but a
            # single-source / partial quote must never trigger new exposure.
            if (
                action == "add"
                and (str((feature.get("data_quality") or {}).get("status") or "") != "ready" or not product["allowed"])
            ):
                action, label, reason = (
                    "hold",
                    "持有／待驗證",
                    "商品新進場資格或獨立同日報價尚未通過；暫停加碼，保留既有部位監控。",
                )
            category = action
            explanation = {
                "label": label,
                "reason": reason,
                "trigger": "部位、價格、集中度或市場風險條件更新時重估",
                "invalidation": "Paper OMS 部位歸零後移出持倉行動",
            }
        else:
            category, explanation = _market_candidate(
                feature,
                score=score,
                liquidity_cutoff=liquidity_cutoff,
                market_regime=market_regime,
            )
            if not product["allowed"]:
                category = "avoid_now" if product.get("classification_verified") else "insufficient_data"
                explanation = {"label": "研究保留／新進場受限", "reason": ";".join(product["reasons"]),
                               "trigger": "官方商品分類、生命週期或授權商品範圍更新後重估",
                               "invalidation": "商品資格通過前不增加部位；研究與既有持倉管理保持可見"}
        provisional.append((score, feature, category, explanation, risk))
    provisional.sort(key=lambda item: (-item[0], item[1]["symbol"]))

    details: dict[str, CandidateDetail] = {}
    rankings: dict[str, list[str]] = {
        category: []
        for category in (
            "actionable_now",
            "near_actionable",
            "wait_for_pullback",
            "wait_for_breakout",
            "avoid_now",
            "high_risk",
            "insufficient_data",
        )
    }
    portfolio_actions: dict[str, list[str]] = {
        "hold": [],
        "add": [],
        "reduce": [],
        "exit": [],
    }
    for rank, (score, feature, category, explanation, risk) in enumerate(
        provisional, start=1
    ):
        symbol = str(feature["symbol"]).upper()
        close = feature.get("close")
        high = feature.get("high")
        low = feature.get("low")
        is_position = symbol in positions
        if is_position:
            portfolio_actions[category].append(symbol)
        else:
            rankings[category].append(symbol)
        previous_rank = previous_ranks.get(symbol)
        details[symbol] = CandidateDetail(
            symbol=symbol,
            entity_id=str(feature.get("entity_id") or symbol),
            name=str(feature.get("name") or symbol),
            exchange=str(feature.get("exchange") or ""),
            industry=feature.get("industry"),
            is_etf=bool(feature.get("is_etf")),
            is_warrant=bool(feature.get("is_warrant")),
            is_managed_stock=bool(feature.get("is_managed_stock")),
            is_special_security=bool(feature.get("is_special_security")),
            product_classification=dict(feature.get("product_classification") or {}),
            lifecycle_status=feature.get("lifecycle_status"),
            product_entry_assessment=product_assessment(feature),
            category=category,
            market_category=_market_category(category),
            portfolio_action=category if is_position else None,
            rank=rank,
            previous_rank=previous_rank,
            rank_change=(previous_rank - rank) if previous_rank is not None else None,
            score=score,
            latest_price=close,
            change_percent=feature.get("change_percent"),
            volume=feature.get("volume"),
            trade_value=feature.get("trade_value"),
            range_position=feature.get("range_position"),
            decision_label=explanation["label"],
            primary_reason=explanation["reason"],
            positive_reasons=[
                f"規則分數 {score:.3f}",
                f"收盤位於日內區間 {float(feature.get('range_position') or 0.5) * 100:.1f}%",
            ],
            contrary_reasons=list(risk["flags"]),
            trigger=explanation["trigger"],
            trigger_price=_round_price(high),
            trigger_event="官方行情、量能或事件更新",
            invalidation=explanation["invalidation"],
            invalidation_price=_round_price(low),
            next_review="下一次價格、成交量、法人、財務或市場 Regime 事件",
            distance_to_trigger_percent=(
                round((float(high) - float(close)) / float(close) * 100, 3)
                if high not in (None, 0) and close not in (None, 0)
                else None
            ),
            risk_level=risk["risk_level"],
            liquidity_status=(
                "pass"
                if float(feature.get("trade_value") or 0) >= liquidity_cutoff
                else "limited"
                if float(feature.get("trade_value") or 0) >= 10_000_000
                else "blocked"
            ),
            host_risk_status=risk["status"],
            portfolio_fit="held" if is_position else "not_held",
            market_fit=market_regime if market_regime in {"bullish", "neutral_bullish", "neutral", "neutral_weak", "bearish"} else "unknown",
            is_position=is_position,
            position_weight_percent=(
                positions[symbol].get("weight_percent") if is_position else None
            ),
            data_quality=DataQuality.model_validate(feature["data_quality"]),
            evidence=feature.get("evidence") or [],
            factor_scores=_factor_scores(feature, score, market_regime),
            model_status="not_analyzed",
        )
    statistics = {
        "liquidity_cutoff_trade_value": round(liquidity_cutoff, 2),
        "classified_count": len(details),
        "category_counts": {
            **{key: len(value) for key, value in rankings.items()},
            **{key: len(value) for key, value in portfolio_actions.items()},
        },
        "method": "deterministic_candidate_ranker.v1",
        "llm_symbol_scan_count": 0,
    }
    return details, rankings, portfolio_actions, statistics
