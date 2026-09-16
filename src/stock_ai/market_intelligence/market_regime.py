from __future__ import annotations

from collections import defaultdict

from .contracts import MarketRegime


def classify_market_regime(features: list[dict]) -> MarketRegime:
    valid = [item for item in features if item.get("data_quality", {}).get("status") == "ready"]
    if not valid:
        return MarketRegime(
            label="data_insufficient",
            summary="官方市場報價尚未形成可判讀的市場寬度。",
            risk_level="unknown",
        )
    advancers = sum(1 for item in valid if float(item.get("change_percent") or 0) > 0.05)
    decliners = sum(1 for item in valid if float(item.get("change_percent") or 0) < -0.05)
    unchanged = max(0, len(valid) - advancers - decliners)
    breadth = round((advancers - decliners) / max(1, len(valid)) * 100, 2)
    if breadth >= 20:
        label, risk = "bullish", "low"
    elif breadth >= 7:
        label, risk = "neutral_bullish", "medium"
    elif breadth <= -20:
        label, risk = "bearish", "high"
    elif breadth <= -7:
        label, risk = "neutral_weak", "high"
    else:
        label, risk = "neutral", "medium"

    industries: dict[str, list[float]] = defaultdict(list)
    for item in valid:
        industry = str(item.get("industry") or "未分類")
        industries[industry].append(float(item.get("change_percent") or 0))
    ranked = sorted(
        (
            (round(sum(values) / len(values), 3), industry)
            for industry, values in industries.items()
            if len(values) >= 2
        ),
        reverse=True,
    )
    return MarketRegime(
        label=label,
        summary=(
            f"上漲 {advancers} 檔、下跌 {decliners} 檔、平盤 {unchanged} 檔；"
            f"市場寬度 {breadth:+.2f}%。"
        ),
        risk_level=risk,
        breadth_percent=breadth,
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        leading_industries=[industry for _score, industry in ranked[:5]],
        weak_industries=[industry for _score, industry in ranked[-5:]],
    )
