from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import FileResponse, StreamingResponse

from open_stock_ai.types import UniverseRequest
from open_stock_ai.agent_runtime.completion_contract import objective_completion_contract

from .agent_drivers import (
    discover_openai_compatible_models,
    load_agent_driver_settings,
    public_agent_preferences,
    save_agent_preferences,
)
from .agent_ui_bridge import agent_ui_bridge
from .agent_service import (
    clear_agent_service,
    get_agent_database_maintenance,
    get_operational_alert_runtime,
    get_agent_run_runtime,
    get_agent_service,
)
from .market_radar import (
    MarketRadarValidationError,
    market_radar_ui_payload,
    validate_market_radar_result,
)
from .market_intelligence.deep_analysis import MAX_DEEP_ANALYSIS_SYMBOLS
from .data_platform import get_market_data_platform
from .universe import (
    UniverseResolutionError,
    resolve_universe,
    universe_payload,
    universe_source_options,
)


router = APIRouter(prefix="/api/agents", tags=["Interoperable AI Agents"])
logger = logging.getLogger(__name__)


class AgentRunRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=8000)
    symbols: list[str] = Field(default_factory=list, max_length=20)
    context_scope: Literal["instrument", "market", "neutral"] | None = None
    intent: dict[str, Any] = Field(default_factory=dict)
    driver: Literal["codex", "openai-compatible", "external-agent"] | None = None
    autonomy: Literal["advisory", "project_execute", "paper_execute", "external_execute", "full_execute"] = "advisory"
    max_steps: int = Field(default=6, ge=1, le=12)
    session_id: str | None = Field(default=None, max_length=100)
    parent_run_id: str | None = Field(default=None, max_length=100)
    idempotency_key: str | None = Field(default=None, max_length=200)


class MarketRadarRunRequest(BaseModel):
    universe_source: Literal[
        "user_watchlist",
        "portfolio_positions",
        "explicit_symbols",
        "workflow_parameters",
        "all_twse_active",
        "all_tpex_active",
        "top_by_volume",
        "top_by_market_cap",
        "sector_members",
        "screening_query",
        "tool_discovered",
    ] = "explicit_symbols"
    symbols: list[str] = Field(default_factory=list, max_length=MAX_DEEP_ANALYSIS_SYMBOLS)
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=12, ge=1, le=MAX_DEEP_ANALYSIS_SYMBOLS)
    driver: Literal["codex", "openai-compatible", "external-agent"] | None = None
    max_steps: int = Field(default=6, ge=1, le=12)
    explain: bool = False
    market_snapshot_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=100)


class AgentSettingsUpdate(BaseModel):
    default_driver: Literal["codex", "openai-compatible", "external-agent"] = "codex"
    openai_base_url: str | None = Field(default=None, max_length=2000)
    openai_model: str | None = Field(default=None, max_length=500)
    openai_timeout_seconds: float | None = Field(default=None, ge=5, le=600)
    openai_no_key: bool | None = None
    openai_api_key: str | None = Field(default=None, max_length=1000)
    clear_openai_api_key: bool | None = None
    external_endpoint: str | None = Field(default=None, max_length=2000)
    external_framework: str | None = Field(default=None, max_length=200)
    external_model: str | None = Field(default=None, max_length=500)
    external_token: str | None = Field(default=None, max_length=2000)
    clear_external_token: bool | None = None


class AgentModelDiscoveryRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2000)


class AgentIntentClassificationRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=8000)
    selected_symbol: str | None = Field(default=None, max_length=32)
    driver: Literal["codex", "openai-compatible", "external-agent"] | None = None


class AgentIntentClassification(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    category: Literal[
        "market_analysis",
        "market_decision",
        "instrument_analysis",
        "portfolio",
        "research",
        "system",
        "general",
    ]
    scope: Literal["market", "instrument", "neutral"]
    use_selected_symbol: bool
    symbols: list[str] = Field(default_factory=list, max_length=5)


class ProviderConformanceRequest(BaseModel):
    include_expensive: bool = True


class AgentSessionRequest(BaseModel):
    title: str = Field(default="Stock AI Agent", min_length=1, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentReplanRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)


class AgentSessionMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    artifact_context_selection: dict[str, Any] | None = None
    intent: Literal[
        "answer_agent_question", "append_requirement", "modify_requirement",
        "correct_fact", "soft_steer", "hard_steer", "fork_branch",
        "cancel_branch", "pause_branch", "resume_branch", "modify_artifact",
        "general_question", "new_goal",
    ] | None = None
    target_branch_id: str | None = Field(default=None, max_length=100)
    affected_branch_ids: list[str] = Field(default_factory=list, max_length=32)
    replacement_objective: str | None = Field(default=None, max_length=8000)


class AgentBranchControlRequest(BaseModel):
    reason: str = Field(default="User requested branch control.", max_length=2000)


class AgentInteractionResponse(BaseModel):
    option_id: str | None = Field(default=None, max_length=100)
    free_text: str | None = Field(default=None, max_length=4000)
    approved: bool | None = None


class AgentArtifactSelectionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    artifact_version: int = Field(ge=1)
    target_type: str = Field(default="artifact", min_length=1, max_length=100)
    path: str = Field(default="artifact", min_length=1, max_length=1000)
    branch_id: str | None = Field(default=None, max_length=100)
    node_id: str | None = Field(default=None, max_length=100)
    evidence_id: str | None = Field(default=None, max_length=100)


class AgentArtifactChangeRequest(BaseModel):
    expected_version: int = Field(ge=1)
    content: Any
    reason: str = Field(min_length=1, max_length=2000)
    message_id: str | None = Field(default=None, max_length=100)
    affected_node_ids: list[str] = Field(default_factory=list, max_length=100)


class AgentArtifactRestoreRequest(BaseModel):
    source_version: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class AgentAutomationProposalRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    context: dict[str, Any] = Field(default_factory=dict)


class AgentAutomationActivationRequest(BaseModel):
    intent: dict[str, Any]
    confirmed: bool = False
    external_permission: bool = False
    credential_refs: list[str] = Field(default_factory=list, max_length=20)


class AgentAutomationPatchRequest(BaseModel):
    intent: dict[str, Any] = Field(default_factory=dict)


class AgentContinueRequest(BaseModel):
    additional_steps: int = Field(default=6, ge=1, le=12)
    max_steps: int | None = Field(default=None, ge=2, le=60)


class AgentApprovalDecision(BaseModel):
    challenge: str = Field(min_length=32, max_length=200)


class AgentWorkflowRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    plan: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentWorkflowRunRequest(BaseModel):
    objective: str | None = Field(default=None, min_length=1, max_length=8000)
    patch: dict[str, Any] | None = None
    session_id: str | None = Field(default=None, max_length=100)
    parent_run_id: str | None = Field(default=None, max_length=100)
    autonomy: Literal["advisory", "project_execute", "paper_execute", "external_execute", "full_execute"] = "advisory"
    max_steps: int = Field(default=6, ge=1, le=12)


class AgentUIStateUpdate(BaseModel):
    state: dict[str, Any] = Field(default_factory=dict)


class AgentUICommandResult(BaseModel):
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class AgentScheduleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=8000)
    trigger_type: Literal["one_shot", "interval", "cron", "event", "condition"] = "one_shot"
    next_run_at: str | None = Field(default=None, max_length=100)
    interval_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    cron_expression: str | None = Field(default=None, max_length=100)
    event_type: str | None = Field(default=None, max_length=200)
    condition: dict[str, Any] | None = None
    misfire_policy: Literal["run_once", "catch_up", "skip"] = "run_once"
    market_calendar: Literal["taiwan"] | None = None
    expires_at: str | None = Field(default=None, max_length=100)
    dedup_key: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=100)
    workflow_id: str | None = Field(default=None, max_length=100)
    symbols: list[str] = Field(default_factory=list, max_length=20)
    autonomy: Literal["advisory"] = "advisory"
    max_steps: int = Field(default=6, ge=1, le=12)


class AgentScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    objective: str | None = Field(default=None, min_length=1, max_length=8000)
    next_run_at: str | None = Field(default=None, max_length=100)
    interval_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    cron_expression: str | None = Field(default=None, max_length=100)
    event_type: str | None = Field(default=None, max_length=200)
    condition: dict[str, Any] | None = None
    misfire_policy: Literal["run_once", "catch_up", "skip"] | None = None
    market_calendar: Literal["taiwan"] | None = None
    expires_at: str | None = Field(default=None, max_length=100)


class AgentScheduleEvent(BaseModel):
    event_type: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


def _selected_driver(requested: str | None) -> str:
    # The running service is the authoritative configuration snapshot for a
    # newly created Run.  Re-reading the preference file here made API calls
    # depend on machine-local state that can disagree with the initialized
    # service (and made a portable/clean launch select an unconfigured
    # OpenAI-compatible driver).  Provider-setting updates rebuild the
    # service, so its default remains the user's current choice without a
    # second, inconsistent source of truth.
    service = get_agent_service()
    selected = requested or getattr(service, "default_driver", None) or load_agent_driver_settings().default_driver
    if selected not in service.drivers:
        raise ValueError(f"Unknown Agent driver: {selected}")
    driver = service.drivers[selected]
    description = driver.describe()
    if description.get("configured") is False:
        raise ValueError(f"Agent driver is not configured: {selected}")
    return selected


_INSTRUMENT_SCOPE_PATTERN = re.compile(r"(?:這檔|這支|這個標的|目前股票|本檔|這家公司)")
_SYMBOL_PATTERN = re.compile(r"(?<!\w)(?:\d{4,6}(?:\.(?:TW|TWO))?|[A-Z]{1,5}(?:\.[A-Z]{1,5})?)(?!\w)")
_MODEL_TASK_KIND_HEADERS = re.compile(
    r"^(?:\s*\[MODEL_TASK_KIND:[a-z_]+\]\s*)+",
    re.IGNORECASE,
)


def _is_evidence_recovery_request(objective: str) -> bool:
    """Recognize an explicit recovery outcome without treating UI labels as commands.

    This is deliberately narrower than an intent classifier: it does not infer
    a stock, source, or workflow.  It only prevents the dangerous capability
    downgrade where a model calls a request for recovery evidence a ``system``
    task because the user named an artifact such as Fishbone or Task Forest.
    Both a recovery action and an evidence/source target must be explicit.
    """

    text = str(objective or "").casefold()
    recovery_terms = (
        "修復", "恢復", "根因", "重新分析", "repair", "recover", "root cause", "diagnose",
    )
    evidence_terms = (
        "資料來源", "替代來源", "來源", "證據", "research", "source", "evidence",
    )
    return any(term in text for term in recovery_terms) and any(
        term in text for term in evidence_terms
    )


def _model_task_kind(intent: dict[str, Any], context_scope: str | None) -> str | None:
    """Translate the selected model's public intent into a Host tool profile.

    The browser obtains this intent from ``/classify-intent`` using the active
    provider.  This deliberately keeps natural-language task routing out of
    the old keyword matcher: the Host only maps a bounded, model-produced
    category to a safety-scoped capability profile.
    """
    category = str(intent.get("category") or "").strip()
    scope = str(intent.get("scope") or context_scope or "").strip()
    category_to_task_kind = {
        "market_analysis": "market_information",
        "market_decision": "market_decision",
        "instrument_analysis": "market_information",
        "portfolio": "market_information",
        "research": "market_information",
        "system": "ui_task",
        "general": "general_answer",
    }
    if category in category_to_task_kind:
        return category_to_task_kind[category]
    # Scope without a model category is a legacy API compatibility signal,
    # not evidence that a model has classified the request.
    if category and scope in {"market", "instrument"}:
        return "market_information"
    return None


def _run_context(request: AgentRunRequest) -> tuple[str, list[str], str]:
    """Attach the model's intent without letting UI context invent a symbol."""
    # Re-run/resume controls may submit the durable Run objective, which
    # already contains this private Host routing header. Strip every leading
    # copy before applying the current model classification so the marker is
    # idempotent across a long Session.
    objective = _MODEL_TASK_KIND_HEADERS.sub("", request.objective.strip()).strip()
    model_task_kind = _model_task_kind(request.intent, request.context_scope)
    model_prefix = f"[MODEL_TASK_KIND:{model_task_kind}]\n" if model_task_kind else ""
    # Context scope is produced by the selected model before this Run is made.
    # Do not replace it with a text/keyword guess when callers omit it.
    market_scope = request.context_scope == "market"
    if market_scope:
        return (
            model_prefix + "[MARKET_SCOPE] This is a neutral whole-market question. Do not use the currently selected "
            "security as an implicit target and do not choose a familiar symbol as a market proxy. "
            "Use broad market evidence, explain the candidate universe and uncertainty, and only discuss "
            "a named security when the user explicitly asks for it.\n\n"
            f"{objective}",
            [],
            "market",
        )
    return (
        model_prefix + objective,
        [str(symbol).strip().upper() for symbol in request.symbols if str(symbol).strip()],
        request.context_scope or "neutral",
    )


def _run_metadata(request: AgentRunRequest, context_scope: str) -> dict[str, Any]:
    raw_intent = request.intent if isinstance(request.intent, dict) else {}
    intent = {
        key: raw_intent[key]
        for key in ("title", "category", "scope", "provider", "model", "source")
        if raw_intent.get(key) is not None and raw_intent.get(key) != ""
    }
    return {"context_scope": context_scope, **({"intent": intent} if intent else {})}


def _effective_autonomy(request: AgentRunRequest, objective: str) -> str:
    """Derive the paper lane exclusively from the current objective.

    The ordinary Agent composer always sends its currently selected autonomy.
    That used to let the default ``advisory`` value override the composer's
    paper-order detection, leaving a verified paper order permanently pending
    and triggering continuation loops. The reverse is equally unsafe: a
    stale ``paper_execute`` selection must not turn an analysis-only objective
    into an impossible Run whose completion contract asks for an order that
    the objective forbids. A current, explicit local paper-order request is
    the only condition that enters the bounded paper lane; all other stale
    paper selections are reset to advisory. Other explicitly selected
    execution modes remain unchanged.
    """
    paper_order_requested = objective_completion_contract(
        objective, None
    )["paper_order_requested"]
    if paper_order_requested and request.autonomy in {"advisory", "paper_execute"}:
        return "paper_execute"
    if request.autonomy == "paper_execute":
        return "advisory"
    return request.autonomy


