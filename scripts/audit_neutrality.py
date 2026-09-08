from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "audits" / "neutrality"
PRODUCTION_ROOTS = (ROOT / "src", ROOT / "config")
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".py", ".yaml", ".yml"}
IGNORED_PARTS = {"__pycache__", "vendor"}

STOCK_PATTERN = re.compile(
    r"2330(?:\.TW)?|0050(?:\.TW)?|2317(?:\.TW)?|2382(?:\.TW)?|2454(?:\.TW)?|"
    r"2308(?:\.TW)?|2881(?:\.TW)?|2882(?:\.TW)?|2412(?:\.TW)?|1303(?:\.TW)?|"
    r"台積電|元大台灣50",
    re.IGNORECASE,
)
UNIVERSE_PATTERN = re.compile(
    r"preferred|default_watchlist|watchlist|fallback_symbol|batch_requests|universe",
    re.IGNORECASE,
)
AI_LABEL_PATTERN = re.compile(
    r"AI 每日選股|AI 判斷|Agent Radar|AI 信心|AI 目標價|AI 停損價|"
    r"AI 建議|AI 交易建議|AI Trade Advice|AI 綜合分析|模型判斷|模型排序|模型解讀",
    re.IGNORECASE,
)
FORMULA_PATTERN = re.compile(
    r"confidence\s*=|target_price\s*=|stop_loss\s*=|_score_to_confidence|"
    r"fixed_stop_loss|\b0\.85\b|\b0\.55\b|\b0\.65\b|\b1\.08\b|\b0\.92\b"
)
MODEL_CALL_PATTERN = re.compile(
    r"\.decide\(|\.responses\.create|chat\.completions\.create|codex_runtime\.run|"
    r"/v1/chat/completions|/api/chat|invoke_model|model_call",
    re.IGNORECASE,
)
SILENT_FALLBACK_PATTERN = re.compile(
    r"except\s+Exception(?:\s+as\s+\w+)?\s*:|\bpass\b|fallback",
    re.IGNORECASE,
)
ROUTE_PATTERN = re.compile(
    r"@(?:app|router)\.(?:get|post|put|delete|patch)\(|include_router\("
)


@dataclass(frozen=True)
class Match:
    path: str
    line: int
    text: str

    def markdown(self) -> str:
        return f"- `{self.path}:{self.line}` — `{self.text.strip()}`"

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "text": self.text.strip()}


def production_files() -> Iterable[Path]:
    for root in PRODUCTION_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if (
                path.is_file()
                and path.suffix.lower() in TEXT_SUFFIXES
                and not IGNORED_PARTS.intersection(path.parts)
            ):
                yield path


def scan(pattern: re.Pattern[str]) -> list[Match]:
    matches: list[Match] = []
    for path in production_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if pattern.search(line):
                matches.append(
                    Match(
                        path=path.relative_to(ROOT).as_posix(),
                        line=line_number,
                        text=line.strip(),
                    )
                )
    return matches


def git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def stock_classification(match: Match) -> tuple[str, str]:
    path = match.path
    text = match.text.casefold()
    if path.startswith("src/stock_ai/ui/static/i18n/") or (
        path.endswith(("index.html", "preferences.js", "explorer.js"))
        and any(term in text for term in ("例如", "e.g.", "example", "placeholder"))
    ):
        return "example_copy", "User-facing example only; allowed after defaults are removed."
    if path in {"src/stock_ai/query.py", "src/stock_ai/realtime_data.py"}:
        return "alias_mapping", "Explicit symbol/name normalization; allowed if never selected implicitly."
    if path == "src/stock_ai/services.py" and re.match(r'^".+"\s*:\s*"\d{4}"', match.text):
        return "alias_mapping", "Explicit company-name normalization; allowed if never selected implicitly."
    if path == "src/stock_ai/services.py" and (
        "company_aliases" in text or "security_seed" in text or re.match(r'^[\"“]?\d{4}', text)
    ):
        return "reference_metadata", "Security metadata may remain if it is not a default Universe."
    return "production_bias", "Runtime/default/fallback/demo data in Production requires removal or neutralization."


