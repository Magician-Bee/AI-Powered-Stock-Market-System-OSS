from __future__ import annotations

from dataclasses import dataclass
from typing import Any


POSITIVE_TERMS = {
    "beat",
    "beats",
    "growth",
    "raise",
    "upgrade",
    "rally",
    "strong",
    "surge",
    "record",
    "profit",
    "partnership",
    "order",
    "buy",
    "bullish",
    "年增",
    "成長",
    "買超",
    "創高",
    "調升",
    "合作",
    "接單",
    "強勁",
}

NEGATIVE_TERMS = {
    "miss",
    "decline",
    "drop",
    "downgrade",
    "selloff",
    "weak",
    "warning",
    "risk",
    "pressure",
    "lawsuit",
    "cut",
    "bearish",
    "衰退",
    "下滑",
    "賣超",
    "警告",
    "風險",
    "壓力",
    "降評",
    "訴訟",
}


@dataclass
class SentimentIntelligence:
    """Lightweight FinGPT-inspired sentiment adapter inside Open Stock AI."""

    def analyze(self, news: list[dict[str, Any]]) -> dict[str, Any]:
        scored = [self.score_item(item) for item in news]
        if not scored:
            return {
                "sentiment_score": 0.0,
                "sentiment_label": "neutral",
                "risks": ["No news available for sentiment scoring."],
                "evidence": [],
                "coverage": 0,
            }
        score = round(sum(item["score"] for item in scored) / len(scored), 3)
        return {
            "sentiment_score": score,
            "sentiment_label": self.label(score),
            "risks": self.risk_terms(scored),
            "evidence": [item["source"] for item in sorted(scored, key=lambda row: abs(row["score"]), reverse=True)[:3]],
            "coverage": len(scored),
            "scored_news": scored[:8],
        }

    def score_item(self, item: dict[str, Any]) -> dict[str, Any]:
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        positives = sorted(term for term in POSITIVE_TERMS if term.lower() in text)
        negatives = sorted(term for term in NEGATIVE_TERMS if term.lower() in text)
        score = 0.0
        if positives or negatives:
            score = (len(positives) - len(negatives)) / max(len(positives) + len(negatives), 1)
        return {
            "score": round(score, 3),
            "positive_terms": positives,
            "negative_terms": negatives,
            "source": item,
        }

    def label(self, score: float) -> str:
        if score >= 0.2:
            return "bullish"
        if score <= -0.2:
            return "bearish"
        return "neutral"

    def risk_terms(self, scored: list[dict[str, Any]]) -> list[str]:
        terms = sorted({term for item in scored for term in item["negative_terms"]})
        return [f"Negative news keyword: {term}" for term in terms[:5]]
