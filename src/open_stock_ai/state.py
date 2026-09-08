from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PipelineState:
    request_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
