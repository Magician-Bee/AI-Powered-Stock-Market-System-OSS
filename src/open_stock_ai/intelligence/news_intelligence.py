from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class NewsIntelligence:
    def summarize(self, news: list[dict[str, Any]], limit: int = 3) -> dict[str, Any]:
        items = news[:limit]
        titles = [str(item.get("title") or "").strip() for item in items if item.get("title")]
        official_count = sum(1 for item in news if item.get("official_verified"))
        return {
            "summary": " | ".join(titles) if titles else "No recent news loaded.",
            "top_titles": titles,
            "news_count": len(news),
            "official_verified_count": official_count,
            "evidence": items,
        }