def _selected_driver_for_api(requested: str | None) -> str:
    try:
        return _selected_driver(requested)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


_INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "category", "scope", "use_selected_symbol", "symbols"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 80},
        "category": {
            "type": "string",
            "enum": [
                "market_analysis", "market_decision", "instrument_analysis", "portfolio",
                "research", "system", "general",
            ],
        },
        "scope": {"type": "string", "enum": ["market", "instrument", "neutral"]},
        "use_selected_symbol": {"type": "boolean"},
        "symbols": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
}


def _literal_taiwan_security_symbols(objective: str) -> list[str]:
    """Resolve literal Taiwan security names from the local security master.

    Intent classification is a convenience model call, not an authority for
    security identity.  In particular, a model must never turn a Chinese
    issuer name into a familiar but unrelated US ticker (for example, turning
    台新新光金 into ``TSN``).  The local security master is the authoritative,
    network-free source for this first pass; when it has no exact name match we
    leave the name in the objective for the governed search tool instead.
    """
    compact_objective = re.sub(r"\s+", "", str(objective or ""))
    if not compact_objective:
        return []
    try:
        rows = get_market_data_platform().securities(market="taiwan", limit=5000)
    except Exception as exc:
        logger.warning("Local security-master lookup failed during intent classification: %s", exc)
        return []
    display_matches: list[tuple[str, str]] = []
    legal_matches: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        for name, matches in (
            (row.get("name"), display_matches),
            (row.get("legal_name"), legal_matches),
        ):
            compact_name = re.sub(r"\s+", "", str(name or ""))
            if len(compact_name) < 2 or compact_name not in compact_objective:
                continue
            matches.append((compact_name, symbol))
            # A short display name is the user-facing identity and must take
            # precedence over a legal name that can mention a parent, merger,
            # or affiliate.
            if matches is display_matches:
                break
    # A name contained inside another matched display name is normally an
    # issuer-family fragment (for example 新光金 in 台新新光金), not a second
    # explicit user target.  Keep independently named comparison targets.
    maximal_display = [
        item
        for item in display_matches
        if not any(
            item[0] != other_name and item[0] in other_name
            for other_name, _other_symbol in display_matches
        )
    ]
    resolved_display = list(
        dict.fromkeys(symbol for _name, symbol in sorted(maximal_display, reverse=True))
    )
    if resolved_display:
        return resolved_display
    resolved_legal = list(
        dict.fromkeys(symbol for _name, symbol in sorted(legal_matches, reverse=True))
    )
    # A legal-name-only match is not enough to choose between related firms.
    return resolved_legal if len(resolved_legal) == 1 else []


def _literal_classifier_symbols(objective: str, symbols: list[str]) -> list[str]:
    """Keep only ticker identifiers actually written by the user."""
    question = str(objective or "").casefold()
    accepted: list[str] = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol:
            continue
        bare = symbol.split(".", 1)[0]
        if symbol.casefold() in question or (
            bare.isdigit()
            and re.search(rf"(?<![0-9A-Za-z]){re.escape(bare)}(?![0-9A-Za-z])", question)
        ):
            accepted.append(symbol)
    return list(dict.fromkeys(accepted))


async def _classify_agent_intent(request: AgentIntentClassificationRequest) -> tuple[AgentIntentClassification, str, str | None]:
    driver_id = _selected_driver_for_api(request.driver)
    service = get_agent_service()
    provider = service.provider_registry.get(driver_id)
    classifier_id = f"IC-{uuid4().hex}"
    selected_symbol = str(request.selected_symbol or "").strip().upper()
    prompt = (
        "Classify the user's Stock AI question before any execution. Return only the JSON schema. "
        "Choose market scope for a broad market, ranking, screening, or 'what can I buy' question. "
        "Use category=market_decision whenever the user explicitly asks to buy, sell, place an order, or make a "
        "paper/simulated trade; do not downgrade those requests to market_analysis. "
        "Choose instrument scope only when the user explicitly names a security or refers to the selected security "
        "with words such as 'this stock'. The selected chart symbol is optional UI context, never an implicit target. "
        "If the user did not explicitly request that security, set use_selected_symbol=false. "
        "Only put security identifiers literally present in the question into symbols; never invent or infer them. "
        "Use category=system only when the requested outcome is a direct UI navigation, setting, or visible-panel operation. "
        "Names of interface artifacts such as Task Forest, Fishbone, DAG, a branch, or a node are context references, "
        "not UI-operation requests by themselves. If the user asks to diagnose, repair, or obtain alternative evidence "
        "for a failed branch, classify it as research unless they also explicitly ask to operate the interface. "
        "Create a concise natural-language title in the same language as the user.\n\n"
        f"USER_QUESTION={request.objective.strip()}\n"
        f"OPTIONAL_SELECTED_SYMBOL={selected_symbol or 'none'}"
    )
    try:
        await provider.start_session(classifier_id, project_root=str(Path(__file__).resolve().parents[2]))
        raw = await provider.generate_structured(classifier_id, prompt, _INTENT_SCHEMA)
        classification = AgentIntentClassification.model_validate(raw)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Intent classification by {driver_id} failed: {exc.__class__.__name__}",
        ) from exc
    finally:
        try:
            await provider.close_session(classifier_id)
        except Exception as exc:
            logger.warning(
                "Intent-classifier session cleanup failed for %s: %s",
                driver_id,
                exc.__class__.__name__,
            )
    if classification.scope != "instrument":
        classification = classification.model_copy(update={"use_selected_symbol": False, "symbols": []})
    # The model chooses the public intent, but an explicit order request is a
    # Host safety boundary: it must expose the governed market-decision path
    # instead of losing the paper-order capability behind market_analysis.
    if objective_completion_contract(request.objective, None)["decision_requested"]:
        classification = classification.model_copy(update={"category": "market_decision"})
    if (
        classification.category == "system"
        and _is_evidence_recovery_request(request.objective)
    ):
        # A visual artifact can identify the failing branch, but evidence
        # recovery needs research capabilities, not UI automation approval.
        # Preserve the model-provided title while correcting only the public
        # capability category, and make the correction visible to callers.
        classification = classification.model_copy(
            update={
                "category": "research",
                "scope": "neutral",
                "use_selected_symbol": False,
                "symbols": [],
            }
        )
    literal_taiwan_symbols = _literal_taiwan_security_symbols(request.objective)
    if literal_taiwan_symbols:
        # An exact local name match wins over a provider-generated ticker and
        # also makes the intended instrument explicit for the Host PlanGraph.
        classification = classification.model_copy(
            update={
                "scope": "instrument",
                "use_selected_symbol": False,
                "symbols": literal_taiwan_symbols,
            }
        )
    elif classification.scope == "instrument":
        classification = classification.model_copy(
            update={"symbols": _literal_classifier_symbols(request.objective, classification.symbols)}
        )
    model = provider.capabilities().get("model")
    return classification, driver_id, str(model) if model else None


@router.get("")
def agent_runtime() -> dict:
    return get_agent_service().describe()


