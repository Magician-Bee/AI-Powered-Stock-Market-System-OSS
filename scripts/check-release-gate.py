#!/usr/bin/env python3
"""Run the machine-readable production release gate."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.governance.release_gate import main


if __name__ == "__main__":
    raise SystemExit(main())
