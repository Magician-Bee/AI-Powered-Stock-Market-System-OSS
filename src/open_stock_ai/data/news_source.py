from __future__ import annotations

from dataclasses import dataclass
from typing import Any


from .data_quality import source_envelope


@dataclass
class NewsSource:
    name = "News"

    def normalize(self, symbol: str, market: str, payload: Any) -> dict[str, Any]:
        return source_envelope(
            source_key="news",
            source_name=self.name,
            symbol=symbol,
            market=market,
            role="news_events",
            payload=payload,
        )
