from __future__ import annotations

import re
import sys
from pathlib import Path

import audit_neutrality


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN = (
    (
        re.compile(r"""symbol\s*:\s*str\s*=\s*["'][^"']+["']"""),
        "Production symbol parameter has a default value",
    ),
    (
        re.compile(r"""\bor\s*["'](?:2330|0050)(?:\.TW)?["']""", re.IGNORECASE),
        "Production code silently falls back to a fixed stock",
    ),
    (
        re.compile(r"""except\s+Exception(?:\s+as\s+\w+)?\s*:\s*pass\b"""),
        "Production code silently swallows an exception",
    ),
    (
        re.compile(r"""(?:AI 信心|AI 目標價|AI 停損價|AI 判斷完成|AI 每日選股|AI 建議|AI 交易建議|Refresh AI Advice)""", re.IGNORECASE),
        "Production UI contains a prohibited unverified-AI label",
    ),
)


def main() -> int:
    violations: list[str] = []
    for match in audit_neutrality.scan(audit_neutrality.STOCK_PATTERN):
        category, _reason = audit_neutrality.stock_classification(match)
        if category == "production_bias":
            violations.append(
                f"{match.path}:{match.line}: unclassified Production stock constant: {match.text}"
            )

    for path in audit_neutrality.production_files():
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for pattern, message in FORBIDDEN:
            for match in pattern.finditer(text):
                # Negative limitation copy is truthful and is not a result label.
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                line = text[line_start : line_end if line_end >= 0 else len(text)]
                if "不代表 AI 判斷" in line:
                    continue
                line_number = text.count("\n", 0, match.start()) + 1
                violations.append(f"{relative}:{line_number}: {message}: {line.strip()}")

    if violations:
        print("Neutrality CI rejected the following Production regressions:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("Neutrality CI passed: no fixed stock defaults, silent model fallback, or false-AI labels.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
