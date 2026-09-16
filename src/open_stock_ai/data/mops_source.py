from __future__ import annotations

from dataclasses import dataclass
from typing import Any


from .data_quality import source_envelope


@dataclass
class MOPSSource:
    name = "MOPS"

    def normalize(self, symbol: str, market: str, payload: Any) -> dict[str, Any]:
        return source_envelope(
            source_key="mops",
            source_name=self.name,
            symbol=symbol,
            market=market,
            role="official_announcements",
            payload=payload,
        )
