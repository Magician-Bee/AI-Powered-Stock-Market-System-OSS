#!/usr/bin/env python3
"""Render the README capability-status block from the authoritative YAML."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_stock_ai.governance import render_readme_capability_status


README = ROOT / "README.md"
START = "<!-- capability-status:start -->"
END = "<!-- capability-status:end -->"


def render(text: str) -> str:
    start = text.index(START)
    end = text.index(END, start) + len(END)
    return text[:start] + render_readme_capability_status() + text[end:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    current = README.read_text(encoding="utf-8")
    rendered = render(current)
    if args.check:
        if current != rendered:
            raise SystemExit("README capability status is not generated from config/capability_status.yaml")
        return 0
    README.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
