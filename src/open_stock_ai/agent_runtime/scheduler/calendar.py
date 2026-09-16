from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Clock boundary kept separate for deterministic scheduler tests."""
    return datetime.now(timezone.utc)