def write_stock_audit(matches: list[Match], output: Path, generated_at: str) -> None:
    items = []
    counts: dict[str, int] = {}
    for match in matches:
        category, rationale = stock_classification(match)
        counts[category] = counts.get(category, 0) + 1
        items.append(
            {
                **match.to_dict(),
                "classification": category,
                "rationale": rationale,
            }
        )
    payload = {
        "schema_version": "stock_ai.stock_bias_audit.v1",
        "baseline_commit": git_head(),
        "generated_at": generated_at,
        "scope": ["src", "config"],
        "counts": counts,
        "items": items,
    }
    (output / "stock-bias-audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def markdown_inventory(
    *,
    title: str,
    purpose: str,
    matches: list[Match],
    generated_at: str,
    notes: list[str],
) -> str:
    rows = "\n".join(match.markdown() for match in matches) or "- No matches."
    note_rows = "\n".join(f"- {note}" for note in notes)
    return (
        f"# {title}\n\n"
        f"- Baseline commit: `{git_head()}`\n"
        f"- Generated at: `{generated_at}`\n"
        f"- Scope: Production `src/` and `config/`; vendored UI files excluded.\n"
        f"- Matches: **{len(matches)}**\n\n"
        f"## Purpose\n\n{purpose}\n\n"
        f"## Classification notes\n\n{note_rows}\n\n"
        f"## Inventory\n\n{rows}\n"
    )


def formula_method_id(match: Match) -> str:
    """Assign every retained heuristic to an auditable stable method family."""

    path = match.path
    mappings = (
        ("agent_runtime/routing.py", "host.multi_intent_hint_weights.v1"),
        ("agent_runtime/orchestrator.py", "host.provider_decision_schema_validation.v1"),
        ("agent_workspace.py", "open_stock_ai.legacy_workspace_projection.v1"),
        ("fingpt_source.py", "external.fingpt_deterministic_forecast_projection.v1"),
        ("finrobot_source.py", "external.finrobot_rule_valuation_projection.v1"),
        ("portfolio_attribution.py", "research.legacy_confidence_attribution_projection.v1"),
        ("portfolio_construction.py", "research.risk_adjusted_priority_projection.v1"),
        ("risk/stop_loss.py", "risk.fixed_stop_reference.v1"),
        ("strategy/strategy_engine.py", "strategy.weighted_rule_score.v2"),
        ("strategy/structured_decision.py", "strategy.fixed_reference_multiplier.v1"),
        ("codex_market.py", "stock_ai.rule_radar_wait_window.v1"),
        ("mvp_features.py", "stock_ai.notification_priority_rule.v1"),
        ("official_events.py", "stock_ai.official_event_match_score.v1"),
        ("realtime_data.py", "stock_ai.entity_match_score.v1"),
        ("services.py", "stock_ai.deterministic_service_rules.v1"),
        ("agent-runtime.js", "ui.model_self_reported_confidence_render.v1"),
        ("codex.js", "ui.rule_reference_range_render.v1"),
        ("quant-research.js", "ui.quant_rule_signal_render.v1"),
    )
    for suffix, method_id in mappings:
        if suffix in path:
            return method_id
    return f"audit.retained_formula.{path.replace('/', '.').replace('-', '_')}.v1"


def write_formula_audit(matches: list[Match], output: Path, generated_at: str) -> None:
    rows = "\n".join(
        f"| `{formula_method_id(match)}` | `{match.path}:{match.line}` | `{match.text.strip()}` |"
        for match in matches
    ) or "| — | — | No matches |"
    content = (
        "# Heuristic Formula Audit\n\n"
        f"- Baseline commit: `{git_head()}`\n"
        f"- Generated at: `{generated_at}`\n"
        "- Scope: Production `src/` and `config/`; vendored UI files excluded.\n"
        f"- Matches: **{len(matches)}**\n"
        f"- Formulas with Method ID: **{len(matches)}**\n\n"
        "Every retained rule or compatibility projection below has a stable Method ID. "
        "These IDs describe deterministic methods; they do not claim model inference or calibrated probability.\n\n"
        "| Method ID | Location | Formula / field |\n"
        "|---|---|---|\n"
        f"{rows}\n"
    )
    (output / "heuristic-formula-audit.md").write_text(content, encoding="utf-8")


def write_context_matrix(output: Path, generated_at: str) -> None:
    content = f"""# Context Exposure Matrix

- Baseline commit: `{git_head()}`
- Generated at: `{generated_at}`
- Status: normative target plus confirmed baseline violations.

| Task kind | Allowed by default | Forbidden by default | Confirmed baseline risk |
|---|---|---|---|
| General | prompt, necessary conversation history | market, portfolio, Git, trading | keyword router and UI state can attach a stock symbol |
| Project | repository, Git, project tools | paper account, stock data | Host classifier is single-label and context is assembled centrally |
| Market information | explicit SymbolContext, read-only market tools | project files, execution | symbol fallback may inject `2330.TW` |
| Market decision | explicit Universe/Symbol, portfolio, risk | project tools | rule confidence and target/stop are produced before a model call |
| UI | UI state and UI tools | stock context without a referential phrase | selected symbol is promoted to active intent |
| Current information | current/web tools | paper account, project files | broad tool categories can be exposed through Host classification |

## Required controls

- Represent intent as multiple hypotheses with negative constraints.
- Represent every symbol as `SymbolContext`; `source=none` must remain valid.
- Treat UI selection as candidate context until the user refers to it.
- Move context selection into an auditable Context Broker.
- Record the context fields and tool capabilities exposed to each model invocation.
"""
    (output / "context-exposure-matrix.md").write_text(content, encoding="utf-8")


