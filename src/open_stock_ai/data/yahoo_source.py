from __future__ import annotations

from dataclasses import dataclass
from typing import Any


from .data_quality import source_envelope


@dataclass
class YahooSource:
    name = "Yahoo Finance"

    def normalize(self, symbol: str, market: str, payload: Any) -> dict[str, Any]:
        return source_envelope(
            source_key="yahoo",
            source_name=self.name,
            symbol=symbol,
            market=market,
            role="market_price_history_news",
            payload=payload,
        )