@router.get("/tools")
def agent_tools() -> dict:
    runtime = get_agent_service().describe()
    return {
        "schema_version": "open_stock_ai.agent_tools.v1",
        "count": len(runtime["tools"]),
        "items": runtime["tools"],
        "boundaries": runtime["boundaries"],
    }


@router.get("/observability")
def agent_observability_dashboard() -> dict:
    """Read-only, durable runtime health and KPI summary for the Agent Dock."""
    runtime = get_agent_run_runtime()
    dashboard = runtime.final_runtime.observability_dashboard()
    slo_dashboard = getattr(runtime, "slo_dashboard", None)
    if callable(slo_dashboard):
        dashboard["slo"] = slo_dashboard()
    dashboard["storage"] = get_agent_database_maintenance().storage_report()
    dashboard["operational_alerts"] = get_operational_alert_runtime().dashboard()
    return dashboard


@router.get("/operational-alerts")
def agent_operational_alerts() -> dict:
    """Read current local alerts and immutable history; no on-call delivery occurs."""

    return get_operational_alert_runtime().dashboard()


@router.get("/chaos-recovery")
def agent_chaos_recovery_dashboard() -> dict:
    """Read durable chaos-recovery evidence; this endpoint cannot run faults."""

    dashboard = get_operational_alert_runtime().dashboard()
    return dict(dashboard.get("chaos_recovery") or {})


@router.get("/storage")
def agent_storage_health() -> dict:
    """Read-only storage preflight; reclamation remains an explicit operator action."""
    return get_agent_database_maintenance().storage_report()


@router.get("/capabilities")
def agent_capabilities() -> dict:
    return agent_tools()