def write_runtime_map(matches: list[Match], output: Path, generated_at: str) -> None:
    content = markdown_inventory(
        title="Runtime Entrypoint Map",
        purpose=(
            "Inventory of HTTP entrypoints and router composition. The target architecture must route "
            "`/api/query`, home radar, and Agent runs through the unified driver registry. Native Codex "
            "routes may remain developer-only."
        ),
        matches=matches,
        generated_at=generated_at,
        notes=[
            "`/api/codex/market-radar` is a confirmed legacy production path.",
            "`/api/agents/runs` is the durable interoperable runtime.",
            "Every market-analysis entrypoint must require an explicit Universe or return an empty state.",
        ],
    )
    (output / "runtime-entrypoint-map.md").write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit stock neutrality and truthful AI provenance.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()

    stock_matches = scan(STOCK_PATTERN)
    universe_matches = scan(UNIVERSE_PATTERN)
    ai_matches = scan(AI_LABEL_PATTERN)
    formula_matches = scan(FORMULA_PATTERN)
    model_matches = scan(MODEL_CALL_PATTERN)
    fallback_matches = scan(SILENT_FALLBACK_PATTERN)
    route_matches = scan(ROUTE_PATTERN)

    write_stock_audit(stock_matches, output, generated_at)
    inventories = (
        (
            "universe-audit.md",
            "Universe and Watchlist Audit",
            "Find fixed lists, implicit watchlists, batch defaults, and fallback symbol selection.",
            universe_matches,
            [
                "Names used only for generic collection types are informational.",
                "Any Production path that chooses symbols without an approved Universe source is a defect.",
                "Empty Universe is a valid, required runtime state.",
            ],
        ),
        (
            "ai-label-audit.md",
            "AI Label Audit",
            "Find UI/API labels that could claim AI participation without a successful model invocation receipt.",
            ai_matches,
            [
                "MODEL/HYBRID labels require provider, model ID, call ID, and success status.",
                "Deterministic outputs must use Rule/Quant wording.",
            ],
        ),
        (
            "heuristic-formula-audit.md",
            "Heuristic Formula Audit",
            "Find fixed confidence, target, stop, threshold, and multiplier formulas.",
            formula_matches,
            [
                "Each retained rule requires a stable method/rule-set ID.",
                "Uncalibrated scores may not be presented as probability or AI confidence.",
                "Fixed target/stop multipliers must be identified as rule reference ranges.",
            ],
        ),
        (
            "model-invocation-map.md",
            "Model Invocation Map",
            "Locate actual provider/model invocation boundaries and distinguish them from deterministic adapters.",
            model_matches,
            [
                "HTTP market-data calls are not model invocations.",
                "Every true invocation must emit a durable invocation receipt.",
                "Provider format/protocol failures must be distinct from semantic answer failures.",
            ],
        ),
        (
            "silent-fallback-audit.md",
            "Silent Fallback Audit",
            "Find broad exception handling, pass statements, and fallback paths requiring manual classification.",
            fallback_matches,
            [
                "Abstract-method `pass` and explicitly surfaced data fallback may be valid.",
                "Model exceptions must never preserve or relabel a deterministic answer as AI.",
                "Every retained fallback needs a reason and provenance visible to callers.",
            ],
        ),
    )
    for filename, title, purpose, matches, notes in inventories:
        if filename == "heuristic-formula-audit.md":
            write_formula_audit(matches, output, generated_at)
            continue
        content = markdown_inventory(
            title=title,
            purpose=purpose,
            matches=matches,
            generated_at=generated_at,
            notes=notes,
        )
        (output / filename).write_text(content, encoding="utf-8")
    write_context_matrix(output, generated_at)
    write_runtime_map(route_matches, output, generated_at)

    summary = {
        "output": str(output),
        "baseline_commit": git_head(),
        "stock_matches": len(stock_matches),
        "universe_matches": len(universe_matches),
        "ai_label_matches": len(ai_matches),
        "formula_matches": len(formula_matches),
        "model_call_matches": len(model_matches),
        "silent_fallback_matches": len(fallback_matches),
        "route_matches": len(route_matches),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
