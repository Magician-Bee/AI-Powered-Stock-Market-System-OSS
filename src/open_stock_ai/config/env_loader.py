from __future__ import annotations

import os
import re
from pathlib import Path


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_project_env(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load a small, predictable subset of dotenv syntax into ``os.environ``.

    Existing process variables win by default. This keeps shell/CI secrets higher
    priority than the local file while making the launcher's generated ``.env``
    effective for OpenStockAI, Paper OMS and local-model settings.
    """
    configured = path or os.getenv("OPEN_STOCK_AI_ENV_FILE")
    env_path = Path(configured).expanduser() if configured else project_root() / ".env"
    if not env_path.is_absolute():
        env_path = project_root() / env_path
    if not env_path.is_file():
        return {}

    loaded: dict[str, str] = {}
    text = env_path.read_text(encoding="utf-8-sig", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key):
            continue
        value = _parse_value(raw_value)
        if override or key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def _parse_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        quote = value[0]
        value = value[1:-1]
        if quote == '"':
            value = (
                value.replace(r"\n", "\n")
                .replace(r"\r", "\r")
                .replace(r"\t", "\t")
                .replace(r'\"', '"')
                .replace(r"\\", chr(92))
            )
        return value
    # Treat a whitespace-prefixed # as an inline comment, but preserve URLs and
    # tokens that legitimately contain # without preceding whitespace.
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
