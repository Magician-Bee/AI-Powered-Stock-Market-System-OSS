from typing import Any

__all__ = ["IntelligenceHub"]


def __getattr__(name: str) -> Any:
    if name == "IntelligenceHub":
        from .intelligence_hub import IntelligenceHub

        return IntelligenceHub
    raise AttributeError(name)
