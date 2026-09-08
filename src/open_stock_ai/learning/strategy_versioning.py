from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class StrategyVersioning:
    @staticmethod
    def hash_files(paths: list[str | Path]) -> str:
        digest = hashlib.sha256()
        for path in sorted(Path(item).resolve() for item in paths):
            digest.update(str(path).encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def hash_data(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
