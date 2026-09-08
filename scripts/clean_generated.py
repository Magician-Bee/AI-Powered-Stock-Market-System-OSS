"""Remove reproducible project outputs without deleting account databases or runtimes."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATED_DIRS = (
    PROJECT_ROOT / ".playwright-cli",
    PROJECT_ROOT / "output" / "backtests",
    PROJECT_ROOT / "output" / "reports",
    PROJECT_ROOT / "output" / "playwright",
    PROJECT_ROOT / "output" / "debug",
    PROJECT_ROOT / "artifacts",
)
PRESERVED_NAMES = {".gitkeep"}


def candidates() -> list[Path]:
    items: list[Path] = []
    for directory in GENERATED_DIRS:
        if not directory.exists():
            continue
        items.extend(
            path
            for path in directory.iterdir()
            if path.name not in PRESERVED_NAMES
        )
    return sorted(items)


def remove(items: list[Path]) -> None:
    for path in items:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List or remove generated reports, backtests and browser artifacts."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually remove the listed paths. Without this flag the command is read-only.",
    )
    args = parser.parse_args()
    items = candidates()
    for path in items:
        print(path.relative_to(PROJECT_ROOT))
    if args.apply:
        remove(items)
        print(f"Removed {len(items)} generated path(s).")
    else:
        print(f"Dry run: {len(items)} generated path(s). Add --apply to remove them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