@router.get("/events/stream")
async def stream_all_agent_events(
    after_id: int = Query(default=0, ge=0),
) -> StreamingResponse:
    async def generate():
        cursor = after_id
        while True:
            items = get_agent_run_runtime().final_runtime.events_after(cursor)
            if not items:
                yield ": keep-alive\n\n"
                await asyncio.sleep(1.0)
                continue
            for item in items:
                cursor = max(cursor, int(item.get("event_row_id") or cursor))
                event_type = str(item.get("type") or "agent.event")
                payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {cursor}\nevent: {event_type}\ndata: {payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/providers")
def agent_providers() -> dict:
    runtime = get_agent_service().describe()
    return runtime.get("providers") or {
        "schema_version": "open_stock_ai.provider_registry.v1",
        "primary_provider": "codex",
        "items": [],
    }


@router.get("/providers/{provider_id}/health")
async def agent_provider_health(provider_id: str) -> dict:
    try:
        provider = get_agent_service().provider_registry.get(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await provider.health()


@router.get("/environment")
def agent_environment(run_id: str | None = Query(default=None, max_length=100)) -> dict:
    return get_agent_run_runtime().environment(run_id)


@router.get("/settings")
def agent_settings() -> dict:
    return public_agent_preferences()


@router.post("/providers/openai-compatible/models")
async def discover_agent_models(request: AgentModelDiscoveryRequest) -> dict:
    current = load_agent_driver_settings()
    saved_key = (
        current.openai_api_key
        if request.base_url.rstrip("/") == current.openai_base_url.rstrip("/")
        else ""
    )
    try:
        result = await discover_openai_compatible_models(
            request.base_url,
            api_key=saved_key,
            timeout_seconds=min(current.openai_timeout_seconds, 30.0),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not result["reachable"]:
        raise HTTPException(status_code=502, detail="無法連線模型服務；請確認位址、網路與 Ollama 監聽設定。")
    return result


@router.post("/classify-intent")
async def classify_agent_intent(request: AgentIntentClassificationRequest) -> dict:
    """Use the selected model to classify a question before attaching UI context."""
    classification, driver_id, model = await _classify_agent_intent(request)
    return {
        "schema_version": "open_stock_ai.agent_intent.v1",
        **classification.model_dump(),
        "provider": driver_id,
        "model": model,
    }


@router.post("/ui/state")
def update_agent_ui_state(payload: AgentUIStateUpdate) -> dict:
    return agent_ui_bridge.update_state(payload.state)


@router.get("/ui/commands")
def agent_ui_commands(after_id: int = Query(default=0, ge=0)) -> dict:
    items = agent_ui_bridge.commands_after(after_id)
    return {
        "schema_version": "open_stock_ai.ui_commands.v1",
        "count": len(items),
        "items": items,
    }


@router.post("/ui/commands/{command_id}/result")
def complete_agent_ui_command(command_id: int, payload: AgentUICommandResult) -> dict:
    return agent_ui_bridge.complete(command_id, payload.model_dump(exclude_none=True))


@router.post("/settings")
def update_agent_settings(request: AgentSettingsUpdate) -> dict:
    values = request.model_dump(exclude_none=True)
    try:
        settings = save_agent_preferences(values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    clear_agent_service()
    return public_agent_preferences(settings)


@router.post("/sessions", status_code=201)
def create_agent_session(request: AgentSessionRequest) -> dict:
    return get_agent_run_runtime().create_session(
        title=request.title,
        metadata=request.metadata,
    )


@router.get("/sessions")
def list_agent_sessions(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    items = get_agent_run_runtime().list_sessions(limit=limit)
    return {
        "schema_version": "open_stock_ai.agent_session_list.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/sessions/{session_id}")
def get_agent_session(session_id: str) -> dict:
    session = get_agent_run_runtime().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    return {
        **session,
        "messages": get_agent_run_runtime().session_messages(session_id),
    }


@router.get("/sessions/{session_id}/messages")
def get_agent_session_messages(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    if get_agent_run_runtime().get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    items = get_agent_run_runtime().session_messages(session_id, limit=limit)
    return {
        "schema_version": "open_stock_ai.agent_message_list.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/sessions/{session_id}/archive")
def export_agent_session_archive(session_id: str) -> dict:
    archive = get_agent_run_runtime().export_session_archive(session_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    return archive


@router.post("/sessions/{session_id}/restore")
def restore_agent_session_archive(
    session_id: str,
    archive: dict[str, Any] = Body(...),
) -> dict:
    try:
        return get_agent_run_runtime().restore_session_archive(
            archive,
            expected_session_id=session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/messages", status_code=202)
async def create_agent_session_message(
    session_id: str,
    request: AgentSessionMessageRequest,
) -> dict:
    try:
        return await get_agent_run_runtime().add_session_message(
            session_id,
            content=request.content,
            artifact_context_selection=request.artifact_context_selection,
            intent=request.intent,
            target_branch_id=request.target_branch_id,
            affected_branch_ids=tuple(request.affected_branch_ids),
            replacement_objective=request.replacement_objective,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/forest")
def get_agent_session_forest(
    session_id: str,
    run_id: str | None = Query(default=None, max_length=100),
) -> dict:
    forest = get_agent_run_runtime().forest(session_id, run_id=run_id)
    if forest is None:
        raise HTTPException(status_code=404, detail="Task Forest not found")
    return forest


@router.get("/sessions/{session_id}/interactions")
def get_agent_session_interactions(
    session_id: str,
    open_only: bool = Query(default=False),
) -> dict:
    if get_agent_run_runtime().get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    items = get_agent_run_runtime().final_runtime.interactions(session_id, open_only=open_only)
    return {"schema_version": "open_stock_ai.interaction_list.v1", "count": len(items), "items": items}


@router.post("/interactions/{interaction_id}/respond")
async def respond_agent_interaction(interaction_id: str, request: AgentInteractionResponse) -> dict:
    response = request.model_dump(exclude_none=True)
    if not response:
        raise HTTPException(status_code=422, detail="Interaction response cannot be empty")
    try:
        return await get_agent_run_runtime().respond_interaction(interaction_id, response)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Interaction not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/branches/{branch_id}")
def get_agent_branch(branch_id: str) -> dict:
    branch = get_agent_run_runtime().branch(branch_id)
    if branch is None:
        raise HTTPException(status_code=404, detail="Agent branch not found")
    return branch


async def _control_branch(branch_id: str, action: str, reason: str) -> dict:
    try:
        return await get_agent_run_runtime().control_branch(
            branch_id,
            action=action,
            reason=reason,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent branch not found") from exc


@router.post("/branches/{branch_id}/pause")
async def pause_agent_branch(branch_id: str, request: AgentBranchControlRequest) -> dict:
    return await _control_branch(branch_id, "pause", request.reason)


@router.post("/branches/{branch_id}/resume")
async def resume_agent_branch(branch_id: str, request: AgentBranchControlRequest) -> dict:
    return await _control_branch(branch_id, "resume", request.reason)


@router.post("/branches/{branch_id}/cancel")
async def cancel_agent_branch(branch_id: str, request: AgentBranchControlRequest) -> dict:
    return await _control_branch(branch_id, "cancel", request.reason)


@router.post("/sessions/{session_id}/archive")
def archive_agent_session(session_id: str) -> dict:
    session = get_agent_run_runtime().archive_session(session_id, archived=True)
    if session is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    return session


@router.post("/sessions/{session_id}/runs", status_code=202)
async def create_session_agent_run(session_id: str, request: AgentRunRequest) -> dict:
    if get_agent_run_runtime().get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    objective, symbols, context_scope = _run_context(request)
    return await get_agent_run_runtime().submit_session_run(
        session_id,
        objective=objective,
        symbols=symbols,
        driver_id=_selected_driver_for_api(request.driver),
        autonomy=_effective_autonomy(request, objective),
        max_steps=request.max_steps,
        parent_run_id=request.parent_run_id,
        idempotency_key=request.idempotency_key,
        run_metadata=_run_metadata(request, context_scope),
    )


@router.post("/run")
async def run_agent(request: AgentRunRequest) -> dict:
    try:
        objective, symbols, context_scope = _run_context(request)
        run = await get_agent_run_runtime().create_run(
            objective=objective,
            symbols=symbols,
            driver_id=_selected_driver_for_api(request.driver),
            autonomy=_effective_autonomy(request, objective),
            max_steps=request.max_steps,
            session_id=request.session_id,
            parent_run_id=request.parent_run_id,
            idempotency_key=request.idempotency_key,
            run_metadata=_run_metadata(request, context_scope),
        )
        return await get_agent_run_runtime().wait(run["run_id"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Agent Runtime failed: {exc}") from exc


@router.post("/run/stream")
async def stream_agent(request: AgentRunRequest) -> StreamingResponse:
    """Compatibility route backed by a durable run that survives stream disconnects."""

    try:
        objective, symbols, context_scope = _run_context(request)
        run = await get_agent_run_runtime().create_run(
            objective=objective,
            symbols=symbols,
            driver_id=_selected_driver_for_api(request.driver),
            autonomy=_effective_autonomy(request, objective),
            max_steps=request.max_steps,
            session_id=request.session_id,
            parent_run_id=request.parent_run_id,
            idempotency_key=request.idempotency_key,
            run_metadata=_run_metadata(request, context_scope),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def generate():
        async for item in get_agent_run_runtime().stream(run["run_id"]):
            yield json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs", status_code=202)
async def create_agent_run(request: AgentRunRequest) -> dict:
    try:
        objective, symbols, context_scope = _run_context(request)
        return await get_agent_run_runtime().create_run(
            objective=objective,
            symbols=symbols,
            driver_id=_selected_driver_for_api(request.driver),
            autonomy=_effective_autonomy(request, objective),
            max_steps=request.max_steps,
            session_id=request.session_id,
            parent_run_id=request.parent_run_id,
            idempotency_key=request.idempotency_key,
            run_metadata=_run_metadata(request, context_scope),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/market-radar/runs", status_code=202)
async def create_market_radar_run(request: MarketRadarRunRequest) -> dict:
    supplied_symbols = tuple(
        dict.fromkeys(
            value.strip().upper()
            for value in request.symbols
            if value.strip()
        )
    )
    try:
        universe = await asyncio.to_thread(
            resolve_universe,
            UniverseRequest(
                source=request.universe_source,
                symbols=supplied_symbols,
                filters=request.filters,
                limit=request.limit,
            ),
        )
    except (UniverseResolutionError, ValueError) as exc:
        if not supplied_symbols and request.universe_source in {
            "explicit_symbols",
            "workflow_parameters",
        }:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "universe_required",
                    "message": "Market Radar requires a resolvable Universe; no default symbols were selected.",
                    "universe": {
                        "source": "none",
                        "symbols": [],
                        "count": 0,
                    },
                },
            ) from exc
        raise HTTPException(
            status_code=422,
            detail={
                "code": "universe_resolution_failed",
                "message": str(exc),
                "universe_source": request.universe_source,
            },
        ) from exc
    symbols = list(universe.symbols)
    if not symbols:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "universe_required",
                "message": "Market Radar requires a non-empty resolved Universe; no default symbols were selected.",
                "universe": {
                    "source": universe.source,
                    "symbols": [],
                    "count": 0,
                },
            },
        )
    objective = (
        "[MARKET_RADAR_TASK] Analyze every symbol in the supplied Market Radar Universe. "
        "First call market.analyze_universe with exactly the supplied symbols. Then return "
        "structured_result matching stock_ai.market_radar_result.v1. Every Universe symbol must have "
        "one item card, at least one observation, an action group, a next action, timing, trigger and "
        "Host-issued evidence IDs. Keep observations, quantitative rule analysis, model analysis and "
        "Host risk evaluation separate. Never replace a model failure with a rule answer labeled as AI. "
        "Use model_self_reported confidence only; it is not a calibrated probability. "
        f"Universe symbols={json.dumps(symbols, ensure_ascii=False)}; "
        f"Universe source={universe.source}; filters={json.dumps(universe.filters, ensure_ascii=False)}; "
        f"explain={request.explain}."
    )
    try:
        run = await get_agent_run_runtime().create_run(
            objective=objective,
            symbols=symbols,
            driver_id=_selected_driver_for_api(request.driver),
            autonomy="advisory",
            max_steps=request.max_steps,
            session_id=request.session_id,
            idempotency_key=request.idempotency_key,
            run_metadata={
                "run_type": "market_radar",
                # The front end may only apply a validated model result to this
                # exact snapshot.  It must never promote a prior scan's answer.
                "market_snapshot_id": request.market_snapshot_id,
                "universe_source": universe.source,
                "universe_filters": universe.filters,
                "universe_created_at": universe.created_at,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        **run,
        "run_type": "market_radar",
        "universe": universe_payload(universe),
    }


@router.get("/market-radar/universe-options")
async def get_market_radar_universe_options() -> dict:
    return await asyncio.to_thread(universe_source_options)


@router.post("/providers/{driver_id}/conformance")
async def run_provider_conformance(
    driver_id: Literal["codex", "openai-compatible", "external-agent"],
    request: ProviderConformanceRequest,
) -> dict:
    service = get_agent_service()
    registry = service.provider_registry
    if registry is None:
        raise HTTPException(status_code=503, detail="Provider registry is unavailable")
    provider = registry.get(driver_id)
    session_id = f"provider-conformance-{uuid4().hex}"
    await provider.start_session(
        session_id,
        project_root=str(Path.cwd()),
    )
    try:
        return await registry.negotiate(
            driver_id,
            session_id=session_id,
            force=True,
            include_expensive=request.include_expensive,
        )
    finally:
        await provider.close_session(session_id)


@router.get("/market-radar/runs/{run_id}")
def get_market_radar_run(run_id: str) -> dict:
    run = get_agent_run_runtime().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Market Radar run not found")
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    driver_id = str(request.get("driver_id") or run.get("driver_id") or "")
    driver = get_agent_service().drivers.get(driver_id)
    description = driver.describe() if driver is not None else {}
    model_id = (
        description.get("model")
        or ("codex-app-server/default" if driver_id == "codex" else None)
    )
    status = str(run.get("status") or "")
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    receipts = result.get("model_invocations") if isinstance(result.get("model_invocations"), list) else []
    receipt = next(
        (
            item
            for item in reversed(receipts)
            if isinstance(item, dict) and item.get("status") == "succeeded"
        ),
        None,
    ) or {
        "call_id": run_id,
        "provider": driver_id or None,
        "model_id": model_id,
        "status": "not_run",
    }
    metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
    universe_source = str(metadata.get("universe_source") or "explicit_symbols")
    inner_completed = result.get("status") == "completed"
    completion_validation = (
        result.get("completion_validation")
        if isinstance(result.get("completion_validation"), dict)
        else {}
    )
    base_succeeded = (
        status == "completed"
        and inner_completed
        and completion_validation.get("passed") is True
        and receipt.get("status") == "succeeded"
    )
    radar_result: dict[str, Any] | None = None
    radar_error: dict[str, Any] | None = None
    if base_succeeded:
        raw_radar_result = result.get("structured_result")
        if not isinstance(raw_radar_result, dict):
            decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
            raw_radar_result = decision.get("market_radar")
        try:
            validated = validate_market_radar_result(
                raw_radar_result,
                symbols=request.get("symbols") or [],
                universe_source=universe_source,
                provider=str(receipt.get("provider") or driver_id or ""),
                model_id=str(receipt.get("model_id") or model_id or ""),
                receipt=receipt,
                tool_trace=(
                    result.get("tool_trace")
                    if isinstance(result.get("tool_trace"), list)
                    else []
                ),
            )
            radar_result = market_radar_ui_payload(validated)
        except MarketRadarValidationError as exc:
            radar_error = {"code": exc.code, "message": str(exc)}
    model_succeeded = base_succeeded and radar_result is not None
    if not model_succeeded and radar_error is None and status in {
        "completed",
        "failed",
        "cancelled",
    }:
        radar_error = {
            "code": "market_radar_run_incomplete",
            "message": (
                "Market Radar is not successful unless the durable run, inner Agent result, "
                "completion validation and real model receipt all succeed."
            ),
        }
    return {
        **run,
        "run_type": "market_radar",
        "model_invocation": receipt,
        "market_radar_status": (
            "succeeded"
            if model_succeeded
            else "failed"
            if radar_error is not None
            else "running"
        ),
        "market_radar_result": radar_result,
        "market_radar_error": radar_error,
        "provenance": {
            "origin": "model" if model_succeeded else "none",
            "provider": driver_id or None,
            "model_id": model_id,
            "model_call_id": receipt.get("call_id"),
            "model_call_succeeded": model_succeeded,
            "universe_source": universe_source,
            "symbols_considered": request.get("symbols") or [],
            "fallback_used": False,
        },
    }


@router.get("/market-radar/runs/{run_id}/stream")
async def reconnect_market_radar_run_stream(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0),
) -> StreamingResponse:
    if get_agent_run_runtime().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Market Radar run not found")

    async def generate():
        async for item in get_agent_run_runtime().stream(run_id, after_sequence=after_sequence):
            sequence = int((item.get("event") or {}).get("sequence") or after_sequence)
            event_type = str(item.get("type") or "message")
            payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            yield f"id: {sequence}\nevent: {event_type}\ndata: {payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs")
def list_agent_runs(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    items = get_agent_run_runtime().list_runs(limit=limit)
    return {
        "schema_version": "open_stock_ai.agent_run_list.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/runs/{run_id}")
def get_agent_run(run_id: str) -> dict:
    run = get_agent_run_runtime().get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


def _artifact_version_items(value: Any) -> list[dict[str, Any]]:
    """Flatten durable snapshot history into the UI's version-record contract."""

    items: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            items.extend(_artifact_version_items(item))
        return items
    if not isinstance(value, dict):
        return items
    if value.get("artifact_id") and value.get("version") is not None:
        return [dict(value)]
    for artifact_id, history in value.items():
        candidates = history if isinstance(history, list) else [history]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            record = dict(candidate)
            record.setdefault("artifact_id", str(artifact_id))
            if record.get("version") is not None:
                items.append(record)
    return items


def _evidence_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return host evidence from durable state and replayable evidence events."""

    evidence_by_id: dict[str, dict[str, Any]] = {}
    raw_evidence = snapshot.get("evidence")
    candidates = raw_evidence if isinstance(raw_evidence, list) else (
        list(raw_evidence.values()) if isinstance(raw_evidence, dict) else []
    )
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("evidence_id"):
            evidence_by_id[str(candidate["evidence_id"])] = dict(candidate)

    run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
    for event in snapshot.get("events") or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type != "research.evidence_added" and not event_type.startswith("evidence."):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else payload
        evidence_id = str(event.get("evidence_id") or evidence.get("evidence_id") or "")
        if not evidence_id:
            continue
        evidence_by_id[evidence_id] = {
            **evidence_by_id.get(evidence_id, {}),
            **evidence,
            "evidence_id": evidence_id,
            "run_id": event.get("run_id") or run.get("run_id"),
            "session_id": event.get("session_id") or run.get("session_id"),
            "branch_id": event.get("branch_id") or evidence.get("branch_id"),
            "artifact_id": event.get("artifact_id") or evidence.get("artifact_id"),
            "artifact_version": event.get("artifact_version") or evidence.get("artifact_version"),
        }
    return list(evidence_by_id.values())


def _agent_ui_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Expose one canonical collection shape to the Agent Dock reducer."""

    return {
        **snapshot,
        "artifact_versions": _artifact_version_items(snapshot.get("artifact_versions")),
        "evidence": _evidence_items(snapshot),
    }


@router.get("/runs/{run_id}/snapshot")
def get_agent_run_snapshot(run_id: str) -> dict:
    snapshot = get_agent_run_runtime().snapshot(run_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return _agent_ui_snapshot(snapshot)


@router.get("/runs/{run_id}/stream")
async def reconnect_agent_run_stream(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0),
) -> StreamingResponse:
    if get_agent_run_runtime().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found")

    async def generate():
        async for item in get_agent_run_runtime().stream(run_id, after_sequence=after_sequence):
            sequence = int((item.get("event") or {}).get("sequence") or after_sequence)
            event_type = str(item.get("type") or "message")
            payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            yield f"id: {sequence}\nevent: {event_type}\ndata: {payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_agent_run(run_id: str) -> dict:
    run = await get_agent_run_runtime().cancel(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@router.post("/runs/{run_id}/pause", status_code=202)
async def pause_agent_run(run_id: str) -> dict:
    run = await get_agent_run_runtime().pause(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_agent_run(run_id: str) -> dict:
    run = await get_agent_run_runtime().resume(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@router.post("/runs/{run_id}/retry", status_code=202)
async def retry_agent_run(run_id: str) -> dict:
    try:
        run = await get_agent_run_runtime().retry(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@router.post("/runs/{run_id}/continue", status_code=202)
async def continue_agent_run(run_id: str, request: AgentContinueRequest) -> dict:
    try:
        run = await get_agent_run_runtime().continue_after_limit(
            run_id,
            additional_steps=request.additional_steps,
            max_steps=request.max_steps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@router.post("/runs/{run_id}/replan", status_code=202)
async def replan_agent_run(run_id: str, request: AgentReplanRequest) -> dict:
    try:
        return await get_agent_run_runtime().request_replan(
            run_id,
            instruction=request.instruction,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runs/{run_id}/plan")
def get_agent_run_plan(run_id: str) -> dict:
    if get_agent_run_runtime().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    plan = get_agent_run_runtime().get_plan(run_id)
    return {
        "schema_version": "open_stock_ai.agent_plan_state.v1",
        "plan": plan,
        "revisions": get_agent_run_runtime().plan_revisions(run_id),
    }


@router.get("/runs/{run_id}/events")
def get_agent_run_events(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0),
) -> dict:
    if get_agent_run_runtime().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    items = get_agent_run_runtime().events(run_id, after_sequence=after_sequence)
    return {
        "schema_version": "open_stock_ai.agent_event_list.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/runs/{run_id}/artifacts")
def get_agent_run_artifacts(run_id: str) -> dict:
    if get_agent_run_runtime().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    items = get_agent_run_runtime().artifacts(run_id)
    return {
        "schema_version": "open_stock_ai.agent_artifact_list.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/artifacts/{artifact_id}")
def get_agent_artifact(artifact_id: str) -> dict:
    artifact = get_agent_run_runtime().final_runtime.artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Agent artifact not found")
    return artifact


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
def open_agent_run_artifact(
    run_id: str,
    artifact_id: str,
    download: bool = Query(default=False),
) -> FileResponse:
    runtime = get_agent_run_runtime()
    artifact = runtime.artifact(run_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Agent artifact not found")
    path = Path(str(artifact.get("path") or "")).resolve()
    artifact_root = runtime.artifact_store.root.resolve() if runtime.artifact_store else None
    if artifact_root is None or not path.is_relative_to(artifact_root):
        raise HTTPException(status_code=403, detail="Agent artifact path is outside the artifact store")
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Agent artifact file is unavailable")
    return FileResponse(
        path,
        media_type=str(artifact.get("media_type") or "application/octet-stream"),
        filename=str(artifact.get("name") or path.name),
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/runs/{run_id}/artifacts/{artifact_id}/versions")
def get_agent_artifact_versions(run_id: str, artifact_id: str) -> dict:
    runtime = get_agent_run_runtime()
    artifact = runtime.artifact(run_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Agent artifact not found")
    runtime.final_runtime.ensure_artifact_version(
        artifact_id,
        {key: value for key, value in artifact.items() if key != "path"},
    )
    items = runtime.final_runtime.artifact_history(artifact_id)
    return {"schema_version": "open_stock_ai.artifact_version_list.v1", "count": len(items), "items": items}


@router.post("/artifacts/{artifact_id}/select", status_code=201)
def select_agent_artifact(artifact_id: str, request: AgentArtifactSelectionRequest) -> dict:
    try:
        return get_agent_run_runtime().final_runtime.select_artifact(
            session_id=request.session_id,
            artifact_id=artifact_id,
            artifact_version=request.artifact_version,
            target_type=request.target_type,
            path=request.path,
            branch_id=request.branch_id,
            node_id=request.node_id,
            evidence_id=request.evidence_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/artifacts/{artifact_id}/propose-change", status_code=201)
async def propose_agent_artifact_change(artifact_id: str, request: AgentArtifactChangeRequest) -> dict:
    try:
        runtime = get_agent_run_runtime()
        arguments = dict(
            artifact_id=artifact_id,
            expected_version=request.expected_version,
            content=request.content,
            changed_by="stock_ai_ui_user",
            reason=request.reason,
            message_id=request.message_id,
            affected_node_ids=tuple(request.affected_node_ids),
        )
        revise = getattr(runtime, "revise_artifact", None)
        if revise is not None:
            return await revise(**arguments)
        return runtime.final_runtime.revise_artifact(**arguments)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/artifacts/{artifact_id}/restore", status_code=201)
async def restore_agent_artifact(artifact_id: str, request: AgentArtifactRestoreRequest) -> dict:
    try:
        runtime = get_agent_run_runtime()
        arguments = dict(
            artifact_id=artifact_id,
            source_version=request.source_version,
            expected_version=request.expected_version,
            changed_by="stock_ai_ui_user",
        )
        restore = getattr(runtime, "restore_artifact", None)
        if restore is not None:
            return await restore(**arguments)
        return runtime.final_runtime.restore_artifact(**arguments)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/automations/preview")
def preview_agent_automation(request: AgentAutomationProposalRequest) -> dict:
    proposal = get_agent_run_runtime().final_runtime.automations.propose(
        request.goal,
        request.context,
    )
    return {
        "schema_version": "open_stock_ai.automation_proposal.v1",
        "opportunity": {
            "kind": proposal.opportunity.kind.value,
            "worthwhile": proposal.opportunity.worthwhile,
            "reason": proposal.opportunity.reason,
        },
        "natural_language": proposal.natural_language,
        "host_status": proposal.host_status,
        "artifact_preview": dict(proposal.artifact_preview),
        "requires_user_confirmation": proposal.requires_user_confirmation,
    }


@router.post("/automations", status_code=201)
def activate_agent_automation(request: AgentAutomationActivationRequest) -> dict:
    try:
        return get_agent_run_runtime().final_runtime.activate_automation(
            request.intent,
            confirmed=request.confirmed,
            external_permission=request.external_permission,
            credential_refs=tuple(request.credential_refs),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/automations")
def list_agent_automations(
    user_id: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=200),
    include_archived: bool = Query(default=False),
) -> dict:
    items = get_agent_run_runtime().final_runtime.list_automations(
        user_id=user_id,
        limit=limit,
        include_archived=include_archived,
    )
    return {"schema_version": "open_stock_ai.automation_list.v1", "count": len(items), "items": items}


@router.get("/automations/backends")
def get_agent_automation_backends() -> dict:
    return {
        "schema_version": "open_stock_ai.automation_backend_status.v1",
        "backends": get_agent_run_runtime().final_runtime.automation_backend_status(),
    }


@router.get("/automations/callback-receipts")
def list_agent_automation_callback_receipts(
    automation_id: str | None = Query(default=None, max_length=100),
    submission_id: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict:
    items = get_agent_run_runtime().final_runtime.list_automation_callback_receipts(
        automation_id=automation_id,
        submission_id=submission_id,
        limit=limit,
    )
    return {
        "schema_version": "open_stock_ai.n8n_callback_receipt_list.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/automations/{automation_id}")
def get_agent_automation(automation_id: str) -> dict:
    item = get_agent_run_runtime().final_runtime.automation(automation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Automation not found")
    return item


@router.patch("/automations/{automation_id}")
def patch_agent_automation(
    automation_id: str,
    request: AgentAutomationPatchRequest,
) -> dict:
    try:
        return get_agent_run_runtime().final_runtime.update_automation(
            automation_id,
            request.intent,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Automation not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/automations/{automation_id}/pause")
def pause_agent_automation(automation_id: str) -> dict:
    try:
        return dict(get_agent_run_runtime().final_runtime.automations.pause(automation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Automation not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/automations/{automation_id}/resume")
def resume_agent_automation(automation_id: str) -> dict:
    try:
        return dict(get_agent_run_runtime().final_runtime.automations.resume(automation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Automation not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/automations/{automation_id}/archive")
def archive_agent_automation(automation_id: str) -> dict:
    """Archive an obsolete Automation without deleting its audit evidence."""

    try:
        return dict(get_agent_run_runtime().final_runtime.automations.archive(automation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Automation not found") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/automations/events", status_code=202)
async def trigger_agent_automation_event(event: AgentScheduleEvent, request: Request) -> dict:
    """Accept an event-engine callback for active durable Automations."""

    runtime = get_agent_run_runtime()
    items = await runtime.trigger_automation_event(
        event.event_type,
        event.payload,
    )
    result = {
        "schema_version": "open_stock_ai.automation_event_result.v1",
        "count": len(items),
        "items": items,
    }
    authentication = getattr(request.state, "automation_callback_authentication", None)
    if isinstance(authentication, dict):
        receipt = runtime.record_verified_automation_callback(
            authentication=authentication,
            event_type=event.event_type,
            payload=event.payload,
            outcomes=items,
        )
        result["callback_receipt_id"] = receipt["receipt_id"]
        result["callback_receipt_sha256"] = receipt["receipt_sha256"]
    return result


@router.get("/runs/{run_id}/checkpoints")
def get_agent_run_checkpoints(run_id: str) -> dict:
    if get_agent_run_runtime().get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    items = get_agent_run_runtime().checkpoints(run_id)
    return {
        "schema_version": "open_stock_ai.agent_checkpoint_list.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/approvals")
def list_agent_approvals(run_id: str | None = Query(default=None, max_length=100)) -> dict:
    items = get_agent_run_runtime().list_approvals(run_id)
    return {
        "schema_version": "open_stock_ai.agent_approval_list.v1",
        "count": len(items),
        "items": items,
    }


@router.post("/approvals/{approval_id}/approve")
async def approve_agent_action(approval_id: str, request: AgentApprovalDecision) -> dict:
    try:
        return await get_agent_run_runtime().resolve_approval(
            approval_id,
            approved=True,
            decided_by="stock_ai_ui_user",
            challenge=request.challenge,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent approval not found") from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/approvals/{approval_id}/deny")
async def deny_agent_action(approval_id: str, request: AgentApprovalDecision) -> dict:
    try:
        return await get_agent_run_runtime().resolve_approval(
            approval_id,
            approved=False,
            decided_by="stock_ai_ui_user",
            challenge=request.challenge,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent approval not found") from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/approvals/{approval_id}/challenge")
def issue_agent_approval_challenge(approval_id: str) -> dict:
    manager = get_agent_run_runtime().approval_manager
    if manager is None:
        raise HTTPException(status_code=503, detail="Agent approval manager is unavailable")
    try:
        return manager.issue_challenge(approval_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent approval not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workflows", status_code=201)
def save_agent_workflow(request: AgentWorkflowRequest) -> dict:
    try:
        return get_agent_run_runtime().save_workflow(
            name=request.name,
            plan=request.plan,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/workflows")
def list_agent_workflows() -> dict:
    items = get_agent_run_runtime().list_workflows()
    return {
        "schema_version": "open_stock_ai.agent_workflow_list.v1",
        "count": len(items),
        "items": items,
    }


@router.get("/workflows/{workflow_id}")
def get_agent_workflow(workflow_id: str) -> dict:
    workflow = get_agent_run_runtime().get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Agent workflow not found")
    return workflow


@router.post("/workflows/{workflow_id}/runs", status_code=202)
async def run_agent_workflow(workflow_id: str, request: AgentWorkflowRunRequest) -> dict:
    try:
        return await get_agent_run_runtime().run_workflow(
            workflow_id,
            objective=request.objective,
            patch=request.patch,
            session_id=request.session_id,
            parent_run_id=request.parent_run_id,
            autonomy=request.autonomy,
            max_steps=request.max_steps,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent workflow not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/schedules", status_code=201)
def create_agent_schedule(request: AgentScheduleRequest) -> dict:
    try:
        return get_agent_run_runtime().create_schedule(request.model_dump(exclude_none=True))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/schedules")
def list_agent_schedules(limit: int = Query(default=100, ge=1, le=200)) -> dict:
    items = get_agent_run_runtime().list_schedules(limit=limit)
    return {"schema_version": "open_stock_ai.agent_schedule_list.v1", "count": len(items), "items": items}


@router.patch("/schedules/{schedule_id}")
def update_agent_schedule(schedule_id: str, request: AgentScheduleUpdate) -> dict:
    schedule = get_agent_run_runtime().update_schedule(
        schedule_id,
        request.model_dump(exclude_none=True),
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Agent schedule not found")
    return schedule


@router.post("/schedules/{schedule_id}/pause")
def pause_agent_schedule(schedule_id: str) -> dict:
    schedule = get_agent_run_runtime().disable_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Agent schedule not found")
    return schedule


@router.post("/schedules/{schedule_id}/resume")
def resume_agent_schedule(schedule_id: str) -> dict:
    schedule = get_agent_run_runtime().resume_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Agent schedule not found")
    return schedule


@router.post("/schedules/events", status_code=202)
async def trigger_agent_schedule_event(request: AgentScheduleEvent) -> dict:
    items = await get_agent_run_runtime().trigger_schedule_event(
        request.event_type,
        request.payload,
    )
    return {
        "schema_version": "open_stock_ai.agent_schedule_event_result.v1",
        "count": len(items),
        "items": items,
    }


@router.delete("/schedules/{schedule_id}")
def disable_agent_schedule(schedule_id: str) -> dict:
    schedule = get_agent_run_runtime().disable_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Agent schedule not found")
    return schedule
