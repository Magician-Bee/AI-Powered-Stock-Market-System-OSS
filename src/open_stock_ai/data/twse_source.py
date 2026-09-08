from __future__ import annotations

from dataclasses import dataclass
from typing import Any


from .data_quality import source_envelope


@dataclass
class TWSESource:
    name = "TWSE"

    def normalize(self, symbol: str, market: str, payload: Any) -> dict[str, Any]:
        return source_envelope(
            source_key="twse",
            source_name=self.name,
            symbol=symbol,
            market=market,
            role="official_market_data",
            payload=payload,
        )
