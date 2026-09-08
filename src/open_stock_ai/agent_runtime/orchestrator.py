from __future__ import annotations

import hashlib
import json
import inspect
import re
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .approval_manager import ApprovalManager, ApprovalRequiredError
from .checkpoint_manager import CheckpointManager
from .context_broker import ContextBroker
from .completion_contract import (
    evaluate_objective_completion,
    objective_completion_contract,
    observation_is_substantive,
)
from .context_broker_v2 import (
    ContextBrokerV2,
    ModelContextProfile,
    TokenBudgetExceeded,
    TokenBudgetManager as HierarchicalTokenBudgetManager,
)
from .branch_result import BranchResultCompressor
from .contracts import (
    AgentDriver,
    AgentRunContext,
    AgentToolRegistry,
    AgentTurnInput,
    build_runtime_event,
)
from .environment_snapshot import EnvironmentSnapshotBuilder
from .error_taxonomy import classify_error
from .memory.manager import MemoryManager
from .model_router import (
    HostModelBudget,
    HostModelExecutor,
    ModelProfile,
    ModelRole,
    ModelRouter,
    ProviderMode,
    ProviderNeutralRequest,
)
from .plan_compiler import (
    PlanCompiler,
    node_capability,
    node_execution_arguments,
    validate_json_value,
)
from .plan_graph import PlanGraph
from .plan_manager import PlanManager
from .policy_engine import PolicyEngine
from .providers.normalizer import ProviderOutputNormalizer
from .rollback_manager import RollbackManager
from .routing import UnifiedMultiIntentRouter
from .recovery_engine import RecoveryEngine
from .repair import (
    ErrorReceipt,
    FailureFingerprint,
    HostModelRepairPipeline,
    PatchValidationError,
    RecoveryDecision as RepairDecision,
    RecoveryLevel,
    RecoveryStrategyLadder,
)
from .research import ResearchPlanner
from .observability import BackgroundWorkQueue
from .untrusted_content import (
    is_external_content_tool,
    label_tool_observation,
    label_untrusted_content,
    provider_untrusted_context_receipt,
)
from .guardrails import (
    CostBudgetExceeded,
    CostBudgetManager,
    RunawayExecutionExceeded,
    RunawayExecutionGuard,
    RunawayExecutionLimits,
)
from .provider_fallback import ProviderFallbackPolicy
from .validators import (
    ValidatorEngine,
    _has_completed_critic_receipt,
    requested_market_evidence_requirements,
)
from .workers import WorkerSupervisor
from .forest import RuntimeForestAuthority, StepExecutionResult
from .interaction import ReflectionCheckpoint


INTERACTION_PROPOSAL_ARGUMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    # Strict structured-output providers require every declared property to
    # be present. Null keeps one stable schema across follow-up and Automation
    # proposals without allowing arbitrary execution fields.
    "required": ["objective", "intent_json"],
    "properties": {
        "objective": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Natural-language objective for ask/follow_up/create_artifact; null for Automation.",
        },
        "intent_json": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": (
                "JSON-encoded open_stock_ai.automation_intent.v1 for draft_automation; "
                "null for other actions. Never include n8n nodes, credentials, webhooks or workflow JSON."
            ),
        },
    },
}


AGENT_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "state",
        "summary",
        "plan_patch",
        "tool_calls",
        "decision",
        "completion_evaluation",
        "routing_patch",
        "structured_result",
        "interaction",
        "reflection",
        "interaction_proposals",
    ],
    "properties": {
        "state": {
            "type": "string",
            "enum": ["continue", "complete", "waiting_user_input", "waiting_decision"],
        },
        "summary": {"type": "string"},
        "plan_patch": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["reason_summary", "operations_json"],
                    "properties": {
                        "reason_summary": {"type": "string"},
                        "operations_json": {
                            "type": "string",
                            "description": (
                                "JSON-encoded PlanPatch operations. Supported operations are add_node, "
                                "update_node, remove_node, set_completion_criteria, set_fallback_rules, "
                                "add_assumption and add_constraint."
                            ),
                        },
                    },
                },
            ]
        },
        "tool_calls": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "name", "arguments"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "arguments": {
                        "type": "string",
                        "description": "A JSON-encoded object containing only arguments allowed by the selected tool schema.",
                    },
                },
            },
        },
        "decision": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "action",
                        "symbol",
                        "confidence",
                        "confidence_type",
                        "rationale",
                        "next_check",
                    ],
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["buy", "sell", "hold", "watch", "cancel_order", "none"],
                        },
                        "symbol": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
                        "confidence_type": {
                            "type": "string",
                            "enum": ["model_self_reported"],
                        },
                        "rationale": {"type": "string"},
                        "next_check": {"type": "string"},
                    },
                },
            ]
        },
        "completion_evaluation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["criteria_met", "criterion_results", "evidence_ids", "remaining_gaps"],
            "properties": {
                "criteria_met": {"type": "boolean"},
                "criterion_results": {
                    "type": "array",
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["criterion", "met", "evidence_ids"],
                        "properties": {
                            "criterion": {"type": "string"},
                            "met": {"type": "boolean"},
                            "evidence_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 50,
                            },
                        },
                    },
                },
                "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                "remaining_gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            },
        },
        "reflection": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "preferred_option", "alternatives", "should_ask_user",
                        "reason_to_ask", "unknowns", "important_risks", "evidence_ids",
                    ],
                    "properties": {
                        "preferred_option": {"type": "string"},
                        "alternatives": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                        "should_ask_user": {"type": "boolean"},
                        "reason_to_ask": {"type": "string"},
                        "unknowns": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                        "important_risks": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                    },
                },
            ]
        },
        "routing_patch": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["primary_task_kind", "intents", "reason_summary"],
                    "properties": {
                        "primary_task_kind": {
                            "type": "string",
                            "enum": [
                                "general_answer",
                                "artifact_task",
                                "project_task",
                                "market_information",
                                "market_decision",
                                "market_radar",
                                "ui_task",
                                "current_information",
                            ],
                        },
                        "intents": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["type", "confidence"],
                                "properties": {
                                    "type": {"type": "string"},
                                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                },
                            },
                        },
                        "reason_summary": {"type": "string"},
                    },
                },
            ]
        },
        "interaction": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "prompt",
                        "agent_view",
                        "preferred_option",
                        "options",
                        "unknowns",
                        "important_risks",
                    ],
                    "properties": {
                        "prompt": {"type": "string"},
                        "agent_view": {"type": "string"},
                        "preferred_option": {"type": "string"},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 5,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["option_id", "label", "reason"],
                                "properties": {
                                    "option_id": {"type": "string"},
                                    "label": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                            },
                        },
                        "unknowns": {"type": "array", "items": {"type": "string"}},
                        "important_risks": {"type": "array", "items": {"type": "string"}},
                    },
                },
            ]
        },
        "structured_result": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
            ]
        },
        "interaction_proposals": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["proposal_id", "title", "reason", "action", "arguments"],
                "properties": {
                    "proposal_id": {"type": "string"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "action": {"type": "string", "enum": ["ask", "draft_automation", "create_artifact", "follow_up"]},
                    "arguments": json.loads(json.dumps(INTERACTION_PROPOSAL_ARGUMENTS_SCHEMA)),
                },
            },
        },
    },
}


PROPOSAL_EVALUATION_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "decision",
        "evidence_compatible",
        "risk_level",
        "impact_level",
        "recommendation",
        "rationale",
        "safe_alternative",
        "evidence_ids",
    ],
    "properties": {
        "schema_version": {
            "type": "string",
            "enum": ["open_stock_ai.proposal_evaluation.v1"],
        },
        "decision": {
            "type": "string",
            "enum": ["accept", "modify", "reject", "ask"],
        },
        "evidence_compatible": {
            "anyOf": [{"type": "boolean"}, {"type": "null"}],
        },
        "risk_level": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical", "prohibited", "unknown"],
        },
        "impact_level": {
            "type": "string",
            "enum": ["local", "branch", "global", "irreversible", "unknown"],
        },
        "recommendation": {"type": "string"},
        "rationale": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 20,
        },
        "safe_alternative": {"type": "string"},
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 50,
        },
    },
}


SYSTEM_PROMPT = """You are the replaceable operator inside Open Stock AI, not a standalone stock chatbot.
The host application is the vehicle: it owns market data, research, portfolio memory, RiskEngine and the paper broker.
Use the supplied tools to observe the actual system before making a decision. Never invent prices, holdings or fills.
Structured tool results are the source of truth. A candidate is not an executable order permission.
Only produce a stock action, symbol and model_self_reported confidence when the user explicitly asks for an investment or trading decision.
For general questions and factual market-data questions, decision must be null; put the direct answer in summary and do not
invent or repeat a default stock symbol or confidence score. A continue-turn summary must explain the evidence gap and why
the requested tool is the next useful step. A complete-turn summary must directly answer the user's question.
For stable general knowledge, answer directly without tools and keep the default completion criterion. In that case,
criterion_results may mark the default criterion met with empty evidence_ids because no external observation is required.
Do not invent a terminal, browser, project, or web verification step merely to prove stable general knowledge.
For current, recent, niche, or source-specific facts, prefer a dedicated
domain tool; otherwise use web.research, which searches and opens multiple real sources. web.search results are discovery
only and are not enough to support a final factual answer until a source is opened with web.fetch. If a query returns no
results, a page is unreadable, or a source lacks the requested field, revise the query or use another source/tool instead
of concluding that the information does not exist. Prefer primary and official sources, state their data date, and clearly
separate an unavailable current-day release from the latest officially published value.
For questions about this project, inspect it with project.list_files, project.search_text, and project.read_file before answering.
For stock decisions, report decision confidence as a percentage from 0 to 100, set
confidence_type=model_self_reported, and never imply that it is a calibrated success probability.
When the user explicitly asks to choose among two or more strategy, plan, or preference alternatives, first form your
own tentative preference and then return state=waiting_decision with a non-null interaction. The interaction must have
2–5 concrete options, identify the preferred_option, explain each reason, and preserve free-text follow-up through the
Host UI. Do not merely list alternatives in a completed summary. This collaboration rule applies to hypothetical or
general discussions too: keep decision=null unless it is an actual evidence-backed stock decision.
When the Host transcript contains an interaction_response, that response resolves the prior choice. Apply the selected
option and return the resulting answer; do not ask another decision that merely repeats or rephrases the same choice
unless an unrelated, material unknown blocks completion.
After each material evidence stage, complete the public reflection object before finalizing: state your preferred
option, viable alternatives, material unknowns and risks, and whether user involvement would materially change the
next action. This is a concise user-safe decision record, not hidden chain-of-thought. If asking the user has material
value, set should_ask_user=true and return waiting_decision with the same reflection grounded in the Host evidence.
After a complete answer, interaction_proposals may contain 0–3 genuinely useful, non-blocking next steps. For
ask/follow_up/create_artifact set arguments.objective and arguments.intent_json=null. For draft_automation set
arguments.objective=null and arguments.intent_json to a JSON-encoded semantic AutomationIntent; never include
backend nodes, credentials, webhooks or workflow JSON. A proposal never executes until the user confirms it.
If the user says not to trade, query markets, or create automation, keep the response strictly hypothetical and
educational. Do not tell the user to execute an order, monitor a position, submit anything, or create a follow-up
automation; explicitly state that no market query, transaction, or automation was performed.
Only paper trading exists. Live broker execution is unavailable regardless of model or Agent framework.
When paper execution is enabled, preview the exact order before submitting the exact same order.
When the user explicitly requested a local paper trade and paper execution is enabled, that request is the decision to
run the simulation. After a successful exact paper.preview_order, submit that exact local order; do not ask the user to
repeat the same choice because research evidence is incomplete. Clearly retain any data or risk warning in the result.
Only tools present in the host capability manifest are executable in this run. Never imply that an inventoried Skill,
MCP server, browser or native framework tool ran unless the host returns a corresponding successful tool result.
Use external.* tools when their named framework capability is relevant; they execute the vendored project code and return provenance.
For Taiwan futures foreign open-interest questions, use market.taifex_foreign_open_interest before generic web search.
For multi-framework stock analysis, first read market.research_pack, then pass its verified observations into relevant
external core tools such as TradingAgents investment_decision, FinRL simulate_environment, Qlib factor_dataset,
or AI-Trader score_signal. Do not call an external tool only to decorate an answer; use its result as evidence.
When the user explicitly requests an independent Critic, verification Branch, or conflict-resolution Branch for a
market analysis, use agent.run_subtasks with role=critic after collecting the primary evidence. The child task must
challenge the current conclusion and return its independently validated result before the parent completes. When the
objective itself starts with Role: critic, perform that bounded critical verification directly and do not delegate
another critic recursively.
Host project mutation tools require project_execute or full_execute; paper mutation tools require paper_execute or full_execute.
External browser, notification and Connector mutations require external_execute or full_execute. Destructive MCP actions
are available only in full_execute; selecting a mode never enables live brokerage.
Create and revise a provider-neutral PlanGraph through plan_patch. You may add, remove or reorder pending nodes, create
parallel branches, subtasks, subagents, approvals, schedules, workflows, validation, checkpoints and completion criteria.
Never rewrite a running or completed node. A workflow is optional guidance and may be copied or modified.
For a multi-step objective, author a concise, objective-specific user-visible plan in the user's language. Avoid generic
titles such as "analyze data" or "finalize answer": every title and description must say what will be learned or decided
for this exact objective. Give nodes meaningful dependencies. When a node is supported by a tool call from the same turn,
include that tool call id in the node's tool_call_ids. The Host owns node status and completion timestamps.
On every turn after Host observations, summary must explain in natural language: what the just-finished step found,
what evidence or uncertainty is still missing, and exactly what happens next. This is a concise public progress report,
not private chain-of-thought.
Keep the PlanGraph objective-specific, but make the final answer consistently easy to scan. When a complete summary
contains several facts, comparisons or a market decision, write Markdown in the user's language and organize it into
natural numbered sections. Choose each section title, section count, order and table columns from the user's actual
question and the evidence found; do not force a fixed answer template or add an irrelevant section merely to fill one.
Lead with the direct answer, group related observations together, and separate supporting evidence, risks and actionable
next checks wherever they are relevant. When several market observations or candidates need comparison, use at least one
compact Markdown table with one comparable fact per row. Use short sentences and lists instead of joining unrelated
evidence into a dense paragraph. Do not invent a table value when Host evidence does not contain it; label it unavailable.
This presentation guidance standardizes readability, not the content or steps of the model-authored plan.
The Host routing hint is provisional. If the objective has a different primary intent, return routing_patch so the next
turn receives the correct minimum context and tool profile; Host negative constraints and execution policy remain binding.
Return only a JSON object matching the supplied schema. Use state=continue while requesting tools and state=complete
only after enough successful validated observations satisfy the current PlanGraph completion criteria. Except for stable
general knowledge that requires no external observation, at completion,
criterion_results must contain every current criterion verbatim and bind it to host-issued evidence IDs; provider text
without matching host evidence cannot satisfy tests, UI state, project mutations, paper state or other postconditions.
At state=complete, remaining_gaps must list only blockers that prevent the current completion criteria from being
satisfied. Put known data limitations, market uncertainty, and risk caveats that support a valid watch/no-action decision
in summary instead; they may coexist with an empty remaining_gaps list when the requested decision is fully answered."""


UNIVERSAL_SYSTEM_PROMPT = """You are the replaceable reasoning provider inside Open Stock AI.
The Host owns tools, market data, evidence validation, risk limits and execution. Never invent tool results, prices,
positions or fills. Use status=need_tools with actions when evidence is missing. Each action needs only tool and a
JSON-encoded arguments string. Use status=final only after successful Host tool results are present, put the user-facing answer in
message, and copy the Host evidence IDs into evidence_ids. remaining_gaps must be empty only when the answer is complete.
When the user explicitly asks to choose among two or more strategy, plan, or preference alternatives, first form your
own tentative preference and then return status=waiting_decision with a non-null interaction. The interaction must have
2–5 concrete options, identify the preferred_option, explain each reason, and preserve free-text follow-up through the
Host UI. Do not merely list alternatives in a final message. This collaboration rule applies to hypothetical or
general discussions too: keep decision=null unless it is an actual evidence-backed stock decision.
When the Host transcript contains an interaction_response, that response resolves the prior choice. Apply the selected
option and return the resulting answer; do not ask another decision that merely repeats or rephrases the same choice
unless an unrelated, material unknown blocks completion.
After each material evidence stage, fill the public reflection object with a preferred option, alternatives, unknowns,
risks and whether user input has material value. If it does, return waiting_decision with a matching interaction;
otherwise continue or finalize with the reflection record. Do not reveal hidden chain-of-thought.
After a final answer, interaction_proposals may contain 0–3 useful, non-blocking next steps. For normal follow-ups
set arguments.objective and intent_json=null. For draft_automation set objective=null and intent_json to a
JSON-encoded semantic AutomationIntent. Never include backend nodes or credentials; confirmation is mandatory.
If the user says not to trade, query markets, or create automation, keep the response strictly hypothetical and
educational. Do not tell the user to execute an order, monitor a position, submit anything, or create a follow-up
automation; explicitly state that no market query, transaction, or automation was performed.
When the user explicitly requests an independent Critic, verification Branch, or conflict-resolution Branch for a
market analysis, use agent.run_subtasks with role=critic after collecting the primary evidence. The child task must
challenge the current conclusion and return its independently validated result before the parent completes. When the
objective itself starts with Role: critic, perform that bounded critical verification directly and do not delegate
another critic recursively.
When a final message contains several facts, comparisons or a market decision, make it easy to scan with natural
numbered Markdown sections. Derive the section titles, count, order and any table columns from the question instead of
using a fixed template. Use a compact Markdown table when several market observations or candidates need comparison.
This presentation rule must not replace Host evidence or make the Host-owned plan generic.
The Host handles planning, completion bookkeeping and safety policy; do not produce a PlanGraph or validation ledger.
If the Host routing hint is wrong, return routing with primary_task_kind, intents and a short reason_summary.
For Market Radar, result must match the supplied MarketRadarResult schema and cover every supplied Universe symbol.
Return exactly one JSON object matching the supplied schema."""


UNIVERSAL_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    # Codex Structured Outputs requires every top-level property to be listed
    # as required. Optional semantic fields therefore use explicit null unions
    # below instead of being omitted from ``required``.
    "required": [
        "status", "message", "actions", "evidence_ids", "remaining_gaps",
        "decision", "routing", "result", "interaction", "reflection",
        "interaction_proposals",
    ],
    "properties": {
        "status": {
            "type": "string",
            "enum": ["need_tools", "final", "waiting_user_input", "waiting_decision"],
        },
        "message": {"type": "string"},
        "actions": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tool", "arguments"],
                "properties": {
                    "tool": {"type": "string"},
                    "arguments": {
                        "type": "string",
                        "description": "A JSON-encoded object containing only selected tool arguments.",
                    },
                },
            },
        },
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 100,
        },
        "remaining_gaps": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 100,
        },
        "decision": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["action", "symbol", "confidence", "rationale", "next_check"],
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["buy", "sell", "hold", "watch", "cancel_order", "none"],
                        },
                        "symbol": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
                        "rationale": {"type": "string"},
                        "next_check": {"type": "string"},
                    },
                },
            ]
        },
        "routing": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["primary_task_kind", "intents", "reason_summary"],
                    "properties": {
                        "primary_task_kind": {"type": "string"},
                        "intents": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["type", "confidence"],
                                "properties": {
                                    "type": {"type": "string"},
                                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                },
                            },
                        },
                        "reason_summary": {"type": "string"},
                    },
                },
            ]
        },
        "result": {"type": "null"},
        "interaction_proposals": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["proposal_id", "title", "reason", "action", "arguments"],
                "properties": {
                    "proposal_id": {"type": "string"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "action": {"type": "string", "enum": ["ask", "draft_automation", "create_artifact", "follow_up"]},
                    "arguments": json.loads(json.dumps(INTERACTION_PROPOSAL_ARGUMENTS_SCHEMA)),
                },
            },
        },
        "interaction": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "prompt",
                        "agent_view",
                        "preferred_option",
                        "options",
                        "unknowns",
                        "important_risks",
                    ],
                    "properties": {
                        "prompt": {"type": "string"},
                        "agent_view": {"type": "string"},
                        "preferred_option": {"type": "string"},
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 5,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["option_id", "label", "reason"],
                                "properties": {
                                    "option_id": {"type": "string"},
                                    "label": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                            },
                        },
                        "unknowns": {"type": "array", "items": {"type": "string"}},
                        "important_risks": {"type": "array", "items": {"type": "string"}},
                    },
                },
            ]
        },
        "reflection": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "preferred_option", "alternatives", "should_ask_user",
                        "reason_to_ask", "unknowns", "important_risks", "evidence_ids",
                    ],
                    "properties": {
                        "preferred_option": {"type": "string"},
                        "alternatives": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                        "should_ask_user": {"type": "boolean"},
                        "reason_to_ask": {"type": "string"},
                        "unknowns": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                        "important_risks": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                    },
                },
            ]
        },
    },
}


class AgentOrchestrator:
    """Host-owned observe/think/act loop shared by every Agent framework."""

    def __init__(
        self,
        *,
        drivers: dict[str, AgentDriver],
        tools: AgentToolRegistry,
        default_driver: str,
        plan_manager: PlanManager | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        approval_manager: ApprovalManager | None = None,
        memory_manager: MemoryManager | None = None,
        snapshot_builder: EnvironmentSnapshotBuilder | None = None,
        worker_supervisor: WorkerSupervisor | None = None,
        validator: ValidatorEngine | None = None,
        recovery: RecoveryEngine | None = None,
        repair_ladder: RecoveryStrategyLadder | None = None,
        policy_engine: PolicyEngine | None = None,
        rollback_manager: RollbackManager | None = None,
        provider_registry: Any | None = None,
        control_provider: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.drivers = dict(drivers)
        self.tools = tools
        self.default_driver = default_driver
        self.plan_manager = plan_manager
        self.checkpoint_manager = checkpoint_manager
        self.approval_manager = approval_manager
        self.memory_manager = memory_manager
        self.snapshot_builder = snapshot_builder
        self.worker_supervisor = worker_supervisor
        self.validator = validator or ValidatorEngine()
        self.recovery = recovery or RecoveryEngine()
        self.repair_ladder = repair_ladder or RecoveryStrategyLadder()
        self.background_work = BackgroundWorkQueue(max_workers=2) if memory_manager else None
        self.policy_engine = policy_engine or PolicyEngine()
        self.rollback_manager = rollback_manager or RollbackManager()
        self.provider_registry = provider_registry
        self.control_provider = control_provider

    def describe(self) -> dict[str, Any]:
        capability_status = getattr(self.tools, "describe_capabilities", None)
        return {
            "schema_version": "open_stock_ai.agent_runtime.v2",
            "architecture": "durable_stock_ai_supervisor_plan_graph_host_executed_tools",
            "default_driver": self.default_driver,
            "agent_identity": "Stock AI Agent",
            "drivers": [driver.describe() for driver in self.drivers.values()],
            "providers": self.provider_registry.describe() if self.provider_registry else None,
            "tools": self.tools.manifest(),
            "capability_registry": capability_status() if callable(capability_status) else None,
            "boundaries": {
                "codex_native_capabilities_preserved": True,
                "native_codex_route": "/api/codex/run (developer diagnostics; disabled by default)",
                "agent_runtime_never_delegates_to_codex_chat": True,
                "live_trading": False,
                "paper_execution_requires_explicit_mode": True,
                "paper_order_requires_matching_preview": True,
                "project_execution_requires_explicit_mode": True,
                "native_agent_tools_preserved": True,
            },
        }

    async def run(
        self,
        *,
        objective: str,
        symbols: list[str] | tuple[str, ...] | None = None,
        driver_id: str | None = None,
        fallback_driver_id: str | None = None,
        autonomy: str = "advisory",
        max_steps: int = 6,
        run_id: str | None = None,
        session_id: str | None = None,
        parent_run_id: str | None = None,
        initial_plan: dict[str, Any] | None = None,
        resume_state: dict[str, Any] | None = None,
        initial_sequence: int = 0,
        session_history: list[dict[str, Any]] | None = None,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        selected_driver = driver_id or self.default_driver
        driver = self.drivers.get(selected_driver)
        if driver is None:
            raise ValueError(f"Unknown Agent driver: {selected_driver}")
        objective_text = objective.strip()
        task_kind = _classify_task(objective_text)
        supplied_symbols = tuple(
            dict.fromkeys(str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol).strip())
        )
        routing = UnifiedMultiIntentRouter().route(
            objective_text,
            task_kind_hint=task_kind,
            supplied_symbols=supplied_symbols,
        )
        task_kind = routing.primary_task_kind
        normalized_symbols = (
            tuple(
                dict.fromkeys(
                    context.symbol
                    for context in routing.symbol_contexts
                    if context.symbol and context.user_confirmed
                )
            )
            if routing.requires_market_context
            else ()
        )
        active_run_id = run_id or f"AR-{uuid4().hex}"
        active_session_id = session_id or active_run_id
        lifecycle_context = AgentRunContext(
            run_id=active_run_id,
            autonomy=autonomy,
            symbols=normalized_symbols,
            driver_id=selected_driver,
            session_id=active_session_id,
            parent_run_id=parent_run_id,
            allow_paper_orders=autonomy in {"paper_execute", "full_execute"},
            allow_project_actions=autonomy in {"project_execute", "full_execute"},
            allow_external_actions=autonomy in {"external_execute", "full_execute"},
        )
        lifecycle_context.state["task_kind"] = task_kind
        lifecycle_context.state["routing"] = routing.model_dump()
        lifecycle_context.state["explicit_paper_order_authorized"] = (
            lifecycle_context.allow_paper_orders
            and objective_completion_contract(objective_text, task_kind)["paper_order_requested"]
        )
        await _driver_lifecycle(driver, "start_run", lifecycle_context)
        lifecycle_drivers: list[AgentDriver] = [driver]
        result: dict[str, Any] | None = None
        try:
            result = await self._run_once(
                objective=objective,
                symbols=symbols,
                driver_id=driver_id,
                fallback_driver_id=fallback_driver_id,
                autonomy=autonomy,
                max_steps=max_steps,
                run_id=active_run_id,
                session_id=active_session_id,
                parent_run_id=parent_run_id,
                initial_plan=initial_plan,
                resume_state=resume_state,
                initial_sequence=initial_sequence,
                session_history=session_history,
                event_sink=event_sink,
                lifecycle_drivers=lifecycle_drivers,
            )
            return result
        finally:
            if result is None or result.get("status") not in {"waiting_approval", "suspended"}:
                try:
                    if self.worker_supervisor is not None:
                        await self.worker_supervisor.close_run(active_run_id)
                    await _tool_lifecycle(self.tools, "close_run", active_run_id)
                finally:
                    closed: set[int] = set()
                    for lifecycle_driver in reversed(lifecycle_drivers):
                        if id(lifecycle_driver) in closed:
                            continue
                        closed.add(id(lifecycle_driver))
                        await _driver_lifecycle(lifecycle_driver, "close_run", active_run_id)

    async def _run_once(
        self,
        *,
        objective: str,
        symbols: list[str] | tuple[str, ...] | None = None,
        driver_id: str | None = None,
        fallback_driver_id: str | None = None,
        autonomy: str = "advisory",
        max_steps: int = 6,
        run_id: str,
        session_id: str,
        parent_run_id: str | None = None,
        initial_plan: dict[str, Any] | None = None,
        resume_state: dict[str, Any] | None = None,
        initial_sequence: int = 0,
        session_history: list[dict[str, Any]] | None = None,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        lifecycle_drivers: list[AgentDriver] | None = None,
    ) -> dict[str, Any]:
        selected_driver = driver_id or self.default_driver
        driver = self.drivers.get(selected_driver)
        if driver is None:
            raise ValueError(f"Unknown Agent driver: {selected_driver}")
        describe_driver = getattr(driver, "describe", None)
        driver_description = describe_driver() if callable(describe_driver) else {}
        model_id = str(
            driver_description.get("model")
            or ("codex-app-server/default" if selected_driver == "codex" else selected_driver)
        )
        if autonomy not in {"advisory", "project_execute", "paper_execute", "external_execute", "full_execute"}:
            raise ValueError(f"Unsupported autonomy mode: {autonomy}")
        objective_text = objective.strip()
        task_kind = _classify_task(objective_text)
        supplied_symbols = tuple(
            dict.fromkeys(str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol).strip())
        )
        routing = UnifiedMultiIntentRouter().route(
            objective_text,
            task_kind_hint=task_kind,
            supplied_symbols=supplied_symbols,
        )
        task_kind = routing.primary_task_kind
        normalized_symbols = (
            tuple(
                dict.fromkeys(
                    symbol_context.symbol
                    for symbol_context in routing.symbol_contexts
                    if symbol_context.symbol and symbol_context.user_confirmed
                )
            )
            if routing.requires_market_context
            else ()
        )
        requires_observation = task_kind != "general_answer"

        context = AgentRunContext(
            run_id=run_id,
            autonomy=autonomy,
            symbols=normalized_symbols,
            driver_id=selected_driver,
            session_id=session_id,
            parent_run_id=parent_run_id,
            allow_paper_orders=autonomy in {"paper_execute", "full_execute"},
            allow_project_actions=autonomy in {"project_execute", "full_execute"},
            allow_external_actions=autonomy in {"external_execute", "full_execute"},
        )
        context.state["task_kind"] = task_kind
        context.state["routing"] = routing.model_dump()
        context.state["explicit_paper_order_authorized"] = (
            context.allow_paper_orders
            and objective_completion_contract(objective_text, task_kind)["paper_order_requested"]
        )
        execution_forest = dict((resume_state or {}).get("execution_forest") or {})
        context.state["execution_forest_identity"] = execution_forest
        context.state["forest_execution_authority"] = _new_forest_execution_authority(
            context,
            objective=objective_text,
        )
        await _tool_prepare(self.tools, context)
        restored_payload = ((resume_state or {}).get("checkpoint") or {}).get("payload") or {}
        # ``execution_forest`` is supplied on every Run, including the first
        # provider dispatch, solely to bind the executor to the durable
        # SQLite Forest.  It must not turn a new Run into a resumed Run.
        is_resume = bool(
            (resume_state or {}).get("checkpoint")
            or (resume_state or {}).get("next_step")
            or (resume_state or {}).get("recovery_context")
        )
        transcript: list[dict[str, Any]] = list(restored_payload.get("transcript") or [
            {
                "role": "host",
                "type": "run_context",
                "content": {
                    "symbols": list(normalized_symbols),
                    "task_kind": task_kind,
                    "autonomy": autonomy,
                    "live_trading_available": False,
                    "paper_execution_enabled": context.allow_paper_orders,
                    "project_execution_enabled": context.allow_project_actions,
                    "external_execution_enabled": context.allow_external_actions,
                    "routing": routing.model_dump(),
                },
            }
        ])
        if not restored_payload.get("transcript") and session_history:
            transcript.append(
                {
                    "role": "host",
                    "type": "conversation_history",
                    "content": _provider_conversation_history(
                        session_history,
                        task_kind=task_kind,
                    ),
                }
            )
        elif session_history:
            # A resumed Run restores its own bounded transcript, so its
            # conversation-history projection is intentionally not replayed.
            # A resolved interaction is different: it is fresh user intent for
            # this exact Run and must be delivered to the next provider turn.
            # Without this bridge, a provider sees the original choice request
            # again and can create a duplicate decision card after the user
            # has already selected an option.
            existing_interaction_ids = {
                str((item.get("content") or {}).get("interaction_id") or "")
                for item in transcript
                if item.get("type") == "interaction_response"
                and isinstance(item.get("content"), dict)
            }
            for item in session_history:
                source = item.get("source") if isinstance(item, dict) else None
                content = item.get("content") if isinstance(item, dict) else None
                if (
                    not isinstance(source, dict)
                    or source.get("type") != "interaction_response"
                    or not isinstance(content, dict)
                ):
                    continue
                interaction_id = str(content.get("interaction_id") or "")
                if interaction_id and interaction_id in existing_interaction_ids:
                    continue
                transcript.append(
                    {
                        "role": "user",
                        "type": "interaction_response",
                        "content": _safe_arguments(content),
                    }
                )
                if interaction_id:
                    existing_interaction_ids.add(interaction_id)
        trace: list[dict[str, Any]] = list(restored_payload.get("trace") or [])
        activity: list[dict[str, Any]] = []
        failure_fingerprints_by_node: dict[str, dict[str, Any]] = {}
        all_tool_manifest = self.tools.manifest()
        context_broker = (
            self.snapshot_builder.context_broker
            if self.snapshot_builder is not None
            and isinstance(getattr(self.snapshot_builder, "context_broker", None), ContextBroker)
            else ContextBroker()
        )
        tool_manifest = context_broker.filter_capabilities(
            all_tool_manifest,
            task_kind=task_kind,
            task_kinds=_context_task_kinds(routing, context),
        )
        tool_manifest = _extend_explicit_paper_market_coverage_capabilities(
            tool_manifest,
            all_tool_manifest=all_tool_manifest,
            objective=objective_text,
            task_kind=task_kind,
            context=context,
        )
        tool_manifest = _extend_host_owned_critic_capability(
            tool_manifest,
            all_tool_manifest=all_tool_manifest,
            objective=objective_text,
            task_kind=task_kind,
        )
        tool_metadata = {item["name"]: item for item in tool_manifest}
        # A checkpoint is a historical execution snapshot, not the authority
        # for PlanGraph revisions.  Always rehydrate the latest append-only
        # plan when a manager is present; otherwise a resumed run can write an
        # older checkpoint graph over a newer recovery revision.
        plan = self.plan_manager.for_run(run_id) if self.plan_manager else (
            PlanGraph.from_dict(restored_payload["plan"])
            if isinstance(restored_payload.get("plan"), dict)
            else None
        )
        if plan is None:
            workflow_plan = PlanGraph.from_dict(initial_plan) if isinstance(initial_plan, dict) else None
            plan = (
                self.plan_manager.create(
                    session_id=session_id,
                    run_id=run_id,
                    objective=objective_text,
                    plan=workflow_plan,
                )
                if self.plan_manager
                else workflow_plan or PlanGraph.create(objective_text)
            )
        if is_resume and self.approval_manager:
            for node in plan.nodes.values():
                # The approval checkpoint can be persisted immediately after
                # ``step.started`` and before the later blocked projection is
                # saved.  In that narrow crash-safe window the authoritative
                # PlanManager revision still says ``running`` even though the
                # tool has never executed.  A resolved approval is the Host
                # receipt that makes that exact node ready; leaving it running
                # removes it from ``executable_nodes`` and incorrectly drives
                # the Run into final-synthesis recovery without ever invoking
                # the approved tool.
                if node.status not in {"running", "blocked", "waiting_approval"}:
                    continue
                approval = self.approval_manager.latest_for_step(run_id, node.node_id)
                if approval and approval.get("status") == "approved":
                    plan.mark(node.node_id, "ready")
        approved_resume_calls = (
            _approved_resume_tool_calls(
                plan=plan,
                run_id=run_id,
                manager=self.approval_manager,
            )
            if is_resume and self.approval_manager
            else []
        )
        if isinstance(restored_payload.get("context_state"), dict):
            context.state.update(restored_payload["context_state"])
            restored_task_kind = str(context.state.get("task_kind") or "").strip()
            if restored_task_kind:
                task_kind = restored_task_kind
                requires_observation = task_kind != "general_answer"
            restored_routing = context.state.get("routing")
            if isinstance(restored_routing, dict):
                try:
                    routing = type(routing).model_validate(restored_routing)
                except (TypeError, ValueError):
                    pass
            # A retry can start in a new process. Preserve the model-corrected
            # task scope from the safe checkpoint so the compiler receives the
            # same Host capabilities that were available before the restart.
            tool_manifest = context_broker.filter_capabilities(
                all_tool_manifest,
                task_kind=task_kind,
                task_kinds=_context_task_kinds(routing, context),
            )
            tool_manifest = _extend_explicit_paper_market_coverage_capabilities(
                tool_manifest,
                all_tool_manifest=all_tool_manifest,
                objective=objective_text,
                task_kind=task_kind,
                context=context,
            )
            tool_metadata = {item["name"]: item for item in tool_manifest}
        context.previewed_orders.update((context.state.get("paper_previews") or {}).keys())
        sequence = max(
            int(initial_sequence or 0),
            int((resume_state or {}).get("last_sequence") or 0),
        )
        previous_event_id: str | None = None

        async def record(event_type: str, **payload: Any) -> None:
            nonlocal sequence, previous_event_id
            sequence += 1
            event = build_runtime_event(
                event_type,
                sequence=sequence,
                run_id=run_id,
                session_id=session_id,
                payload=payload,
                plan_id=plan.plan_id,
                plan_revision=plan.revision_number,
                previous_event_id=previous_event_id,
            )
            previous_event_id = event["event_id"]
            activity.append(event)
            if event_sink is not None:
                emitted = event_sink(event)
                if inspect.isawaitable(emitted):
                    await emitted

        async def record_repair_decision(
            *,
            classified: Any,
            node_id: str,
            tool_name: str,
            arguments: dict[str, Any],
            output: Any,
            previous_level: RecoveryLevel | None = None,
        ) -> RepairDecision:
            receipt = ErrorReceipt(
                category=str(classified.category),
                component="tool_executor",
                # The Plan node remains on the receipt as ``branch_id``.  Keep
                # the failure path stable across model-created replacement
                # nodes so the same tool failure has the same fingerprint for
                # the whole Run (P39).
                location=f"$.tools.{tool_name}",
                expected="Host-validated tool result",
                actual=str(classified.message),
                retryable=bool(classified.retryable),
                completed_work_preserved=True,
                branch_id=node_id,
            )
            fingerprint = FailureFingerprint.from_receipt(
                receipt,
                provider=selected_driver,
                model=selected_driver,
                tool=tool_name,
                schema_version="open_stock_ai.tool_result.v1",
            )
            failure_fingerprints_by_node[node_id] = fingerprint.to_dict()
            decision = self.repair_ladder.decide(
                receipt,
                fingerprint=fingerprint,
                output=output,
                arguments=arguments,
                previous_level=previous_level,
            )
            await record(
                "error.receipt.created",
                node_id=node_id,
                tool=tool_name,
                error_receipt=receipt.to_dict(),
                failure_fingerprint=fingerprint.to_dict(),
                completed_work_preserved=True,
            )
            await record(
                "repair.attempted",
                node_id=node_id,
                tool=tool_name,
                error_id=receipt.error_id,
                fingerprint=fingerprint.digest,
                level=int(decision.level),
                strategy=decision.strategy,
                identical_retry_blocked=decision.identical_retry_blocked,
                preserve_other_branches=decision.preserve_other_branches,
                arguments=arguments,
                output=output,
            )
            return decision

        async def report_running_reasoning_nodes(
            *,
            turn_step: int,
            turn_summary: str,
            remaining_gaps: list[str],
        ) -> None:
            running = [
                node
                for node in sorted(
                    plan.nodes.values(),
                    key=lambda item: (item.order_index, item.node_id),
                )
                if node.node_type == "reasoning" and node.status == "running"
            ]
            if not running:
                return
            for node in running:
                related_trace = [
                    item
                    for item in trace
                    if str(item.get("call_id") or "") in set(node.tool_call_ids)
                ]
                step_summary = _reasoning_step_summary(
                    node_title=node.title,
                    related_trace=related_trace,
                    fallback=turn_summary,
                )
                node.result_summary = step_summary
                node.metadata = {
                    **node.metadata,
                    "remaining_gaps": list(remaining_gaps),
                }
                plan.mark(node.node_id, "completed")
                next_node = next(
                    (
                        item
                        for item in sorted(
                            plan.nodes.values(),
                            key=lambda candidate: (
                                candidate.order_index,
                                candidate.node_id,
                            ),
                        )
                        if item.status in {"pending", "ready"}
                        and item.node_type == "reasoning"
                    ),
                    None,
                )
                next_step = next_node.title if next_node else "整理並回覆最終結論"
                node.metadata["next_step"] = next_step
                await record(
                    "plan.step.reported",
                    step=turn_step,
                    step_id=node.node_id,
                    node_id=node.node_id,
                    node_type=node.node_type,
                    title=node.title,
                    result_summary=step_summary,
                    remaining_gaps=list(remaining_gaps),
                    next_step=next_step,
                    host_results=[
                        _result_summary(item)
                        for item in related_trace
                    ],
                    plan=plan.to_dict(),
                )
                await record(
                    "plan.node.completed",
                    step=turn_step,
                    node_id=node.node_id,
                    node_type=node.node_type,
                    result_summary=step_summary,
                    remaining_gaps=list(remaining_gaps),
                    next_step=next_step,
                )
                await record(
                    "step.completed",
                    step=turn_step,
                    step_id=node.node_id,
                    node_id=node.node_id,
                    node_type=node.node_type,
                    title=node.title,
                    result_summary=step_summary,
                    remaining_gaps=list(remaining_gaps),
                    next_step=next_step,
                )
            if self.plan_manager:
                self.plan_manager.save_state(plan, run_id=run_id)

        # External framework providers run below this host-owned loop.  Give
        # them a run-local event recorder so Codex model requests and selected
        # framework tools appear in the same audited activity stream.  The
        # callback never leaves this process and is discarded with the run.
        context.state["record_event"] = record
        memories = (
            self.memory_manager.retrieve(objective_text, session_id=session_id)
            if self.memory_manager
            else []
        )
        current_snapshot = (
            self.snapshot_builder.build(
                context,
                plan=plan.to_dict(),
                pending_approvals=self.approval_manager.pending(run_id) if self.approval_manager else [],
                memories=memories,
            )
            if self.snapshot_builder
            else None
        )

        await record(
            "run.resumed" if is_resume else "run.started",
            run_id=run_id,
            session_id=session_id,
            driver=selected_driver,
            autonomy=autonomy,
            symbols=list(normalized_symbols),
            objective=objective.strip(),
            task_kind=task_kind,
            available_tool_count=len(tool_manifest),
            execution_mode="durable_plan_graph",
        )
        await record(
            "assistant.message.started",
            summary="Agent response started.",
            status="streaming",
        )
        await record(
            "memory.retrieved",
            returned_count=len(memories),
            relevant_count=len(memories),
            memory_ids=[
                str(item.get("memory_id"))
                for item in memories
                if isinstance(item, dict) and item.get("memory_id")
            ],
            selection_policy="governed_relevance_filter",
        )
        if current_snapshot is not None:
            await record(
                "context.snapshot.created",
                snapshot_id=current_snapshot.snapshot_id,
                snapshot_hash=current_snapshot.hash,
                snapshot=current_snapshot.to_dict(),
                reason="resume" if is_resume else "run_start",
            )
            transcript.append(
                {
                    "role": "host",
                    "type": "environment_snapshot",
                    "content": current_snapshot.to_dict(),
                }
            )
        transcript.append(
            {
                "role": "host",
                "type": "plan_graph",
                "content": plan.to_dict(),
            }
        )
        if memories:
            transcript.append({"role": "host", "type": "retrieved_memory", "content": memories})
        await record(
            "plan.proposed",
            plan_id=plan.plan_id,
            revision=plan.revision_number,
            plan=plan.to_dict(),
            restored=is_resume,
        )
        research_plan: dict[str, Any] | None = None
        if routing.requires_market_context or task_kind in {
            "market_information",
            "market_decision",
            "market_radar",
            "current_information",
        }:
            research_plan = asdict(
                ResearchPlanner().plan(
                    objective_text,
                    symbols=normalized_symbols,
                    decision_horizon="current",
                )
            )
            transcript.append(
                {"role": "host", "type": "research_plan", "content": research_plan}
            )
            await record(
                "research.plan.created",
                research_plan_id=research_plan["plan_id"],
                research_plan=research_plan,
                source_requirements=research_plan["source_requirements"],
                stop_criteria=research_plan["stop_criteria"],
            )
        successful_observations = sum(
            1 for item in trace if _observation_is_substantive(item)
        )
        final_turn: dict[str, Any] | None = None
        # A fresh UI run still defaults to a small budget, while an explicitly
        # continued run may extend the same durable graph.  The Host keeps a
        # hard ceiling so a provider cannot create an unbounded loop.  The
        # small extension beyond the normal recovery budget is reserved for a
        # durable completion-projection repair (P78), not provider-controlled
        # open-ended work.
        bounded_steps = max(1, min(int(max_steps), 60))
        # A runtime-level resume knows whether it must repeat an interrupted
        # turn or continue after a durable terminal checkpoint.  Prefer that
        # explicit cursor over an older checkpoint payload; otherwise a
        # suspended continuation can silently restart at step 1 and replay
        # every completed branch.
        resumed_step = int(
            (resume_state or {}).get("next_step")
            or (restored_payload.get("context_state") or {}).get("current_step")
            or 1
        )
        start_step = max(1, min(resumed_step, bounded_steps)) if is_resume else 1
        # Context and token limits are Host-owned.  A provider never receives
        # the unbounded historical transcript as its working prompt; it gets a
        # scoped ContextBroker v2 package and a compressed evidence-linked
        # branch result instead.  The accounting snapshot is checkpointed in
        # ``context.state`` so a resumed Run cannot reset its own allowance.
        context_broker_v2 = ContextBrokerV2()
        budget_manager = HierarchicalTokenBudgetManager()
        budget_snapshot = dict(context.state.get("token_budget") or {})
        session_scope = f"session:{session_id}"
        run_scope = f"run:{run_id}"
        branch_scope = f"branch:{run_id}:root"
        session_limit = max(48_000, bounded_steps * 48_000)
        run_limit = max(36_000, bounded_steps * 40_000)
        session_budget = budget_manager.register(
            session_scope,
            scope_type="session",
            limit=session_limit,
        )
        run_budget = budget_manager.register(
            run_scope,
            scope_type="run",
            limit=run_limit,
            parent_scope_id=session_scope,
        )
        branch_budget = budget_manager.register(
            branch_scope,
            scope_type="branch",
            limit=max(24_000, bounded_steps * 32_000),
            parent_scope_id=run_scope,
        )
        prior_used = min(
            int(budget_snapshot.get("run_used") or 0),
            run_budget.limit,
        )
        if prior_used:
            budget_manager.consume(run_scope, prior_used)
        context.state["token_budget"] = {
            "session_scope": session_scope,
            "run_scope": run_scope,
            "branch_scope": branch_scope,
            "session_limit": session_budget.limit,
            "run_limit": run_budget.limit,
            "session_used": budget_manager.get(session_scope).consumed,
            "run_used": budget_manager.get(run_scope).consumed,
            "branch_used": branch_budget.consumed,
        }
        budget_exhausted = False
        cost_budget_exhausted = False
        paused_approval: dict[str, Any] | None = None
        paused_interaction: dict[str, Any] | None = None
        completion_validation: dict[str, Any] | None = None
        host_finalized_verified_paper_order = False
        host_finalized_analysis_data_unavailable = False
        provider_protocol = "universal_v1"
        provider_conformance: dict[str, Any] | None = None
        is_critic_run = objective_text.lstrip().lower().startswith("role: critic")
        recovery_only_exhausted = False
        completion_output_repair_exhausted = False
        cost_budget_manager = CostBudgetManager()
        cost_budget_manager.register(
            "global",
            "global",
            limit=max(120_000, bounded_steps * 12_000),
        )
        cost_budget_manager.register(
            "session",
            session_id,
            limit=max(96_000, bounded_steps * 10_000),
        )
        cost_budget_manager.ensure(
            "provider",
            selected_driver,
            limit=max(72_000, bounded_steps * 8_000),
        )
        runaway_guard = RunawayExecutionGuard(
            RunawayExecutionLimits(
                max_depth=5,
                max_nodes=128,
                max_tool_calls=max(32, bounded_steps * 8),
                max_repeated_signature=2,
                max_no_progress_turns=3,
            )
        )
        context.state["execution_guard"] = {
            "cost_budgets": cost_budget_manager.snapshot(),
            "runaway": runaway_guard.snapshot(),
        }
        # A local server restart must not make the same invalid final answer
        # look new again.  This counter is included in every checkpoint below
        # so P39's "no identical retry" rule survives the durable resume path.
        completion_output_failures = {
            str(key): int(value)
            for key, value in (
                context.state.get("completion_output_failures") or {}
            ).items()
            if isinstance(value, int) and value > 0
        }
        recovery_surface_no_progress = 0
        last_trace_count = len(trace)

        for step in range(start_step, bounded_steps + 1):
            host_resumed_approval_dispatch = bool(approved_resume_calls)
            try:
                runaway_guard.observe_turn(
                    progress=step == start_step or len(trace) > last_trace_count
                )
            except RunawayExecutionExceeded as exc:
                cost_budget_exhausted = True
                final_turn = {
                    "state": "continue",
                    "summary": "Host execution guard blocked a no-progress run.",
                    "tool_calls": [],
                    "decision": None,
                    "completion_evaluation": {
                        "remaining_gaps": ["execution_guard_blocked"]
                    },
                }
                await record(
                    "execution_guard.blocked",
                    step=step,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    guard={
                        "cost_budgets": cost_budget_manager.snapshot(),
                        "runaway": runaway_guard.snapshot(),
                    },
                )
                break
            last_trace_count = len(trace)
            context.state["current_step"] = step
            if _should_host_finalize_verified_paper_order(
                turn={"state": "complete", "tool_calls": []},
                trace=trace,
                context=context,
                objective=objective_text,
                task_kind=task_kind,
            ):
                # The exact local paper order already has a durable preview
                # and submit receipt.  Do not spend another remote-model turn
                # just to request a prose finalisation: that used to hit the
                # provider budget after the successful simulated order, then
                # misleadingly create a "rebuild goal" input draft.
                host_finalized_verified_paper_order = True
                final_turn = {
                    "state": "complete",
                    "summary": _verified_paper_order_summary(trace),
                    "tool_calls": [],
                    "decision": None,
                    "completion_evaluation": _host_completion_evaluation(plan, trace),
                }
                known_evidence_ids = {
                    str(value)
                    for item in trace
                    if item.get("ok") is True
                    for value in (
                        item.get("call_id"),
                        item.get("node_id"),
                        (item.get("validation") or {}).get("evidence_hash"),
                    )
                    if value
                }
                known_evidence_ids.update(
                    str(memory["memory_id"])
                    for memory in memories
                    if memory.get("memory_id")
                )
                if current_snapshot is not None:
                    known_evidence_ids.update(
                        {current_snapshot.snapshot_id, current_snapshot.hash}
                    )
                known_evidence_ids.update(
                    node.node_id
                    for node in plan.nodes.values()
                    if node.status in {"completed", "skipped"}
                )
                evidence_catalog = {
                    str(value): item
                    for item in trace
                    if item.get("ok") is True
                    for value in (
                        item.get("call_id"),
                        item.get("node_id"),
                        (item.get("validation") or {}).get("evidence_hash"),
                    )
                    if value
                }
                completion_validation = self.validator.validate_completion(
                    state="complete",
                    objective=objective_text,
                    task_kind=task_kind,
                    final_summary=final_turn["summary"],
                    has_pending_tool_calls=False,
                    plan=plan.to_dict(),
                    successful_observations=successful_observations,
                    evidence_required=requires_observation,
                    completion_evaluation=final_turn["completion_evaluation"],
                    known_evidence_ids=known_evidence_ids,
                    evidence_catalog=evidence_catalog,
                    failure_recovery_coverage=_failure_recovery_coverage(plan, trace),
                    decision=None,
                ).to_dict()
                if completion_validation.get("passed") is True:
                    _complete_finalize_nodes(plan)
                    if self.plan_manager:
                        self.plan_manager.save_state(plan, run_id=run_id)
                await record(
                    "completion.host_finalized",
                    step=step,
                    reason="verified_explicit_local_paper_order_without_provider_finalization",
                    completion_validation=completion_validation,
                )
                break
            controls = self.control_provider(run_id) if self.control_provider else []
            for control in controls:
                transcript.append(
                    {"role": "host", "type": "control_message", "content": control}
                )
                await record(
                    "plan.replan.requested",
                    step=step,
                    control_id=control.get("control_id"),
                    instruction=(control.get("payload") or {}).get("instruction"),
                )
            async def model_event_sink(model_event: dict[str, Any]) -> None:
                payload = dict(model_event)
                event_type = str(payload.pop("type", "model.sdk.event"))
                payload.setdefault("step", step)
                await record(event_type, **payload)

            if step == start_step and self.provider_registry is not None:
                try:
                    provider_conformance = await self.provider_registry.negotiate(
                        selected_driver,
                        session_id=run_id,
                    )
                    provider_protocol = str(
                        provider_conformance.get("protocol") or "universal_v1"
                    )
                    await record(
                        "provider.conformance.completed",
                        step=step,
                        provider=selected_driver,
                        protocol=provider_protocol,
                        report=provider_conformance.get("report"),
                    )
                except Exception as exc:
                    provider_protocol = "universal_v1"
                    await record(
                        "provider.conformance.failed",
                        step=step,
                        provider=selected_driver,
                        protocol=provider_protocol,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )

            disclosed_tool_manifest = context_broker.disclose_capabilities(
                all_tool_manifest,
                task_kind=task_kind,
                step=step,
                task_kinds=_context_task_kinds(routing, context),
                phase=("research" if research_plan is not None and step == 1 else None),
            )
            disclosed_tool_manifest = _limit_market_information_recursion(
                disclosed_tool_manifest,
                task_kind=task_kind,
                objective=objective_text,
            )
            recovery_surface, recovery_requirements = _recovery_tool_surface(
                trace,
                tool_manifest,
            )
            recovery_allowed_names = {
                str(item.get("name") or "") for item in recovery_surface
            }
            if recovery_surface:
                # P40 L4/L5 are not a best-effort sentence hidden inside a
                # huge tool list.  Once a local branch has failed, give the
                # next model turn a narrow, safe execution surface containing
                # only independently viable alternatives.  The failed tool is
                # deliberately absent, so a provider cannot spend the rest of
                # the Run repeating it or issuing a fluent completion draft.
                disclosed_tool_manifest = recovery_surface
                recovery_signature = json.dumps(recovery_requirements, sort_keys=True)
                if context.state.get("recovery_tool_surface") != recovery_signature:
                    context.state["recovery_tool_surface"] = recovery_signature
                    transcript.append(
                        {
                            "role": "host",
                            "type": "recovery_tool_surface",
                            "content": {
                                "requirements": recovery_requirements,
                                "instruction": (
                                    "This is a recovery-only turn. Use one of the provided tools to "
                                    "obtain independent, validated evidence for the failed local branch. "
                                    "Do not retry an omitted tool and do not return a final answer until "
                                    "the Host records recovery_for."
                                ),
                            },
                        }
                    )
                    await record(
                        "repair.recovery_surface_enforced",
                        step=step,
                        strategy=RecoveryLevel.ALTERNATIVE_TOOL.strategy,
                        requirements=recovery_requirements,
                        allowed_tools=sorted(recovery_allowed_names),
                        summary="Host restricted this turn to safe alternative recovery capabilities.",
                    )
            retired_recovery_tools = _retired_recovery_tool_names(trace)
            if retired_recovery_tools:
                # Once an independent, validated alternative has repaired a
                # failed branch, the original capability remains retired for
                # this Run.  Providers sometimes replay old structured calls
                # from their transcript; exposing the old tool again turns a
                # repaired local failure into a fresh-looking retry.
                disclosed_tool_manifest = [
                    item
                    for item in disclosed_tool_manifest
                    if str(item.get("name") or "") not in retired_recovery_tools
                ]
                retired_signature = ",".join(sorted(retired_recovery_tools))
                if context.state.get("retired_recovery_tools") != retired_signature:
                    context.state["retired_recovery_tools"] = retired_signature
                    transcript.append(
                        {
                            "role": "host",
                            "type": "retired_recovery_tools",
                            "content": {
                                "tools": sorted(retired_recovery_tools),
                                "instruction": (
                                    "These capabilities already failed and their local branches have "
                                    "validated alternate evidence. Do not request them again in this Run."
                                ),
                            },
                        }
                    )
                    await record(
                        "repair.retired_tools_hidden",
                        step=step,
                        tools=sorted(retired_recovery_tools),
                        summary="Host hid recovered-failure tools from later provider turns.",
                    )
            if _completed_critic_observation(trace):
                disclosed_tool_manifest = [
                    item
                    for item in disclosed_tool_manifest
                    if item.get("name") != "agent.run_subtasks"
                ]
                if not context.state.get("critic_join_completed"):
                    context.state["critic_join_completed"] = True
                    transcript.append(
                        {
                            "role": "host",
                            "type": "critic_join_completed",
                            "content": {
                                "instruction": (
                                    "The independent Critic has completed and is already joined. "
                                    "Do not delegate another Critic. Use parent-Run tools only for "
                                    "any still-missing evidence dimensions, then synthesize."
                                )
                            },
                        }
                    )
                    await record("critic.join.completed", step=step)
            if is_critic_run:
                disclosed_tool_manifest = _critic_disclosed_tools(
                    disclosed_tool_manifest,
                    successful_observations=successful_observations,
                )
                if (
                    successful_observations >= 3
                    and not context.state.get("critic_evidence_budget_closed")
                ):
                    context.state["critic_evidence_budget_closed"] = True
                    transcript.append(
                        {
                            "role": "host",
                            "type": "critic_evidence_budget_closed",
                            "content": {
                                "validated_observations": successful_observations,
                                "instruction": (
                                    "The scoped Critic evidence budget is complete. "
                                    "Synthesize the independent challenge now without requesting more tools."
                                ),
                            },
                        }
                    )
                    await record(
                        "critic.evidence_budget.closed",
                        step=step,
                        validated_observations=successful_observations,
                    )
            active_node = next(
                (
                    node
                    for node in plan.nodes.values()
                    if node.status in {"running", "ready", "blocked"}
                ),
                None,
            )
            active_branch = {
                "branch_id": getattr(active_node, "node_id", None) or run_id,
                "objective": objective_text,
                "completion_criteria": list(plan.completion_criteria),
                "current_step": step,
            }
            evidence_items = [
                {
                    "evidence_id": item.get("call_id") or item.get("node_id"),
                    "tool": item.get("tool"),
                    "summary": (
                        label_untrusted_content(
                            item.get("result_summary"),
                            source=f"tool:{item.get('tool') or 'unknown'}",
                            content_type="evidence_summary",
                            source_id=str(item.get("call_id") or item.get("node_id") or "") or None,
                        )
                        if is_external_content_tool(str(item.get("tool") or ""))
                        else item.get("result_summary")
                    ),
                    "validation": item.get("validation"),
                }
                for item in trace
                if item.get("ok") is True
            ]
            context_package = context_broker_v2.assemble(
                current_objective=objective_text,
                current_user_message=objective_text,
                active_branch=active_branch,
                relevant_memory=memories,
                relevant_evidence=evidence_items,
                recent_decisions=[
                    item.get("content")
                    for item in transcript
                    if item.get("type") in {"interaction_response", "control_message"}
                ],
                tool_schemas=disclosed_tool_manifest,
                failure_ledger=[item for item in trace if item.get("ok") is False],
                plan=plan.to_dict(),
                output_contract={"completion_criteria": list(plan.completion_criteria)},
                profile=ModelContextProfile(),
                turn=step,
            )
            provider_transcript = _provider_transcript_v2(
                package=context_package,
                trace=trace,
                transcript=transcript,
            )
            # ContextBroker v2 may trim a very large project capability surface
            # to fit the provider context budget.  The actual SDK tool list must
            # use the same fitted set; otherwise schemas removed from the
            # package are still duplicated in ``tools`` and can exhaust the
            # next turn before the provider is called.
            provider_tool_manifest = list(context_package.tool_schemas)
            turn_scope = f"turn:{run_id}:{step}"
            turn_budget = budget_manager.register(
                turn_scope,
                scope_type="model_turn",
                limit=36_000,
                parent_scope_id=branch_scope,
            )
            prompt_tokens = (
                0
                if host_resumed_approval_dispatch
                else _estimate_context_tokens(
                    {"transcript": provider_transcript, "tools": provider_tool_manifest}
                )
            )
            try:
                budget_manager.consume(turn_scope, prompt_tokens)
            except TokenBudgetExceeded as exc:
                budget_exhausted = True
                final_turn = {
                    "state": "continue",
                    "summary": "Host token budget reached before another provider turn.",
                    "tool_calls": [],
                    "decision": None,
                    "completion_evaluation": {"remaining_gaps": ["token_budget_exhausted"]},
                }
                await record(
                    "budget.exhausted",
                    step=step,
                    scope=turn_scope,
                    estimated_prompt_tokens=prompt_tokens,
                    error=str(exc),
                )
                break
            try:
                cost_budget_manager.charge(
                    prompt_tokens,
                    session_id=session_id,
                    provider_id=selected_driver,
                )
            except CostBudgetExceeded as exc:
                cost_budget_exhausted = True
                final_turn = {
                    "state": "continue",
                    "summary": "Host cost budget reached before another provider turn.",
                    "tool_calls": [],
                    "decision": None,
                    "completion_evaluation": {"remaining_gaps": ["cost_budget_exhausted"]},
                }
                await record(
                    "cost_budget.exhausted",
                    step=step,
                    scope=exc.scope,
                    identifier=exc.identifier,
                    requested_units=exc.requested,
                    consumed_units=exc.used,
                    limit_units=exc.limit,
                )
                break
            context.state["token_budget"] = {
                "session_scope": session_scope,
                "run_scope": run_scope,
                "branch_scope": branch_scope,
                "session_limit": session_budget.limit,
                "run_limit": run_budget.limit,
                "session_used": budget_manager.get(session_scope).consumed,
                "run_used": budget_manager.get(run_scope).consumed,
                "branch_used": budget_manager.get(branch_scope).consumed,
                "turn_scope": turn_scope,
                "turn_used": budget_manager.get(turn_scope).consumed,
            }
            turn_input = AgentTurnInput(
                run_id=run_id,
                objective=objective.strip(),
                system_prompt=(
                    SYSTEM_PROMPT
                    if provider_protocol == "advanced_v1"
                    else UNIVERSAL_SYSTEM_PROMPT
                ),
                transcript=tuple(provider_transcript),
                tools=tuple(() if task_kind == "general_answer" else provider_tool_manifest),
                output_schema=_schema_for_task(
                    task_kind,
                    protocol=provider_protocol,
                    objective=objective_text,
                ),
                metadata={
                    "step": step,
                    "max_steps": bounded_steps,
                    "driver": selected_driver,
                    "autonomy": autonomy,
                    "task_kind": task_kind,
                    "provider_protocol": provider_protocol,
                    "tool_disclosure": {
                        "phase": "initial" if step == 1 else "expanded",
                        "disclosed_count": len(provider_tool_manifest),
                        "context_omitted_count": context_package.omitted_counts.get("tools", 0),
                        "task_scoped_count": len(tool_manifest),
                        "global_count": len(all_tool_manifest),
                    },
                    "decision_allowed": task_kind == "market_decision",
                    "session_id": session_id,
                    "plan_graph": plan.to_dict(),
                    "environment_snapshot_hash": current_snapshot.hash if current_snapshot else None,
                    "context_package": context_package.to_provider_payload(),
                    "token_budget": dict(context.state["token_budget"]),
                },
                event_sink=model_event_sink,
            )
            untrusted_context_receipt = provider_untrusted_context_receipt(
                context_package.evidence,
                policy=context_package.context.get("content_security") or {},
            )
            if (
                not host_resumed_approval_dispatch
                and untrusted_context_receipt["external_content_count"]
            ):
                await record(
                    "provider.untrusted_context.bound",
                    step=step,
                    model_call_id=f"{run_id}:turn:{step}",
                    driver=selected_driver,
                    receipt=untrusted_context_receipt,
                    summary="外部資料已以 data-only 邊界綁定到本次模型請求。",
                )
            raw_turn: Any | None = None
            if host_resumed_approval_dispatch:
                raw_turn = {
                    "state": "continue",
                    "summary": "Host 正在執行已批准的本機操作；不重新詢問模型或使用者。",
                    "tool_calls": approved_resume_calls,
                    "decision": None,
                }
                approved_resume_calls = []
                await record(
                    "approval.resumed_tool_dispatch",
                    step=step,
                    tool_calls=[
                        {"id": call["id"], "name": call["name"]}
                        for call in raw_turn["tool_calls"]
                    ],
                    summary="Host 正在執行已批准的本機操作；不重新詢問模型或使用者。",
                )
            else:
                await record(
                    "model.turn.started",
                    step=step,
                    model_call_id=f"{run_id}:turn:{step}",
                    driver=selected_driver,
                    execution_mode="stock_ai_provider",
                    summary="等待模型回傳工具計畫；這是執行狀態，不是私有思考內容。",
                )
            try:
                if raw_turn is None:
                    raw_turn = await driver.decide(turn_input)
            except Exception as exc:
                await record(
                    "model.turn.failed",
                    step=step,
                    model_call_id=f"{run_id}:turn:{step}",
                    driver=selected_driver,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                fallback_policy = ProviderFallbackPolicy(
                    primary_driver=selected_driver,
                    mode=("explicit_alternate" if fallback_driver_id else "fail_closed"),
                    allowed_alternates=((str(fallback_driver_id),) if fallback_driver_id else ()),
                )
                fallback_decision = fallback_policy.decide(
                    alternate_driver=fallback_driver_id,
                    task_kind=task_kind,
                    autonomy=autonomy,
                )
                await record(
                    "provider.fallback.evaluated",
                    step=step,
                    primary_driver=selected_driver,
                    alternate_driver=fallback_driver_id,
                    status=fallback_decision.status,
                    reason=fallback_decision.reason,
                    quality_change=fallback_decision.quality_change,
                    user_notice=fallback_decision.user_notice,
                    receipt=fallback_decision.to_dict(),
                )
                if fallback_decision.status != "allowed" or not fallback_decision.selected_driver:
                    raise
                fallback = self.drivers.get(fallback_decision.selected_driver)
                if fallback is None:
                    await record(
                        "provider.fallback.failed",
                        step=step,
                        primary_driver=selected_driver,
                        alternate_driver=fallback_decision.selected_driver,
                        error_type="UnknownAgentDriver",
                        error="Explicit fallback driver is not registered.",
                        receipt=fallback_decision.to_dict(),
                    )
                    raise RuntimeError(
                        f"Explicit fallback driver is not registered: {fallback_decision.selected_driver}"
                    ) from exc
                try:
                    context.driver_id = fallback_decision.selected_driver
                    await _driver_lifecycle(fallback, "start_run", context)
                    if lifecycle_drivers is not None:
                        lifecycle_drivers.append(fallback)
                    selected_driver = fallback_decision.selected_driver
                    driver = fallback
                    provider_protocol = "universal_v1"
                    provider_conformance = None
                    turn_input = AgentTurnInput(
                        run_id=turn_input.run_id,
                        objective=turn_input.objective,
                        system_prompt=UNIVERSAL_SYSTEM_PROMPT,
                        transcript=turn_input.transcript,
                        tools=turn_input.tools,
                        output_schema=turn_input.output_schema,
                        metadata={
                            **turn_input.metadata,
                            "driver": selected_driver,
                            "provider_protocol": provider_protocol,
                        },
                        event_sink=turn_input.event_sink,
                    )
                    await record(
                        "provider.fallback.started",
                        step=step,
                        primary_driver=fallback_decision.primary_driver,
                        selected_driver=selected_driver,
                        quality_change=fallback_decision.quality_change,
                        receipt=fallback_decision.to_dict(),
                    )
                    raw_turn = await driver.decide(turn_input)
                except Exception as fallback_exc:
                    await record(
                        "provider.fallback.failed",
                        step=step,
                        primary_driver=fallback_decision.primary_driver,
                        alternate_driver=fallback_decision.selected_driver,
                        error_type=type(fallback_exc).__name__,
                        error=str(fallback_exc),
                        receipt=fallback_decision.to_dict(),
                    )
                    raise RuntimeError(
                        f"Explicit provider fallback failed: {fallback_decision.selected_driver}"
                    ) from fallback_exc
            response_tokens = 0 if host_resumed_approval_dispatch else _estimate_context_tokens(raw_turn)
            try:
                budget_manager.consume(turn_scope, response_tokens)
            except TokenBudgetExceeded as exc:
                budget_exhausted = True
                await record(
                    "budget.exhausted",
                    step=step,
                    scope=turn_scope,
                    estimated_response_tokens=response_tokens,
                    error=str(exc),
                )
                final_turn = {
                    "state": "continue",
                    "summary": "Host token budget reached after a provider response; the response was not accepted as a final answer.",
                    "tool_calls": [],
                    "decision": None,
                    "completion_evaluation": {"remaining_gaps": ["token_budget_exhausted"]},
                }
                break
            try:
                cost_budget_manager.charge(
                    response_tokens,
                    session_id=session_id,
                    provider_id=selected_driver,
                )
            except CostBudgetExceeded as exc:
                cost_budget_exhausted = True
                final_turn = {
                    "state": "continue",
                    "summary": "Host cost budget reached after a provider response; the response was not accepted as a final answer.",
                    "tool_calls": [],
                    "decision": None,
                    "completion_evaluation": {"remaining_gaps": ["cost_budget_exhausted"]},
                }
                await record(
                    "cost_budget.exhausted",
                    step=step,
                    scope=exc.scope,
                    identifier=exc.identifier,
                    requested_units=exc.requested,
                    consumed_units=exc.used,
                    limit_units=exc.limit,
                )
                break
            context.state["execution_guard"] = {
                "cost_budgets": cost_budget_manager.snapshot(),
                "runaway": runaway_guard.snapshot(),
            }
            context.state["token_budget"] = {
                **dict(context.state.get("token_budget") or {}),
                "session_used": budget_manager.get(session_scope).consumed,
                "run_used": budget_manager.get(run_scope).consumed,
                "branch_used": budget_manager.get(branch_scope).consumed,
                "turn_used": budget_manager.get(turn_scope).consumed,
            }
            raw_turn = ProviderOutputNormalizer().normalize(raw_turn).payload
            grounded_preference = _grounded_preference_recall_summary(
                objective_text,
                memories,
            )
            if grounded_preference and isinstance(raw_turn, dict):
                # A governed, explicit user preference is a factual recall
                # request, not an invitation for a model to reinterpret the
                # preference.  Preserve the model's execution state, but bind
                # its user-facing answer to the durable source text.
                raw_turn = {**raw_turn, "summary": grounded_preference}
                await record(
                    "memory.preference.recalled",
                    memory_ids=[
                        str(item.get("memory_id"))
                        for item in memories
                        if item.get("kind") == "user_preference"
                        or item.get("layer") == "user_preference"
                    ][:5],
                    summary="Host grounded the preference-recall answer in governed user memory.",
                )
            # Stable general knowledge never needs a plan mutation or a tool call.
            # Some otherwise capable local models still populate optional structured
            # fields with schema-shaped objects (for example ``operations={}``).
            # Drop those fields before strict normalization so a correct direct
            # answer is not rejected for an irrelevant planner formatting mistake.
            if task_kind == "general_answer" and isinstance(raw_turn, dict):
                raw_turn = {
                    **raw_turn,
                    "plan_patch": None,
                    "tool_calls": [],
                    "decision": None,
                }
            turn = _normalize_turn(raw_turn)
            if _should_host_create_explicit_text_artifact(
                task_kind=task_kind,
                objective=objective_text,
                trace=trace,
                turn=turn,
                tool_metadata=tool_metadata,
            ):
                artifact_call = _host_text_artifact_call(objective_text)
                turn = {
                    **turn,
                    "state": "continue",
                    "summary": "Host 正在建立使用者明確要求的本機文字 Artifact。",
                    "tool_calls": [artifact_call],
                    "decision": None,
                }
                await record(
                    "artifact.host_create_compiled",
                    step=step,
                    call_id=artifact_call["id"],
                    tool=artifact_call["name"],
                    summary=(
                        "模型未提出工具呼叫；Host 已依明確、本機、低風險的文字 Artifact "
                        "請求編譯 artifact.create_text。"
                    ),
                )
            reflection_required = _proactive_reflection_required(
                objective=objective_text,
                task_kind=task_kind,
                trace=trace,
            )
            reflection = turn.get("reflection")
            if reflection_required and turn.get("state") == "complete" and reflection is None:
                # A material market answer must always leave a public
                # reflection receipt. Prefer the model-authored object; if a
                # provider omits the optional field, create a bounded,
                # evidence-only fallback without spending another turn or
                # pretending that hidden chain-of-thought exists.
                reflection = _fallback_public_reflection(
                    objective=objective_text,
                    task_kind=task_kind,
                    trace=trace,
                    summary=str(turn.get("summary") or ""),
                )
                turn = {
                    **turn,
                    "reflection": reflection,
                }
                await record(
                    "reflection.completed",
                    step=step,
                    reflection=_public_reflection(reflection),
                    public_summary_only=True,
                    source="host_evidence_fallback",
                )
            if isinstance(reflection, dict):
                reflection, rejected_evidence_ids = _ground_reflection_evidence(
                    reflection,
                    trace,
                )
                checkpoint = ReflectionCheckpoint.create(
                    session_id=session_id,
                    branch_id=str(active_branch.get("branch_id") or run_id),
                    problem=objective_text,
                    tentative_judgment=str(
                        reflection.get("preferred_option")
                        or turn.get("summary")
                        or "Agent 暫定判斷"
                    ),
                    evidence_summary=tuple(reflection.get("main_evidence") or ()),
                    preferred_option=str(
                        reflection.get("preferred_option") or "Agent 暫定建議"
                    ),
                    alternatives=tuple(
                        reflection.get("alternatives")
                        or ("保留目前方案並繼續驗證",)
                    ),
                    unknowns=tuple(reflection.get("unknowns") or ()),
                    important_risks=tuple(
                        reflection.get("important_risks") or ()
                    ),
                    user_decision_question=(
                        str(
                            reflection.get("reason_to_ask")
                            or "這項不確定性會改變下一步；要採用 Agent 建議嗎？"
                        )
                        if reflection.get("should_ask_user") is True
                        else None
                    ),
                )
                reflection = {
                    **reflection,
                    **checkpoint.public_summary(),
                    "evidence_ids": list(reflection.get("evidence_ids") or ()),
                }
                turn = {**turn, "reflection": reflection}
                if rejected_evidence_ids:
                    await record(
                        "reflection.evidence_rejected",
                        step=step,
                        reflection_id=checkpoint.reflection_id,
                        rejected_evidence_ids=rejected_evidence_ids,
                        summary="Host removed reflection Evidence IDs that were not issued by this Run.",
                    )
            # The Host fallback above is deliberately capable of identifying
            # a material decision.  Keep this as a second, independent check:
            # using ``elif`` left a fallback with ``should_ask_user=true`` in
            # the completed state, so no durable Decision Checkpoint or UI
            # card was ever created.
            if (
                isinstance(reflection, dict)
                and reflection.get("should_ask_user") is True
                and _should_defer_reflection_until_paper_order_receipt(
                    context=context,
                    objective=objective_text,
                    task_kind=task_kind,
                    trace=trace,
                )
            ):
                # A local paper order was explicitly requested.  The model
                # must not replace its still-missing preview/submit receipt
                # with a generic "choose the next step" card: that leaves a
                # routine task visibly waiting, then restarted, without ever
                # attempting the bounded paper capability.  Let the normal
                # completion feedback route the next turn to the missing
                # Host tool instead.  A real receipt is still required before
                # a final result can claim that an order happened.
                reflection = {
                    **reflection,
                    "should_ask_user": False,
                    "reason_to_ask": "",
                }
                turn = {
                    **turn,
                    "state": "continue",
                    "interaction": None,
                    "reflection": reflection,
                }
                await record(
                    "paper_order.reflection_deferred",
                    step=step,
                    summary=(
                        "Host deferred a generic reflection because the explicitly requested "
                        "paper order has no verified submit receipt yet."
                    ),
                )
            elif (
                isinstance(reflection, dict)
                and reflection.get("should_ask_user") is True
                and _should_host_finalize_verified_paper_order(
                    turn=turn,
                    trace=trace,
                    context=context,
                    objective=objective_text,
                    task_kind=task_kind,
                )
            ):
                # The bounded local paper protocol has already received the
                # user's explicit authorization and its completion receipt is
                # Host-verified. Keep the public reflection for auditability,
                # but never turn an optional post-trade thought into a new
                # blocking decision. Otherwise a routine preview -> submit
                # flow visibly loops at "waiting for user decision" after it
                # has actually completed.
                reflection = {
                    **reflection,
                    "should_ask_user": False,
                    "reason_to_ask": "",
                }
                turn = {**turn, "reflection": reflection}
            if isinstance(reflection, dict) and reflection.get("should_ask_user") is True:
                if turn.get("state") != "waiting_decision":
                    turn = {
                        **turn,
                        "state": "waiting_decision",
                        "interaction": _reflection_to_interaction(reflection),
                    }
                elif turn.get("interaction") is None:
                    turn = {**turn, "interaction": _reflection_to_interaction(reflection)}
            if isinstance(reflection, dict):
                await record(
                    "reflection.completed",
                    step=step,
                    reflection=_public_reflection(reflection),
                    public_summary_only=True,
                )
            replayed_retired_calls = [
                call
                for call in turn.get("tool_calls") or []
                if str(call.get("name") or "") in retired_recovery_tools
            ]
            if replayed_retired_calls:
                turn = {
                    **turn,
                    "tool_calls": [
                        call
                        for call in turn.get("tool_calls") or []
                        if str(call.get("name") or "") not in retired_recovery_tools
                    ],
                }
                transcript.append(
                    {
                        "role": "host",
                        "type": "retired_recovery_call_rejected",
                        "content": {
                            "rejected_tools": sorted(
                                {str(call.get("name") or "") for call in replayed_retired_calls}
                            ),
                            "reason": "A repaired branch cannot reopen its retired failed capability.",
                        },
                    }
                )
                await record(
                    "repair.retired_tool_call_rejected",
                    step=step,
                    rejected_tools=sorted(
                        {str(call.get("name") or "") for call in replayed_retired_calls}
                    ),
                    summary="Host ignored a provider replay of a retired failed capability.",
                )
            if recovery_allowed_names:
                recovery_calls = [
                    call for call in turn.get("tool_calls") or []
                    if str(call.get("name") or "") in recovery_allowed_names
                ]
                rejected_calls = [
                    str(call.get("name") or "")
                    for call in turn.get("tool_calls") or []
                    if str(call.get("name") or "") not in recovery_allowed_names
                ]
                if rejected_calls:
                    transcript.append(
                        {
                            "role": "host",
                            "type": "recovery_call_rejected",
                            "content": {
                                "rejected_tools": rejected_calls,
                                "allowed_tools": sorted(recovery_allowed_names),
                                "reason": "Only independent recovery capabilities may run for the unresolved branch.",
                            },
                        }
                    )
                    await record(
                        "repair.recovery_call_rejected",
                        step=step,
                        rejected_tools=rejected_calls,
                        allowed_tools=sorted(recovery_allowed_names),
                        summary="Host rejected a non-recovery tool call while a local branch was unresolved.",
                    )
                if recovery_calls:
                    recovery_surface_no_progress = 0
                    turn = {**turn, "tool_calls": recovery_calls}
                else:
                    # Do not merely log a rejected recovery call and then
                    # continue into the executor with its original payload.
                    # That would let an unrelated page read masquerade as a
                    # recovery attempt in the same turn.
                    turn = {**turn, "state": "continue", "tool_calls": []}
                    recovery_surface_no_progress += 1
                    transcript.append(
                        {
                            "role": "host",
                            "type": "recovery_no_progress",
                            "content": {
                                "attempt": recovery_surface_no_progress,
                                "allowed_tools": sorted(recovery_allowed_names),
                                "instruction": "A recovery tool call is required; an unsupported completion draft is not progress.",
                            },
                        }
                    )
                    if recovery_surface_no_progress >= 2:
                        # A provider declining a valid, narrowly disclosed
                        # recovery surface is not proof that recovery is
                        # exhausted.  P35/P40 require the Host to continue
                        # from the Error Receipt's smallest local boundary.
                        # Compile only read-only, schema-fillable calls from
                        # the already-approved L4/L5 alternatives.  This is a
                        # transparent deterministic fallback, not fabricated
                        # model reasoning: every generated call, strategy and
                        # recovery_for relation is emitted as a Host event.
                        host_recovery_calls = _host_recovery_calls(
                            trace=trace,
                            recovery_surface=recovery_surface,
                            objective=objective_text,
                            symbols=symbols,
                        )
                        if host_recovery_calls:
                            recovery_surface_no_progress = 0
                            turn = {
                                **turn,
                                "state": "continue",
                                "summary": (
                                    "模型未執行可用替代工具；Host 正依錯誤收據在失敗分支上"
                                    "執行安全的獨立替代證據策略。"
                                ),
                                "tool_calls": host_recovery_calls,
                                "plan_patch": None,
                                "decision": None,
                            }
                            transcript.append(
                                {
                                    "role": "host",
                                    "type": "host_recovery_dispatch",
                                    "content": {
                                        "strategy": RecoveryLevel.ALTERNATIVE_TOOL.strategy,
                                        "calls": _safe_arguments(host_recovery_calls),
                                        "reason": (
                                            "The provider twice declined the bounded recovery surface; "
                                            "Host is executing the next safe, schema-validated local alternative."
                                        ),
                                    },
                                }
                            )
                            await record(
                                "repair.host_applied",
                                step=step,
                                strategy=RecoveryLevel.ALTERNATIVE_TOOL.strategy,
                                calls=_safe_arguments(host_recovery_calls),
                                preserve_other_branches=True,
                                summary="Host dispatched safe alternative evidence calls after provider recovery refusal.",
                            )
                        else:
                            # L8 is allowed only after the Host has either
                            # validated or exhausted every schema-fillable,
                            # non-mutating L4/L5 alternative for this local
                            # branch.  The caller can then offer an actual
                            # source/priority choice without losing work.
                            recovery_only_exhausted = True
                            final_turn = {
                                **turn,
                                "state": "continue",
                                "tool_calls": [],
                                "decision": None,
                                "completion_evaluation": {
                                    "criteria_met": False,
                                    "criterion_results": [],
                                    "evidence_ids": [],
                                    "remaining_gaps": [
                                        "all_safe_recovery_alternatives_exhausted",
                                    ],
                                },
                            }
                            await record(
                                "repair.autonomous_strategies_exhausted",
                                step=step,
                                strategy=RecoveryLevel.ALTERNATIVE_SOURCE.strategy,
                                allowed_tools=sorted(recovery_allowed_names),
                                summary="All safe alternative recovery calls were attempted for the local branch before escalation.",
                            )
                            break
            if _is_protocol_meta_response(turn.get("summary")):
                # A structured-output model can occasionally mistake the Host's
                # JSON contract for the user objective.  That sentence is not a
                # user-facing answer, even if its shape passes schema validation.
                # Keep all validated observations, provide the model a precise
                # correction in its next transcript turn, and never leak the
                # protocol error to the UI as a completed answer.
                rejected_summary = str(turn.get("summary") or "")
                turn = {
                    **turn,
                    "state": "continue",
                    "summary": "Host rejected a protocol-meta response; answer OBJECTIVE from the validated evidence.",
                    "plan_patch": None,
                    "tool_calls": [],
                    "decision": None,
                    "completion_evaluation": {
                        "criteria_met": False,
                        "criterion_results": [],
                        "evidence_ids": [],
                        "remaining_gaps": ["provider_protocol_meta_response"],
                    },
                }
                transcript.append(
                    {
                        "role": "host",
                        "type": "provider_response_rejected",
                        "content": {
                            "reason": "protocol_meta_response",
                            "rejected_summary": rejected_summary,
                            "required_action": "Answer the user's OBJECTIVE using the validated tool evidence; do not discuss JSON schemas or runtime protocols.",
                        },
                    }
                )
                await record(
                    "model.turn.rejected",
                    step=step,
                    reason="provider_protocol_meta_response",
                    rejected_summary=rejected_summary,
                    summary="Host rejected a provider response that described the internal JSON protocol instead of answering the user objective.",
                )
            corrected_routing = UnifiedMultiIntentRouter().apply_model_correction(
                routing,
                turn.get("routing_patch"),
            )
            # A model may propose an informational reclassification after it
            # sees blocked research. That must not remove the bounded local
            # paper-order path explicitly requested by the user.
            if (
                task_kind == "market_decision"
                and objective_completion_contract(objective_text, task_kind)["paper_order_requested"]
                and corrected_routing.primary_task_kind != "market_decision"
            ):
                corrected_routing = routing
            if corrected_routing.primary_task_kind != task_kind:
                previous_task_kind = task_kind
                routing = corrected_routing
                task_kind = routing.primary_task_kind
                requires_observation = task_kind != "general_answer"
                context.state["task_kind"] = task_kind
                context.state["routing"] = routing.model_dump()
                tool_manifest = context_broker.filter_capabilities(
                    all_tool_manifest,
                    task_kind=task_kind,
                task_kinds=_context_task_kinds(routing, context),
                )
                tool_manifest = _limit_market_information_recursion(
                    tool_manifest,
                    task_kind=task_kind,
                    objective=objective_text,
                )
                tool_manifest = _extend_explicit_paper_market_coverage_capabilities(
                    tool_manifest,
                    all_tool_manifest=all_tool_manifest,
                    objective=objective_text,
                    task_kind=task_kind,
                    context=context,
                )
                tool_metadata = {item["name"]: item for item in tool_manifest}
                turn = {
                    **turn,
                    "state": "continue",
                    "tool_calls": [],
                    "summary": (
                        f"Primary intent corrected from {previous_task_kind} to {task_kind}; "
                        "requesting the matching Host context and tools."
                    ),
                }
                await record(
                    "routing.corrected",
                    step=step,
                    previous_task_kind=previous_task_kind,
                    task_kind=task_kind,
                    routing=routing.model_dump(),
                )
            if task_kind == "general_answer" and turn["state"] == "complete":
                turn = {
                    **turn,
                    "plan_patch": None,
                    "tool_calls": [],
                    "decision": None,
                }
            if task_kind == "general_answer" and turn["state"] == "complete" and _requires_hypothetical_only_notice(objective_text):
                notice = "這是純假設討論；未查詢市場、未執行交易，也未建立自動化。"
                if notice not in str(turn.get("summary") or ""):
                    turn = {**turn, "summary": f"{str(turn.get('summary') or '').rstrip()}\n\n{notice}"}
            explicit_choice_interaction = _materialize_explicit_choice_interaction(
                objective=objective_text,
                turn=turn,
                transcript=transcript,
            )
            if explicit_choice_interaction is not None:
                # A choice between user-named alternatives is a collaboration
                # checkpoint, even when a smaller provider puts its tentative
                # recommendation in ordinary prose instead of the structured
                # ``waiting_decision`` envelope.  Do not spend more provider
                # turns asking it to restate the same answer: the Host can
                # bind the already stated recommendation to the alternatives
                # in the user's objective and render the durable card itself.
                turn = {
                    **turn,
                    "state": "waiting_decision",
                    "interaction": explicit_choice_interaction,
                    "tool_calls": [],
                    "decision": None,
                }
                await record(
                    "interaction.explicit_choice_materialized",
                    step=step,
                    preferred_option=explicit_choice_interaction["preferred_option"],
                    options=explicit_choice_interaction["options"],
                    summary=(
                        "Host materialized the user's explicit choice as a durable decision "
                        "card after the provider supplied only prose."
                    ),
                )
            elif _explicit_choice_recommendation_reprompt_required(
                objective=objective_text,
                turn=turn,
                transcript=transcript,
            ):
                # A provider may recognize that the user asked for a choice,
                # yet return a generic "please choose" card without stating a
                # tentative preference.  That is incomplete provider output,
                # not a reason to send the person into a prompt loop or close
                # their requested choice.  Ask once more inside this Run.
                options = _explicit_choice_options(objective_text)
                turn = {
                    **turn,
                    "state": "continue",
                    "interaction": None,
                    "tool_calls": [],
                    "decision": None,
                    "summary": (
                        "Host is obtaining a concrete tentative recommendation "
                        "for the user's explicit alternatives before rendering the decision card."
                    ),
                }
                transcript.append(
                    {
                        "role": "host",
                        "type": "explicit_choice_recommendation_required",
                        "content": {
                            "options": options,
                            "instruction": (
                                "State one tentative preference between these exact user-named "
                                "alternatives, then provide the bounded decision card."
                            ),
                        },
                    }
                )
                await record(
                    "interaction.explicit_choice_reprompted",
                    step=step,
                    options=options,
                    summary=(
                        "Host requested one concrete recommendation before showing the "
                        "user-requested choice card."
                    ),
                )
            resolved_interaction = next(
                (
                    dict(item.get("content") or {})
                    for item in reversed(transcript)
                    if item.get("type") == "interaction_response"
                    and isinstance(item.get("content"), dict)
                ),
                None,
            )
            if (
                turn["state"] in {"waiting_user_input", "waiting_decision"}
                and resolved_interaction is not None
            ):
                # Providers occasionally restate a just-resolved multiple-choice
                # question despite receiving the interaction response. A pending
                # card must never turn into an unbounded card loop. The Host has
                # the authoritative option label and reason, so it can close this
                # duplicate wait while retaining a user-visible, auditable answer.
                selected = dict(resolved_interaction.get("selected_option") or {})
                label = str(selected.get("label") or resolved_interaction.get("response") or "使用者選擇")
                reason = str(selected.get("reason") or "")
                summary = f"已依你的選擇「{label}」完成這個討論。"
                if reason:
                    summary += f" 此選項的原先理由是：{reason}。"
                summary += " 這是純假設整理；未查詢市場、未交易，也未建立自動化。"
                turn = {
                    **turn,
                    "state": "complete",
                    "summary": summary,
                    "plan_patch": None,
                    "tool_calls": [],
                    "decision": None,
                    "interaction": None,
                    "completion_evaluation": {
                        "criteria_met": True,
                        "criterion_results": [],
                        "evidence_ids": [],
                        "remaining_gaps": [],
                    },
                }
                await record(
                    "interaction.duplicate_wait_prevented",
                    step=step,
                    interaction_id=resolved_interaction.get("interaction_id"),
                    selected_option=selected or resolved_interaction.get("response"),
                    summary="Host closed a duplicate provider decision request after the user had already selected an option.",
                )
            public_turn_summary = _safe_public_paper_turn_summary(
                turn=turn,
                context=context,
                objective=objective_text,
                task_kind=task_kind,
                trace=trace,
            )
            if public_turn_summary is not None:
                # Provider prose is not an execution receipt.  In particular,
                # a model may narrate an invented quantity while the Host is
                # deliberately replacing it with the fixed local sandbox
                # order.  The activity timeline is user-visible, so never
                # expose that contradictory draft between Host protocol steps.
                turn = {**turn, "summary": public_turn_summary}
            await record(
                "model.turn.completed",
                step=step,
                model_call_id=f"{run_id}:turn:{step}",
                driver=selected_driver,
                execution_mode="stock_ai_provider",
                summary=str(turn.get("summary") or "Model turn completed."),
            )
            await record(
                "reasoning.summary",
                step=step,
                reason_summary=str(turn.get("summary") or ""),
                summary=str(turn.get("summary") or ""),
                disclosure="public_auditable_summary_only",
            )
            pending_paper_order = _pending_verified_paper_order(trace)
            if _should_host_submit_verified_paper_order(
                turn=turn,
                pending_order=pending_paper_order,
                context=context,
            ):
                # The user already chose this local, non-live paper operation
                # by selecting paper execution and issuing the objective. A
                # provider must not turn a production-research warning, an
                # invalid made-up tool, or a duplicate yes/no prompt into a
                # loop after it has previewed the exact order.  Preserve the
                # warning for the final summary and continue through the
                # verified Paper Broker path.
                turn = {
                    **turn,
                    "state": "continue",
                    "interaction": None,
                    "tool_calls": [
                        {
                            "id": f"host-paper-submit-{step}",
                            "name": "paper.submit_order",
                            "arguments": pending_paper_order,
                        }
                    ],
                }
                await record(
                    "paper_order.auto_continue",
                    step=step,
                    tool_name="paper.submit_order",
                    arguments=pending_paper_order,
                    reason="explicit_paper_order_authorized_after_verified_preview",
                )
            if (
                turn["state"] in {"waiting_user_input", "waiting_decision"}
                and (
                    _objective_disallows_external_acceptance_wait(objective_text)
                    or _turn_requests_external_acceptance(turn)
                )
            ):
                # A local goal can explicitly say that external acceptance is
                # not a completion condition.  A provider must not turn that
                # boundary into a blocking card, then make the same Run appear
                # to start, pause and stop without an answer.  This does not
                # bypass approval: waiting_approval is intentionally excluded.
                reflection = turn.get("reflection")
                if isinstance(reflection, dict):
                    reflection = {
                        **reflection,
                        "should_ask_user": False,
                        "reason_to_ask": "",
                    }
                turn = {
                    **turn,
                    "state": "continue",
                    "interaction": None,
                    "reflection": reflection,
                }
                await record(
                    "interaction.external_acceptance_wait_suppressed",
                    step=step,
                    summary=(
                        "Host ignored a provider wait for external acceptance because "
                        "external acceptance is not a valid blocking condition for this local Run."
                    ),
                )
            routine_paper_reason = _routine_paper_wait_suppression_reason(
                turn=turn,
                context=context,
                objective=objective_text,
                task_kind=task_kind,
            )
            if routine_paper_reason is not None:
                # A paper simulation is a Host-owned, non-live sandbox.  Once
                # the user has asked for it against one selected security, the
                # Host can deterministically create its minimal one-share
                # preview and submit path.  Asking the user to restate side,
                # quantity, a provider limitation, or an external acceptance
                # condition is a model-control failure, not a genuine user
                # decision.  ``waiting_approval`` is intentionally excluded:
                # this never weakens an approval for a real external action.
                reflection = turn.get("reflection")
                if isinstance(reflection, dict):
                    reflection = {
                        **reflection,
                        "should_ask_user": False,
                        "reason_to_ask": "",
                    }
                turn = {
                    **turn,
                    "state": "continue",
                    "interaction": None,
                    "reflection": reflection,
                }
                await record(
                    "paper_order.routine_wait_suppressed",
                    step=step,
                    reason=routine_paper_reason,
                    summary=(
                        "Host continued the requested local paper sandbox instead of asking the user "
                        "to resolve an internal provider or acceptance condition."
                    ),
                )
            information_option = _should_host_continue_information_clarification(
                turn,
                task_kind=task_kind,
                context=context,
            )
            if information_option is not None:
                # This is a Host policy decision rather than a user-authored
                # prompt convention. The provider is asking for public
                # information it can obtain through the bounded market
                # capability surface, so keep the Run moving and let it report
                # verified limitations instead of creating a pointless card.
                reflection = turn.get("reflection")
                if isinstance(reflection, dict):
                    reflection = {
                        **reflection,
                        "should_ask_user": False,
                        "reason_to_ask": "",
                    }
                turn = {
                    **turn,
                    "state": "continue",
                    "interaction": None,
                    "reflection": reflection,
                }
                policy = {
                    "selected_option": _safe_arguments(information_option),
                    "instruction": (
                        "This is a routine market-information request. Do not ask the user to provide public "
                        "market data or choose a general-information fallback. Use the available Host market "
                        "tools to resolve named issuers and gather evidence. If exact evidence remains unavailable, "
                        "finish with a bounded comparison that states the verified limitation."
                    ),
                }
                transcript.append(
                    {
                        "role": "host",
                        "type": "information_clarification_auto_continue",
                        "content": policy,
                    }
                )
                await record(
                    "interaction.information_clarification_auto_continued",
                    step=step,
                    selected_option=policy["selected_option"],
                    summary=(
                        "Host continued a routine market-information request with its best-effort "
                        "public-data path; no user data upload or decision is required."
                    ),
                )
            routine_completion_reason = _routine_interaction_completion_reason(
                turn=turn,
                context=context,
                task_kind=task_kind,
                objective=objective_text,
            )
            if routine_completion_reason is not None:
                # A user asks the Stock AI to analyse or simulate a paper
                # trade; they do not need to be taught how the model should
                # deal with an ordinary research uncertainty.  After the
                # Host has exhausted the available in-process path, publish
                # the bounded answer (and its limitations) rather than
                # turning provider self-talk into a blocking input card.
                reflection = turn.get("reflection")
                if isinstance(reflection, dict):
                    reflection = {
                        **reflection,
                        "should_ask_user": False,
                        "reason_to_ask": "",
                    }
                turn = {
                    **turn,
                    "state": "complete",
                    "summary": _host_routine_completion_summary(
                        task_kind=task_kind,
                        objective=objective_text,
                        trace=trace,
                    ),
                    "interaction": None,
                    "tool_calls": [],
                    "reflection": reflection,
                }
                await record(
                    "interaction.routine_wait_completed",
                    step=step,
                    reason=routine_completion_reason,
                    summary=(
                        "Host completed the ordinary advisory path instead of asking the user to "
                        "resolve an internal research or model condition."
                    ),
                )
            if turn["state"] in {"waiting_user_input", "waiting_decision"}:
                interaction = dict(turn.get("interaction") or {})
                interaction_id = f"INT-{uuid4().hex}"
                active_node = next(
                    (
                        node.node_id
                        for node in sorted(plan.nodes.values(), key=lambda item: item.order_index)
                        if node.status in {"running", "ready", "pending"}
                    ),
                    None,
                )
                paused_interaction = {
                    "interaction_id": interaction_id,
                    "waiting_state": turn["state"],
                    "node_id": active_node,
                    "prompt": interaction["prompt"],
                    "agent_view": interaction["agent_view"],
                    "preferred_option": interaction["preferred_option"],
                    "options": interaction["options"],
                    "unknowns": interaction.get("unknowns") or [],
                    "important_risks": interaction.get("important_risks") or [],
                    "tentative_judgment": str(turn.get("summary") or ""),
                }
                await record(
                    "reflection.created",
                    **paused_interaction,
                    public_summary_only=True,
                )
                await record("interaction.requested", **paused_interaction)
                checkpoint = self._checkpoint(
                    session_id=session_id,
                    run_id=run_id,
                    sequence=sequence,
                    plan=plan,
                    transcript=transcript,
                    trace=trace,
                    context=context,
                )
                if checkpoint:
                    await record(
                        "checkpoint.created",
                        step=step,
                        checkpoint=_checkpoint_reference(checkpoint),
                    )
                final_turn = turn
                break
            await report_running_reasoning_nodes(
                turn_step=step,
                turn_summary=str(turn.get("summary") or ""),
                remaining_gaps=[
                    str(item)
                    for item in (
                        (turn.get("completion_evaluation") or {}).get("remaining_gaps")
                        or []
                    )
                    if str(item).strip()
                ],
            )
            if task_kind != "market_decision" and turn.get("decision") is not None:
                turn = {**turn, "decision": None}
            transcript.append({"role": "agent", "type": "decision", "content": turn})
            calls, rejected_calls = _partition_tool_calls(
                turn["tool_calls"],
                tool_metadata=tool_metadata,
            )
            unavailable_calls = [
                call for call in calls if str(call.get("name") or "") not in tool_metadata
            ]
            if unavailable_calls:
                calls = [call for call in calls if call not in unavailable_calls]
                turn = {**turn, "tool_calls": calls, "state": "continue"}
                transcript[-1] = {"role": "agent", "type": "decision", "content": turn}
                unavailable_names = sorted(
                    {str(call.get("name") or "") for call in unavailable_calls}
                )
                feedback = {
                    "error": "The requested tool is not in this Host-scoped capability surface.",
                    "rejected_calls": [
                        {"id": call.get("id"), "name": call.get("name")}
                        for call in unavailable_calls
                    ],
                    "available_tools": sorted(tool_metadata),
                    "required_action": "Use only one of the listed Host capabilities; unknown calls are never added to the durable plan.",
                }
                await record(
                    "tool.capability.rejected",
                    step=step,
                    rejected_tools=unavailable_names,
                    available_tool_count=len(tool_metadata),
                    summary="Host rejected an undisclosed provider tool before it could block the durable plan.",
                )
                transcript.append({"role": "host", "type": "tool_capability_error", "content": feedback})
                if not calls:
                    final_turn = turn
                    continue
            if rejected_calls and self.provider_registry is not None:
                unrepaired_calls: list[dict[str, Any]] = []
                for rejected_call in rejected_calls:
                    metadata = tool_metadata.get(rejected_call["name"], {})
                    available_repair_tokens = budget_manager.get(branch_scope).remaining
                    if not metadata or available_repair_tokens < 512:
                        unrepaired_calls.append(rejected_call)
                        continue
                    schema_errors = list(
                        rejected_call.get("_argument_schema_errors") or []
                    )
                    actual = (
                        json.dumps(
                            rejected_call.get("arguments") or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        )
                        if schema_errors
                        else "malformed JSON string"
                    )
                    receipt = ErrorReceipt(
                        category="schema_validation" if schema_errors else "invalid_json",
                        component=rejected_call["name"],
                        location="$.arguments",
                        expected="JSON object matching the registered tool input schema",
                        actual=actual,
                        retryable=True,
                        branch_id=str(rejected_call.get("node_id") or "") or None,
                    )
                    await record(
                        "repair.started",
                        step=step,
                        call_id=rejected_call["id"],
                        tool=rejected_call["name"],
                        error_receipt=receipt.to_dict(),
                        allowed_patch_scopes=["$.arguments"],
                    )
                    try:
                        repair_outcome = await _repair_provider_tool_call(
                            provider=self.provider_registry.get(selected_driver),
                            provider_id=selected_driver,
                            model_id=model_id,
                            run_id=run_id,
                            call=rejected_call,
                            metadata=metadata,
                            receipt=receipt,
                            budget_limit=min(4_096, available_repair_tokens),
                            event_sink=model_event_sink,
                        )
                        repair_tokens = (
                            repair_outcome.input_tokens + repair_outcome.output_tokens
                        )
                        if repair_tokens:
                            budget_manager.consume(branch_scope, repair_tokens)
                    except Exception as exc:
                        unrepaired_calls.append(rejected_call)
                        await record(
                            "repair.failed",
                            step=step,
                            call_id=rejected_call["id"],
                            tool=rejected_call["name"],
                            error_receipt=receipt.to_dict(),
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                    else:
                        repaired_call = {
                            **rejected_call,
                            "arguments": dict(repair_outcome.value["arguments"]),
                        }
                        calls.append(repaired_call)
                        await record(
                            "repair.host_applied"
                            if repair_outcome.strategy == "host_repair"
                            else "repair.model_requested",
                            step=step,
                            call_id=rejected_call["id"],
                            tool=rejected_call["name"],
                            error_receipt=receipt.to_dict(),
                            strategy=repair_outcome.strategy,
                            model_id=repair_outcome.model_id,
                            input_tokens=repair_outcome.input_tokens,
                            output_tokens=repair_outcome.output_tokens,
                            patch_scope="$.arguments",
                        )
                        await record(
                            "repair.completed",
                            step=step,
                            call_id=rejected_call["id"],
                            tool=rejected_call["name"],
                            strategy=repair_outcome.strategy,
                            schema_validated=True,
                        )
                rejected_calls = unrepaired_calls
            calls, redundant_critic_calls = _partition_redundant_critic_calls(
                calls,
                trace=trace,
            )
            if redundant_critic_calls:
                turn = {
                    **turn,
                    "tool_calls": calls,
                    "state": "continue",
                    "plan_patch": None,
                }
                transcript[-1] = {"role": "agent", "type": "decision", "content": turn}
                feedback = {
                    "error": (
                        "The independent Critic already completed and was joined. "
                        "Do not call agent.run_subtasks again. Use any remaining parent-Run "
                        "market tools, or return the final evidence-grounded synthesis now."
                    ),
                    "blocked_call_ids": [item.get("id") for item in redundant_critic_calls],
                }
                transcript.append(
                    {"role": "host", "type": "policy_feedback", "content": feedback}
                )
                await record(
                    "critic.duplicate_delegation.blocked",
                    step=step,
                    blocked_call_ids=feedback["blocked_call_ids"],
                )
                if not calls:
                    final_turn = turn
                    continue
            if rejected_calls:
                turn = {**turn, "tool_calls": calls, "state": "continue"}
                transcript[-1] = {"role": "agent", "type": "decision", "content": turn}
                argument_error = {
                    "error": "One or more tool calls had invalid JSON arguments and were not added to the plan.",
                    "rejected_calls": [
                        {
                            "id": call["id"],
                            "name": call["name"],
                            "message": call["arguments"].get("_agent_argument_error")
                            or _tool_schema_error_message(
                                call.get("_argument_schema_errors") or []
                            ),
                            "raw_arguments": call["arguments"].get("_raw_arguments"),
                            "schema_errors": call.get("_argument_schema_errors") or [],
                        }
                        for call in rejected_calls
                    ],
                    "required_action": (
                        "Retry those tool calls with one JSON object that matches each tool's input schema."
                    ),
                }
                await record(
                    "tool.arguments.rejected",
                    step=step,
                    rejected_call_count=len(rejected_calls),
                    rejected_calls=argument_error["rejected_calls"],
                    summary="不合法的工具參數未加入執行計畫；已要求模型依 schema 修正。",
                )
                transcript.append(
                    {"role": "host", "type": "tool_argument_error", "content": argument_error}
                )
                if not calls:
                    final_turn = turn
                    continue
            forced_paper_call = _host_explicit_paper_protocol_call(
                context=context,
                objective=objective_text,
                task_kind=task_kind,
                trace=trace,
                tool_metadata=tool_metadata,
                step=step,
            )
            if forced_paper_call is not None:
                discarded_calls = [str(call.get("name") or "") for call in calls]
                calls = [forced_paper_call]
                rejected_calls = []
                turn = {
                    **turn,
                    "state": "continue",
                    "summary": (
                        "Host is executing the explicitly authorized local paper-order protocol "
                        f"step: {forced_paper_call['name']}."
                    ),
                    "plan_patch": None,
                    "tool_calls": calls,
                    "decision": None,
                    "interaction": None,
                }
                transcript[-1] = {"role": "agent", "type": "decision", "content": turn}
                await record(
                    "paper_order.host_protocol.dispatched",
                    step=step,
                    tool_name=forced_paper_call["name"],
                    arguments=forced_paper_call["arguments"],
                    discarded_provider_tools=discarded_calls,
                    reason="explicit_local_paper_order_requires_host_verified_protocol",
                    summary=(
                        "Host dispatched the next bounded local paper-order step from explicit "
                        "user parameters instead of accepting an unrelated provider plan."
                    ),
                )
            host_coverage_calls = _host_explicit_market_coverage_calls(
                objective=objective_text,
                task_kind=task_kind,
                symbols=context.symbols,
                trace=trace,
                calls=calls,
                tool_metadata=tool_metadata,
                step=step,
            )
            if host_coverage_calls:
                calls.extend(host_coverage_calls)
                turn = {
                    **turn,
                    "state": "continue",
                    "plan_patch": None,
                    "tool_calls": calls,
                    "decision": None,
                    "interaction": None,
                }
                transcript[-1] = {"role": "agent", "type": "decision", "content": turn}
                await record(
                    "market_coverage.host_protocol.dispatched",
                    step=step,
                    tool_names=[call["name"] for call in host_coverage_calls],
                    symbol=str(context.symbols[0]) if context.symbols else None,
                    summary=(
                        "Host dispatched the explicitly requested, missing read-only market "
                        "evidence before allowing a final synthesis."
                    ),
                )
            if turn.get("plan_patch"):
                provider_patch, patch_repairs = _repair_provider_plan_patch(
                    plan,
                    turn["plan_patch"],
                    allowed_capabilities=set(tool_metadata),
                )
                if patch_repairs:
                    await record(
                        "plan.patch.repaired",
                        step=step,
                        reason="Removed dangling provider-authored dependencies.",
                        repairs=patch_repairs,
                    )
                try:
                    plan = self._apply_plan_patch(
                        plan,
                        run_id=run_id,
                        patch=provider_patch,
                        reason=provider_patch["reason_summary"],
                    )
                    await record(
                        "plan.revised",
                        step=step,
                        plan_id=plan.plan_id,
                        revision=plan.revision_number,
                        reason_summary=provider_patch["reason_summary"],
                        patch=provider_patch,
                        plan=plan.to_dict(),
                    )
                except Exception as exc:
                    error = classify_error(exc).to_dict()
                    await record("plan.compile.failed", step=step, error=error)
                    transcript.append(
                        {"role": "host", "type": "plan_error", "content": error}
                    )
                    # Provider-neutral tool calls are authoritative requests, while a
                    # model-authored PlanPatch is only an optional planning aid. Local
                    # models commonly emit RFC-6902 operations (``add``) instead of our
                    # graph operations (``add_node``). Do not discard otherwise valid,
                    # schema-checked tool calls: the host creates safe executable nodes
                    # for them below and still applies policy, approval and validation.
                    if calls:
                        await record(
                            "plan.patch.ignored",
                            step=step,
                            reason="invalid_optional_provider_plan_patch",
                            preserved_tool_call_count=len(calls),
                        )
                    else:
                        final_turn = turn
                        continue
            auto_patch = _tool_call_plan_patch(plan, calls, tool_metadata, step)
            if auto_patch["operations"]:
                plan = self._apply_plan_patch(
                    plan,
                    run_id=run_id,
                    patch=auto_patch,
                    reason="Host linked requested tool calls to executable plan nodes.",
                )
                await record(
                    "plan.revised",
                    step=step,
                    plan_id=plan.plan_id,
                    revision=plan.revision_number,
                    reason_summary="Linked model tool requests to plan nodes.",
                    patch=auto_patch,
                    plan=plan.to_dict(),
                )
            compiled = PlanCompiler(tool_manifest).compile(plan, autonomy=autonomy)
            await record(
                "plan.compiled" if compiled.valid else "plan.compile.failed",
                step=step,
                plan_id=plan.plan_id,
                revision=plan.revision_number,
                result=compiled.to_dict(),
            )
            if not compiled.valid:
                transcript.append(
                    {"role": "host", "type": "plan_compile_errors", "content": compiled.to_dict()}
                )
                final_turn = turn
                continue
            host_progress = True
            while host_progress and not paused_approval:
                host_progress = False
                reasoning_started = any(
                    item.node_type == "reasoning" and item.status == "running"
                    for item in plan.nodes.values()
                )
                for node_id in compiled.executable_nodes:
                    node = plan.nodes[node_id]
                    if node.node_type in {
                        "tool",
                        "subtask",
                        "subagent",
                        "schedule",
                        "workflow",
                        "finalize",
                    }:
                        continue
                    if node.node_type == "reasoning":
                        linked_to_current_turn = bool(
                            set(node.tool_call_ids)
                            & {str(call.get("id") or "") for call in calls}
                        )
                        if calls and not linked_to_current_turn:
                            continue
                        if reasoning_started and not linked_to_current_turn:
                            continue
                        plan.mark(node_id, "running")
                        await record(
                            "step.started",
                            step=step,
                            step_id=node_id,
                            node_id=node_id,
                            parent_step_id=node.parent_id,
                            node_type=node.node_type,
                            title=node.title,
                            description=node.description,
                            expected_tool_call_ids=list(node.tool_call_ids),
                        )
                        await record(
                            "plan.step.progress",
                            step=step,
                            step_id=node_id,
                            node_id=node_id,
                            node_type=node.node_type,
                            title=node.title,
                            description=node.description,
                            state="running",
                            expected_tool_call_ids=list(node.tool_call_ids),
                            plan=plan.to_dict(),
                        )
                        reasoning_started = True
                        host_progress = True
                        if self.plan_manager:
                            self.plan_manager.save_state(plan, run_id=run_id)
                        if not calls and not node.tool_call_ids:
                            await report_running_reasoning_nodes(
                                turn_step=step,
                                turn_summary=str(turn.get("summary") or node.title),
                                remaining_gaps=[
                                    str(item)
                                    for item in (
                                        (turn.get("completion_evaluation") or {}).get(
                                            "remaining_gaps"
                                        )
                                        or []
                                    )
                                    if str(item).strip()
                                ],
                            )
                            reasoning_started = any(
                                item.node_type == "reasoning"
                                and item.status == "running"
                                for item in plan.nodes.values()
                            )
                        continue
                    plan.mark(node_id, "running")
                    await record(
                        "step.started",
                        step=step,
                        step_id=node_id,
                        node_id=node_id,
                        parent_step_id=node.parent_id,
                        node_type=node.node_type,
                        title=node.title,
                        assigned_agent=node.assigned_agent,
                    )
                    host_result: dict[str, Any] | None = None
                    if node.node_type == "checkpoint":
                        checkpoint = self._checkpoint(
                            session_id=session_id,
                            run_id=run_id,
                            sequence=sequence,
                            plan=plan,
                            transcript=transcript,
                            trace=trace,
                            context=context,
                        )
                        if checkpoint is None:
                            plan.mark(node_id, "failed")
                            await record(
                                "plan.node.failed",
                                step=step,
                                node_id=node_id,
                                node_type=node.node_type,
                                reason="checkpoint_manager_unavailable",
                            )
                            await record(
                                "step.failed",
                                step=step,
                                step_id=node_id,
                                node_id=node_id,
                                node_type=node.node_type,
                                error_summary="checkpoint_manager_unavailable",
                            )
                            continue
                        host_result = {"checkpoint": _checkpoint_reference(checkpoint)}
                        await record(
                            "checkpoint.created",
                            step=step,
                            checkpoint=_checkpoint_reference(checkpoint),
                        )
                    elif node.node_type == "validation":
                        validation = self.validator.validate_plan_node(
                            node=node.to_dict(),
                            dependency_evidence=trace,
                        )
                        await record(
                            "validation.passed" if validation.passed else "validation.failed",
                            step=step,
                            node_id=node_id,
                            validator="plan_node_validator",
                            validation=validation.to_dict(),
                        )
                        if not validation.passed:
                            plan.mark(node_id, "failed")
                            await record(
                                "step.failed",
                                step=step,
                                step_id=node_id,
                                node_id=node_id,
                                node_type=node.node_type,
                                error_summary="Host validation failed",
                                validation=validation.to_dict(),
                            )
                            transcript.append(
                                {
                                    "role": "host",
                                    "type": "validation_failure",
                                    "content": {
                                        "node_id": node_id,
                                        "validation": validation.to_dict(),
                                    },
                                }
                            )
                            continue
                        host_result = {"validation": validation.to_dict()}
                    elif node.node_type == "approval":
                        target_tool = str(
                            node.tool_name or node.metadata.get("target_tool") or ""
                        )
                        if (
                            target_tool == "paper.submit_order"
                            and context.state.get("explicit_paper_order_authorized") is True
                        ):
                            host_result = {
                                "approval": {
                                    "status": "not_required",
                                    "reason": "explicit_paper_order_authorized",
                                    "tool_name": target_tool,
                                }
                            }
                            await record(
                                "approval.not_required",
                                step=step,
                                node_id=node_id,
                                tool_name=target_tool,
                                reason="explicit_paper_order_authorized",
                            )
                            # The following shared completion code marks this
                            # Host node completed and unlocks the exact order.
                        elif self.approval_manager is None:
                            plan.mark(node_id, "failed")
                            await record(
                                "plan.node.failed",
                                step=step,
                                node_id=node_id,
                                node_type=node.node_type,
                                reason="approval_manager_unavailable",
                            )
                            await record(
                                "step.failed",
                                step=step,
                                step_id=node_id,
                                node_id=node_id,
                                node_type=node.node_type,
                                error_summary="approval_manager_unavailable",
                            )
                            continue
                        else:
                            try:
                                approval = self.approval_manager.require(
                                    run_id=run_id,
                                    step_id=node_id,
                                    tool_name=target_tool,
                                    arguments=node_execution_arguments(node),
                                    resource_scope=_resource_scope(
                                        target_tool,
                                        node_execution_arguments(node),
                                    ),
                                    risk_class=str(
                                        node.metadata.get("risk_class") or "local_reversible"
                                    ),
                                )
                            except ApprovalRequiredError as exc:
                                paused_approval = exc.approval
                                plan.mark(node_id, "waiting_approval")
                                await record(
                                    "approval.requested",
                                    step=step,
                                    node_id=node_id,
                                    approval=paused_approval,
                                )
                                await record(
                                    "step.waiting_approval",
                                    step=step,
                                    step_id=node_id,
                                    node_id=node_id,
                                    node_type=node.node_type,
                                    approval_id=paused_approval.get("approval_id"),
                                )
                                checkpoint = self._checkpoint(
                                    session_id=session_id,
                                    run_id=run_id,
                                    sequence=sequence,
                                    plan=plan,
                                    transcript=transcript,
                                    trace=trace,
                                    context=context,
                                )
                                if checkpoint:
                                    await record(
                                        "checkpoint.created",
                                        step=step,
                                        checkpoint=_checkpoint_reference(checkpoint),
                                    )
                                break
                            host_result = {"approval": approval}
                            await record(
                                "approval.resolved",
                                step=step,
                                node_id=node_id,
                                approval_id=approval["approval_id"],
                                status=approval["status"],
                            )
                    else:
                        plan.mark(node_id, "failed")
                        await record(
                            "plan.node.failed",
                            step=step,
                            node_id=node_id,
                            node_type=node.node_type,
                            reason="unsupported_host_node",
                        )
                        await record(
                            "step.failed",
                            step=step,
                            step_id=node_id,
                            node_id=node_id,
                            node_type=node.node_type,
                            error_summary="unsupported_host_node",
                        )
                        continue
                    plan.mark(node_id, "completed")
                    trace.append(
                        {
                            "step": step,
                            "call_id": node_id,
                            "node_id": node_id,
                            "tool": f"host.{node.node_type}",
                            "arguments": node_execution_arguments(node),
                            "ok": True,
                            "started_at": _now(),
                            "finished_at": _now(),
                            "result": host_result or {},
                            "result_summary": _result_summary(host_result or {}),
                            "validation": {
                                "passed": True,
                                "validator": f"host_{node.node_type}",
                                "evidence_hash": _hash_payload(host_result or {}),
                            },
                        }
                    )
                    await record(
                        "plan.node.completed",
                        step=step,
                        node_id=node_id,
                        node_type=node.node_type,
                        result=host_result,
                    )
                    await record(
                        "step.completed",
                        step=step,
                        step_id=node_id,
                        node_id=node_id,
                        node_type=node.node_type,
                        result_summary=_result_summary(host_result or {}),
                    )
                    host_progress = True
                    if self.plan_manager:
                        self.plan_manager.save_state(plan, run_id=run_id)
                if host_progress and not paused_approval:
                    compiled = PlanCompiler(tool_manifest).compile(plan, autonomy=autonomy)
                    await record(
                        "plan.compiled",
                        step=step,
                        plan_id=plan.plan_id,
                        revision=plan.revision_number,
                        reason="host_nodes_dispatched",
                        result=compiled.to_dict(),
                    )
            if paused_approval:
                final_turn = turn
                break
            ready_node_ids = set(compiled.executable_nodes)
            requested_by_node = {_call_node_id(call): call for call in calls}
            dispatch_calls: list[dict[str, Any]] = []
            for node_id in compiled.executable_nodes:
                node = plan.nodes[node_id]
                capability_name = node_capability(node)
                if capability_name is None or node.node_type == "approval":
                    continue
                requested = requested_by_node.get(node_id)
                if requested is not None and (
                    requested["name"] != capability_name
                    or requested["arguments"] != node_execution_arguments(node)
                ):
                    error = {
                        "code": "plan_call_mismatch",
                        "node_id": node_id,
                        "message": "Model tool call did not match the compiled PlanGraph node.",
                    }
                    await record("plan.dispatch.blocked", step=step, error=error)
                    transcript.append({"role": "host", "type": "plan_error", "content": error})
                    continue
                dispatch_calls.append(
                    {
                        "id": str(node.metadata.get("model_call_id") or node.node_id),
                        "name": capability_name,
                        "arguments": node_execution_arguments(node),
                        "node_id": node.node_id,
                    }
                )
            blocked_requested = [
                {
                    "id": call["id"],
                    "name": call["name"],
                    "node_id": _call_node_id(call),
                    "dependencies": list(plan.nodes[_call_node_id(call)].dependencies),
                }
                for call in calls
                if _call_node_id(call) in plan.nodes
                and _call_node_id(call) not in ready_node_ids
                and plan.nodes[_call_node_id(call)].status not in {"completed", "skipped"}
            ]
            for call in calls:
                node_id = _call_node_id(call)
                if (
                    node_id in plan.nodes
                    and plan.nodes[node_id].status == "completed"
                    and _reusable_observation(
                        trace,
                        call,
                        tool_metadata.get(call["name"], {}),
                    )
                    is not None
                ):
                    await record(
                        "tool.reused",
                        step=step,
                        call_id=call["id"],
                        tool=call["name"],
                        node_id=node_id,
                        reason="completed_plan_node",
                    )
            if blocked_requested:
                await record(
                    "plan.dispatch.blocked",
                    step=step,
                    reason="dependencies_not_completed",
                    requested=blocked_requested,
                )
                transcript.append(
                    {
                        "role": "host",
                        "type": "plan_dispatch_blocked",
                        "content": blocked_requested,
                    }
                )
            calls = dispatch_calls
            await record(
                "plan.current",
                step=step,
                summary=turn["summary"],
                state=turn["state"],
                requested_tools=[
                    {
                        "id": call["id"],
                        "name": call["name"],
                        "arguments": _safe_arguments(call["arguments"]),
                        "skills": _tool_skills(tool_metadata.get(call["name"], {}), call),
                        "packages": tool_metadata.get(call["name"], {}).get("packages", []),
                        "schedules": tool_metadata.get(call["name"], {}).get("schedules", []),
                    }
                    for call in calls
                ],
                decision=turn.get("decision"),
                plan_id=plan.plan_id,
                revision=plan.revision_number,
            )

            if calls:
                for requested_call in calls:
                    requested_tool = str(requested_call.get("name") or "")
                    cost_budget_manager.ensure(
                        "tool",
                        requested_tool,
                        limit=max(8, bounded_steps * 2),
                    )
                    try:
                        cost_budget_manager.charge(
                            1,
                            session_id=session_id,
                            tool_name=requested_tool,
                        )
                        runaway_guard.observe_tool(
                            tool_name=requested_tool,
                            arguments=dict(requested_call.get("arguments") or {}),
                        )
                    except (CostBudgetExceeded, RunawayExecutionExceeded) as exc:
                        cost_budget_exhausted = True
                        final_turn = {
                            "state": "continue",
                            "summary": "Host execution guard blocked further tool dispatch.",
                            "tool_calls": [],
                            "decision": None,
                            "completion_evaluation": {
                                "remaining_gaps": ["execution_guard_blocked"]
                            },
                        }
                        await record(
                            "execution_guard.blocked",
                            step=step,
                            tool=requested_tool,
                            error_type=type(exc).__name__,
                            error=str(exc),
                            guard={
                                "cost_budgets": cost_budget_manager.snapshot(),
                                "runaway": runaway_guard.snapshot(),
                            },
                        )
                        break
                if cost_budget_exhausted:
                    context.state["execution_guard"] = {
                        "cost_budgets": cost_budget_manager.snapshot(),
                        "runaway": runaway_guard.snapshot(),
                    }
                    break
                observations = []
                parallel_prepared: dict[str, dict[str, Any]] = {}
                parallel_candidates: list[tuple[dict[str, Any], dict[str, Any], str, Any]] = []
                if len(calls) > 1:
                    for candidate in calls:
                        candidate_metadata = tool_metadata.get(candidate["name"], {})
                        candidate_node_id = str(
                            candidate.get("node_id") or _call_node_id(candidate)
                        )
                        candidate_policy = self.policy_engine.evaluate(
                            tool=candidate_metadata,
                            arguments=candidate["arguments"],
                            context=context,
                        )
                        retry_policy = candidate_metadata.get("retry_policy") or {}
                        if (
                            _parallel_read_only_tool(candidate_metadata)
                            and candidate_policy.action == "allow"
                            and not candidate_policy.required_approval
                            and int(retry_policy.get("max_attempts") or 1) == 1
                            and _reusable_observation(trace, candidate, candidate_metadata) is None
                            and _prior_repeated_tool_failure(trace, candidate) is None
                        ):
                            parallel_candidates.append(
                                (
                                    candidate,
                                    candidate_metadata,
                                    candidate_node_id,
                                    candidate_policy,
                                )
                            )
                if len(parallel_candidates) > 1:
                    await record(
                        "tool.parallel_batch.started",
                        step=step,
                        call_ids=[item[0]["id"] for item in parallel_candidates],
                        node_ids=[item[2] for item in parallel_candidates],
                        tool_count=len(parallel_candidates),
                        execution_mode="parallel_read_only",
                        summary=f"Started {len(parallel_candidates)} independent read-only tools in parallel.",
                    )
                    for candidate, candidate_metadata, candidate_node_id, candidate_policy in parallel_candidates:
                        candidate_started_at = _now()
                        if candidate_node_id in plan.nodes:
                            plan.mark(candidate_node_id, "running")
                        candidate_node = plan.nodes.get(candidate_node_id)
                        await record(
                            "step.started",
                            step=step,
                            step_id=candidate_node_id,
                            node_id=candidate_node_id,
                            parent_step_id=candidate_node.parent_id if candidate_node else None,
                            node_type=candidate_node.node_type if candidate_node else "tool",
                            title=candidate_node.title if candidate_node else candidate["name"],
                            assigned_agent=candidate_node.assigned_agent if candidate_node else None,
                        )
                        candidate_skills = _tool_skills(candidate_metadata, candidate)
                        for skill_id in candidate_skills:
                            await record(
                                "skill.selected",
                                step=step,
                                node_id=candidate_node_id,
                                call_id=candidate["id"],
                                skill_id=skill_id,
                                tool=candidate["name"],
                                source="host_capability_registry",
                            )
                            await record(
                                "skill.started",
                                step=step,
                                node_id=candidate_node_id,
                                call_id=candidate["id"],
                                skill_id=skill_id,
                                tool=candidate["name"],
                            )
                        for package_id in candidate_metadata.get("packages", []):
                            await record(
                                "package.loaded",
                                step=step,
                                node_id=candidate_node_id,
                                call_id=candidate["id"],
                                package_id=package_id,
                                tool=candidate["name"],
                            )
                        if candidate_metadata.get("mcp_server"):
                            await record(
                                "mcp.tool.selected",
                                step=step,
                                node_id=candidate_node_id,
                                call_id=candidate["id"],
                                mcp_server=candidate_metadata.get("mcp_server"),
                                tool=candidate["name"],
                            )
                        await record(
                            "policy.evaluated",
                            step=step,
                            node_id=candidate_node_id,
                            call_id=candidate["id"],
                            tool=candidate["name"],
                            policy=candidate_policy.to_dict(),
                        )
                        await record(
                            "tool.queued",
                            step=step,
                            node_id=candidate_node_id,
                            call_id=candidate["id"],
                            tool=candidate["name"],
                            execution_mode="parallel_read_only",
                        )
                        await record(
                            "tool.started",
                            step=step,
                            call_id=candidate["id"],
                            tool=candidate["name"],
                            category=candidate_metadata.get("category"),
                            arguments=_safe_arguments(candidate["arguments"]),
                            skills=candidate_skills,
                            packages=candidate_metadata.get("packages", []),
                            schedules=candidate_metadata.get("schedules", []),
                            node_id=candidate_node_id,
                            risk_class=candidate_metadata.get("risk_class"),
                            tool_provider=candidate_metadata.get("provider"),
                            execution_mode="parallel_read_only",
                        )
                        parallel_prepared[str(candidate["id"])] = {
                            "started_at": candidate_started_at,
                            "metadata": candidate_metadata,
                            "node_id": candidate_node_id,
                            "policy_decision": candidate_policy,
                            "selected_skills": candidate_skills,
                            "before_snapshot": (
                                self.snapshot_builder.build(
                                    context,
                                    plan=plan.to_dict(),
                                    memories=memories,
                                )
                                if self.snapshot_builder
                                else None
                            ),
                        }
                    if self.plan_manager:
                        self.plan_manager.save_state(plan, run_id=run_id)
                    parallel_results = await self._execute_parallel_tools_with_forest(
                        parallel_candidates=parallel_candidates,
                        context=context,
                    )
                    for candidate_spec, candidate_result in zip(
                        parallel_candidates,
                        parallel_results,
                        strict=True,
                    ):
                        parallel_prepared[str(candidate_spec[0]["id"])]["execution"] = candidate_result
                    await record(
                        "tool.parallel_batch.completed",
                        step=step,
                        call_ids=[item[0]["id"] for item in parallel_candidates],
                        tool_count=len(parallel_candidates),
                        failed_count=sum(
                            isinstance(item, BaseException) for item in parallel_results
                        ),
                        execution_mode="parallel_read_only",
                        summary=f"Finished {len(parallel_candidates)} parallel read-only tools.",
                    )
                for call in calls:
                    prepared_parallel = parallel_prepared.get(str(call["id"]))
                    started_at = (
                        str(prepared_parallel["started_at"])
                        if prepared_parallel is not None
                        else _now()
                    )
                    metadata = tool_metadata.get(call["name"], {})
                    node_id = str(call.get("node_id") or _call_node_id(call))
                    reused = _reusable_observation(trace, call, metadata)
                    if reused is not None:
                        observations.append(reused)
                        # A durable idempotency hit is still a successful
                        # execution of *this* PlanGraph node.  Previously we
                        # emitted ``tool.reused``/``step.completed`` only,
                        # leaving a restored pending node pending forever.
                        # The next provider turn then requested the same
                        # already-validated tool again until its step budget
                        # expired, even though no evidence was missing.  Keep
                        # the plan projection authoritative and persist this
                        # smallest-boundary recovery before moving on.
                        if node_id in plan.nodes and plan.nodes[node_id].status not in {
                            "completed",
                            "skipped",
                        }:
                            plan.mark(node_id, "completed")
                            if self.plan_manager:
                                self.plan_manager.save_state(plan, run_id=run_id)
                            await record(
                                "plan.node.completed",
                                step=step,
                                node_id=node_id,
                                node_type=plan.nodes[node_id].node_type,
                                tool_call_id=call["id"],
                                result_summary=_result_summary(reused),
                                reused=True,
                                reason="durable_idempotency_match",
                            )
                        await record(
                            "tool.reused",
                            step=step,
                            call_id=call["id"],
                            tool=call["name"],
                            node_id=node_id,
                            result_summary=_result_summary(reused),
                            reason="durable_idempotency_match",
                        )
                        await record(
                            "step.completed",
                            step=step,
                            step_id=node_id,
                            node_id=node_id,
                            tool_call_id=call["id"],
                            result_summary=_result_summary(reused),
                            reused=True,
                        )
                        continue
                    prior_failure = _prior_repeated_tool_failure(trace, call)
                    if prior_failure is not None:
                        identical_arguments = prior_failure.get("arguments") == call.get("arguments")
                        alternatives = _alternative_tools_for_failure(
                            call["name"],
                            tool_manifest,
                        )
                        error = {
                            "schema_version": "open_stock_ai.execution_error.v1",
                            "category": (
                                "identical_retry_blocked"
                                if identical_arguments
                                else "repeated_tool_failure_blocked"
                            ),
                            "recoverable": True,
                            "retryable": False,
                            "action_hints": [
                                "choose_alternate_tool",
                                "choose_alternate_source",
                                "use_existing_verified_evidence",
                            ],
                            "exception_type": "IdenticalRetryBlocked",
                            "message": (
                                f"Host blocked repeated use of failed tool {call['name']} after its "
                                "source/transport retry was exhausted. Do not request it again in this Run; choose a different "
                                "tool/source or finish from existing verified evidence."
                            ),
                        }
                        recovery = {
                            "schema_version": "open_stock_ai.recovery_decision.v1",
                            "action": "choose_alternate_tool",
                            "retry_delay_seconds": 0,
                            "reason": "This tool already exhausted a source/transport failure in this Run.",
                            "requires_model_replan": True,
                            "alternatives": alternatives,
                        }
                        observation = {
                            "id": call["id"],
                            "name": call["name"],
                            "ok": False,
                            "error": error,
                            "recovery": recovery,
                            "repair_ladder": {
                                "level": int(RecoveryLevel.ALTERNATIVE_TOOL),
                                "strategy": RecoveryLevel.ALTERNATIVE_TOOL.strategy,
                                "identical_retry_blocked": True,
                            },
                            "prior_failure_call_id": prior_failure.get("call_id"),
                            "alternatives": alternatives,
                        }
                        if node_id in plan.nodes:
                            plan.mark(node_id, "failed")
                            if self.plan_manager:
                                self.plan_manager.save_state(plan, run_id=run_id)
                        transcript.append(
                            {
                                "role": "host",
                                "type": "recovery_directive",
                                "content": {
                                    "node_id": node_id,
                                    "tool": call["name"],
                                    "arguments": _safe_arguments(call["arguments"]),
                                    "error": error,
                                    "recovery": recovery,
                                    "repair_ladder": observation["repair_ladder"],
                                    "prior_failure_call_id": prior_failure.get("call_id"),
                                },
                            }
                        )
                        await record(
                            "repair.strategy_changed",
                            step=step,
                            node_id=node_id,
                            call_id=call["id"],
                            tool=call["name"],
                            from_strategy="retry_temporary_failure",
                            strategy=RecoveryLevel.ALTERNATIVE_TOOL.strategy,
                            identical_retry_blocked=True,
                            identical_arguments=identical_arguments,
                            prior_failure_call_id=prior_failure.get("call_id"),
                            alternatives=alternatives,
                            summary="Blocked repeated failed tool and required an alternate tool/source.",
                        )
                        await record(
                            "tool.failed",
                            step=step,
                            node_id=node_id,
                            call_id=call["id"],
                            tool=call["name"],
                            error=error,
                            execution_skipped=True,
                        )
                        await record(
                            "step.failed",
                            step=step,
                            step_id=node_id,
                            node_id=node_id,
                            call_id=call["id"],
                            error_summary=error["message"],
                            error=error,
                        )
                        observations.append(observation)
                        trace.append(
                            {
                                "step": step,
                                "call_id": call["id"],
                                "tool": call["name"],
                                "arguments": call["arguments"],
                                "ok": False,
                                "started_at": started_at,
                                "finished_at": _now(),
                                "result_summary": _result_summary(observation),
                                "node_id": node_id,
                                "error": error,
                                "recovery": recovery,
                                "failure_fingerprint": prior_failure.get("failure_fingerprint"),
                                "execution_skipped": True,
                            }
                        )
                        continue
                    if prepared_parallel is None and node_id in plan.nodes:
                        plan.mark(node_id, "running")
                        if self.plan_manager:
                            self.plan_manager.save_state(plan, run_id=run_id)
                    node = plan.nodes.get(node_id)
                    selected_skills = (
                        list(prepared_parallel["selected_skills"])
                        if prepared_parallel is not None
                        else _tool_skills(metadata, call)
                    )
                    if prepared_parallel is None:
                        await record(
                            "step.started",
                            step=step,
                            step_id=node_id,
                            node_id=node_id,
                            parent_step_id=node.parent_id if node else None,
                            node_type=node.node_type if node else "tool",
                            title=node.title if node else call["name"],
                            assigned_agent=node.assigned_agent if node else None,
                        )
                        for skill_id in selected_skills:
                            await record(
                                "skill.selected",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                skill_id=skill_id,
                                tool=call["name"],
                                source="host_capability_registry",
                            )
                            await record(
                                "skill.started",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                skill_id=skill_id,
                                tool=call["name"],
                            )
                        for package_id in metadata.get("packages", []):
                            await record(
                                "package.loaded",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                package_id=package_id,
                                tool=call["name"],
                            )
                        if metadata.get("mcp_server"):
                            await record(
                                "mcp.tool.selected",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                mcp_server=metadata.get("mcp_server"),
                                tool=call["name"],
                            )
                        policy_decision = self.policy_engine.evaluate(
                            tool=metadata,
                            arguments=call["arguments"],
                            context=context,
                        )
                        await record(
                            "policy.evaluated",
                            step=step,
                            node_id=node_id,
                            call_id=call["id"],
                            tool=call["name"],
                            policy=policy_decision.to_dict(),
                        )
                    else:
                        policy_decision = prepared_parallel["policy_decision"]
                    if policy_decision.action == "deny":
                        policy_error = PermissionError(policy_decision.reason)
                        classified = classify_error(policy_error)
                        recovery = self.recovery.decide(
                            classified,
                            attempt=1,
                            max_attempts=1,
                            mutation_started=False,
                            rollback_available=False,
                        )
                        if node_id in plan.nodes:
                            plan.mark(node_id, "failed")
                        observation = {
                            "id": call["id"],
                            "name": call["name"],
                            "ok": False,
                            "error": classified.to_dict(),
                            "recovery": recovery.to_dict(),
                        }
                        await record(
                            "policy.denied",
                            step=step,
                            node_id=node_id,
                            tool=call["name"],
                            error=observation["error"],
                        )
                        for skill_id in selected_skills:
                            await record(
                                "skill.failed",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                skill_id=skill_id,
                                tool=call["name"],
                                error=observation["error"],
                            )
                        await record(
                            "step.failed",
                            step=step,
                            step_id=node_id,
                            node_id=node_id,
                            call_id=call["id"],
                            error_summary=policy_decision.reason,
                            error=observation["error"],
                        )
                        observations.append(observation)
                        trace.append(
                            {
                                "step": step,
                                "call_id": call["id"],
                                "tool": call["name"],
                                "arguments": call["arguments"],
                                "ok": False,
                                "started_at": started_at,
                                "finished_at": _now(),
                                "result_summary": _result_summary(observation),
                                "node_id": node_id,
                                "error": observation["error"],
                                "recovery": observation["recovery"],
                            }
                        )
                        continue
                    approval: dict[str, Any] | None = None
                    if self.approval_manager and policy_decision.required_approval:
                        approval = _approved_dependency(
                            plan=plan,
                            node_id=node_id,
                            run_id=run_id,
                            tool_name=call["name"],
                            arguments=call["arguments"],
                            manager=self.approval_manager,
                        )
                        if approval is None:
                            try:
                                approval = self.approval_manager.require(
                                    run_id=run_id,
                                    step_id=node_id,
                                    tool_name=call["name"],
                                    arguments=call["arguments"],
                                    resource_scope=_resource_scope(call["name"], call["arguments"]),
                                    risk_class=str(metadata.get("risk_class") or "local_reversible"),
                                )
                            except ApprovalRequiredError as exc:
                                paused_approval = exc.approval
                                plan.mark(node_id, "waiting_approval")
                                await record(
                                    "approval.requested",
                                    step=step,
                                    node_id=node_id,
                                    approval=paused_approval,
                                )
                                await record(
                                    "step.waiting_approval",
                                    step=step,
                                    step_id=node_id,
                                    node_id=node_id,
                                    call_id=call["id"],
                                    approval_id=paused_approval.get("approval_id"),
                                )
                                checkpoint = self._checkpoint(
                                    session_id=session_id,
                                    run_id=run_id,
                                    sequence=sequence,
                                    plan=plan,
                                    transcript=transcript,
                                    trace=trace,
                                    context=context,
                                )
                                if checkpoint:
                                    await record(
                                        "checkpoint.created",
                                        step=step,
                                        checkpoint=_checkpoint_reference(checkpoint),
                                    )
                                break
                        if approval is not None:
                            await record(
                                "approval.resolved",
                                step=step,
                                node_id=node_id,
                                approval_id=approval["approval_id"],
                                status=approval["status"],
                            )
                    if prepared_parallel is None:
                        await record(
                            "tool.queued",
                            step=step,
                            node_id=node_id,
                            call_id=call["id"],
                            tool=call["name"],
                        )
                        await record(
                            "tool.started",
                            step=step,
                            call_id=call["id"],
                            tool=call["name"],
                            category=metadata.get("category"),
                            arguments=_safe_arguments(call["arguments"]),
                            skills=selected_skills,
                            packages=metadata.get("packages", []),
                            schedules=metadata.get("schedules", []),
                            node_id=node_id,
                            risk_class=metadata.get("risk_class"),
                            tool_provider=metadata.get("provider"),
                        )
                    before_snapshot = (
                        prepared_parallel.get("before_snapshot")
                        if prepared_parallel is not None
                        else (
                            self.snapshot_builder.build(
                                context, plan=plan.to_dict(), memories=memories
                            )
                            if self.snapshot_builder
                            else None
                        )
                    )
                    if metadata.get("mutating"):
                        checkpoint = self._checkpoint(
                            session_id=session_id,
                            run_id=run_id,
                            sequence=sequence,
                            plan=plan,
                            transcript=transcript,
                            trace=trace,
                            context=context,
                            rollback={"tool": call["name"], "arguments": _safe_arguments(call["arguments"])},
                        )
                        if checkpoint:
                            await record(
                                "checkpoint.created",
                                step=step,
                                checkpoint=_checkpoint_reference(checkpoint),
                            )
                    rollback_result: dict[str, Any] | None = None
                    result: dict[str, Any] | None = None
                    try:
                        policy = metadata.get("retry_policy") or {}
                        max_attempts = max(1, min(int(policy.get("max_attempts") or 1), 5))
                        worker_id: str | None = None
                        last_repair_decision: RepairDecision | None = None
                        if prepared_parallel is not None:
                            parallel_execution = prepared_parallel.get("execution")
                            if isinstance(parallel_execution, BaseException):
                                raise parallel_execution
                            result, worker_id = parallel_execution
                        for attempt in range(
                            1,
                            1 if prepared_parallel is not None else max_attempts + 1,
                        ):
                            try:
                                # A provider may create durable child Runs.
                                # Preserve the actual Plan node that owns this
                                # call so it can attach each child to the
                                # matching Task Forest Branch.
                                context.state["current_plan_node_id"] = node_id
                                result, worker_id = await self._execute_ordered_tool_with_forest(
                                    call=call,
                                    metadata=metadata,
                                    context=context,
                                    step_id=node_id,
                                )
                                break
                            except Exception as attempt_exc:
                                classified_attempt = classify_error(attempt_exc)
                                repair_decision = await record_repair_decision(
                                    classified=classified_attempt,
                                    node_id=node_id,
                                    tool_name=call["name"],
                                    arguments=dict(call["arguments"]),
                                    output=classified_attempt.to_dict(),
                                    previous_level=(
                                        last_repair_decision.level
                                        if last_repair_decision is not None
                                        else None
                                    ),
                                )
                                last_repair_decision = repair_decision
                                retry = self.recovery.decide(
                                    classified_attempt,
                                    attempt=attempt,
                                    max_attempts=max_attempts,
                                    mutation_started=bool(metadata.get("mutating")),
                                    rollback_available=bool(metadata.get("rollback_support")),
                                )
                                if (
                                    retry.action != "retry_same_step"
                                    or repair_decision.level is not RecoveryLevel.RETRY_TEMPORARY_FAILURE
                                    or repair_decision.identical_retry_blocked
                                ):
                                    raise
                                await record(
                                    "recovery.retry_scheduled",
                                    step=step,
                                    node_id=node_id,
                                    call_id=call["id"],
                                    tool=call["name"],
                                    attempt=attempt,
                                    max_attempts=max_attempts,
                                    recovery=retry.to_dict(),
                                )
                                await record(
                                    "tool.retrying",
                                    step=step,
                                    node_id=node_id,
                                    call_id=call["id"],
                                    tool=call["name"],
                                    attempt=attempt + 1,
                                    retry_count=attempt,
                                    max_attempts=max_attempts,
                                )
                                if retry.retry_delay_seconds:
                                    await asyncio.sleep(retry.retry_delay_seconds)
                        if result is None:
                            raise RuntimeError(f"{call['name']} produced no result")
                        after_snapshot = (
                            self.snapshot_builder.build(context, plan=plan.to_dict(), memories=memories)
                            if self.snapshot_builder
                            else None
                        )
                        before_evidence = before_snapshot.to_dict() if before_snapshot else {"phase": "before"}
                        after_evidence = (
                            after_snapshot.to_dict()
                            if after_snapshot
                            else {"phase": "after", "result": result}
                        )
                        await record(
                            "validation.started",
                            step=step,
                            node_id=node_id,
                            call_id=call["id"],
                            tool=call["name"],
                            validator=str(metadata.get("validator") or "default"),
                        )
                        validation = self.validator.validate_tool_result(
                            tool=metadata,
                            arguments=call["arguments"],
                            result=result,
                            before=before_evidence,
                            after=after_evidence,
                            postconditions=plan.nodes[node_id].postconditions,
                        )
                        await record(
                            "validation.passed" if validation.passed else "validation.failed",
                            step=step,
                            node_id=node_id,
                            call_id=call["id"],
                            tool=call["name"],
                            validation=validation.to_dict(),
                            evidence_ids=[
                                value
                                for value in (
                                    validation.to_dict().get("evidence_hash"),
                                    call["id"],
                                    node_id,
                                )
                                if value
                            ],
                        )
                        if not validation.passed:
                            rollback_result = await self.rollback_manager.rollback_if_available(
                                tool_name=call["name"],
                                result=result,
                                context=context,
                                execute=self.tools.execute,
                            )
                            if rollback_result is not None:
                                await record(
                                    "rollback.completed"
                                    if rollback_result.get("confirmed")
                                    else "rollback.failed",
                                    step=step,
                                    node_id=node_id,
                                    tool=call["name"],
                                    rollback=rollback_result,
                                )
                            raise RuntimeError(
                                f"Host validator rejected {call['name']}: {validation.to_dict()}"
                            )
                        observation = {
                            "id": call["id"],
                            "name": call["name"],
                            "ok": True,
                            "result": result,
                            "validation": validation.to_dict(),
                            "worker_id": worker_id,
                        }
                        recovery_for = _recovery_links_for_call(
                            trace,
                            call,
                            tool_manifest,
                            objective=objective_text,
                            symbols=normalized_symbols,
                            result=result,
                        )
                        if recovery_for:
                            # This durable relation is the only way a failed
                            # branch can be isolated at completion time.  It
                            # makes both the Task Forest and completion
                            # validator show the actual recovery path instead
                            # of silently treating unrelated work as a fix.
                            observation["recovery_for"] = recovery_for
                        canonical_evidence = _canonical_tool_evidence(
                            observation=observation,
                            call=call,
                            metadata=metadata,
                            run_id=run_id,
                        )
                        if _observation_is_substantive(observation):
                            successful_observations += 1
                        plan.mark(node_id, "completed")
                        if recovery_for:
                            if node_id in plan.nodes:
                                plan.nodes[node_id].metadata = {
                                    **plan.nodes[node_id].metadata,
                                    "recovery_for": recovery_for,
                                }
                            # The recovery link belongs to both ends of the
                            # relation.  Persist it on the failed node as well
                            # so a snapshot reload can render the Fishbone from
                            # durable Task-Forest state instead of depending on
                            # a transient stream reducer (P37/P56/P60).
                            for relation in recovery_for:
                                failed_node_id = str(relation.get("failed_node_id") or "")
                                failed_node = plan.nodes.get(failed_node_id)
                                if failed_node is None:
                                    continue
                                recovered_by = list(failed_node.metadata.get("recovered_by") or [])
                                if relation not in recovered_by:
                                    recovered_by.append(relation)
                                failed_node.metadata = {
                                    **failed_node.metadata,
                                    "recovered_by": recovered_by,
                                }
                        if self.plan_manager:
                            self.plan_manager.save_state(plan, run_id=run_id)
                        if approval and self.approval_manager:
                            self.approval_manager.consume(
                                approval["approval_id"],
                                result={"validation": validation.to_dict()},
                            )
                        await record(
                            "tool.completed",
                            step=step,
                            call_id=call["id"],
                            tool=call["name"],
                            result_summary=_result_summary(observation),
                            node_id=node_id,
                            worker_id=worker_id,
                            tool_provider=metadata.get("provider"),
                            source=(canonical_evidence[0].get("source") if canonical_evidence else None),
                            source_url=(canonical_evidence[0].get("source_url") if canonical_evidence else None),
                            freshness=(canonical_evidence[0].get("freshness") if canonical_evidence else "unknown"),
                            published_at=(canonical_evidence[0].get("published_at") if canonical_evidence else None),
                            observed_at=(canonical_evidence[0].get("observed_at") if canonical_evidence else _now()),
                            validation=validation.to_dict(),
                            evidence_ids=[
                                value
                                for value in (
                                    validation.to_dict().get("evidence_hash"),
                                    call["id"],
                                    node_id,
                                )
                                if value
                            ],
                        )
                        if recovery_for:
                            await record(
                                "recovery.linked",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                tool=call["name"],
                                recovery_for=recovery_for,
                                message="Validated alternative evidence linked to failed plan branch.",
                            )
                        if call["name"] in {"artifact.create_text", "artifact.create_structured"} and result.get("artifact_id"):
                            # ArtifactStore is the Host authority for the file.
                            # Publish its receipt as a first-class runtime event
                            # so an already-open Agent Dock can render the card
                            # without relying on a later full snapshot reload.
                            await record(
                                "artifact.created",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                artifact=result,
                                artifact_id=str(result["artifact_id"]),
                            )
                        if call["name"] == "artifact.patch_structured" and result.get("artifact_id"):
                            await record(
                                "artifact.updated",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                artifact=result.get("artifact") or result,
                                artifact_version=result,
                                artifact_id=str(result["artifact_id"]),
                            )
                        if research_plan is not None and (
                            call["name"].startswith(("market.", "web."))
                        ):
                            for evidence in canonical_evidence:
                                await record(
                                    "research.evidence_added",
                                    step=step,
                                    node_id=node_id,
                                    call_id=call["id"],
                                    tool=call["name"],
                                    tool_provider=metadata.get("provider"),
                                    evidence_ids=[evidence["evidence_id"]],
                                    evidence=evidence,
                                    research_plan_id=research_plan["plan_id"],
                                )
                        for skill_id in selected_skills:
                            await record(
                                "skill.completed",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                skill_id=skill_id,
                                tool=call["name"],
                            )
                        await record(
                            "step.completed",
                            step=step,
                            step_id=node_id,
                            node_id=node_id,
                            call_id=call["id"],
                            result_summary=_result_summary(observation),
                            evidence_ids=[
                                value
                                for value in (
                                    validation.to_dict().get("evidence_hash"),
                                    call["id"],
                                )
                                if value
                            ],
                        )
                        if before_snapshot and after_snapshot:
                            current_snapshot = after_snapshot
                            transcript.append(
                                {
                                    "role": "host",
                                    "type": "environment_diff",
                                    "content": self.snapshot_builder.diff(before_snapshot, after_snapshot),
                                }
                            )
                    except asyncio.CancelledError:
                        if node_id in plan.nodes:
                            plan.mark(node_id, "cancelled")
                            if self.plan_manager:
                                self.plan_manager.save_state(plan, run_id=run_id)
                        await asyncio.shield(
                            record(
                                "tool.cancelled",
                                step=step,
                                call_id=call["id"],
                                tool=call["name"],
                                node_id=node_id,
                                reason="run_cancelled",
                            )
                        )
                        await asyncio.shield(
                            record(
                                "step.cancelled",
                                step=step,
                                step_id=node_id,
                                node_id=node_id,
                                call_id=call["id"],
                                reason="run_cancelled",
                            )
                        )
                        raise
                    except Exception as exc:
                        classified = classify_error(exc)
                        if last_repair_decision is None:
                            last_repair_decision = await record_repair_decision(
                                classified=classified,
                                node_id=node_id,
                                tool_name=call["name"],
                                arguments=dict(call["arguments"]),
                                output=classified.to_dict(),
                            )
                        policy = metadata.get("retry_policy") or {}
                        max_attempts = max(1, min(int(policy.get("max_attempts") or 1), 5))
                        recovery = self.recovery.decide(
                            classified,
                            attempt=max_attempts,
                            max_attempts=max_attempts,
                            mutation_started=bool(metadata.get("mutating")),
                            rollback_available=bool(metadata.get("rollback_support")),
                        )
                        if (
                            recovery.action == "rollback_mutation"
                            and rollback_result is None
                            and isinstance(result, dict)
                        ):
                            rollback_result = await self.rollback_manager.rollback_if_available(
                                tool_name=call["name"],
                                result=result,
                                context=context,
                                execute=self.tools.execute,
                            )
                            if rollback_result is not None:
                                await record(
                                    "rollback.completed"
                                    if rollback_result.get("confirmed")
                                    else "rollback.failed",
                                    step=step,
                                    node_id=node_id,
                                    tool=call["name"],
                                    rollback=rollback_result,
                                )
                        if recovery.action == "refresh_snapshot" and self.snapshot_builder:
                            current_snapshot = self.snapshot_builder.build(
                                context,
                                plan=plan.to_dict(),
                                pending_approvals=(
                                    self.approval_manager.pending(run_id)
                                    if self.approval_manager
                                    else []
                                ),
                                memories=memories,
                            )
                            transcript.append(
                                {
                                    "role": "host",
                                    "type": "environment_snapshot",
                                    "content": current_snapshot.to_dict(),
                                }
                            )
                            await record(
                                "context.snapshot.created",
                                step=step,
                                snapshot_id=current_snapshot.snapshot_id,
                                snapshot_hash=current_snapshot.hash,
                                snapshot=current_snapshot.to_dict(),
                                reason="recovery_refresh",
                            )
                        transcript.append(
                            {
                                "role": "host",
                                "type": "recovery_directive",
                                "content": {
                                    "node_id": node_id,
                                    "tool": call["name"],
                                    "error": classified.to_dict(),
                                    "recovery": recovery.to_dict(),
                                    "rollback": rollback_result,
                                    "repair_ladder": {
                                        "level": int(last_repair_decision.level),
                                        "strategy": last_repair_decision.strategy,
                                        "identical_retry_blocked": last_repair_decision.identical_retry_blocked,
                                        "preserve_other_branches": last_repair_decision.preserve_other_branches,
                                    },
                                    "required_recovery": {
                                        "failed_node_id": node_id,
                                        "failed_call_id": call["id"],
                                        "acceptable_alternatives": _alternative_tools_for_failure(
                                            call["name"], tool_manifest
                                        ),
                                        "completion_blocked_until": "A distinct validated alternative call records recovery_for this failed node.",
                                    },
                                },
                            }
                        )
                        if node_id in plan.nodes:
                            plan.mark(node_id, "failed")
                            if self.plan_manager:
                                self.plan_manager.save_state(plan, run_id=run_id)
                        observation = {
                            "id": call["id"],
                            "name": call["name"],
                            "ok": False,
                            "error": classified.to_dict(),
                            "recovery": recovery.to_dict(),
                            "repair_ladder": {
                                "level": int(last_repair_decision.level),
                                "strategy": last_repair_decision.strategy,
                                "identical_retry_blocked": last_repair_decision.identical_retry_blocked,
                            },
                        }
                        await record(
                            "recovery.started",
                            step=step,
                            node_id=node_id,
                            recovery=recovery.to_dict(),
                            required_recovery={
                                "failed_node_id": node_id,
                                "failed_call_id": call["id"],
                                "acceptable_alternatives": _alternative_tools_for_failure(
                                    call["name"], tool_manifest
                                ),
                                "completion_blocked_until": (
                                    "A distinct validated alternative call records recovery_for this failed node."
                                ),
                            },
                        )
                        await record(
                            "tool.failed",
                            step=step,
                            node_id=node_id,
                            call_id=call["id"],
                            tool=call["name"],
                            error=observation["error"],
                        )
                        for skill_id in selected_skills:
                            await record(
                                "skill.failed",
                                step=step,
                                node_id=node_id,
                                call_id=call["id"],
                                skill_id=skill_id,
                                tool=call["name"],
                                error=observation["error"],
                            )
                        await record(
                            "step.failed",
                            step=step,
                            step_id=node_id,
                            node_id=node_id,
                            call_id=call["id"],
                            error_summary=classified.message,
                            error=observation["error"],
                        )
                    observations.append(observation)
                    trace.append(
                        {
                            "step": step,
                            "call_id": call["id"],
                            "tool": call["name"],
                            "arguments": call["arguments"],
                            "ok": observation["ok"],
                            "started_at": started_at,
                            "finished_at": _now(),
                            "result_summary": _result_summary(observation),
                            "node_id": node_id,
                            "validation": observation.get("validation"),
                            "recovery": observation.get("recovery"),
                            "recovery_for": observation.get("recovery_for"),
                            "error": observation.get("error"),
                            "failure_fingerprint": failure_fingerprints_by_node.get(node_id),
                            "result": _safe_arguments(observation.get("result")),
                        }
                    )
                    checkpoint = self._checkpoint(
                        session_id=session_id,
                        run_id=run_id,
                        sequence=sequence,
                        plan=plan,
                        transcript=transcript,
                        trace=trace,
                        context=context,
                    )
                    if checkpoint:
                        await record(
                            "checkpoint.created",
                            step=step,
                            checkpoint=_checkpoint_reference(checkpoint),
                        )
                if paused_approval:
                    final_turn = turn
                    break
                transcript.append({"role": "host", "type": "tool_results", "content": observations})
                final_turn = turn
                if _should_host_finalize_analysis_only_data_unavailable(
                    task_kind=task_kind,
                    objective=objective_text,
                    context=context,
                    plan=plan,
                    trace=trace,
                ):
                    # A verified ``data_blocked`` workspace is not decision
                    # evidence and cannot become a trading recommendation. It
                    # is, however, a complete answer to an explicitly
                    # analysis-only request that asks the Host to disclose
                    # unavailable data and stop. Without this terminal path,
                    # local models can repeat the same tools until the cost or
                    # step guard interrupts the Run.
                    host_finalized_analysis_data_unavailable = True
                    turn = {
                        **turn,
                        "state": "complete",
                        "summary": _verified_data_unavailable_analysis_summary(trace),
                        "tool_calls": [],
                        "decision": None,
                        "completion_evaluation": _host_completion_evaluation(plan, trace),
                    }
                    final_turn = turn
                    await record(
                        "completion.host_finalized",
                        step=step,
                        reason="verified_analysis_only_data_unavailable",
                    )
                elif _should_host_finalize_ui_task(
                    task_kind=task_kind,
                    plan=plan,
                    trace=trace,
                ):
                    # UI bridge acknowledgements are the Host authority for a
                    # bounded local interface request. Some OpenAI-compatible
                    # models keep restating a successful UI command instead of
                    # producing their final envelope, which used to spend the
                    # remaining turns and incorrectly end at a cost boundary.
                    # Once every planned executable UI node is validated, end
                    # the Run from the durable Host receipts rather than asking
                    # the model to repeat the same action.
                    turn = {
                        **turn,
                        "state": "complete",
                        "summary": _verified_ui_task_summary(trace),
                        "tool_calls": [],
                        "decision": None,
                        "completion_evaluation": _host_completion_evaluation(plan, trace),
                    }
                    final_turn = turn
                    await record(
                        "completion.host_finalized",
                        step=step,
                        reason="verified_ui_bridge_operation",
                    )
                else:
                    continue

            observation_requirement_met = _evidence_requirement_met(task_kind, trace)
            # Some OpenAI-compatible local models execute every requested tool and
            # then keep returning an empty ``continue`` turn, even after the host
            # has told them that the evidence requirement is satisfied.  Do not
            # waste the remaining turn budget (and do not label a successful run
            # as ``max_steps_reached``) in that situation.  This is deliberately
            # conservative: it only applies after a prior host completion prompt,
            # there are no new requested calls, every executable plan node has
            # host-validated evidence, and the model has supplied a user-facing
            # summary.  The completion evidence is constructed solely from the
            # host trace, never invented from model text.
            if _should_host_finalize_verified_paper_order(
                turn=turn,
                trace=trace,
                context=context,
                objective=objective_text,
                task_kind=task_kind,
            ):
                # The paper broker receipt is the authority for this bounded,
                # non-live operation.  Do not let a provider contradict that
                # receipt with a stale production-risk narrative, or consume
                # the remaining run budget trying to rewrite a summary.
                host_finalized_verified_paper_order = True
                turn = {
                    **turn,
                    "state": "complete",
                    "summary": _verified_paper_order_summary(trace),
                    "completion_evaluation": _host_completion_evaluation(plan, trace),
                }
                await record(
                    "completion.host_finalized",
                    step=step,
                    reason="verified_explicit_local_paper_order",
                )
            elif _should_host_finalize_from_validated_evidence(
                turn=turn,
                plan=plan,
                trace=trace,
                transcript=transcript,
                evidence_requirement_met=observation_requirement_met,
            ):
                turn = {
                    **turn,
                    "state": "complete",
                    "completion_evaluation": _host_completion_evaluation(plan, trace),
                }
                await record(
                    "completion.host_finalized",
                    step=step,
                    reason="validated_evidence_after_repeated_empty_continue",
                )
            if turn["state"] == "complete":
                if is_critic_run and successful_observations >= 3:
                    # The Critic's evidence IDs and criteria are Host-owned.
                    # Local models often describe the right challenge while
                    # emitting incomplete or stale criterion bindings. Bind
                    # every declared local criterion to the validated Critic
                    # trace before completion validation; model prose still
                    # has to pass the normal semantic/risk checks below.
                    turn = {
                        **turn,
                        "completion_evaluation": _host_completion_evaluation(plan, trace),
                    }
                    await record(
                        "critic.completion_evidence.bound",
                        step=step,
                        evidence_ids=(turn["completion_evaluation"] or {}).get(
                            "evidence_ids", []
                        ),
                    )
                if (
                    provider_protocol == "universal_v1"
                    and not (turn.get("completion_evaluation") or {}).get("criterion_results")
                ):
                    evidence_ids = list(
                        (turn.get("completion_evaluation") or {}).get("evidence_ids") or []
                    )
                    if not evidence_ids:
                        evidence_ids = [
                            str(item.get("call_id"))
                            for item in trace
                            if item.get("ok") is True
                            and str(item.get("call_id") or "").strip()
                        ]
                    turn = {
                        **turn,
                        "completion_evaluation": {
                            **(turn.get("completion_evaluation") or {}),
                            "criterion_results": [
                                {
                                    "criterion": criterion,
                                    "met": True,
                                    "evidence_ids": evidence_ids,
                                }
                                for criterion in plan.completion_criteria
                            ],
                        },
                    }
                known_evidence_ids = {
                    str(value)
                    for item in trace
                    if item.get("ok") is True
                    for value in (
                        item.get("call_id"),
                        item.get("node_id"),
                        (item.get("validation") or {}).get("evidence_hash"),
                    )
                    if value
                }
                known_evidence_ids.update(
                    str(memory["memory_id"])
                    for memory in memories
                    if memory.get("memory_id")
                )
                if current_snapshot is not None:
                    known_evidence_ids.update(
                        {current_snapshot.snapshot_id, current_snapshot.hash}
                    )
                known_evidence_ids.update(
                    node.node_id
                    for node in plan.nodes.values()
                    if node.status in {"completed", "skipped"}
                )
                evidence_catalog: dict[str, dict[str, Any]] = {}
                for item in trace:
                    if item.get("ok") is not True:
                        continue
                    for value in (
                        item.get("call_id"),
                        item.get("node_id"),
                        (item.get("validation") or {}).get("evidence_hash"),
                    ):
                        if value:
                            evidence_catalog[str(value)] = item
                completion = self.validator.validate_completion(
                    state=turn["state"],
                    objective=objective_text,
                    task_kind=task_kind,
                    final_summary=str(turn.get("summary") or ""),
                    has_pending_tool_calls=bool(turn["tool_calls"]),
                    plan=plan.to_dict(),
                    successful_observations=successful_observations,
                    evidence_required=(
                        requires_observation
                        and not host_finalized_analysis_data_unavailable
                    ),
                    completion_evaluation=turn.get("completion_evaluation"),
                    known_evidence_ids=known_evidence_ids,
                    evidence_catalog=evidence_catalog,
                    failure_recovery_coverage=_failure_recovery_coverage(plan, trace),
                    decision=turn.get("decision"),
                )
                if (
                    not completion.passed
                    and _should_host_finalize_analysis_only_after_numeric_rejection(
                        task_kind=task_kind,
                        turn=turn,
                        plan=plan,
                        trace=trace,
                        completion_validation=completion.to_dict(),
                    )
                ):
                    # A validated analysis-only Run must not turn into a
                    # budget-exhaustion loop merely because a provider adds
                    # one ungrounded number to an otherwise complete answer.
                    # Keep the numeric-grounding rejection intact, but replace
                    # only that unsafe prose with a bounded Host summary.  The
                    # second validation below remains the authority for
                    # completion; no provider text or inferred trade advice is
                    # promoted to the user.
                    turn = {
                        **turn,
                        "state": "complete",
                        "summary": _verified_analysis_only_summary(trace),
                        "tool_calls": [],
                        "decision": None,
                        "completion_evaluation": _host_completion_evaluation(plan, trace),
                    }
                    completion = self.validator.validate_completion(
                        state=turn["state"],
                        objective=objective_text,
                        task_kind=task_kind,
                        final_summary=str(turn.get("summary") or ""),
                        has_pending_tool_calls=False,
                        plan=plan.to_dict(),
                        successful_observations=successful_observations,
                        evidence_required=(
                            requires_observation
                            and not host_finalized_analysis_data_unavailable
                        ),
                        completion_evaluation=turn.get("completion_evaluation"),
                        known_evidence_ids=known_evidence_ids,
                        evidence_catalog=evidence_catalog,
                        failure_recovery_coverage=_failure_recovery_coverage(plan, trace),
                        decision=None,
                    )
                    await record(
                        "completion.host_finalized",
                        step=step,
                        reason="validated_analysis_only_evidence_after_numeric_rejection",
                    )
                if completion.passed:
                    await report_running_reasoning_nodes(
                        turn_step=step,
                        turn_summary=str(turn.get("summary") or ""),
                        remaining_gaps=[
                            str(item)
                            for item in (
                                (turn.get("completion_evaluation") or {}).get(
                                    "remaining_gaps"
                                )
                                or []
                            )
                            if str(item).strip()
                        ],
                    )
                    _complete_finalize_nodes(plan)
                    if self.plan_manager:
                        self.plan_manager.save_state(plan, run_id=run_id)
                completion_validation = completion.to_dict()
                await record(
                    "validation.passed" if completion.passed else "validation.failed",
                    step=step,
                    validator="completion_evaluator",
                    validation=completion_validation,
                )
                if not completion.passed and turn["state"] == "complete":
                    failed_checks = sorted(
                        str(item.get("name") or "")
                        for item in completion_validation.get("checks") or []
                        if isinstance(item, dict) and item.get("passed") is False
                    )
                    fingerprint_key = json.dumps(failed_checks, ensure_ascii=False)
                    completion_output_failures[fingerprint_key] = (
                        completion_output_failures.get(fingerprint_key, 0) + 1
                    )
                    failure_count = completion_output_failures[fingerprint_key]
                    context.state["completion_output_failures"] = dict(
                        completion_output_failures
                    )
                    checkpoint = self._checkpoint(
                        session_id=session_id,
                        run_id=run_id,
                        sequence=sequence,
                        plan=plan,
                        transcript=transcript,
                        trace=trace,
                        context=context,
                    )
                    if checkpoint:
                        await record(
                            "checkpoint.created",
                            step=step,
                            checkpoint=_checkpoint_reference(checkpoint),
                            reason="completion_validation_failure",
                        )
                    if failure_count >= 3:
                        receipt = ErrorReceipt(
                            category="invalid_final_answer",
                            component="provider.final_synthesis",
                            location="$.summary",
                            expected=(
                                "A grounded final answer that covers the requested scope "
                                "without unsupported numeric claims."
                            ),
                            actual=", ".join(failed_checks) or "completion validation failed",
                            retryable=False,
                            same_error_count=failure_count,
                            completed_work_preserved=True,
                        )
                        fingerprint = FailureFingerprint.from_receipt(
                            receipt,
                            provider=selected_driver,
                            model=model_id,
                            tool="provider.final_synthesis",
                            schema_version="open_stock_ai.agent_decision.v2",
                        )
                        completion_output_repair_exhausted = True
                        await record(
                            "error.receipt.created",
                            step=step,
                            error_receipt=receipt.to_dict(),
                            failure_fingerprint=fingerprint.to_dict(),
                            completed_work_preserved=True,
                        )
                        await record(
                            "repair.autonomous_strategies_exhausted",
                            step=step,
                            strategy="provider_final_synthesis_repair",
                            failed_checks=failed_checks,
                            same_error_count=failure_count,
                            summary=(
                                "Provider repeatedly returned an invalid final synthesis; "
                                "completed evidence is preserved and an L8 decision is required."
                            ),
                        )
                        final_turn = {
                            **turn,
                            "state": "continue",
                            "tool_calls": [],
                            "decision": None,
                            "completion_evaluation": {
                                "criteria_met": False,
                                "criterion_results": [],
                                "evidence_ids": [],
                                "remaining_gaps": [
                                    "provider_final_synthesis_repair_exhausted"
                                ],
                            },
                        }
                        break
            if (
                turn["state"] == "complete"
                # A routine, explicitly-authorized paper order is complete
                # from its verified preview and execution receipt.  Its
                # accompanying market analysis may legitimately be marked
                # data_blocked, so requiring a second substantive market.*
                # observation here would re-enter the model loop and submit
                # the same simulated order repeatedly.
                and (
                    observation_requirement_met
                    or host_finalized_verified_paper_order
                    or host_finalized_analysis_data_unavailable
                )
                and completion_validation
                and completion_validation["passed"]
            ):
                final_turn = turn
                break

            feedback = _evidence_feedback(
                task_kind,
                trace,
                self.tools.manifest(),
                objective=objective_text,
                completion_validation=completion_validation,
            )
            transcript.append({"role": "host", "type": "policy_feedback", "content": feedback})
            await record(
                "policy.feedback",
                step=step,
                summary=(
                    str(feedback["error"])
                    if requires_observation
                    else "一般問答不必套用股票決策格式，請直接回答。"
                ),
            )
            final_turn = turn
        if paused_approval:
            result = {
                "schema_version": "open_stock_ai.agent_run.v2",
                "run_id": run_id,
                "session_id": session_id,
                "status": "waiting_approval",
                "driver": selected_driver,
                "objective": objective_text,
                "summary": "等待使用者批准後從安全 checkpoint 繼續。",
                "decision": None,
                "plan": plan.to_dict(),
                "pending_approval": paused_approval,
                "tool_trace": trace,
                "activity": activity,
                "live_execution_count": 0,
            }
            await record(
                "run.waiting_approval",
                run_id=run_id,
                session_id=session_id,
                approval=paused_approval,
            )
            result["activity"] = activity
            result["model_invocations"] = _model_invocation_receipts(
                activity,
                selected_driver,
                model_id,
            )
            return result

        if paused_interaction:
            waiting_state = str(paused_interaction["waiting_state"])
            result = {
                "schema_version": "open_stock_ai.agent_run.v2",
                "run_id": run_id,
                "session_id": session_id,
                "status": waiting_state,
                "driver": selected_driver,
                "objective": objective_text,
                "summary": str(final_turn.get("summary") or paused_interaction["prompt"]),
                "decision": None,
                "plan": plan.to_dict(),
                "pending_interaction": paused_interaction,
                "tool_trace": trace,
                "activity": activity,
                "live_execution_count": 0,
            }
            await record(
                f"run.{waiting_state}",
                run_id=run_id,
                session_id=session_id,
                interaction=paused_interaction,
            )
            result["activity"] = activity
            result["model_invocations"] = _model_invocation_receipts(
                activity,
                selected_driver,
                model_id,
            )
            return result

        final_turn = final_turn or {
            "state": "complete",
            "summary": "Agent produced no decision.",
            "tool_calls": [],
            "decision": None,
        }
        observation_requirement_met = _evidence_requirement_met(task_kind, trace)
        completed = (
            final_turn.get("state") == "complete"
            and (
                observation_requirement_met
                or host_finalized_verified_paper_order
                or host_finalized_analysis_data_unavailable
            )
            and not final_turn.get("tool_calls")
            and bool(completion_validation and completion_validation.get("passed"))
            and not _unresolved_failure_nodes(plan, trace)
        )
        if not completed:
            incomplete_reason = (
                "Host cost budget or execution guard blocked progress before the completion criteria were satisfied."
                if cost_budget_exhausted
                else "Host token budget exhausted before the completion criteria were satisfied."
                if budget_exhausted
                else "Step budget exhausted before the completion criteria were satisfied."
            )
            for node in plan.nodes.values():
                if node.status != "running":
                    continue
                plan.mark(node.node_id, "blocked")
                node.error_summary = incomplete_reason
                node.metadata = {
                    **node.metadata,
                    "blocked_reason": "max_steps_reached",
                    "remaining_gaps": list(
                        (final_turn.get("completion_evaluation") or {}).get(
                            "remaining_gaps"
                        )
                        or []
                    ),
                }
                await record(
                    "step.blocked",
                    step=bounded_steps,
                    step_id=node.node_id,
                    node_id=node.node_id,
                    node_type=node.node_type,
                    title=node.title,
                    blocked_reason="max_steps_reached",
                    error_summary=incomplete_reason,
                    remaining_gaps=node.metadata["remaining_gaps"],
                )
        recovered_failure_nodes = {
            str(link.get("failed_node_id") or "")
            for item in trace
            if isinstance(item, dict) and item.get("ok") is True
            for link in (item.get("recovery_for") or [])
            if isinstance(link, dict)
        }
        pending_recovery_nodes = [
            str(item.get("node_id") or item.get("call_id") or item.get("tool") or "")
            for item in trace
            if isinstance(item, dict)
            and item.get("ok") is False
            and isinstance(item.get("recovery"), dict)
            and str(item.get("node_id") or "") not in recovered_failure_nodes
        ]
        pending_recovery_nodes = list(dict.fromkeys([
            *pending_recovery_nodes,
            *_unresolved_failure_nodes(plan, trace),
        ]))
        objective_contract_check = _objective_contract_check(
            objective=objective_text,
            task_kind=task_kind,
            trace=trace,
            decision=final_turn.get("decision"),
            completion_validation=completion_validation,
        )
        goal_completion_gaps = [
            str(item)
            for item in objective_contract_check.get("missing_requirements") or []
            if str(item).strip()
        ]
        resource_boundary_gaps = {
            "cost_budget_exhausted",
            "token_budget_exhausted",
            "execution_guard_blocked",
        }
        resource_boundary = bool(
            resource_boundary_gaps
            & {
                str(item)
                for item in (final_turn.get("completion_evaluation") or {}).get(
                    "remaining_gaps"
                )
                or []
            }
        )
        # A Host resource boundary explains why this Run stopped. It is not
        # an objective obligation that a continuation of the same Run can
        # satisfy; otherwise the Dock offers Start/Continue into the same
        # guarded path and creates a visible restart loop.
        if resource_boundary:
            goal_completion_gaps = [
                gap for gap in goal_completion_gaps if gap not in resource_boundary_gaps
            ]
        executions = [item for item in trace if item["tool"] == "paper.submit_order" and item["ok"]]
        public_summary = (
            str(final_turn.get("summary") or "").strip()
            if completed
            else _incomplete_run_summary(
                successful_observations=successful_observations,
                completion_validation=(
                    {
                        **(completion_validation or {}),
                        "remaining_gaps": list(
                            dict.fromkeys(
                                [
                                    *(
                                        (completion_validation or {}).get("remaining_gaps")
                                        or []
                                    ),
                                    *goal_completion_gaps,
                                ]
                            )
                        ),
                    }
                    if completion_validation
                    else {
                        "remaining_gaps": list(
                            dict.fromkeys(
                                [
                                    *((final_turn.get("completion_evaluation") or {}).get("remaining_gaps") or []),
                                    *goal_completion_gaps,
                                ]
                            )
                        )
                    }
                ),
            )
        )
        partial_completion = (
            not completed
            and successful_observations > 0
            and (
                any(item.get("ok") is False for item in trace)
                or bool(goal_completion_gaps)
            )
        )
        terminal_status = "completed" if completed else (
            "partially_completed" if partial_completion else "max_steps_reached"
        )
        recovery_pending = (
            not completed
            and not resource_boundary
            and not completion_output_repair_exhausted
            and not recovery_only_exhausted
            and bool(pending_recovery_nodes or goal_completion_gaps)
        )
        result = {
            "schema_version": "open_stock_ai.agent_run.v2",
            "run_id": run_id,
            "session_id": session_id,
            "parent_run_id": parent_run_id,
            "status": terminal_status,
            "driver": selected_driver,
            "execution_mode": "durable_plan_graph",
            "forest_execution": _forest_execution_snapshot(context),
            "native_codex_delegation_count": 0,
            "autonomy": autonomy,
            "symbols": list(normalized_symbols),
            "objective": objective.strip(),
            "task_kind": task_kind,
            # Only Host-validated model output is allowed to become the public
            # answer.  A max-step terminal state may contain a fluent but
            # unsupported model draft; exposing that text as the result would
            # turn a correctly rejected hallucination into misleading UI.
            "summary": public_summary,
            "decision": final_turn.get("decision") if completed else None,
            "structured_result": final_turn.get("structured_result") if completed else None,
            "interaction_proposals": final_turn.get("interaction_proposals") if completed else [],
            "provider_protocol": provider_protocol,
            "provider_conformance": provider_conformance,
            "successful_observation_count": successful_observations,
            "tool_trace": trace,
            # This is a Host-owned bridge between the momentary durable
            # incomplete checkpoint and the next automatic P40 pass.  UI/SSE
            # consumers must not interpret that narrow gap as the end of the
            # user's task.
            "recovery_pending": recovery_pending,
            "pending_recovery_node_ids": pending_recovery_nodes,
            "goal_completion_gaps": goal_completion_gaps,
            "recovery_state": (
                "resource_boundary_reached"
                if resource_boundary
                else "objective_completion_gap_pending"
                if goal_completion_gaps and not completion_output_repair_exhausted and not recovery_only_exhausted
                else (
                    "completion_output_repair_exhausted"
                    if completion_output_repair_exhausted
                    else "autonomous_strategies_exhausted"
                    if recovery_only_exhausted
                    else None
                )
            ),
            "plan": plan.to_dict(),
            "completion_validation": completion_validation,
            # A resource boundary is a Host receipt, not a form for users to
            # tell the system how to recover.  Keep the checkpoint and visible
            # limitation in this result; never prefill a new instruction into
            # the composer or invite a duplicate execution.
            "next_goal_draft": None,
            "token_budget": dict(context.state.get("token_budget") or {}),
            "execution_guard": dict(context.state.get("execution_guard") or {
                "cost_budgets": cost_budget_manager.snapshot(),
                "runaway": runaway_guard.snapshot(),
            }),
            "environment_snapshot_hash": current_snapshot.hash if current_snapshot else None,
            "activity": activity,
            "model_invocations": _model_invocation_receipts(
                activity,
                selected_driver,
                model_id,
            ),
            "paper_execution_count": len(executions),
            "live_execution_count": 0,
            "boundaries": {
                "live_trading": "disabled",
                "paper_execution_enabled": context.allow_paper_orders,
                "paper_preview_required": True,
                "project_execution_enabled": context.allow_project_actions,
            },
        }
        await record(
            "assistant.message.delta",
            summary=str(result.get("summary") or ""),
            content=str(result.get("summary") or ""),
        )
        await record(
            "assistant.message.completed",
            summary=str(result.get("summary") or ""),
            content=str(result.get("summary") or ""),
            status=result["status"],
            completion_validation=completion_validation,
        )
        await record(
            "run.completed" if completed else (
                "run.partially_completed" if partial_completion else "run.max_steps_reached"
            ),
            run_id=run_id,
            status=result["status"],
            summary=result["summary"],
            decision=result["decision"],
            successful_observation_count=successful_observations,
            paper_execution_count=len(executions),
            live_execution_count=0,
            recovery_pending=recovery_pending,
            pending_recovery_node_ids=pending_recovery_nodes,
        )
        if self.plan_manager:
            self.plan_manager.save_state(
                plan,
                run_id=run_id,
                status="completed" if completed else "incomplete",
            )
        if completed and self.memory_manager and self.background_work:
            self.background_work.submit(
                self.memory_manager.remember_run,
                session_id=session_id,
                run_id=run_id,
                objective=objective_text,
                summary=str(result.get("summary") or ""),
                evidence={
                    "successful_observation_count": successful_observations,
                    "completion_validation": completion_validation,
                },
            )
        result["activity"] = activity
        return result

    def _apply_plan_patch(
        self,
        plan: PlanGraph,
        *,
        run_id: str,
        patch: dict[str, Any],
        reason: str,
    ) -> PlanGraph:
        if self.plan_manager:
            return self.plan_manager.revise(
                plan,
                run_id=run_id,
                patch={"operations": patch.get("operations") or []},
                reason_summary=reason,
            )
        return plan.apply_patch({"operations": patch.get("operations") or []})

    def _checkpoint(
        self,
        *,
        session_id: str,
        run_id: str,
        sequence: int,
        plan: PlanGraph,
        transcript: list[dict[str, Any]],
        trace: list[dict[str, Any]],
        context: AgentRunContext,
        rollback: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.checkpoint_manager:
            return None
        checkpoint_state = dict(context.state)
        checkpoint_state.pop("forest_execution_authority", None)
        checkpoint_state["forest_execution"] = _forest_execution_snapshot(context)
        return self.checkpoint_manager.save(
            session_id=session_id,
            run_id=run_id,
            sequence=sequence,
            plan=plan,
            transcript=transcript,
            trace=trace,
            context_state=checkpoint_state,
            rollback=rollback,
        )

    async def _execute_parallel_tools_with_forest(
        self,
        *,
        parallel_candidates: list[tuple[dict[str, Any], dict[str, Any], str, Any]],
        context: AgentRunContext,
    ) -> list[Any]:
        """Execute a read-only batch through the Run's authoritative Forest.

        PlanGraph still supplies Host policy and public node identifiers, but
        scheduling, local-step lifecycle and capability receipts are owned by
        the one Run-scoped ``RuntimeForestAuthority``.  This deliberately
        avoids a fresh, throw-away Forest for every batch.
        """
        authority = _forest_execution_authority(context)
        candidate_by_call = {str(item[0]["id"]): item for item in parallel_candidates}
        errors: dict[str, BaseException] = {}

        async def run_step(capability: dict[str, Any]) -> StepExecutionResult:
            call_id = str(capability["call_id"])
            call, metadata, node_id, _policy = candidate_by_call[call_id]
            try:
                execution = await self._execute_tool(
                    call=call,
                    metadata=metadata,
                    context=_parallel_tool_context(context, node_id),
                    step_id=node_id,
                )
            except BaseException as exc:
                errors[call_id] = exc
                raise
            return StepExecutionResult(
                payload={"execution": execution, "call_id": call_id, "node_id": node_id},
                conclusion=f"Executed {call['name']}",
            )

        execution = await authority.execute_capabilities(
            [
                {
                    "call_id": str(call["id"]),
                    "node_id": node_id,
                    "tool_name": str(call["name"]),
                }
                for call, _metadata, node_id, _policy in parallel_candidates
            ],
            runner=run_step,
            parallel=True,
        )
        # Reading the completed LocalStep payload, rather than the provider's
        # order, is the Forest-to-PlanGraph handoff.  This is also what makes
        # the adapter recoverable if a sibling fails.
        results: list[Any] = []
        for call, _metadata, _node_id, _policy in parallel_candidates:
            call_id = str(call["id"])
            if call_id in errors:
                results.append(errors[call_id])
                continue
            branch = authority.branch_for_call(call_id, execution)
            step = next(iter(branch.local_plan.steps.values()))
            tool_execution = (step.result or {}).get("execution")
            if tool_execution is None:
                results.append(RuntimeError(
                    f"Task Forest completed without an execution receipt for {call_id}; "
                    f"status={execution.report.status.value}"
                ))
            else:
                results.append(tool_execution)
        return results

    async def _execute_ordered_tool_with_forest(
        self,
        *,
        call: dict[str, Any],
        metadata: dict[str, Any],
        context: AgentRunContext,
        step_id: str,
    ) -> tuple[dict[str, Any], str | None]:
        """Run one ordered capability through the Run's same Forest engine."""
        authority = _forest_execution_authority(context)
        failure: BaseException | None = None

        async def run_step(_: dict[str, Any]) -> StepExecutionResult:
            nonlocal failure
            try:
                execution = await self._execute_tool(
                    call=call,
                    metadata=metadata,
                    context=_parallel_tool_context(context, step_id),
                    step_id=step_id,
                )
            except BaseException as exc:
                failure = exc
                raise
            return StepExecutionResult(
                payload={"execution": execution},
                conclusion=f"Executed {call['name']}",
            )

        execution = await authority.execute_capabilities(
            [{"call_id": str(call["id"]), "node_id": step_id, "tool_name": str(call["name"])}],
            runner=run_step,
            parallel=False,
        )
        if failure is not None:
            raise failure
        branch = authority.branch_for_call(str(call["id"]), execution)
        step = next(iter(branch.local_plan.steps.values()))
        tool_execution = (step.result or {}).get("execution")
        if tool_execution is None:
            raise RuntimeError(
                f"Task Forest completed without a receipt for {call['id']}; status={execution.report.status.value}"
            )
        return tool_execution

    async def _execute_tool(
        self,
        *,
        call: dict[str, Any],
        metadata: dict[str, Any],
        context: AgentRunContext,
        step_id: str,
    ) -> tuple[dict[str, Any], str | None]:
        if self.worker_supervisor is None:
            return await self.tools.execute(call["name"], call["arguments"], context), None

        async def handler(_: Any) -> dict[str, Any]:
            return await self.tools.execute(call["name"], call["arguments"], context)

        worker_id, result = await self.worker_supervisor.execute(
            run_id=context.run_id,
            step_id=step_id,
            worker_type=_worker_type(metadata),
            timeout_seconds=int(metadata.get("timeout_seconds") or 120),
            payload={
                "tool": call["name"],
                "arguments": call["arguments"],
                "audit_arguments": _safe_arguments(call["arguments"]),
                "context": {
                    "run_id": context.run_id,
                    "session_id": context.session_id,
                    "parent_run_id": context.parent_run_id,
                    "autonomy": context.autonomy,
                    "symbols": list(context.symbols),
                    "allow_paper_orders": context.allow_paper_orders,
                    "allow_project_actions": context.allow_project_actions,
                    "allow_external_actions": context.allow_external_actions,
                    "previewed_orders": sorted(context.previewed_orders),
                    "state": {
                        key: value
                        for key, value in context.state.items()
                        if key != "record_event"
                    },
                },
            },
            handler=handler,
            event_sink=(
                context.state.get("record_event")
                if callable(context.state.get("record_event"))
                else None
            ),
        )
        return result, worker_id


def _parallel_read_only_tool(metadata: dict[str, Any]) -> bool:
    """Return whether a capability can share a same-turn read-only batch.

    The Host derives this from the registered capability contract rather than
    trusting a model-authored parallel hint. Any declared mutation, side
    effect, execution permission, rollback path, or non-read-only risk keeps
    the call on the ordered serial path.
    """

    return bool(metadata) and all(
        (
            str(metadata.get("risk_class") or "read_only") == "read_only",
            not bool(metadata.get("mutating")),
            not bool(metadata.get("destructive")),
            not bool(metadata.get("side_effects")),
            not bool(metadata.get("required_permissions")),
            not bool(metadata.get("rollback_support")),
            not bool(metadata.get("requires_paper_execution")),
            not bool(metadata.get("requires_project_execution")),
            not bool(metadata.get("requires_external_execution")),
            not bool(metadata.get("requires_full_execution")),
        )
    )


def _forest_execution_authority(context: AgentRunContext) -> RuntimeForestAuthority:
    """Return the one scheduler-owned Forest for this Agent Run."""

    authority = context.state.get("forest_execution_authority")
    restored_snapshot = context.state.get("forest_execution")
    if (
        isinstance(restored_snapshot, dict)
        and isinstance(authority, RuntimeForestAuthority)
        and authority.snapshot().get("execution_count") == 0
    ):
        identity = _execution_forest_identity(context)
        authority = RuntimeForestAuthority.from_snapshot(
            restored_snapshot,
            session_id=context.session_id,
            run_id=context.run_id,
            objective=f"Agent Run {context.run_id}",
            **identity,
        )
        context.state["forest_execution_authority"] = authority
    if isinstance(authority, RuntimeForestAuthority):
        return authority
    authority = _new_forest_execution_authority(
        context,
        objective=f"Agent Run {context.run_id}",
    )
    context.state["forest_execution_authority"] = authority
    return authority


def _execution_forest_identity(context: AgentRunContext) -> dict[str, str | None]:
    """Return the durable forest identity reserved before provider dispatch."""

    identity = context.state.get("execution_forest_identity")
    payload = dict(identity) if isinstance(identity, dict) else {}
    return {
        "forest_id": str(payload.get("forest_id") or "").strip() or None,
        "root_branch_id": str(payload.get("root_branch_id") or payload.get("branch_id") or "").strip() or None,
        "objective_version_id": str(payload.get("objective_id") or "").strip() or None,
    }


def _new_forest_execution_authority(
    context: AgentRunContext,
    *,
    objective: str,
) -> RuntimeForestAuthority:
    return RuntimeForestAuthority(
        session_id=context.session_id,
        run_id=context.run_id,
        objective=objective,
        **_execution_forest_identity(context),
    )


def _forest_execution_snapshot(context: AgentRunContext) -> dict[str, Any] | None:
    authority = context.state.get("forest_execution_authority")
    return authority.snapshot() if isinstance(authority, RuntimeForestAuthority) else None


def _parallel_tool_context(context: AgentRunContext, node_id: str) -> AgentRunContext:
    """Give each parallel call isolated transient state and shared run identity."""

    return AgentRunContext(
        run_id=context.run_id,
        autonomy=context.autonomy,
        symbols=context.symbols,
        driver_id=context.driver_id,
        session_id=context.session_id,
        parent_run_id=context.parent_run_id,
        allow_paper_orders=context.allow_paper_orders,
        allow_project_actions=context.allow_project_actions,
        allow_external_actions=context.allow_external_actions,
        previewed_orders=set(context.previewed_orders),
        state={**context.state, "current_plan_node_id": node_id},
    )


async def _repair_provider_tool_call(
    *,
    provider: Any,
    provider_id: str,
    model_id: str,
    run_id: str,
    call: dict[str, Any],
    metadata: dict[str, Any],
    receipt: ErrorReceipt,
    budget_limit: int,
    event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
) -> Any:
    """Repair malformed arguments through the configured provider's bounded role."""

    arguments = dict(call.get("arguments") or {})
    raw_arguments = str(arguments.get("_raw_arguments") or "")
    if not raw_arguments:
        # Parsed JSON may still violate the tool contract (for example an
        # undocumented key or an out-of-range value).  Preserve that exact
        # object for the bounded repair role rather than allowing it into the
        # durable plan and repeatedly failing compilation.
        raw_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    document = {"arguments": raw_arguments}
    expected_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["arguments"],
        "properties": {
            "arguments": dict(metadata.get("input_schema") or {"type": "object"})
        },
    }

    async def execute_provider(request: ProviderNeutralRequest) -> Any:
        output_contract = {
            key: value
            for key, value in request.output_contract.items()
            if key != "x-target-document-schema"
        }
        prompt = json.dumps(
            {
                "task": request.task,
                "context": request.context,
                "output_contract": output_contract,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return await provider.generate_structured(
            run_id,
            prompt,
            output_contract,
            event_sink=event_sink,
        )

    capabilities = provider.capabilities()
    configured_model = str(capabilities.get("model") or model_id or provider_id)
    profile = ModelProfile(
        model_id=configured_model,
        provider=provider_id,
        mode=ProviderMode.STRUCTURED_JSON,
        roles=frozenset({ModelRole.REPAIR}),
        context_window=max(8_192, min(131_072, int(budget_limit) * 8)),
        cost_tier=0 if provider_id == "openai-compatible" else 1,
        reliability=0.9,
        local=provider_id == "openai-compatible",
    )
    pipeline = HostModelRepairPipeline(
        HostModelExecutor(
            ModelRouter([profile]),
            {configured_model: execute_provider},
        )
    )
    return await pipeline.repair(
        document,
        receipt=receipt,
        allowed_scopes=("$.arguments",),
        expected_schema=expected_schema,
        budget=HostModelBudget(
            scope_id=f"repair:{run_id}:{call['id']}",
            limit=max(512, int(budget_limit)),
            max_cost_tier=1,
        ),
        reserve_output_tokens=max(128, min(1_024, int(budget_limit) // 4)),
    )


def _normalize_turn(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Agent driver must return a JSON object")
    state = str(value.get("state") or "").strip()
    if state not in {"continue", "complete", "waiting_user_input", "waiting_decision"}:
        raise ValueError(
            "Agent decision state must be continue, complete, waiting_user_input or waiting_decision"
        )
    calls = value.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) > 8:
        raise ValueError("Agent tool_calls must be an array with at most 8 items")
    normalized_calls: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            raise ValueError("Each Agent tool call must be an object")
        name = _canonical_provider_tool_name(str(call.get("name") or "").strip())
        arguments = call.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                # Smaller OpenAI-compatible models occasionally produce one
                # malformed arguments string after several valid tool turns.
                # Keep the turn recoverable: the tool schema will reject this
                # sentinel as a normal tool failure, and that verified failure
                # is returned to the model so it can correct the next turn.
                arguments = {
                    "_agent_argument_error": (
                        "工具參數不是有效的 JSON 物件；請根據工具 schema 修正後重試。"
                    ),
                    "_raw_arguments": arguments[:2000],
                }
        name, arguments = _unwrap_openai_compatible_tool_envelope(name, arguments)
        if not name or not isinstance(arguments, dict):
            raise ValueError("Agent tool call requires a name and JSON object arguments")
        normalized_calls.append(
            {
                "id": str(call.get("id") or f"call-{index + 1}"),
                "name": name,
                "arguments": arguments,
            }
        )
    decision = value.get("decision")
    if decision is not None and not isinstance(decision, dict):
        raise ValueError("Agent decision must be an object or null")
    if isinstance(decision, dict) and decision.get("confidence") is not None:
        try:
            confidence = float(decision["confidence"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Agent decision confidence must be numeric") from exc
        if 0 < confidence <= 1:
            confidence *= 100
        decision = {
            **decision,
            "confidence": max(0.0, min(confidence, 100.0)),
            "confidence_type": "model_self_reported",
        }
    plan_patch = value.get("plan_patch")
    if plan_patch is not None:
        if not isinstance(plan_patch, dict):
            raise ValueError("Agent plan_patch must be an object or null")
        operations = plan_patch.get("operations")
        if operations is None:
            operations = plan_patch.get("operations_json")
        operations = _normalize_plan_patch_operations(operations)
        if not isinstance(operations, list):
            raise ValueError("Agent plan_patch operations must be an array")
        plan_patch = {
            "reason_summary": str(plan_patch.get("reason_summary") or "Model revised the plan."),
            "operations": operations,
        }
    completion_evaluation = value.get("completion_evaluation")
    if not isinstance(completion_evaluation, dict):
        completion_evaluation = {
            "criteria_met": state == "complete" and not normalized_calls,
            "criterion_results": [],
            "evidence_ids": [],
            "remaining_gaps": [],
        }
    else:
        completion_evaluation = {
            "criteria_met": completion_evaluation.get("criteria_met") is True,
            "criterion_results": [
                {
                    "criterion": str(item.get("criterion") or ""),
                    "met": item.get("met") is True,
                    "evidence_ids": [
                        str(value)
                        for value in item.get("evidence_ids") or []
                        if str(value).strip()
                    ],
                }
                for item in completion_evaluation.get("criterion_results") or []
                if isinstance(item, dict)
            ],
            "evidence_ids": [
                str(item)
                for item in completion_evaluation.get("evidence_ids") or []
                if str(item).strip()
            ],
            "remaining_gaps": [
                str(item)
                for item in completion_evaluation.get("remaining_gaps") or []
                if str(item).strip()
            ],
        }
    reflection_value = value.get("reflection")
    if isinstance(reflection_value, dict):
        reflection = {
            "preferred_option": str(reflection_value.get("preferred_option") or "").strip(),
            "alternatives": [
                str(item).strip()
                for item in reflection_value.get("alternatives") or []
                if str(item).strip()
            ][:8],
            "should_ask_user": reflection_value.get("should_ask_user") is True,
            "reason_to_ask": str(reflection_value.get("reason_to_ask") or "").strip(),
            "unknowns": [str(item).strip() for item in reflection_value.get("unknowns") or [] if str(item).strip()][:20],
            "important_risks": [
                str(item).strip()
                for item in reflection_value.get("important_risks") or []
                if str(item).strip()
            ][:20],
            "evidence_ids": [
                str(item)
                for item in reflection_value.get("evidence_ids") or []
                if str(item).strip()
            ][:50],
        }
    else:
        reflection = None
    interaction = value.get("interaction")
    if state in {"waiting_user_input", "waiting_decision"}:
        if not isinstance(interaction, dict):
            raise ValueError("A waiting state requires an interaction object")
        options = [dict(item) for item in interaction.get("options") or [] if isinstance(item, dict)]
        option_ids = {str(item.get("option_id") or "") for item in options}
        preferred = str(interaction.get("preferred_option") or "")
        if len(options) < 2 or not preferred or preferred not in option_ids:
            raise ValueError("Interaction requires two options and a valid preferred option")
        interaction = {
            "prompt": str(interaction.get("prompt") or "").strip(),
            "agent_view": str(interaction.get("agent_view") or "").strip(),
            "preferred_option": preferred,
            "options": options,
            "unknowns": [str(item) for item in interaction.get("unknowns") or []],
            "important_risks": [str(item) for item in interaction.get("important_risks") or []],
        }
        if not interaction["prompt"] or not interaction["agent_view"]:
            raise ValueError("Interaction prompt and agent view are required")
    else:
        interaction = None
    interaction_proposals = [
        {
            "proposal_id": str(item.get("proposal_id") or f"proposal-{index + 1}"),
            "title": str(item.get("title") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "action": str(item.get("action") or "ask").strip().casefold(),
            "arguments": dict(item.get("arguments") or {}),
        }
        for index, item in enumerate(value.get("interaction_proposals") or [])
        if isinstance(item, dict)
        and str(item.get("title") or "").strip()
        and str(item.get("reason") or "").strip()
        and str(item.get("action") or "ask").strip().casefold()
        in {"ask", "draft_automation", "create_artifact", "follow_up"}
    ][:3]
    return {
        "state": state,
        "summary": str(value.get("summary") or ""),
        "plan_patch": plan_patch,
        "tool_calls": normalized_calls,
        "decision": decision,
        "completion_evaluation": completion_evaluation,
        "reflection": reflection,
        "routing_patch": value.get("routing_patch") if isinstance(value.get("routing_patch"), dict) else None,
        "interaction": interaction,
        "interaction_proposals": interaction_proposals,
        "structured_result": (
            value.get("structured_result")
            if isinstance(value.get("structured_result"), dict)
            else value.get("result")
            if isinstance(value.get("result"), dict)
            else None
        ),
    }


def _canonical_provider_tool_name(name: str) -> str:
    """Repair a narrow, observed provider namespace alias.

    Some OpenAI-compatible local models preserve the generic protocol word
    ``tool`` when copying the disclosed ``agent.run_subtasks`` capability.
    Persisting that literal alias creates one unknown Plan node which then
    prevents every later valid market node from compiling.  Canonicalize only
    this exact semantic identity before the call receives a Plan node; unknown
    names remain unknown and therefore cannot gain execution authority.
    """

    return {
        "tool.run_subtasks": "agent.run_subtasks",
    }.get(name, name)


def _unwrap_openai_compatible_tool_envelope(
    name: str,
    arguments: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Normalize the observed generic ``assistant`` function envelope.

    Some OpenAI-compatible local models serialize a function call as
    ``assistant({tool: '<actual name>', arguments: '<JSON>'})`` instead of
    emitting the actual function name in the protocol's ``name`` field.  The
    outer label has no execution authority.  Unwrap only that exact shape and
    leave every other unknown name intact for the Host capability boundary.
    """
    if name != "assistant":
        return name, arguments
    nested_name = arguments.get("tool")
    nested_arguments = arguments.get("arguments")
    if not isinstance(nested_name, str) or not nested_name.strip():
        return name, arguments
    if isinstance(nested_arguments, str):
        try:
            nested_arguments = json.loads(nested_arguments)
        except json.JSONDecodeError:
            return name, arguments
    if not isinstance(nested_arguments, dict):
        return name, arguments
    return _canonical_provider_tool_name(nested_name.strip()), nested_arguments


def _limit_market_information_recursion(
    manifest: list[dict[str, Any]],
    *,
    task_kind: str,
    objective: str,
) -> list[dict[str, Any]]:
    """Hide recursive delegation from routine single-symbol research.

    A normal analysis needs one concrete market receipt, not a child Task
    Forest.  An explicit Critic is also Host-dispatched from the durable
    evidence contract: exposing generic delegation to the model lets it turn
    one requested Critic into unrelated specialist children before the Host
    can create the bounded verification branch.
    """
    if task_kind != "market_information":
        return manifest
    return [item for item in manifest if str(item.get("name") or "") != "agent.run_subtasks"]


def _partition_tool_calls(
    calls: list[dict[str, Any]],
    *,
    tool_metadata: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep malformed or schema-invalid calls out of the executable PlanGraph."""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for call in calls:
        arguments = call.get("arguments") or {}
        if isinstance(arguments, dict) and arguments.get("_agent_argument_error"):
            rejected.append(call)
            continue
        metadata = (tool_metadata or {}).get(str(call.get("name") or ""))
        schema = dict(metadata.get("input_schema") or {}) if metadata else {}
        errors = validate_json_value(arguments, schema)
        if errors:
            rejected.append({**call, "_argument_schema_errors": errors})
            continue
        accepted.append(call)
    return accepted, rejected


def _tool_schema_error_message(errors: list[dict[str, Any]]) -> str | None:
    if not errors:
        return None
    return "; ".join(
        f"{str(error.get('path') or '$')}: {str(error.get('message') or 'schema validation failed')}"
        for error in errors
        if isinstance(error, dict)
    ) or "tool arguments do not match the registered schema"


def _normalize_plan_patch_operations(value: Any) -> list[dict[str, Any]]:
    """Accept safe, common local-model representations of PlanPatch operations."""

    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            # Plan patches are advisory model output. A malformed optional patch
            # must not discard already validated Host tool evidence or fail the
            # entire run; executable tool calls are compiled and authorized
            # independently below.
            return []
    if isinstance(value, dict):
        if not value:
            return []
        nested = value.get("operations")
        if isinstance(nested, list):
            value = nested
        elif value.get("op") or value.get("type"):
            value = [value]
        else:
            # Some OpenAI-compatible/local models populate an optional schema
            # placeholder object here. It carries no executable authority; the host
            # derives executable nodes from independently validated tool_calls.
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _repair_provider_plan_patch(
    plan: PlanGraph,
    patch: dict[str, Any],
    *,
    allowed_capabilities: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep a useful model-authored Plan when it references undeclared nodes.

    Provider PlanPatch is optional and carries no execution authority. A dangling
    dependency should therefore be removed and audited instead of poisoning the
    mutable PlanGraph or discarding objective-specific titles and descriptions.
    """

    operations = [
        dict(item) for item in patch.get("operations") or [] if isinstance(item, dict)
    ]
    declared = set(plan.nodes)
    declared.update(
        str((operation.get("node") or {}).get("node_id") or "")
        for operation in operations
        if operation.get("op") == "add_node"
        and isinstance(operation.get("node"), dict)
    )
    declared.discard("")
    repaired: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    for operation in operations:
        action = str(operation.get("op") or "")
        current = dict(operation)
        if action == "add_node" and isinstance(operation.get("node"), dict):
            node = dict(operation["node"])
            node_id = str(node.get("node_id") or "")
            capability = _provider_patch_capability(node)
            if capability and allowed_capabilities is not None and capability not in allowed_capabilities:
                repairs.append(
                    {
                        "op": action,
                        "node_id": node_id,
                        "removed_unknown_capability": capability,
                    }
                )
                continue
            dependencies = [str(item) for item in node.get("dependencies") or []]
            kept = [
                dependency
                for dependency in dependencies
                if dependency in declared and dependency != node_id
            ]
            removed = [dependency for dependency in dependencies if dependency not in kept]
            if removed:
                repairs.append(
                    {
                        "op": action,
                        "node_id": node_id,
                        "removed_dependency_ids": removed,
                    }
                )
            node["dependencies"] = kept
            parent_id = str(node.get("parent_id") or "")
            if parent_id and parent_id not in declared:
                repairs.append(
                    {
                        "op": action,
                        "node_id": node_id,
                        "removed_parent_id": parent_id,
                    }
                )
                node["parent_id"] = None
            current["node"] = node
        elif action == "update_node":
            changes = dict(operation.get("changes") or {})
            if "dependencies" in changes:
                dependencies = [str(item) for item in changes.get("dependencies") or []]
                kept = [dependency for dependency in dependencies if dependency in declared]
                removed = [dependency for dependency in dependencies if dependency not in kept]
                if removed:
                    repairs.append(
                        {
                            "op": action,
                            "node_id": str(operation.get("node_id") or ""),
                            "removed_dependency_ids": removed,
                        }
                    )
                changes["dependencies"] = kept
            current["changes"] = changes
        elif action == "add_dependency":
            dependency_id = str(operation.get("dependency_id") or "")
            if dependency_id not in declared:
                repairs.append(
                    {
                        "op": action,
                        "node_id": str(operation.get("node_id") or ""),
                        "removed_dependency_ids": [dependency_id],
                    }
                )
                continue
        repaired.append(current)
    return {
        **patch,
        "operations": repaired,
    }, repairs


def _provider_patch_capability(node: dict[str, Any]) -> str | None:
    """Return a normalized executable capability from an advisory PlanPatch node."""

    node_type = str(node.get("node_type") or "")
    if node_type == "tool":
        name = str(node.get("tool_name") or "").strip()
    elif node_type in {"subtask", "subagent"}:
        name = str(node.get("tool_name") or "agent.run_subtasks").strip()
    elif node_type == "schedule":
        name = str(node.get("tool_name") or "schedule.create").strip()
    elif node_type == "workflow":
        name = str(node.get("tool_name") or "workflow.run").strip()
    elif node_type == "approval":
        name = str(node.get("tool_name") or (node.get("metadata") or {}).get("target_tool") or "").strip()
    else:
        return None
    return _canonical_provider_tool_name(name) if name else None


def _reusable_observation(
    trace: list[dict[str, Any]],
    call: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    matching_id = [item for item in trace if item.get("call_id") == call["id"]]
    for item in matching_id:
        if item.get("tool") != call["name"] or item.get("arguments") != call["arguments"]:
            # Provider call IDs are advisory and are often reused after a durable
            # checkpoint resume. Identity and idempotency are bound to the host's
            # content-derived plan node plus exact tool/arguments, never the model ID.
            continue
        if item.get("ok") is True:
            return {
                "id": call["id"],
                "name": call["name"],
                "ok": True,
                "result": item.get("result") or item.get("result_summary") or {},
                "validation": item.get("validation"),
                "reused": True,
            }
    if metadata.get("idempotency") != "arguments":
        return None
    for item in reversed(trace):
        if (
            item.get("tool") == call["name"]
            and item.get("arguments") == call["arguments"]
            and item.get("ok") is True
        ):
            return {
                "id": call["id"],
                "name": call["name"],
                "ok": True,
                "result": item.get("result") or item.get("result_summary") or {},
                "validation": item.get("validation"),
                "reused": True,
            }
    return None


def _prior_repeated_tool_failure(
    trace: list[dict[str, Any]],
    call: dict[str, Any],
) -> dict[str, Any] | None:
    """Find a failed call whose source must not be retried in this Run.

    Exact arguments are always blocked.  Once a tool's own retry policy has
    exhausted a source/transport failure, cosmetic query rewrites must also
    move to an alternate source.  Argument-validation failures remain eligible
    for a genuine argument rewrite.
    """

    for item in reversed(trace):
        if item.get("ok") is not False or item.get("tool") != call.get("name"):
            continue
        if item.get("arguments") == call.get("arguments"):
            return item
        category = str((item.get("error") or {}).get("category") or "")
        if category in {
            "timeout",
            "transport",
            "rate_limit",
            "api_500",
            "source_missing",
            "source_stale",
            "provider_failure",
            "provider_unavailable",
            "execution_failure",
            "repeated_tool_failure_blocked",
            "identical_retry_blocked",
        }:
            return item
    return None


def _alternative_tools_for_failure(
    failed_tool: str,
    manifest: list[dict[str, Any]],
) -> list[str]:
    """Return diverse executable alternatives without hard-coding a workflow.

    A same-category fallback is useful, but it must not crowd out every
    independent source.  Selecting the first representative of each category
    gives a failed market source a chance to recover through research/web (and
    vice versa) before filling the remaining bounded slots.
    """

    failed = next((item for item in manifest if item.get("name") == failed_tool), {})
    failed_category = str(failed.get("category") or "")
    candidates = [
        item
        for item in manifest
        if item.get("name") != failed_tool
        and (
            str(item.get("category") or "") == failed_category
            or _has_independent_recovery_evidence_contract(item)
        )
    ]
    candidates.sort(
        key=lambda item: (
            0 if str(item.get("category") or "") == failed_category else 1,
            0
            if str(item.get("category") or "") in {"market", "research", "web", "browser"}
            else 1,
            str(item.get("name") or ""),
        )
    )
    selected: list[str] = []
    seen_categories: set[str] = set()
    for item in candidates:
        name = str(item.get("name") or "")
        category = str(item.get("category") or "")
        if not name or category in seen_categories:
            continue
        selected.append(name)
        seen_categories.add(category)
        if len(selected) >= 6:
            return selected
    for item in candidates:
        name = str(item.get("name") or "")
        if name and name not in selected:
            selected.append(name)
        if len(selected) >= 6:
            break
    return selected


def _has_independent_recovery_evidence_contract(item: dict[str, Any]) -> bool:
    """Whether a capability can establish an alternative evidence path.

    A raw document fetch is intentionally insufficient by itself: without a
    preceding source-discovery/evidence contract it could be any unrelated
    page (for example ``example.com``) and must not clear a failed market or
    research branch.  Capability names are a Host-owned semantic contract,
    not model-authored workflow text.
    """

    name = str(item.get("name") or "")
    category = str(item.get("category") or "")
    return (
        "research" in category
        or name.endswith(".research")
        or name.endswith(".search")
        or name == "browser.open"
    )


def _unresolved_recovery_failures(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return failures that still need a Host-validated ``recovery_for`` link."""

    recovered_nodes = {
        str(link.get("failed_node_id") or "")
        for item in trace
        if item.get("ok") is True
        for link in (item.get("recovery_for") or [])
        if isinstance(link, dict)
    }
    return [
        item
        for item in trace
        if item.get("ok") is False
        and isinstance(item.get("recovery"), dict)
        and str(item.get("node_id") or "") not in recovered_nodes
    ]


def _retired_recovery_tool_names(trace: list[dict[str, Any]]) -> set[str]:
    """Return failed capabilities that a validated alternative has replaced.

    This is intentionally scoped to the current Run.  It is not a global
    circuit-breaker: a later Run can re-evaluate the source from scratch.
    """

    recovered_nodes = {
        str(link.get("failed_node_id") or "")
        for item in trace
        if item.get("ok") is True
        for link in (item.get("recovery_for") or [])
        if isinstance(link, dict)
    }
    return {
        str(item.get("tool") or "")
        for item in trace
        if item.get("ok") is False
        and isinstance(item.get("recovery"), dict)
        and str(item.get("node_id") or "") in recovered_nodes
        and str(item.get("tool") or "")
    }


def _recovery_tool_surface(
    trace: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the bounded L4/L5 tool surface for unresolved local failures.

    This remains data-driven: alternatives come from the current capability
    manifest and each failed tool's declared category, rather than a stock- or
    provider-specific fallback table.  Mutating, paper-execution and approval
    tools are excluded because an automatic recovery must remain reversible.
    """

    unresolved = _unresolved_recovery_failures(trace)
    if not unresolved:
        return [], []
    allowed_names: set[str] = set()
    requirements: list[dict[str, Any]] = []
    for failure in unresolved:
        failed_tool = str(failure.get("tool") or "")
        if not failed_tool:
            continue
        alternatives = _alternative_tools_for_failure(failed_tool, manifest)
        allowed_names.update(alternatives)
        requirements.append(
            {
                "failed_node_id": str(failure.get("node_id") or ""),
                "failed_tool": failed_tool,
                "alternatives": alternatives,
            }
        )
    if not allowed_names:
        return [], requirements
    surface = [
        item
        for item in manifest
        if str(item.get("name") or "") in allowed_names
        and not bool(item.get("mutating"))
        and not bool(item.get("requires_paper_execution"))
        and not bool(item.get("requires_approval"))
    ]
    return surface, requirements


def _host_recovery_calls(
    *,
    trace: list[dict[str, Any]],
    recovery_surface: list[dict[str, Any]],
    objective: str,
    symbols: list[str] | None,
) -> list[dict[str, Any]]:
    """Compile the next safe L4/L5 calls when a provider declines them.

    The Host never invents a new business workflow here.  It selects from the
    same independent, non-mutating alternatives that were explicitly exposed
    to the provider, copies only schema-compatible context from the failed
    receipt, and makes each call auditable as ``host_recovery_dispatch``.
    Returning no calls means every fillable candidate was already attempted or
    the manifest cannot safely express the missing inputs; only then may the
    caller advance toward L8.
    """

    manifest_by_name = {
        str(item.get("name") or ""): item
        for item in recovery_surface
        if isinstance(item, dict) and str(item.get("name") or "")
    }
    attempted = {
        _hash_payload(
            {
                "name": item.get("tool"),
                "arguments": item.get("arguments") or {},
            }
        )
        for item in trace
        if isinstance(item, dict) and str(item.get("tool") or "")
    }
    calls: list[dict[str, Any]] = []
    for failure in _unresolved_recovery_failures(trace):
        failed_tool = str(failure.get("tool") or "")
        alternatives = _alternative_tools_for_failure(
            failed_tool,
            list(manifest_by_name.values()),
        )
        for tool_name in alternatives:
            metadata = manifest_by_name.get(tool_name)
            if metadata is None or not _has_independent_recovery_evidence_contract(metadata):
                continue
            if any(
                bool(metadata.get(flag))
                for flag in ("mutating", "requires_approval", "requires_paper_execution")
            ):
                continue
            arguments = _host_recovery_arguments(
                metadata=metadata,
                failure=failure,
                objective=objective,
                symbols=symbols,
            )
            if arguments is None:
                continue
            signature = _hash_payload({"name": tool_name, "arguments": arguments})
            if signature in attempted:
                continue
            failed_node_id = str(failure.get("node_id") or "unknown")
            calls.append(
                {
                    "id": f"host-recovery-{failed_node_id[-8:]}-{len(calls) + 1}",
                    "name": tool_name,
                    "arguments": arguments,
                }
            )
            attempted.add(signature)
            # One distinct recovery attempt per failed local node keeps the
            # branch bounded and lets parallel read-only tools run naturally.
            break
    return calls


def _host_recovery_arguments(
    *,
    metadata: dict[str, Any],
    failure: dict[str, Any],
    objective: str,
    symbols: list[str],
) -> dict[str, Any] | None:
    """Fill only unambiguous required schema fields for a recovery call."""

    schema = metadata.get("input_schema")
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    required = schema.get("required") or []
    if not isinstance(required, list):
        return None
    previous = failure.get("arguments")
    previous = dict(previous) if isinstance(previous, dict) else {}
    symbol = _recovery_symbol(previous, symbols, objective)
    query = _recovery_query(previous, objective)
    arguments: dict[str, Any] = {}
    for field in required:
        if not isinstance(field, str):
            return None
        if field in previous and previous[field] not in (None, "", [], {}):
            arguments[field] = previous[field]
            continue
        if field == "symbol" and symbol:
            arguments[field] = symbol
            continue
        if field == "symbols" and symbol:
            arguments[field] = [symbol]
            continue
        if field == "query" and query:
            arguments[field] = query
            continue
        return None
    # These optional values remain deterministic refinements of already-known
    # local context.  They are deliberately omitted when not obvious.
    if "symbol" in properties and symbol and "symbol" not in arguments:
        arguments["symbol"] = symbol
    if "symbols" in properties and symbol and "symbols" not in arguments:
        arguments["symbols"] = [symbol]
    if "query" in properties and query and "query" not in arguments:
        arguments["query"] = query
    if "market" in properties and "market" not in arguments and symbol.endswith((".TW", ".TWO")):
        arguments["market"] = "TW"
    if "horizon" in properties and "horizon" not in arguments:
        arguments["horizon"] = str(previous.get("horizon") or "swing")
    if "source_count" in properties and "source_count" not in arguments:
        arguments["source_count"] = 3
    return arguments


def _recovery_symbol(
    arguments: dict[str, Any],
    symbols: list[str] | None,
    objective: str,
) -> str:
    explicit = arguments.get("symbol")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    listed = arguments.get("symbols")
    if isinstance(listed, list):
        for value in listed:
            if isinstance(value, str) and value.strip():
                return value.strip()
    for value in symbols or []:
        if isinstance(value, str) and value.strip():
            return value.strip()
    match = re.search(r"\b(\d{4,6})(?:\.(TW|TWO))?\b", f"{arguments.get('query') or ''} {objective}", re.I)
    if not match:
        return ""
    return f"{match.group(1)}.{(match.group(2) or 'TW').upper()}"


def _recovery_query(arguments: dict[str, Any], objective: str) -> str:
    existing = str(arguments.get("query") or "").strip()
    if existing:
        return existing[:500]
    url = str(arguments.get("url") or "").strip()
    if url:
        host = re.sub(r"^https?://", "", url, flags=re.I).split("/", 1)[0]
        path_terms = " ".join(
            part for part in re.split(r"[^A-Za-z0-9]+", url) if part and part.lower() not in {"http", "https", "www"}
        )
        return f"site:{host} {path_terms} {objective}"[:500]
    return f"Independent source verification: {objective}"[:500]


def _recovery_links_for_call(
    trace: list[dict[str, Any]],
    call: dict[str, Any],
    manifest: list[dict[str, Any]],
    *,
    objective: str = "",
    symbols: list[str] | tuple[str, ...] = (),
    result: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return explicit recovery receipts satisfied by one successful call.

    The relation is intentionally conservative: a later call must be a
    different, Host-approved alternative for the failed capability.  A normal
    success elsewhere in the plan is not allowed to erase a failure.
    """

    # A valid recovery receipt closes one local failure boundary exactly once.
    # Without this guard each subsequent successful alternative (for example a
    # later ``web.research`` call in a different branch) would be attached to
    # the already repaired failure again.  That created duplicate Fishbone
    # links and made the Task Forest look like the Host was repeatedly
    # recovering the same source.
    already_recovered = {
        str(link.get("failed_node_id") or "")
        for item in trace
        if item.get("ok") is True
        for link in (item.get("recovery_for") or [])
        if isinstance(link, dict)
    }
    recovered: list[dict[str, str]] = []
    current_name = str(call.get("name") or "")
    current_id = str(call.get("id") or "")
    for item in trace:
        if item.get("ok") is not False:
            continue
        failed_tool = str(item.get("tool") or "")
        if not failed_tool or failed_tool == current_name:
            continue
        if current_name not in _alternative_tools_for_failure(failed_tool, manifest):
            continue
        if not _recovery_scope_is_relevant(
            failed=item,
            call=call,
            result=result or {},
            objective=objective,
            symbols=symbols,
        ):
            continue
        failed_node_id = str(item.get("node_id") or "")
        if not failed_node_id or failed_node_id in already_recovered:
            continue
        recovered.append(
            {
                "failed_node_id": failed_node_id,
                "failed_call_id": str(item.get("call_id") or ""),
                "recovery_call_id": current_id,
                "recovery_tool": current_name,
            }
        )
    return recovered


def _recovery_scope_is_relevant(
    *,
    failed: dict[str, Any],
    call: dict[str, Any],
    result: dict[str, Any],
    objective: str,
    symbols: list[str] | tuple[str, ...],
) -> bool:
    """Prevent an allowed-but-unrelated source from closing a failure.

    Tool-family compatibility is necessary but not sufficient. The Host also
    compares concrete symbols, source hosts, and market intent so a successful
    ``example.com`` fetch or an unrelated research query cannot satisfy a
    failed market branch merely because it is technically an allowed tool.
    """

    current_name = str(call.get("name") or "")
    failed_name = str(failed.get("tool") or "")
    current_arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    failed_arguments = failed.get("arguments") if isinstance(failed.get("arguments"), dict) else {}
    current_text = " ".join(
        str(value)
        for value in (
            # A market capability often expresses its subject as a structured
            # symbol instead of repeating it in human-readable summary text.
            # Treat that Host-validated identity as scope evidence so a
            # same-symbol market alternative can actually repair a failed
            # market research pack.  Without it, completed alternatives kept
            # failing the relevance check and the Run repeatedly requested
            # more evidence until a budget boundary stopped it.
            current_arguments.get("symbol"),
            current_arguments.get("symbols"),
            current_arguments.get("query"),
            current_arguments.get("url"),
            result.get("symbol"),
            result.get("symbols"),
            result.get("ticker"),
            result.get("query"),
            result.get("title"),
            result.get("content"),
            result.get("summary"),
        )
        if value
    ).casefold()
    if "example.com" in current_text or "example.test" in current_text:
        return False
    failed_symbol_values = (
        failed_arguments.get("symbols")
        if isinstance(failed_arguments.get("symbols"), list)
        else []
    )
    requested_symbols = {
        str(value).casefold()
        for value in [
            *(symbols or ()),
            failed_arguments.get("symbol"),
            *failed_symbol_values,
        ]
        if str(value or "").strip()
    }
    concrete_codes = set(re.findall(r"(?<!\d)(\d{4,6}(?:\.(?:tw|two))?)(?!\d)", current_text))
    if requested_symbols:
        normalized_requested = {item.casefold() for item in requested_symbols}
        if not any(
            symbol in current_text
            or symbol.split(".", 1)[0] in concrete_codes
            for symbol in normalized_requested
        ):
            return False
    if failed_name.startswith("market.") and current_name.startswith("web."):
        market_terms = (
            "股票", "台股", "臺股", "市場", "股價", "行情", "財報", "營收",
            "stock", "market", "price", "financial", "taiwan", "twse", "tpex",
        )
        if not any(term in current_text for term in market_terms) and not concrete_codes:
            return False
    failed_url = str(failed_arguments.get("url") or "").casefold()
    if failed_name == "web.fetch" and current_name == "web.research" and failed_url:
        host = re.sub(r"^https?://", "", failed_url).split("/", 1)[0]
        if host and host not in current_text and not any(
            token in current_text for token in ("official", "權威", "替代", "independent", "source")
        ):
            return False
    return True


def _failure_recovery_coverage(
    plan: PlanGraph,
    trace: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Build validator input from Host-owned recovery links only."""

    failed_node_ids = {
        node.node_id
        for node in plan.nodes.values()
        if node.status in {"failed", "blocked", "cancelled"}
        and node.node_type != "finalize"
    }
    coverage: dict[str, list[str]] = {}
    for item in trace:
        if item.get("ok") is not True:
            continue
        recovery_for = item.get("recovery_for") or []
        if not isinstance(recovery_for, list):
            continue
        evidence_ids = [
            str(value)
            for value in (
                item.get("call_id"),
                item.get("node_id"),
                (item.get("validation") or {}).get("evidence_hash"),
            )
            if value
        ]
        for relation in recovery_for:
            if not isinstance(relation, dict):
                continue
            failed_node_id = str(relation.get("failed_node_id") or "")
            if failed_node_id in failed_node_ids and evidence_ids:
                coverage.setdefault(failed_node_id, []).extend(evidence_ids)
    return {
        node_id: list(dict.fromkeys(evidence_ids))
        for node_id, evidence_ids in coverage.items()
    }


def _unresolved_failure_nodes(
    plan: PlanGraph,
    trace: list[dict[str, Any]],
) -> list[str]:
    """Return failed executable nodes without a Host-owned recovery link."""

    recovered = {
        str(link.get("failed_node_id") or "")
        for item in trace
        if isinstance(item, dict) and item.get("ok") is True
        for link in (item.get("recovery_for") or [])
        if isinstance(link, dict) and str(link.get("failed_node_id") or "").strip()
    }
    failed = {
        str(item.get("node_id") or item.get("call_id") or "")
        for item in trace
        if isinstance(item, dict)
        and item.get("ok") is False
        and str(item.get("node_id") or item.get("call_id") or "").strip()
    }
    failed.update(
        node.node_id
        for node in plan.nodes.values()
        if node.node_type != "finalize"
        and node.status in {"failed", "blocked", "cancelled"}
    )
    return sorted(node_id for node_id in failed if node_id not in recovered)


def _tool_call_plan_patch(
    plan: PlanGraph,
    calls: list[dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    step: int,
) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    completed_dependencies = [
        node.node_id for node in plan.nodes.values() if node.status == "completed"
    ]
    reasoning_nodes = [
        node
        for node in sorted(
            plan.nodes.values(),
            key=lambda item: (item.order_index, item.node_id),
        )
        if node.node_type == "reasoning"
        and node.status in {"proposed", "pending", "ready"}
    ]
    assigned_parents: dict[str, Any] = {}
    claimed_node_ids: set[str] = set()
    for call in calls:
        call_id = str(call.get("id") or "")
        explicit = next(
            (
                node
                for node in reasoning_nodes
                if call_id and call_id in node.tool_call_ids
            ),
            None,
        )
        if explicit is not None:
            assigned_parents[call_id] = explicit
            claimed_node_ids.add(explicit.node_id)
    unclaimed_nodes = [
        node
        for node in reasoning_nodes
        if node.node_id not in claimed_node_ids and not node.tool_call_ids
    ]
    for call in calls:
        call_id = str(call.get("id") or "")
        if call_id in assigned_parents or not unclaimed_nodes:
            continue
        parent = unclaimed_nodes.pop(0)
        assigned_parents[call_id] = parent
        claimed_node_ids.add(parent.node_id)
        operations.append(
            {
                "op": "update_node",
                "node_id": parent.node_id,
                "changes": {
                    "tool_call_ids": [*parent.tool_call_ids, call_id],
                },
            }
        )
    for call in calls:
        node_id = _call_node_id(call)
        if node_id in plan.nodes:
            continue
        tool = metadata.get(call["name"], {})
        parent = assigned_parents.get(str(call.get("id") or ""))
        operations.append(
            {
                "op": "add_node",
                "node": {
                    "node_id": node_id,
                    "node_type": "tool",
                    "title": _tool_plan_title(call),
                    "dependencies": (
                        list(parent.dependencies)
                        if parent is not None
                        else completed_dependencies
                    ),
                    "status": "pending",
                    "tool_name": call["name"],
                    "arguments": call["arguments"],
                    "postconditions": (
                        [{"predicate": "host_validated_mutation"}]
                        if tool.get("mutating")
                        else []
                    ),
                    "parent_id": parent.node_id if parent is not None else None,
                    "mandatory": bool(
                        tool.get("requires_paper_execution") or tool.get("requires_full_execution")
                    ),
                    "metadata": {"model_call_id": call["id"], "turn_step": step},
                },
            }
        )
    return {
        "reason_summary": "Host linked requested tool calls to executable plan nodes.",
        "operations": operations,
    }


def _tool_plan_title(call: dict[str, Any]) -> str:
    name = str(call.get("name") or "host.capability")
    arguments = call.get("arguments") or {}
    symbol = str(arguments.get("symbol") or arguments.get("ticker") or "").strip()
    subject = symbol or "目標"
    query = " ".join(str(arguments.get("query") or "").split())
    url = str(arguments.get("url") or "").strip()
    labels = {
        "market.research_pack": f"取得 {subject} 行情、新聞與技術證據",
        "market.analyze_symbol": f"驗證 {subject} 策略訊號與風險",
        "market.institutional_flow": f"取得 {subject} 法人買賣證據",
        "market.monthly_revenue": f"取得 {subject} 官方月營收證據",
        "market.scan_watchlist": "掃描自選清單候選標的",
        "market.analyze_universe": "比較候選股票與風險",
        "market.search_taiwan_securities": f"確認 {subject} 台股標的資料",
        "market.taifex_foreign_open_interest": "取得期交所外資未平倉證據",
        "web.research": f"公開來源研究：{query[:72]}" if query else "查證最新公開來源",
        "web.fetch": f"讀取來源：{url[:72]}" if url else "讀取並驗證來源內容",
        "project.list_files": "盤點專案檔案",
        "project.search_text": "定位相關程式與設定",
        "project.read_file": "讀取並確認程式內容",
    }
    return labels.get(name, f"使用 {name} 取得 Host 驗證資料")


def _call_node_id(call: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"tool": call.get("name"), "arguments": call.get("arguments") or {}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"tool-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]}"


def _hash_payload(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _complete_finalize_nodes(plan: PlanGraph) -> None:
    completed = {
        node.node_id for node in plan.nodes.values() if node.status in {"completed", "skipped"}
    }
    for node in plan.nodes.values():
        if (
            node.node_type == "finalize"
            and node.status in {"pending", "ready"}
            and all(dependency in completed for dependency in node.dependencies)
        ):
            node.status = "completed"


def _host_completion_evaluation(
    plan: PlanGraph,
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a completion receipt from validated host observations only."""

    evidence_ids = [
        str(item.get("call_id"))
        for item in trace
        if item.get("ok") is True and str(item.get("call_id") or "").strip()
    ]
    unresolved = _unresolved_failure_nodes(plan, trace)
    return {
        "criteria_met": not unresolved,
        "criterion_results": [
            {
                "criterion": criterion,
                "met": not unresolved,
                "evidence_ids": evidence_ids,
            }
            for criterion in plan.completion_criteria
        ],
        "evidence_ids": evidence_ids,
        "remaining_gaps": [f"unresolved_recovery:{node_id}" for node_id in unresolved],
    }


def _objective_contract_check(
    *,
    objective: str,
    task_kind: str | None,
    trace: list[dict[str, Any]],
    decision: dict[str, Any] | None,
    completion_validation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the Host-owned outcome gap, even when a provider said complete.

    Completion validation is persisted in older runs and can therefore be
    absent or stale. Recompute this small contract from the current Host trace
    so the durable runtime can resume a false-completed objective safely.
    """

    check = evaluate_objective_completion(
        objective=objective,
        task_kind=task_kind,
        observations=(item for item in trace if isinstance(item, dict)),
        decision=decision,
    )
    persisted = next(
        (
            item
            for item in (completion_validation or {}).get("checks") or []
            if isinstance(item, dict) and item.get("name") == "objective_completion_contract"
        ),
        None,
    )
    if isinstance(persisted, dict):
        check = {
            **check,
            "persisted_passed": persisted.get("passed"),
            "persisted_missing_requirements": list(
                persisted.get("missing_requirements") or []
            ),
        }
    return check


def _should_host_finalize_from_validated_evidence(
    *,
    turn: dict[str, Any],
    plan: PlanGraph,
    trace: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
    evidence_requirement_met: bool,
) -> bool:
    if (
        turn.get("state") != "continue"
        or turn.get("tool_calls")
        or not str(turn.get("summary") or "").strip()
        or not evidence_requirement_met
    ):
        return False
    # Alternative evidence cannot close a failed branch until the Host has
    # linked that evidence to the original failure.
    if _unresolved_failure_nodes(plan, trace):
        return False
    if any(
        node.status not in {"completed", "skipped"}
        and node.node_type not in {"reasoning", "finalize"}
        for node in plan.nodes.values()
    ):
        return False
    # Require two consecutive empty provider turns after validated evidence,
    # rather than turning the first formatting/planning pause into completion.
    empty_continue_turns = sum(
        1
        for item in transcript
        if item.get("role") == "agent"
        and item.get("type") == "decision"
        and (item.get("content") or {}).get("state") == "continue"
        and not (item.get("content") or {}).get("tool_calls")
    )
    return empty_continue_turns >= 2


def _should_host_finalize_verified_paper_order(
    *,
    turn: dict[str, Any],
    trace: list[dict[str, Any]],
    context: AgentRunContext,
    objective: str,
    task_kind: str | None,
) -> bool:
    """Finish an explicit local paper order from Host receipts, not model prose."""

    if (
        turn.get("state") not in {"continue", "complete"}
        or turn.get("tool_calls")
        or context.allow_paper_orders is not True
        or context.state.get("explicit_paper_order_authorized") is not True
    ):
        return False
    completion = evaluate_objective_completion(
        objective=objective,
        task_kind=task_kind,
        observations=trace,
        decision=None,
    )
    return completion["passed"] is True and completion["requirements"]["paper_order_requested"] is True


def _should_host_finalize_analysis_only_after_numeric_rejection(
    *,
    task_kind: str | None,
    turn: dict[str, Any],
    plan: PlanGraph,
    trace: list[dict[str, Any]],
    completion_validation: dict[str, Any],
) -> bool:
    """Bound a safe analysis-only answer when only model numerics were rejected.

    This is deliberately narrower than general completion repair.  It applies
    only after a provider has requested completion, every required Host check
    except numeric grounding already passed, and no mutation or unresolved
    recovery exists.  It never applies to trading, automation or project work.
    """

    if (
        task_kind != "market_information"
        or turn.get("state") != "complete"
        or turn.get("tool_calls")
        or not _evidence_requirement_met(task_kind, trace)
        or _unresolved_failure_nodes(plan, trace)
    ):
        return False
    failed_checks = {
        str(item.get("name") or "")
        for item in completion_validation.get("checks") or []
        if isinstance(item, dict) and item.get("passed") is False
    }
    return bool(failed_checks) and failed_checks <= {
        "final_answer_numeric_claims_are_evidence_grounded",
        "final_answer_period_measurements_are_evidence_bound",
    }


def _should_host_finalize_analysis_only_data_unavailable(
    *,
    task_kind: str | None,
    objective: str,
    context: AgentRunContext,
    plan: PlanGraph,
    trace: list[dict[str, Any]],
) -> bool:
    """Stop a no-trade analysis once Host evidence proves data is unavailable.

    A data-blocked receipt remains unsuitable for a market decision or a
    research certification.  A normal information request is analysis-only by
    default: users say what they want analysed, rather than having to add
    procedural language such as "only analyse" or "do not trade".  This
    narrow terminal path therefore reports the verified limitation whenever
    the request does not itself ask for a decision or an order.
    """

    if (
        task_kind != "market_information"
        or context.allow_paper_orders is True
        or _unresolved_failure_nodes(plan, trace)
    ):
        return False
    contract = objective_completion_contract(objective, task_kind)
    if any(
        contract[name]
        for name in ("decision_requested", "order_requested", "paper_order_requested")
    ):
        return False
    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        if str(item.get("tool") or "") != "market.analyze_symbol":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        data_status = (
            result.get("data_status")
            if isinstance(result.get("data_status"), dict)
            else {}
        )
        if (
            str(result.get("recommendation_bucket") or "").casefold() == "data_blocked"
            or data_status.get("decision_ready") is False
        ):
            return True
    return False


def _should_host_finalize_ui_task(
    *,
    task_kind: str | None,
    plan: PlanGraph,
    trace: list[dict[str, Any]],
) -> bool:
    """Finish a bounded UI request from validated UI bridge evidence.

    This is deliberately limited to ``ui_task`` runs. It never authorizes an
    arbitrary mutation or covers an unresolved failure; every executable Plan
    node must already be completed before the Host produces the terminal
    acknowledgement.
    """

    if task_kind != "ui_task" or _unresolved_failure_nodes(plan, trace):
        return False
    if any(isinstance(item, dict) and item.get("ok") is False for item in trace):
        return False
    completed = {"completed", "skipped"}
    if any(
        node.status not in completed
        and node.node_type not in {"reasoning", "finalize"}
        for node in plan.nodes.values()
    ):
        return False
    return any(
        isinstance(item, dict)
        and item.get("ok") is True
        and str(item.get("tool") or "").startswith("ui.")
        and _observation_is_substantive(item)
        for item in trace
    )


def _verified_ui_task_summary(trace: list[dict[str, Any]]) -> str:
    """Render a concise final acknowledgement from Host-validated UI tools."""

    tools = list(
        dict.fromkeys(
            str(item.get("tool") or "")
            for item in trace
            if isinstance(item, dict)
            and item.get("ok") is True
            and str(item.get("tool") or "").startswith("ui.")
        )
    )
    rendered = "、".join(tools) or "ui 操作"
    return (
        f"Host 已完成並驗證介面操作：{rendered}。"
        "此 Run 僅執行本機介面操作；未建立自動化、未建立或送出紙上訂單、"
        "未發送外部通知，也未執行真實交易。"
    )


def _objective_explicitly_analysis_only(objective: str) -> bool:
    lowered = str(objective or "").casefold()
    return any(
        phrase in lowered
        for phrase in (
            "只做市場分析",
            "只做分析",
            "僅分析",
            "不要交易",
            "不交易",
            "不要下單",
            "不下單",
            "analysis only",
            "do not trade",
            "do not place",
        )
    )


def _verified_data_unavailable_analysis_summary(trace: list[dict[str, Any]]) -> str:
    """Render a bounded no-trade result from a Host-validated data receipt."""

    symbol = "該標的"
    source = "未取得可安全用於判讀的市場來源時間"
    timestamp = "來源沒有提供可用時間"
    blockers: list[str] = []
    critic_requested = any(
        isinstance(item, dict)
        and str(item.get("tool") or "") == "agent.run_subtasks"
        and str(
            (
                (item.get("arguments") or {}).get("role")
                if isinstance(item.get("arguments"), dict)
                else ((item.get("result") or {}).get("role") if isinstance(item.get("result"), dict) else "")
            )
            or ""
        ).casefold()
        == "critic"
        for item in trace
    )
    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        if str(item.get("tool") or "") != "market.analyze_symbol":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        data_status = (
            result.get("data_status")
            if isinstance(result.get("data_status"), dict)
            else {}
        )
        if (
            str(result.get("recommendation_bucket") or "").casefold() != "data_blocked"
            and data_status.get("decision_ready") is not False
        ):
            continue
        symbol = str(result.get("symbol") or symbol).strip().upper() or symbol
        source = str(data_status.get("price_source") or source).strip() or source
        timestamp = (
            str(data_status.get("exchange_timestamp") or timestamp).strip() or timestamp
        )
        candidates = [
            *list(data_status.get("blockers") or []),
            *list((result.get("research_status") or {}).get("blockers") or []),
        ]
        blockers = [str(value).strip() for value in candidates if str(value).strip()]
        break
    blocker_text = (
        "、".join(dict.fromkeys(blockers[:6])) or "沒有可用於決策的完整市場資料"
    )
    critic_summary = ""
    if critic_requested:
        critic_summary = (
            "獨立 Critic：已完成並 Join 反方子分支；其結果只用於揭露資料限制與反方風險，"
            "不會把資料不足改寫為投資結論。"
            if _completed_critic_observation(trace)
            else "獨立 Critic：子分支沒有取得完成 receipt，因此本次不宣稱已完成反方驗證。"
        )
    return (
        f"已完成 {symbol} 的資料可用性檢查。本次沒有取得可安全用於價格趨勢或技術判讀的決策級市場資料，"
        "因此不以推測補齊趨勢、指標或投資建議。"
        f"資料來源：{source}。資料時間：{timestamp}。"
        f"已驗證的主要限制：{blocker_text}。"
        f"{critic_summary}"
        "結論：本次只回報資料不足，未建立自動化、未建立紙上訂單，也未向任何實盤券商送單。"
    )


def _verified_analysis_only_summary(trace: list[dict[str, Any]]) -> str:
    """Render a complete, non-numeric analysis summary from Host receipts.

    Numbers are intentionally omitted.  The numeric validator has already
    proved that the provider's display values cannot be bound exactly enough to
    publish.  A generic short fallback is not sufficient for a multi-part
    request: it fails the same scope validator that caused the fallback and
    sends a safe, completed Run back through another expensive model turn.
    Preserve every completed Host-owned dimension instead, without promoting
    ungrounded provider claims or numbers.
    """

    symbol = "該標的"
    has_research_pack = False
    has_analysis = False
    has_monthly_revenue = False
    has_institutional_flow = False
    has_portfolio_status = False
    has_external_research = False
    has_independent_critic = False
    risk_blocked = False
    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        candidate = str(result.get("symbol") or "").strip().upper()
        if candidate:
            symbol = candidate
        tool = str(item.get("tool") or "")
        has_research_pack = has_research_pack or tool == "market.research_pack"
        has_analysis = has_analysis or tool == "market.analyze_symbol"
        has_monthly_revenue = has_monthly_revenue or tool == "market.monthly_revenue"
        has_institutional_flow = has_institutional_flow or tool == "market.institutional_flow"
        has_independent_critic = has_independent_critic or (
            tool == "agent.run_subtasks" and _has_completed_critic_receipt(result)
        )
        has_portfolio_status = has_portfolio_status or "portfolio_status" in result
        result_text = json.dumps(result, ensure_ascii=False, sort_keys=True).casefold()
        has_external_research = has_external_research or any(
            marker in result_text
            for marker in ("fingpt", "finrobot", "tradingagents")
        )
        risk = result.get("risk_summary") if isinstance(result.get("risk_summary"), dict) else {}
        risk_blocked = risk_blocked or risk.get("approved") is False

    sections = [
        f"已完成 {symbol} 的受限市場分析；以下只保留本次通過 Host 驗證的工具收據，不重述模型中無法逐項核對的數字或價格。",
        (
            "持股／投資組合：已讀取本次分析脈絡中的部位狀態；本次是假設性分析，沒有讀取真實帳戶，也不把未提供的持倉細節視為事實。"
            if has_portfolio_status
            else "持股／投資組合：本次沒有讀取真實帳戶；分析只以使用者指定的假設持有情境評估，不假設持倉數量或成本。"
        ),
        (
            "技術面：Host 已完成行情、趨勢與技術特徵檢查，並保留資料時間、來源與可追溯收據；不將該觀察擴張為保證報酬或買賣價格。"
            if has_analysis or has_research_pack
            else "技術面：本次沒有足夠的 Host 技術觀察可安全延伸，因此不以推測補齊。"
        ),
        (
            "基本面／月營收：已取得月營收資料收據，供基本面脈絡比對；本摘要不重述未經逐項綁定的模型數字。"
            if has_monthly_revenue
            else "基本面／月營收：本次沒有取得可驗證的月營收收據，這是保留的資料限制。"
        ),
        (
            "法人：已取得法人流向資料收據，可與技術與基本面觀察交叉檢視；不將單一資料點解讀成確定方向。"
            if has_institutional_flow
            else "法人：本次沒有取得可驗證的法人流向收據，不能以推測補齊。"
        ),
        (
            "外部研究：已納入可追溯的外部研究框架或研究證據，僅作交叉驗證，不把外部內容直接當成交易指令。"
            if has_external_research
            else "外部研究：本次沒有取得可安全發布的外部研究收據，這是結論的限制。"
        ),
        (
            "獨立 Critic：已建立並完成獨立反方子分支；其證據已加入本次判讀，用來挑戰而非覆寫主分支的已驗證結果。"
            if has_independent_critic
            else "獨立 Critic：本次沒有完成獨立反方子分支，因此不假稱已經過反方驗證。"
        ),
        (
            "風險：研究與風險閘門未核准任何執行，資料與研究限制已保留；因此不提出交易指令。"
            if risk_blocked
            else "風險：本次只保留已驗證的風險觀察，不延伸為任何交易授權。"
        ),
        "結論：本次僅完成分析；未建立自動化、未建立紙上訂單，也未向任何實盤券商送單。",
    ]
    return "\n".join(sections)


def _should_defer_reflection_until_paper_order_receipt(
    *,
    context: AgentRunContext,
    objective: str,
    task_kind: str | None,
    trace: list[dict[str, Any]],
) -> bool:
    """Keep an explicit local paper order on the Host execution path.

    A paper-order request already carries the user's bounded authorization.
    Before a verified ``paper.submit_order`` receipt exists, a provider's
    generic reflection is not a meaningful external acceptance condition; it
    is a premature detour that would otherwise leave the Run waiting forever.
    """

    if (
        context.allow_paper_orders is not True
        or context.state.get("explicit_paper_order_authorized") is not True
    ):
        return False
    contract = objective_completion_contract(objective, task_kind)
    if contract["paper_order_requested"] is not True:
        return False
    return not any(
        isinstance(item, dict)
        and item.get("ok") is True
        and str(item.get("tool") or "") == "paper.submit_order"
        for item in trace
    )


def _verified_paper_order_summary(trace: list[dict[str, Any]]) -> str:
    """Return a user-safe summary whose claims are limited to Host receipts."""

    analysis_summary = _paper_order_analysis_summary(trace)

    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        if str(item.get("tool") or "") != "paper.submit_order":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        order = (result.get("broker") or {}).get("order") or result.get("order") or {}
        symbol = str(order.get("symbol") or "").strip().upper()
        side = str(order.get("side") or order.get("action") or "").strip().casefold()
        action = {"buy": "買進", "sell": "賣出"}.get(side, "交易")
        subject = f" {symbol}" if symbol else ""
        summary = (
            f"已完成{subject} 的紙上模擬{action}；Host 已驗證預覽、訂單持久化與執行狀態，"
            "未送往實盤券商。"
        )
        return f"{summary}{analysis_summary}"
    return f"已完成 Host 驗證的紙上模擬交易；未送往實盤券商。{analysis_summary}"


def _paper_order_analysis_summary(trace: list[dict[str, Any]]) -> str:
    """Preserve the completed analysis scope in a paper-run terminal receipt.

    A paper sandbox request commonly combines an analysis request with a local
    order.  Returning only the fill receipt makes the completed fundamental,
    technical, and risk work appear to have vanished, which in turn invites a
    user to repeat the same request.  The provider's prose is intentionally
    not reused here: this short summary only exposes dimensions that a Host
    tool actually produced and never turns a paper fill into a recommendation.
    """

    analysis: dict[str, Any] | None = None
    has_monthly_revenue = False
    for item in trace:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        tool = str(item.get("tool") or "")
        if tool == "market.monthly_revenue":
            has_monthly_revenue = True
        if tool != "market.analyze_symbol":
            continue
        result = item.get("result")
        if isinstance(result, dict):
            analysis = result

    if analysis is None:
        return ""

    symbol = str(analysis.get("symbol") or "該標的").strip().upper() or "該標的"
    data_status = analysis.get("data_status")
    data_status = data_status if isinstance(data_status, dict) else {}
    risk_summary = analysis.get("risk_summary")
    risk_summary = risk_summary if isinstance(risk_summary, dict) else {}
    data_blocked = (
        str(analysis.get("recommendation_bucket") or "").casefold() == "data_blocked"
        or data_status.get("decision_ready") is False
    )
    technical = (
        "技術面：Host 已完成行情與技術訊號檢查；結果僅供分析，不構成買賣建議。"
        if data_status.get("analysis_ready") is True
        else "技術面：本次沒有取得可安全發布的 Host 技術判讀證據。"
    )
    fundamental = (
        "基本面：已取得官方月營收證據，未以模型未驗證數字補寫。"
        if has_monthly_revenue
        else "基本面：本次沒有取得可驗證的基本面證據。"
    )
    risk = (
        "風險：Host 風險閘門未核准研究／策略交易訊號；紙上單不代表投資決策。"
        if risk_summary.get("approved") is False
        else "風險：僅保留 Host 已驗證的風險檢查，紙上單不代表投資決策。"
    )
    limitation = (
        "市場分析資料未達決策門檻；本筆僅為依使用者指示執行的紙上沙盒交易。"
        if data_blocked
        else "本筆是本機紙上沙盒交易，不會改變研究或實盤執行門檻。"
    )
    return f" 分析摘要（{symbol}）：{fundamental}{technical}{risk}{limitation}"


def _resource_scope(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    scope: dict[str, Any] = {"tool": tool_name}
    for key in ("path", "cwd", "symbol", "schedule_id", "url", "server", "name"):
        if key in arguments:
            scope[key] = arguments[key]
    return scope


def _context_task_kinds(routing: Any, context: AgentRunContext) -> list[str]:
    kinds = [str(intent.type) for intent in routing.intents]
    if context.state.get("explicit_paper_order_authorized") is True:
        kinds.append("paper_execution")
    return list(dict.fromkeys(kinds))


def _should_host_submit_verified_paper_order(
    *,
    turn: dict[str, Any],
    pending_order: dict[str, Any] | None,
    context: AgentRunContext,
) -> bool:
    """Prefer the exact Host-verified paper order over further model detours."""

    if (
        pending_order is None
        or context.allow_paper_orders is not True
        or context.state.get("explicit_paper_order_authorized") is not True
    ):
        return False
    # A provider may repeat the submit tool but change the side, size or lot
    # type after Host has verified the preview. Seeing the tool name alone is
    # not consent for that mutation: only the exact preview payload may reach
    # the local Paper Broker. Any mismatch is replaced by the Host call below.
    return not any(
        isinstance(call, dict)
        and str(call.get("name") or "") == "paper.submit_order"
        and isinstance(call.get("arguments"), dict)
        and dict(call["arguments"]) == pending_order
        for call in turn.get("tool_calls") or []
    )


def _host_explicit_paper_protocol_call(
    *,
    context: AgentRunContext,
    objective: str,
    task_kind: str | None,
    trace: list[dict[str, Any]],
    tool_metadata: dict[str, dict[str, Any]],
    step: int,
) -> dict[str, Any] | None:
    """Advance a bounded explicit paper order without depending on model planning.

    A provider may correctly explain that production research is blocked while
    still failing to call the separately authorized local Paper Broker.  That
    explanation must not turn a concrete, non-live instruction into a retry
    loop.  The Host derives only a fully specified order and advances it in
    the auditable sequence analysis -> preview -> submit.  Missing symbol,
    side, or quantity remains a user-input problem; this helper never guesses
    an order.
    """

    if (
        context.allow_paper_orders is not True
        or context.state.get("explicit_paper_order_authorized") is not True
        or objective_completion_contract(objective, task_kind)["paper_order_requested"] is not True
    ):
        return None
    # Once the Host has a current, submit-eligible preview, its exact payload
    # is more authoritative than an underspecified natural-language request.
    # Do this before parsing the objective: a routine request such as "建立一
    # 筆紙上模擬交易" may omit side and quantity, while the verified preview
    # already contains both. Previously that omission made this helper return
    # early and visibly forced a needless partial-completion/recovery cycle.
    pending_order = _pending_verified_paper_order(trace)
    if pending_order is not None:
        if "paper.submit_order" not in tool_metadata:
            return None
        return {
            "id": f"host-paper-submit-{step}",
            "name": "paper.submit_order",
            "arguments": pending_order,
        }
    order = _explicit_paper_order_from_objective(objective, symbols=context.symbols)
    if order is None:
        return None
    successful_tools = {
        str(item.get("tool") or "")
        for item in trace
        if isinstance(item, dict) and item.get("ok") is True
    }
    if not successful_tools.intersection({"market.analyze_symbol", "market.research_pack"}):
        if "market.analyze_symbol" not in tool_metadata:
            return None
        return {
            "id": f"host-paper-analysis-{step}",
            "name": "market.analyze_symbol",
            "arguments": {"symbol": order["symbol"]},
        }
    if "paper.preview_order" not in successful_tools:
        if "paper.preview_order" not in tool_metadata:
            return None
        return {
            "id": f"host-paper-preview-{step}",
            "name": "paper.preview_order",
            "arguments": order,
        }
    return None


def _host_explicit_market_coverage_calls(
    *,
    objective: str,
    task_kind: str | None,
    symbols: tuple[str, ...] | list[str],
    trace: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    tool_metadata: dict[str, dict[str, Any]],
    step: int,
) -> list[dict[str, Any]]:
    """Fill explicitly requested, read-only market evidence before synthesis.

    This uses the same Host-owned evidence contract as final validation and
    paper-lane capability filtering. It does not infer investment work from a
    vague question or manufacture a decision; it merely completes the
    deterministic, read-only evidence obligations the user named.
    """

    if task_kind not in {"market_information", "market_decision"} or not symbols:
        return []
    requirements = requested_market_evidence_requirements(
        objective=objective,
        task_kind=task_kind,
    )
    # Preserve the model-directed surface for a single narrow research topic.
    # Multi-dimensional evidence is a Host completion contract and must be
    # dispatched before the model can produce a premature final summary.
    if len(requirements["requested_dimensions"]) < 2:
        return []
    available_tools = set(tool_metadata)
    planned_tools = {str(call.get("name") or "") for call in calls if isinstance(call, dict)}
    completed_tools = {
        str(item.get("tool") or "")
        for item in trace
        if isinstance(item, dict) and item.get("ok") is True
    }
    symbol = str(symbols[0]).strip().upper()
    if not symbol:
        return []
    required = {
        "market.monthly_revenue": {"symbol": symbol, "limit": 12},
        "market.institutional_flow": {"symbol": symbol, "limit": 5},
        "web.research": {"query": f"{symbol} official market research", "source_count": 5},
    }
    coverage_calls = [
        {
            "id": f"host-market-coverage-{name.rsplit('.', 1)[-1]}-{step}",
            "name": name,
            "arguments": arguments,
        }
        for name, arguments in required.items()
        if name in requirements["required_capability_names"]
        if name in available_tools and name not in planned_tools and name not in completed_tools
    ]
    critic_completed = any(
        str(item.get("tool") or "") == "agent.run_subtasks"
        and isinstance(item.get("result"), dict)
        and _has_completed_critic_receipt(item["result"])
        for item in trace
        if isinstance(item, dict) and item.get("ok") is True
    )
    if (
        "agent.run_subtasks" in requirements["required_capability_names"]
        and not critic_completed
        and "agent.run_subtasks" in available_tools
        and "agent.run_subtasks" not in planned_tools
    ):
        coverage_calls.append(
            {
                "id": f"host-market-coverage-critic-{step}",
                "name": "agent.run_subtasks",
                "arguments": {
                    "objectives": [
                        (
                            f"Independently challenge the parent analysis for {symbol}. "
                            "Use your own Host-validated market evidence, identify supporting "
                            "and conflicting evidence, and list unconfirmed risks. Do not trade, "
                            "create automation, access an account, or request user input."
                        )
                    ],
                    "role": "critic",
                    "max_steps": 6,
                },
            }
        )
    return coverage_calls


def _extend_explicit_paper_market_coverage_capabilities(
    tool_manifest: list[dict[str, Any]],
    *,
    all_tool_manifest: list[dict[str, Any]],
    objective: str,
    task_kind: str | None,
    context: AgentRunContext,
) -> list[dict[str, Any]]:
    """Keep explicitly requested read-only coverage available in paper mode.

    The routine paper lane intentionally hides broad research tools. When a
    multi-dimensional evidence contract applies, restore only the exact
    read-only capabilities required by that shared contract. This keeps the
    capability filter, Host dispatch and final validator in one policy path
    without reopening the generic research surface.
    """

    if (
        task_kind not in {"market_information", "market_decision"}
        or context.allow_paper_orders is not True
        or context.state.get("explicit_paper_order_authorized") is not True
    ):
        return tool_manifest
    requirements = requested_market_evidence_requirements(
        objective=objective,
        task_kind=task_kind,
    )
    if len(requirements["requested_dimensions"]) < 2:
        return tool_manifest
    required_names = set(requirements["required_capability_names"])
    current_names = {str(item.get("name") or "") for item in tool_manifest}
    additions = [
        item
        for item in all_tool_manifest
        if str(item.get("name") or "") in required_names
        and str(item.get("name") or "") not in current_names
    ]
    return [*tool_manifest, *additions]


def _extend_host_owned_critic_capability(
    tool_manifest: list[dict[str, Any]],
    *,
    all_tool_manifest: list[dict[str, Any]],
    objective: str,
    task_kind: str | None,
) -> list[dict[str, Any]]:
    """Retain the exact Critic capability for Host dispatch, never model choice."""

    requirements = requested_market_evidence_requirements(
        objective=objective,
        task_kind=task_kind,
    )
    if "agent.run_subtasks" not in requirements["required_capability_names"]:
        return tool_manifest
    if any(str(item.get("name") or "") == "agent.run_subtasks" for item in tool_manifest):
        return tool_manifest
    critic = next(
        (
            item
            for item in all_tool_manifest
            if str(item.get("name") or "") == "agent.run_subtasks"
        ),
        None,
    )
    return [*tool_manifest, critic] if critic is not None else tool_manifest


def _explicit_paper_order_from_objective(
    objective: str,
    *,
    symbols: tuple[str, ...] | list[str] = (),
) -> dict[str, Any] | None:
    """Parse a bounded local-paper order without fabricating a trade thesis.

    A routine paper simulation against exactly one user-selected security is a
    local test operation, not an investment recommendation.  If the user did
    not specify direction and size, the Host uses one odd-lot share bought in
    the local Paper Broker.  It is deterministic, cannot reach a live broker,
    and is labelled as a sandbox default in the durable receipt.  This keeps a
    plain-language request such as "analyse this stock and make one paper
    trade" from being turned into a needless question loop by a provider.
    """

    text = str(objective or "")
    contract = objective_completion_contract(text, "market_decision")
    if contract["paper_order_requested"] is not True:
        return None
    objective_symbols = re.findall(r"\b(\d{4,6}\.(?:TW|TWO))\b", text, flags=re.IGNORECASE)
    selected_symbols = tuple(
        dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    )
    all_symbols = tuple(dict.fromkeys(symbol.upper() for symbol in objective_symbols))
    if not all_symbols:
        all_symbols = selected_symbols
    if len(all_symbols) != 1:
        return None
    normalized = text.casefold()
    side = (
        "buy"
        if any(token in normalized for token in ("買進", "買入", "買一", "buy"))
        else "sell"
        if any(token in normalized for token in ("賣出", "賣一", "sell"))
        else "add"
        if "加碼" in normalized
        else "reduce"
        if "減碼" in normalized
        else None
    )
    quantity = re.search(
        r"(?<!\d)(\d+(?:\.\d+)?)\s*(股|shares?|張|lots?)(?![A-Za-z])",
        text,
        flags=re.IGNORECASE,
    )
    uses_sandbox_default = side is None and quantity is None
    if side is None and not uses_sandbox_default:
        return None
    if quantity is None and not uses_sandbox_default:
        return None
    if uses_sandbox_default:
        side = "buy"
        amount = 1.0
        unit = "股"
    else:
        amount = float(quantity.group(1))
        unit = quantity.group(2).casefold()
    if amount <= 0:
        return None
    amount_value: int | float = int(amount) if amount.is_integer() else amount
    order: dict[str, Any] = {
        "symbol": all_symbols[0],
        "side": side,
        "order_type": "market",
        "time_in_force": "rod",
        "rationale": (
            "Host-selected minimal local paper sandbox order requested by the user."
            if uses_sandbox_default
            else "Explicit local paper order requested by the user."
        ),
    }
    if unit in {"股", "share", "shares"}:
        order["quantity_shares"] = amount_value
        order["lot_type"] = "odd_lot"
    else:
        order["quantity_lots"] = amount_value
        order["lot_type"] = "board_lot"
    return order


def _routine_paper_wait_suppression_reason(
    *,
    turn: dict[str, Any],
    context: AgentRunContext,
    objective: str,
    task_kind: str | None,
) -> str | None:
    """Keep a routine local paper request autonomous after a provider detour.

    Only a request that already has a deterministic Host order qualifies.  A
    missing/multiple symbol, an explicit sell or quantity conflict, and every
    real external approval therefore stay outside this policy.
    """

    if turn.get("state") not in {"waiting_user_input", "waiting_decision"}:
        return None
    if task_kind != "market_decision" or context.allow_paper_orders is not True:
        return None
    if context.state.get("explicit_paper_order_authorized") is not True:
        return None
    order = _explicit_paper_order_from_objective(objective, symbols=context.symbols)
    if order is None:
        return None
    if _turn_requests_external_acceptance(turn):
        return "external_acceptance_is_not_part_of_local_paper_sandbox"
    return "host_owned_local_paper_sandbox_has_deterministic_default"


def _safe_public_paper_turn_summary(
    *,
    turn: dict[str, Any],
    context: AgentRunContext,
    objective: str,
    task_kind: str | None,
    trace: list[dict[str, Any]],
) -> str | None:
    """Replace unverified provider narration in the routine paper lane.

    This does not hide a model failure.  It makes the visible timeline state
    only what the Host has already fixed or verified: the non-live boundary,
    the deterministic one-share sandbox order, and then the real broker
    receipt.  Other task kinds retain the provider's normal public summary.
    """

    if task_kind != "market_decision" or context.allow_paper_orders is not True:
        return None
    if context.state.get("explicit_paper_order_authorized") is not True:
        return None
    if _explicit_paper_order_from_objective(objective, symbols=context.symbols) is None:
        return None
    if any(
        isinstance(item, dict)
        and item.get("ok") is True
        and str(item.get("tool") or "") == "paper.submit_order"
        for item in trace
    ):
        return _verified_paper_order_summary(trace)
    if _pending_verified_paper_order(trace) is not None:
        return "Host 已完成同一筆本機紙上單預覽，正在提交已驗證的安全沙盒訂單；不會送往實盤券商。"
    return "Host 正在依使用者目標準備本機紙上模擬：預設為 1 股零股買進，且不會送往實盤券商。"


def _pending_verified_paper_order(trace: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the latest exact preview eligible for an explicitly requested paper run."""

    if any(
        item.get("ok") is True and str(item.get("tool") or "") == "paper.submit_order"
        for item in trace
        if isinstance(item, dict)
    ):
        return None
    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        if str(item.get("tool") or "") != "paper.preview_order":
            continue
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if result.get("can_submit") is True and arguments:
            return dict(arguments)
    return None


def _approved_dependency(
    *,
    plan: PlanGraph,
    node_id: str,
    run_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    manager: ApprovalManager,
) -> dict[str, Any] | None:
    node = plan.nodes.get(node_id)
    if node is None:
        return None
    for dependency in node.dependencies:
        guard = plan.nodes.get(dependency)
        if guard is None or guard.node_type != "approval" or guard.status != "completed":
            continue
        approval = manager.latest_for_step(run_id, guard.node_id)
        if (
            approval
            and approval.get("status") == "approved"
            and approval.get("tool_name") == tool_name
            and (approval.get("payload") or {}).get("arguments") == arguments
        ):
            return approval
    return None


def _approved_resume_tool_calls(
    *,
    plan: PlanGraph,
    run_id: str,
    manager: ApprovalManager,
) -> list[dict[str, Any]]:
    """Recover approved mutation nodes before asking a provider another turn.

    A user approval binds one exact tool, arguments and plan node. On resume,
    that node must run first. Re-querying the provider can replace it with a
    different mutation and force a second approval for a request the user has
    already approved.
    """

    calls: list[dict[str, Any]] = []
    for node in sorted(plan.nodes.values(), key=lambda item: (item.order_index, item.node_id)):
        if node.node_type != "tool" or node.status != "ready" or not node.tool_name:
            continue
        approval = manager.latest_for_step(run_id, node.node_id)
        arguments = node_execution_arguments(node)
        if (
            approval is None
            or approval.get("status") != "approved"
            or approval.get("tool_name") != node.tool_name
            or (approval.get("payload") or {}).get("arguments") != arguments
        ):
            continue
        calls.append(
            {
                "id": str(node.metadata.get("model_call_id") or node.node_id),
                "name": str(node.tool_name),
                "arguments": arguments,
            }
        )
    return calls


def _worker_type(metadata: dict[str, Any]) -> str:
    backend = str(metadata.get("execution_backend") or "")
    category = str(metadata.get("category") or "")
    if backend and backend != "in_process":
        return backend
    if category.startswith("project"):
        return "project"
    if category == "terminal":
        return "terminal"
    if category == "browser":
        return "browser"
    if category == "ui":
        return "in_process"
    if category.startswith("git"):
        return "project"
    if category.startswith("external"):
        return "external"
    return "in_process"


def _schema_for_task(
    task_kind: str,
    *,
    protocol: str = "advanced_v1",
    objective: str = "",
) -> dict[str, Any]:
    proposal_evaluation = "[MODEL_OUTPUT_SCHEMA:proposal_evaluation]" in objective
    if protocol == "universal_v1":
        schema = json.loads(json.dumps(UNIVERSAL_DECISION_SCHEMA))
        if task_kind == "market_radar":
            from stock_ai.market_radar import market_radar_output_schema

            schema["properties"]["result"] = market_radar_output_schema()
        elif proposal_evaluation:
            schema["properties"]["result"] = json.loads(
                json.dumps(PROPOSAL_EVALUATION_RESULT_SCHEMA)
            )
        if task_kind != "market_decision":
            schema["properties"]["decision"] = {"type": "null"}
        return schema
    schema = json.loads(json.dumps(AGENT_DECISION_SCHEMA))
    if task_kind != "market_decision":
        schema["properties"]["decision"] = {
            "type": "null",
            "description": "General and factual information queries must not emit a stock decision or confidence score.",
        }
    if task_kind == "market_radar":
        from stock_ai.market_radar import market_radar_output_schema

        if "structured_result" not in schema["required"]:
            schema["required"].append("structured_result")
        schema["properties"]["structured_result"] = market_radar_output_schema()
    elif proposal_evaluation:
        schema["properties"]["structured_result"] = {
            "anyOf": [
                {"type": "null"},
                json.loads(json.dumps(PROPOSAL_EVALUATION_RESULT_SCHEMA)),
            ]
        }
    else:
        schema["properties"]["structured_result"] = {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                    "required": [],
                },
            ]
        }
    return schema


def _classify_task(objective: str) -> str:
    text = str(objective or "").strip()
    lowered = text.casefold()
    artifact_operation = _is_explicit_artifact_operation(lowered)
    # A negative capability constraint is a Host safety boundary, not a weak
    # routing hint.  It must win even when the model supplied an incorrect
    # task-kind header based on the word “市場” in “不要查市場”.  A direct
    # local Artifact operation is deliberately preserved: it needs no market
    # capability and remains within its narrow Host-owned scope.
    explicitly_no_market_lookup = (
        "不要查市場", "不查市場", "不要市場資料", "不需要市場資料",
        "no market lookup", "do not query market", "do not fetch market data",
    )
    if not artifact_operation and any(phrase in lowered for phrase in explicitly_no_market_lookup):
        return "general_answer"
    model_hint = re.match(
        r"^\[model_task_kind:(general_answer|artifact_task|project_task|market_information|market_decision|market_radar|ui_task|current_information)\]",
        lowered,
    )
    if model_hint:
        # Intent classification is advisory.  An explicit local Artifact
        # operation has a narrower Host-owned capability profile and must not
        # be stripped into a no-tool general answer by an imprecise model tag.
        if artifact_operation:
            return "artifact_task"
        hinted = model_hint.group(1)
        # The provider-produced routing header is only a capability hint.  It
        # must never downgrade an explicit buy/sell/order request to factual
        # market information, otherwise the decision/approval contract and
        # its completion gate disappear before the Master Agent can act.
        public_contract = objective_completion_contract(text, hinted)
        if public_contract["decision_requested"]:
            return "market_decision"
        return hinted
    if "[market_radar_task]" in lowered:
        return "market_radar"
    decision_phrases = (
        "該買", "可以買", "要買", "買進", "該賣", "賣出", "加碼", "減碼", "進場", "出場",
        "下單", "持有嗎", "續抱", "觀望", "值得投資", "投資建議", "買還是", "賣還是",
        "should i buy", "should i sell", "buy or sell", "trade decision", "position size",
    )
    analysis_only_phrases = (
        "只做市場分析", "只做分析", "不要給投資建議", "不需要投資建議",
        "不要下單", "不下單", "不送出任何模擬或真實委託", "不送出委託",
        "analysis only", "no investment advice", "do not place", "do not trade",
    )
    explicitly_analysis_only = any(phrase in lowered for phrase in analysis_only_phrases)
    if (not explicitly_analysis_only and any(phrase in lowered for phrase in decision_phrases)) or lowered == "decide":
        return "market_decision"
    ui_operation_forbidden = any(
        phrase in lowered
        for phrase in (
            "不要操作介面", "不操作介面", "不要操作 ui",
            "do not operate the ui", "do not operate ui", "do not use the user interface",
        )
    )
    ui_terms = (
        "ui.", "操作介面", "介面切換", "切換到設定", "切換到首頁", "打開設定",
        "打開面板", "開啟面板", "開啟設定", "開啟 agent", "選擇股票", "目前介面",
        "current view", "navigate to", "open panel",
        "operate the ui", "user interface",
    )
    if not ui_operation_forbidden and any(term in lowered for term in ui_terms):
        return "ui_task"
    if artifact_operation:
        return "artifact_task"
    project_terms = (
        "這個專案", "目前專案", "這個系統", "目前系統", "程式碼", "哪個檔案", "資料夾結構",
        "測試失敗", "git ", "github", "前端程式", "後端程式", "專案裡", "project file",
        "codebase", "repository", "repo ", "dependency test", "完整 dag", "subtask",
        "mutation", "modify file", "edit file", "write file", "fingpt", "finrobot",
        "finrl", "qlib", "tradingagents", "external tool",
    )
    if any(term in lowered for term in project_terms):
        return "project_task"
    if (
        any(phrase in lowered for phrase in ("不要分析股票", "不分析股票", "不談股票", "not about stocks"))
        and not re.search(r"(?<!\d)\d{4,6}(?:\.tw|\.two)?(?!\d)", lowered)
    ):
        return "general_answer"
    has_explicit_symbol = bool(
        re.search(r"\b[A-Z]{1,6}(?:\.(?:TW|TWO)|-USD)?\b", text.upper())
        or re.search(r"(?<!\d)\d{4,6}(?:\.TW|\.TWO)?(?!\d)", text.upper())
    )
    if has_explicit_symbol and any(phrase in lowered for phrase in ("分析", "評估", "看法", "展望", "analyze", "evaluate")):
        return "market_information" if explicitly_analysis_only else "market_decision"
    market_terms = (
        "股票", "台股", "臺股", "期貨", "選擇權", "外資", "法人", "未平倉", "淨空", "淨多",
        "股價", "行情", "市場", "財報", "營收", "指數", "盤勢", "成交量", "market", "stock", "futures",
    )
    if any(term in lowered for term in market_terms):
        return "market_information"
    # A user naturally continues an existing stock discussion with company
    # names plus research language, not necessarily a ticker or the word
    # 「股票」. This must still expose the evidence-gathering surface; without
    # it the provider can only ask the user to supply the missing data.
    stock_research_terms = (
        "技術面", "技術分析", "基本面", "籌碼", "估值", "本益比", "殖利率",
        "營收", "財報", "獲利", "波動", "風險差異", "風險比較", "比較風險",
        "risk comparison", "technical analysis", "fundamental analysis",
        "valuation", "earnings risk",
    )
    if any(term in lowered for term in stock_research_terms):
        return "market_information"
    current_terms = (
        "今天", "今日", "現在", "目前", "最新", "剛剛", "新聞", "天氣", "現任", "價格", "匯率",
        "比分", "賽程", "法規", "規定", "版本", "current", "latest", "today", "news", "weather",
        "price", "schedule", "score", "who is the", "as of",
    )
    if any(term in lowered for term in current_terms):
        return "current_information"
    return "general_answer"


def _proactive_reflection_required(
    *,
    objective: str,
    task_kind: str | None,
    trace: list[dict[str, Any]],
) -> bool:
    """Require a public Agent reflection after material market evidence.

    Reflection is a normal Agent checkpoint, not a special branch reserved for
    explicit buy/best-candidate wording. The Host still only asks the model to
    reflect after a real tool trace exists, and the model decides whether the
    remaining uncertainty warrants a user-facing question.
    """

    if not trace or task_kind not in {"market_information", "market_decision", "market_radar"}:
        return False
    if task_kind == "market_information":
        return any(
            _observation_is_substantive(item)
            for item in trace
            if str(item.get("tool") or item.get("name") or "")
            in {"web.fetch", "web.research"}
            or str(item.get("tool") or item.get("name") or "").startswith("market.")
        )
    contract = objective_completion_contract(objective, task_kind)
    return bool(contract.get("decision_requested") or contract.get("best_candidate_requested"))


def _public_reflection(reflection: dict[str, Any]) -> dict[str, Any]:
    """Keep reflection events user-safe and free of hidden reasoning."""

    return {
        "reflection_id": str(reflection.get("reflection_id") or "").strip() or None,
        "preferred_option": str(reflection.get("preferred_option") or "").strip(),
        "alternatives": [str(item) for item in reflection.get("alternatives") or [] if str(item).strip()],
        "should_ask_user": reflection.get("should_ask_user") is True,
        "reason_to_ask": str(reflection.get("reason_to_ask") or "").strip(),
        "unknowns": [str(item) for item in reflection.get("unknowns") or [] if str(item).strip()],
        "important_risks": [
            str(item) for item in reflection.get("important_risks") or [] if str(item).strip()
        ],
        "evidence_ids": [str(item) for item in reflection.get("evidence_ids") or [] if str(item).strip()],
        "main_evidence": [
            str(item) for item in reflection.get("main_evidence") or [] if str(item).strip()
        ],
    }


def _ground_reflection_evidence(
    reflection: dict[str, Any],
    trace: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Bind a public reflection only to Host-issued evidence receipts."""

    catalog: dict[str, str] = {}
    for item in trace:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        summary = str(
            item.get("result_summary")
            or item.get("tool")
            or item.get("name")
            or "Host 驗證證據"
        ).strip()
        for value in (
            item.get("call_id"),
            item.get("node_id"),
            (item.get("validation") or {}).get("evidence_hash"),
        ):
            if value:
                catalog[str(value)] = summary
    claimed = [
        str(item) for item in reflection.get("evidence_ids") or [] if str(item).strip()
    ]
    valid = list(dict.fromkeys(item for item in claimed if item in catalog))
    rejected = list(dict.fromkeys(item for item in claimed if item not in catalog))
    # A provider may omit IDs while correctly reflecting on a Host evidence
    # stage. Bind the public receipt to the actual successful trace rather
    # than leave the Decision Card's main evidence blank.
    if not valid:
        valid = list(catalog)[:20]
    main_evidence = list(dict.fromkeys(catalog[item] for item in valid if item in catalog))[:8]
    return {
        **reflection,
        "evidence_ids": valid,
        "main_evidence": main_evidence,
    }, rejected


def _fallback_public_reflection(
    *,
    objective: str,
    task_kind: str | None,
    trace: list[dict[str, Any]],
    summary: str,
) -> dict[str, Any]:
    """Build a safe public reflection when a provider omits the field.

    This is deliberately an evidence receipt, not a model-reasoning
    imitation: it names the verified path, preserves uncertainty, and leaves
    high-impact decision requests to the existing user checkpoint.
    """

    tools = list(dict.fromkeys(
        str(item.get("tool") or item.get("name") or "")
        for item in trace
        if str(item.get("tool") or item.get("name") or "")
    ))
    evidence_ids: list[str] = []
    for item in trace:
        for value in item.get("evidence_ids") or []:
            if str(value).strip() and str(value) not in evidence_ids:
                evidence_ids.append(str(value))
        validation = item.get("validation") or {}
        if validation.get("evidence_hash") and str(validation["evidence_hash"]) not in evidence_ids:
            evidence_ids.append(str(validation["evidence_hash"]))
    preferred = summary.strip() or (
        f"已依據 Host 驗證的 {', '.join(tools[:4]) or '資料來源'} 完成「{objective[:120]}」的研究摘要。"
    )
    risks = ["資料可能受來源延遲、覆蓋範圍與市場狀態影響，後續更新需重新驗證。"]
    if task_kind in {"market_decision", "market_radar"}:
        risks.append("這份反思不是交易授權；任何高影響決策仍需使用者確認。")
    return {
        "preferred_option": preferred,
        "alternatives": ["補充第二個獨立來源", "保留目前結論並等待下一次更新"],
        "should_ask_user": task_kind in {"market_decision", "market_radar"},
        "reason_to_ask": "目前證據可能改變下一步；請選擇是否補充來源或保留目前方案。",
        "unknowns": ["來源更新時間與未涵蓋的反方資料仍需持續觀察。"],
        "important_risks": risks,
        "evidence_ids": evidence_ids[:20],
    }


def _reflection_to_interaction(reflection: dict[str, Any]) -> dict[str, Any]:
    """Convert a model reflection into a deterministic public decision card."""

    preferred = str(reflection.get("preferred_option") or "Agent 暫定推薦").strip()
    alternatives = [
        str(item).strip()
        for item in reflection.get("alternatives") or []
        if str(item).strip()
    ]
    labels = [preferred, *alternatives]
    unique_labels = list(dict.fromkeys(labels))[:5]
    if len(unique_labels) < 2:
        unique_labels.append("保留目前方案並繼續驗證")
    options = [
        {
            "option_id": "agent_recommendation" if index == 0 else f"reflection_alternative_{index}",
            "label": label,
            "reason": (
                "Agent 目前依據已驗證證據的暫定推薦。"
                if index == 0
                else "Agent 在反思中保留的合理替代方案。"
            ),
        }
        for index, label in enumerate(unique_labels)
    ]
    return {
        "prompt": str(
            reflection.get("reason_to_ask")
            or "這個不確定性可能改變下一步；你希望採用 Agent 暫定推薦，還是選擇其他方案？"
        ).strip(),
        "agent_view": preferred,
        "preferred_option": options[0]["option_id"],
        "options": options,
        "unknowns": [str(item) for item in reflection.get("unknowns") or []],
        "important_risks": [str(item) for item in reflection.get("important_risks") or []],
        "main_evidence": [str(item) for item in reflection.get("main_evidence") or []],
        "evidence_ids": [str(item) for item in reflection.get("evidence_ids") or []],
    }


def _materialize_explicit_choice_interaction(
    *,
    objective: str,
    turn: dict[str, Any],
    transcript: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Recover a user-requested choice when a provider leaves it in prose.

    This is intentionally narrower than a general summary parser.  The Host
    only intervenes when the user named exactly two alternatives and the
    provider's completed/no-tool response names one of those alternatives as
    its recommendation.  That preserves a real Decision Card without
    inventing a preference, market evidence, or additional work.
    """

    if turn.get("state") not in {
        "continue",
        "complete",
        "waiting_user_input",
        "waiting_decision",
    } or turn.get("tool_calls"):
        return None
    if any(item.get("type") == "interaction_response" for item in transcript if isinstance(item, dict)):
        return None
    options = _explicit_choice_options(objective)
    if len(options) != 2:
        return None
    summary = str(turn.get("summary") or "").strip()
    preferred_index = _explicit_choice_preferred_index(options=options, turn=turn)
    if preferred_index is None:
        return None
    rendered_options = [
        {
            "option_id": f"explicit_choice_{index + 1}",
            "label": label,
            "reason": (
                "Agent 在公開摘要中列為暫定建議。"
                if index == preferred_index
                else "使用者原始問題中的明確替代方案。"
            ),
        }
        for index, label in enumerate(options)
    ]
    return {
        "prompt": "Agent 已提出暫定建議；請選擇接受建議、改選替代方案，或輸入自己的想法。",
        "agent_view": summary,
        "preferred_option": rendered_options[preferred_index]["option_id"],
        "options": rendered_options,
        "unknowns": [],
        "important_risks": ["此選擇僅延續使用者指定的假設討論，不會執行市場、交易或自動化操作。"],
        # This internal marker is consumed before the UI projection.  It
        # distinguishes an explicit user choice from a provider-created
        # "tell me how to continue" detour, which the routine-wait guard must
        # still close automatically.
        "host_explicit_choice_checkpoint": True,
    }


def _explicit_choice_recommendation_reprompt_required(
    *,
    objective: str,
    turn: dict[str, Any],
    transcript: list[dict[str, Any]],
) -> bool:
    """Return whether one internal retry is needed for a requested choice."""

    if turn.get("state") not in {"waiting_user_input", "waiting_decision"}:
        return False
    if turn.get("tool_calls") or _explicit_choice_options(objective) == []:
        return False
    if any(item.get("type") == "interaction_response" for item in transcript if isinstance(item, dict)):
        return False
    if any(
        item.get("type") == "explicit_choice_recommendation_required"
        for item in transcript
        if isinstance(item, dict)
    ):
        return False
    return _explicit_choice_preferred_index(
        options=_explicit_choice_options(objective),
        turn=turn,
    ) is None


def _explicit_choice_preferred_index(*, options: list[str], turn: dict[str, Any]) -> int | None:
    """Map a provider's stated preference back to the user's exact labels."""

    interaction = turn.get("interaction")
    interaction = interaction if isinstance(interaction, dict) else {}
    reflection = turn.get("reflection")
    reflection = reflection if isinstance(reflection, dict) else {}
    candidates = [
        str(turn.get("summary") or ""),
        str(reflection.get("preferred_option") or ""),
        str(interaction.get("preferred_option") or ""),
    ]
    selected_provider_option = next(
        (
            option
            for option in interaction.get("options") or []
            if isinstance(option, dict)
            and str(option.get("option_id") or "") == str(interaction.get("preferred_option") or "")
        ),
        None,
    )
    if isinstance(selected_provider_option, dict):
        candidates.append(
            " ".join(
                str(selected_provider_option.get(field) or "")
                for field in ("option_id", "label", "reason")
            )
        )
    normalized_candidates = " ".join(candidates).casefold()
    compact_candidates = re.sub(r"\s+", "", normalized_candidates)
    for index, label in enumerate(options):
        normalized_label = re.sub(r"\s+", "", label).casefold()
        if normalized_label and normalized_label in compact_candidates:
            return index
        aliases = _explicit_choice_option_aliases(label)
        if any(alias in normalized_candidates for alias in aliases):
            return index
    return None


def _explicit_choice_option_aliases(label: str) -> tuple[str, ...]:
    """Provide narrow bilingual aliases for common plan-choice labels."""

    normalized = str(label or "").casefold()
    aliases: list[str] = []
    if "保守" in normalized or "conservative" in normalized:
        aliases.extend(("保守", "conservative"))
    if "平衡" in normalized or "balanced" in normalized:
        aliases.extend(("平衡", "balanced"))
    return tuple(aliases)


def _explicit_choice_options(objective: str) -> list[str]:
    """Extract two concrete alternatives from a natural-language choice request."""

    text = str(objective or "").strip()
    patterns = (
        r"(?:請)?在\s*(?P<first>[^，。；;？?與或跟和]{1,48})\s*(?:與|或|還是|跟|和)\s*(?P<second>[^，。；;？?]{1,48}?)(?:之間|當中|中|裡)?\s*(?:幫我)?(?:選|選擇)",
        r"(?:請)?(?:選擇|選)\s*(?P<first>[^，。；;？?與或跟和]{1,48})\s*(?:與|或|還是|跟|和)\s*(?P<second>[^，。；;？?]{1,48})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match is None:
            continue
        options = [
            re.sub(r"\s+", " ", str(match.group(name) or "")).strip(" ：:，,。.")
            for name in ("first", "second")
        ]
        if all(options) and options[0].casefold() != options[1].casefold():
            return options
    return []


def _requires_hypothetical_only_notice(objective: str) -> bool:
    """Return whether a general answer must confirm that no action was taken."""

    lowered = objective.casefold()
    no_market = any(
        phrase in lowered
        for phrase in (
            "不要查市場", "不查市場", "不要市場資料",
            "no market lookup", "do not query market", "do not fetch market data",
        )
    )
    no_trade = any(
        phrase in lowered
        for phrase in (
            "不要交易", "不交易", "不要下單", "不下單",
            "do not trade", "do not place an order", "do not place orders",
        )
    )
    no_automation = any(
        phrase in lowered
        for phrase in (
            "不要建立自動化", "不建立自動化", "不要自動化",
            "do not create automation", "no automation",
        )
    )
    return no_market or no_trade or no_automation


def _objective_disallows_external_acceptance_wait(objective: str) -> bool:
    """Return whether an external acceptance card is forbidden for this Run.

    This is intentionally narrower than a blanket "do not ask questions"
    switch: the Host still preserves real approval gates and local recovery
    escalation.  It only rejects an optional provider request to wait for an
    external acceptance condition that the user explicitly made irrelevant.
    """

    lowered = objective.casefold()
    return any(
        phrase in lowered
        for phrase in (
            "不得等待外部驗收條件",
            "不要等待外部驗收條件",
            "不等待外部驗收條件",
            "不必等待外部驗收條件",
            "無需等待外部驗收條件",
            "不得等待使用者提供外部驗收條件",
            "不要等待使用者提供外部驗收條件",
            "do not wait for external acceptance",
            "no external acceptance required",
        )
    )


def _turn_requests_external_acceptance(turn: dict[str, Any]) -> bool:
    """Recognize an optional external-acceptance wait without prompt magic.

    It is not a valid blocker for a local analysis or paper-preview Run.
    Genuine approval and decision checkpoints use their own structured states
    and do not match these markers.
    """

    interaction = turn.get("interaction")
    interaction = interaction if isinstance(interaction, dict) else {}
    options = [item for item in interaction.get("options") or [] if isinstance(item, dict)]
    values = [
        turn.get("summary"),
        interaction.get("prompt"),
        interaction.get("agent_view"),
        *(interaction.get("unknowns") or []),
        *(
            value
            for option in options
            for value in (
                option.get("option_id"),
                option.get("label"),
                option.get("reason"),
            )
        ),
    ]
    normalized = " ".join(str(value or "").casefold() for value in values)
    return any(
        marker in normalized
        for marker in (
            "external acceptance",
            "acceptance condition",
            "external validation",
            "外部驗收",
            "外部驗證條件",
        )
    )


def _should_host_continue_information_clarification(
    turn: dict[str, Any],
    *,
    task_kind: str,
    context: AgentRunContext,
) -> dict[str, Any] | None:
    """Select the safe best-effort branch for a routine research question.

    A provider may ask the user to upload public data merely because it has
    not used the Host market tools yet. That is not a user decision: the Host
    can resolve issuer names, collect available evidence, and clearly report
    any remaining data gap. Keep this intentionally narrow so actual
    investment decisions, approvals, paper orders, and account actions still
    stop for user input.
    """

    if turn.get("state") not in {"waiting_user_input", "waiting_decision"}:
        return None
    if task_kind not in {"market_information", "current_information"}:
        return None
    if context.allow_paper_orders or context.allow_external_actions:
        return None
    interaction = turn.get("interaction")
    if not isinstance(interaction, dict):
        return None
    options = [dict(item) for item in interaction.get("options") or [] if isinstance(item, dict)]
    if len(options) < 2:
        return None
    selected = next(
        (
            option
            for option in options
            if str(option.get("option_id") or "").strip().casefold()
            in {"general_info", "general_information", "best_effort", "public_overview"}
        ),
        None,
    )
    if selected is None:
        return None
    text = " ".join(
        str(value or "")
        for value in (
            turn.get("summary"),
            interaction.get("prompt"),
            interaction.get("agent_view"),
            *(interaction.get("unknowns") or []),
        )
    ).casefold()
    data_markers = (
        "provide data", "market data", "risk metrics", "clarification", "general, high",
        "資料", "數據", "數值", "指標", "補充", "釐清", "一般資訊",
    )
    if not any(marker in text for marker in data_markers):
        return None
    unsafe_markers = (
        "approve", "approval", "confirm", "execute", "trade", "order", "buy", "sell",
        "account", "broker", "授權", "核准", "確認", "執行", "下單", "買進", "賣出", "帳戶", "券商",
    )
    all_option_text = " ".join(
        str(value or "")
        for option in options
        for value in (option.get("option_id"), option.get("label"), option.get("reason"))
    ).casefold()
    if any(marker in all_option_text for marker in unsafe_markers):
        return None
    return selected


def _routine_interaction_completion_reason(
    *,
    turn: dict[str, Any],
    context: AgentRunContext,
    task_kind: str,
    objective: str,
) -> str | None:
    """Close model-created local-task questions with a Host outcome.

    The user describes the outcome they want; they should never need to teach
    a provider how to handle an internal limitation, select a generic next
    step, or repeat a request merely because the provider misclassified it as
    UI, project, or general work.  Therefore this boundary is deliberately
    independent of ``task_kind``.  A provider's task classification is useful
    for selecting tools, but must not decide whether the user is trapped in a
    wait loop.

    The two genuine exceptions stay explicit: an external action may require
    the normal approval protocol, and a user who actually requested broker or
    account setup owns that credential boundary.  Paper orders are handled by
    the preceding Host-owned preview/submit protocol before this guard runs.
    """

    if turn.get("state") not in {"waiting_user_input", "waiting_decision"}:
        return None
    if context.allow_external_actions:
        return None
    # The paper protocol above owns only a *deterministic* local paper order.
    # Paper mode itself must not make every market question a user-input
    # boundary: if issuer resolution or order parameters did not yield one
    # bounded sandbox order, asking the person to teach the model what to do
    # merely recreates the loop this guard exists to prevent.  In that case
    # finish with the verified limitation instead.  A real broker/account
    # setup remains protected by the explicit objective check below.
    if (
        task_kind == "market_decision"
        and context.allow_paper_orders
        and context.state.get("explicit_paper_order_authorized") is True
        and _explicit_paper_order_from_objective(objective, symbols=context.symbols) is not None
    ):
        return None
    interaction = turn.get("interaction")
    interaction = interaction if isinstance(interaction, dict) else {}
    if interaction.get("host_explicit_choice_checkpoint") is True:
        return None
    # Whether an account, credential, or broker is involved is determined by
    # the user's requested outcome, not by a provider sentence.  Small remote
    # models routinely mention a broker setup while answering an ordinary
    # stock-analysis request.  Treating that self-generated caveat as a
    # blocking question is exactly the "tell me how to proceed" loop: the
    # user did not ask to configure an account, and the Host can still finish
    # the bounded analysis with its verified limitations.
    if _objective_requests_user_owned_external_setup(objective):
        return None
    return "host_owned_advisory_or_research_path"


def _objective_requests_user_owned_external_setup(objective: str) -> bool:
    """Return whether the user explicitly asked to configure an external account.

    This intentionally needs both an external-account noun and a setup verb.
    A plain company name such as ``台新`` or a model-generated mention of a
    "broker/account/API key" must not create a user-input requirement for a
    local analysis or paper sandbox.
    """

    text = str(objective or "").casefold()
    account_terms = (
        "broker", "account", "credential", "api key", "token",
        "券商", "帳戶", "帳號", "憑證", "金鑰", "授權碼",
    )
    setup_terms = (
        "connect", "login", "log in", "authorize", "authorise", "configure",
        "連線", "登入", "授權", "設定", "綁定", "連接", "開通",
    )
    return any(term in text for term in account_terms) and any(
        term in text for term in setup_terms
    )


def _host_routine_completion_summary(
    *,
    task_kind: str,
    objective: str,
    trace: list[dict[str, Any]],
) -> str:
    """Replace a provider's internal-question prose with a bounded outcome.

    The user-facing timeline must never repeat instructions such as "tell the
    Agent how to answer".  This receipt is intentionally modest: it reports
    only whether Host tools produced evidence and retains missing information
    as a limitation rather than turning it into a question.
    """

    completed_tools = list(dict.fromkeys(
        str(item.get("tool") or item.get("name") or "")
        for item in trace
        if isinstance(item, dict) and item.get("ok") is True
        and str(item.get("tool") or item.get("name") or "").strip()
    ))
    subject = str(objective or "").strip().replace("\n", " ")[:120] or "這項任務"
    scope = {
        "market_information": "市場分析",
        "current_information": "最新資訊查核",
        "market_decision": "受限市場評估",
    }.get(task_kind, "本機任務")
    if completed_tools:
        return (
            f"Host 已完成「{subject}」的{scope}，並保留 "
            f"{', '.join(completed_tools[:4])} 的可驗證收據。"
            "未能取得的資料已列為限制；不需要你提供系統操作方式、外部驗收或帳戶設定。"
        )
    return (
        f"Host 已結束「{subject}」的{scope}；目前沒有可安全採用的額外證據。"
        "系統已保留限制並停止，不需要你提供系統操作方式、外部驗收或帳戶設定。"
    )


def _resource_boundary_goal_draft(
    *,
    objective: str,
    task_kind: str,
    symbols: tuple[str, ...] | list[str],
    remaining_gaps: list[Any],
) -> dict[str, Any]:
    """Build a visible, non-executing successor after a Host resource stop.

    A fresh provider Run would reset the per-Run budget and repeat the same
    loop.  The UI therefore prepares this successor for the user to send only
    after seeing the exact stop reason and preserved checkpoint.
    """

    reasons = [str(item) for item in remaining_gaps if str(item).strip()]
    reason = "、".join(reasons) or "host_resource_boundary"
    symbol_text = "、".join(str(item) for item in symbols if str(item).strip())
    scope = f"標的：{symbol_text}。" if symbol_text else ""
    return {
        "title": "從已驗證 checkpoint 重建目標",
        "reason_code": reason,
        "requires_user_send": True,
        "objective": (
            f"請接續下列 {task_kind or 'local'} 任務：{objective.strip()}\n"
            f"{scope}先讀取同一工作階段中已通過 Host 驗證的證據與失敗收據，"
            "只規劃尚未完成且可獨立驗證的最小下一步；不要重跑已完成工作、"
            "不要等待外部驗收條件。若資料或資源仍不足，直接輸出原因並結束。"
        ),
    }


def _is_protocol_meta_response(summary: Any) -> bool:
    """Detect a provider message about the Host contract rather than the objective."""

    normalized = " ".join(str(summary or "").casefold().split())
    if not normalized:
        return False
    markers = (
        "user requested a json schema",
        "user requested json schema",
        "requested a json schema",
        "requested json schema",
        "要求 json schema",
        "要求json schema",
        "使用者要求 json",
        "使用者要求json",
        "輸出契約",
        "output contract",
    )
    return any(marker in normalized for marker in markers)


def _is_explicit_artifact_operation(text: str) -> bool:
    """Recognize direct local Artifact mutation requests, not capability preferences.

    A preference such as "保留可點擊修改的 Artifact" describes a desired
    product capability; it must remain a no-tool answer.  Artifact scope is
    granted only when the user directly asks the Host to create or revise one.
    """

    normalized = str(text or "").casefold().strip()
    artifact = r"(?:artifact|產物)"
    mutation = r"(?:建立|創建|產生|生成|寫入|更新|修改|還原|create|generate|write|revise|update|restore)"
    request = r"(?:請|幫我|我要|把|將|對|針對)"
    direct_patterns = (
        rf"{request}.{{0,20}}{mutation}.{{0,24}}{artifact}",
        rf"{request}.{{0,20}}{artifact}.{{0,24}}{mutation}",
        rf"^(?:{mutation}).{{0,24}}{artifact}",
        rf"^(?:{artifact}).{{0,24}}{mutation}",
        r"artifact\.(?:create_text|create|update|revise|restore)",
    )
    return any(re.search(pattern, normalized) for pattern in direct_patterns)


def _should_host_create_explicit_text_artifact(
    *,
    task_kind: str,
    objective: str,
    trace: list[dict[str, Any]],
    turn: dict[str, Any],
    tool_metadata: dict[str, dict[str, Any]],
) -> bool:
    """Compile the smallest safe Artifact mutation when a provider omits it.

    A direct request to create a local text Artifact already supplies both the
    desired mutation and its narrow Host capability.  Leaving that routine
    mutation entirely to a remote model made the native Agent Dock enter a
    no-progress loop even though the only disclosed tool was
    ``artifact.create_text``.  This fallback is deliberately limited to a
    fresh explicit *create* request with no model tool calls and never applies
    to revisions, structured artifacts, market reads, orders, automations, or
    external accounts.
    """

    if task_kind != "artifact_task" or "artifact.create_text" not in tool_metadata:
        return False
    if any(
        item.get("ok") is True
        and str(item.get("name") or item.get("tool") or "").startswith("artifact.")
        for item in trace
        if isinstance(item, dict)
    ):
        return False
    if turn.get("tool_calls"):
        return False
    text = _artifact_public_objective(objective).casefold()
    return bool(
        re.search(
            r"(?:請|幫我|我要|建立|創建|產生|生成).{0,32}"
            r"(?:純文字|文字|text|markdown).{0,32}"
            r"(?:artifact|產物)",
            text,
        )
    )


def _host_text_artifact_call(objective: str) -> dict[str, Any]:
    """Build an auditable, non-speculative local text Artifact request."""

    public_objective = " ".join(_artifact_public_objective(objective).split())
    symbol_match = re.search(r"(?<!\d)(\d{4,6}(?:\.(?:TW|TWO))?)(?!\d)", public_objective, re.IGNORECASE)
    subject = symbol_match.group(1).upper() if symbol_match else "本機"
    title = f"{subject} 研究摘要"
    return {
        "id": f"host-artifact-create-{uuid4().hex}",
        "name": "artifact.create_text",
        "arguments": {
            "name": f"{title}.md",
            "content": (
                f"# {title}\n\n"
                "## 使用者請求\n"
                f"{public_objective}\n\n"
                "## Host 邊界\n"
                "- 此為本機、run-scoped 的純文字 Artifact。\n"
                "- 未新增市場查詢、未建立或送出任何訂單。\n"
                "- 未建立自動化，亦未讀取或操作任何真實帳戶。\n\n"
                "## 證據範圍\n"
                "本 Artifact 只保存本次明確請求與 Host 邊界；未重新驗證的歷史市場資料"
                "不會被寫成新的市場結論。"
            ),
        },
    }


def _artifact_public_objective(objective: str) -> str:
    """Remove private routing headers before persisting user-facing text."""

    value = str(objective or "").strip()
    header = re.compile(r"^\[model_task_kind:[^\]]+\]\s*", re.IGNORECASE)
    while header.search(value):
        value = header.sub("", value, count=1).strip()
    return value


def _grounded_preference_recall_summary(
    objective: str,
    memories: list[dict[str, Any]],
) -> str | None:
    """Return a factual preference-recall answer without granting it rule status."""

    value = str(objective or "").casefold()
    requested = (
        "偏好" in value and any(anchor in value for anchor in ("先前", "之前", "架構", "延續"))
    ) or any(
        phrase in value
        for phrase in ("previous preference", "prior preference", "remembered preference")
    )
    if not requested:
        return None
    for memory in memories:
        if memory.get("kind") != "user_preference" and memory.get("layer") != "user_preference":
            continue
        content = re.sub(
            r"^\s*\[model_task_kind:[^\]]+\]\s*",
            "",
            str(memory.get("content") or ""),
            flags=re.IGNORECASE,
        ).strip()
        if content:
            return f"依據你先前明確記錄的偏好：{content}"
    return None


def _uses_selected_symbol(objective: str) -> bool:
    lowered = str(objective or "").casefold()
    return any(
        phrase in lowered
        for phrase in ("這支股票", "這檔股票", "這一支", "目前選取", "目前這檔", "current stock", "selected symbol")
    )


def _observation_is_substantive(observation: dict[str, Any]) -> bool:
    """Return whether a successful receipt contains usable evidence.

    Analysis-only tools may safely return a validated ``data_blocked``
    workspace.  That is a valid Host receipt, but it is not evidence that the
    user's factual objective has been answered.
    """

    return observation_is_substantive(observation)


def _evidence_requirement_met(task_kind: str, trace: list[dict[str, Any]]) -> bool:
    if task_kind == "general_answer":
        return True
    successful = {
        str(item.get("tool") or item.get("name") or "")
        for item in trace
        if _observation_is_substantive(item)
    }
    if task_kind in {"market_decision", "market_radar"}:
        return any(name.startswith("market.") and name != "market.taifex_foreign_open_interest" for name in successful)
    if task_kind == "market_information":
        return any(name.startswith("market.") for name in successful) or bool(
            successful & {"web.fetch", "web.research"}
        )
    if task_kind == "current_information":
        return bool(successful & {"web.fetch", "web.research"}) or any(
            name.startswith("market.") for name in successful
        )
    if task_kind == "ui_task":
        return any(name.startswith("ui.") for name in successful)
    if task_kind == "artifact_task":
        return any(name.startswith("artifact.") for name in successful)
    if task_kind == "project_task":
        # Tool identity and execution receipts, rather than a hard-coded prefix
        # taxonomy, establish that a project action actually ran.  This keeps
        # custom and external framework tools usable without granting them any
        # additional execution permission.
        return bool(successful)
    return bool(successful)


def _evidence_feedback(
    task_kind: str,
    trace: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
    *,
    objective: str = "",
    completion_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    successful = {
        str(item.get("tool") or item.get("name") or "")
        for item in trace
        if _observation_is_substantive(item)
    }
    failed = [str(item.get("tool") or "") for item in trace if item.get("ok") is False]
    unresolved_failed_nodes = [
        str(item.get("node_id") or item.get("call_id") or item.get("tool") or "")
        for item in trace
        if item.get("ok") is False
        and not any(
            str(link.get("failed_node_id") or "")
            == str(item.get("node_id") or "")
            for successful_item in trace
            if successful_item.get("ok") is True
            for link in (successful_item.get("recovery_for") or [])
            if isinstance(link, dict)
        )
    ]
    unsupported_numeric_claims = sorted(
        {
            str(value)
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "final_answer_numeric_claims_are_evidence_grounded"
            and check.get("passed") is False
            for value in check.get("unsupported_claims") or []
            if str(value).strip()
        },
        key=lambda value: (len(value), value),
    )
    placeholder_measurements = sorted(
        {
            str(value)
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "final_answer_has_no_placeholder_measurements"
            and check.get("passed") is False
            for value in check.get("placeholders") or []
            if str(value).strip()
        },
        key=lambda value: (len(value), value.casefold()),
    )
    scope_check = next(
        (
            check
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "final_answer_covers_requested_scope"
            and check.get("passed") is False
        ),
        None,
    )
    market_coverage_check = next(
        (
            check
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "requested_market_evidence_coverage"
            and check.get("passed") is False
        ),
        None,
    )
    objective_contract_check = next(
        (
            check
            for check in (completion_validation or {}).get("checks") or []
            if isinstance(check, dict)
            and check.get("name") == "objective_completion_contract"
            and check.get("passed") is False
        ),
        None,
    )
    verified_summary = _reasoning_step_summary(
        node_title="本次任務",
        related_trace=trace,
        fallback="Host 已保留本次通過驗證的工具證據。",
    )
    comprehensive_market_request = (
        task_kind in {"market_information", "market_decision", "market_radar"}
        and (
            len(str(objective or "")) >= 48
            or sum(str(objective or "").count(token) for token in ("、", "，", ",", ";", "；")) >= 3
        )
    )
    grounded_rewrite_rule = (
        "請逐項引用下方 Host 事實；數值必須原樣複製，不得四捨五入。"
        "使用者要求的面向若未出現在 Host 事實中（例如法人流向），請明確寫『本次已驗證證據未取得』，"
        "不得用常識、歷史印象或假設補齊。不要猜測 blocker 內容，也不要新增交易價位或操作指令。"
        if comprehensive_market_request
        else ""
    )
    if unresolved_failed_nodes:
        alternatives = {
            failed_tool: _alternative_tools_for_failure(failed_tool, manifest)
            for failed_tool in sorted(set(failed))
            if failed_tool
        }
        message = (
            "先前有可恢復的工具失敗，目標尚未完成。不可回傳 state=complete，也不可只重述失敗。"
            f"未完成的失敗分支：{'、'.join(unresolved_failed_nodes)}。"
            f"請立刻使用不同的替代能力／來源取得可驗證證據：{json.dumps(alternatives, ensure_ascii=False)}；"
            "請依 error action_hints 選擇 choose_alternate_tool 或 alternative_source；"
            "成功的替代呼叫會由 Host 綁定到失敗分支，其他已完成分支會保留。"
        )
    elif objective_contract_check is not None:
        missing = "、".join(
            str(value)
            for value in objective_contract_check.get("missing_requirements") or []
            if str(value).strip()
        )
        message = (
            "目前的工具收據雖然格式有效，但還沒有滿足使用者要求的結果契約，不能停止。"
            f"尚缺：{missing or '目標結果的必要驗證'}。"
            "請先分析缺口並採取下一個可驗證行動；若某個來源失敗，請診斷原因、改用獨立替代來源，"
            "修正後重新執行。取得足夠候選、風險檢查、決策或核准收據前，不得回傳 state=complete。"
        )
    elif unsupported_numeric_claims:
        rendered = "、".join(unsupported_numeric_claims[:12])
        message = (
            "剛才的完成草稿含有未被 Host 證據支持的數值："
            f"{rendered}。請移除這些數值及其衍生結論，或只使用成功工具結果中可核對的數值；"
            "不要重新呼叫已完成的工具。修正後回傳 state=complete。"
            f"{grounded_rewrite_rule}可直接引用的 Host 事實：{verified_summary}"
        )
    elif placeholder_measurements:
        rendered = "、".join(placeholder_measurements[:8])
        message = (
            "剛才的完成草稿以佔位符冒充尚未取得的市場／財務數值："
            f"{rendered}。請移除佔位符與其衍生結論，明確說明資料不足，"
            "或只使用成功工具結果中可核對的值；不要重新呼叫已完成的工具。"
        )
    elif market_coverage_check is not None:
        missing = "、".join(
            str(value)
            for value in market_coverage_check.get("missing_dimensions") or []
        )
        required = "、".join(
            str(value)
            for value in market_coverage_check.get("required_tools") or []
        )
        message = (
            "使用者明確要求的市場證據面向尚未完整，不能只靠既有綜合分析直接完成。"
            f"目前缺少：{missing or '未辨識的必要面向'}。"
            f"請呼叫這些 Host 工具取得獨立證據：{required or '對應的市場證據工具'}；"
            "已完成且通過驗證的工具不要重複呼叫。取得證據後再逐項回答並回傳 state=complete。"
        )
    elif scope_check is not None:
        message = (
            "剛才的完成草稿只有標題或導言，尚未實際回答使用者列出的完整範圍。"
            f"目前回答有 {scope_check.get('answer_characters', 0)} 字、"
            f"{scope_check.get('content_units', 0)} 個內容單元；請根據使用者原始問題逐項形成具體結論、"
            "明確標示無法取得的資料與反方風險，不要用一句『以下是完整分析』代替內容，"
            "也不要重新呼叫已完成的工具。以下是 Host 從已驗證結果整理的可引用事實："
            f"{verified_summary}{grounded_rewrite_rule}"
        )
    elif task_kind != "general_answer" and _evidence_requirement_met(task_kind, trace):
        message = (
            "已有主系統驗證通過的工具證據；若使用者目標已完成，請停止重複呼叫工具，"
            "回傳 state=complete 並以成功的 evidence_ids 完成回答。"
        )
    elif task_kind == "general_answer":
        message = "No tool is required; return state=complete with a direct answer in summary."
    elif "web.search" in successful and not successful.intersection({"web.fetch", "web.research"}):
        message = (
            "搜尋結果只用來發現來源，尚不足以完成答案；請用 web.fetch 打開其中一個直接網址，"
            "或改用 web.research 自動搜尋並讀取多個來源。"
        )
    elif task_kind == "market_decision":
        message = "股票決策尚缺主系統行情證據；請先使用 market.research_pack 或 market.analyze_symbol。"
    elif task_kind == "project_task":
        message = "專案問題尚未讀取實際檔案；請使用 project.search_text、project.list_files 或 project.read_file。"
    elif task_kind == "artifact_task":
        message = "Artifact 尚未取得 Host 建立或讀取收據；請使用 artifact.create_text 或 artifact.list。"
    elif task_kind == "ui_task":
        message = "介面任務尚未有已確認的 UI Bridge 操作結果；請使用 ui.* 工具操作或讀取介面狀態。"
    else:
        message = (
            "即時或來源型問題尚缺已讀取的可靠來源。若使用者只提供公司名稱而沒有可執行代號，"
            "先使用 market.search_taiwan_securities 解析正式代號，再呼叫對應 market.* 工具；"
            "否則使用 web.research。若工具回傳 data_blocked、沒有資料或來源失敗，該結果不算完成證據，"
            "請改寫查詢並嘗試其他來源。"
        )
    return {
        "error": message,
        "failed_tools": failed,
        "verified_evidence_summary": verified_summary,
        "grounded_rewrite_rule": grounded_rewrite_rule,
        "available_tools": [item["name"] for item in manifest],
    }


def _provider_conversation_history(
    session_history: list[dict[str, Any]],
    *,
    task_kind: str,
) -> list[dict[str, Any]]:
    """Keep dialogue continuity without treating old answers as current evidence.

    Current-market tasks often share a Session with earlier Agent answers. Those
    answers are useful conversational context, but a provider can otherwise copy
    their dated numbers into a new answer even when this run's Host trace does
    not support them. The Host retains the original durable history; only the
    model-facing projection replaces assistant prose with an explicit
    non-evidentiary marker.
    """

    current_information_tasks = {
        "market_information",
        "market_decision",
        "market_radar",
        "current_information",
    }
    projected: list[dict[str, Any]] = []
    for item in session_history[-40:]:
        if not isinstance(item, dict):
            continue
        entry = _safe_arguments(item)
        if (
            task_kind in current_information_tasks
            and str(entry.get("role") or "").casefold() == "assistant"
        ):
            entry = {
                **entry,
                "content": {
                    "summary": (
                        "Historical assistant output is conversational context only. "
                        "Do not cite or copy it as evidence; use this run's Host tool results."
                    )
                },
                "source": {
                    **(
                        entry.get("source")
                        if isinstance(entry.get("source"), dict)
                        else {}
                    ),
                    "evidence_scope": "historical_non_evidentiary",
                },
            }
        projected.append(entry)
    return projected


def _result_summary(observation: dict[str, Any]) -> dict[str, Any]:
    if not observation.get("ok"):
        return observation.get("error") or {}
    result = observation.get("result")
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    if result.get("schema_version") == "stock_ai.taifex_foreign_open_interest.v1":
        return {
            "schema_version": result.get("schema_version"),
            "latest_trade_date": result.get("latest_trade_date"),
            "interpretation": result.get("interpretation"),
            "position": result.get("position"),
            "source": result.get("source"),
        }
    if result.get("schema_version") == "open_stock_ai.web_research.v1":
        return {
            "schema_version": result.get("schema_version"),
            "query": result.get("query"),
            "search_result_count": result.get("search_result_count"),
            "source_count": result.get("source_count"),
            "search_providers": result.get("search_providers") or [],
        }
    keys = (
        "schema_version",
        "symbol",
        "count",
        "recommendation_bucket",
        "execution_permission",
        "status",
        "execution_boundary",
    )
    return {key: result.get(key) for key in keys if key in result}


def _evidence_claim_text(observation: dict[str, Any]) -> str:
    summary = _result_summary(observation)
    if not observation.get("ok"):
        message = str(summary.get("message") or summary.get("error") or "工具執行失敗")
        return " ".join(message.split())
    symbol = str(summary.get("symbol") or "").strip()
    if summary.get("schema_version") == "open_stock_ai.web_research.v1":
        query = " ".join(str(summary.get("query") or "").split())
        source_count = int(summary.get("source_count") or 0)
        source_label = f"{source_count} 個來源" if source_count else "外部來源"
        return f"外部研究已取得 {source_label}{f'：{query}' if query else '。'}"
    bucket = str(summary.get("recommendation_bucket") or "").strip()
    if bucket.casefold() == "data_blocked":
        return f"{symbol + '：' if symbol else ''}資料不足，尚無法形成可靠判斷。"
    for key in ("interpretation", "summary", "message", "status"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return f"{symbol + '：' if symbol else ''}{' '.join(value.split())}"
    compact = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    return compact[:240] if compact else "Host 已驗證工具結果。"


def _canonical_tool_evidence(
    *,
    observation: dict[str, Any],
    call: dict[str, Any],
    metadata: dict[str, Any],
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build semantic Evidence records without confusing model, worker and data providers."""

    result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
    tool = str(call.get("name") or "unknown_tool")
    call_id = str(call.get("id") or f"EV-{uuid4().hex}")
    tool_provider = str(metadata.get("provider") or "host")
    observed_at = _now()
    result_provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}

    candidates: list[dict[str, Any]] = []
    if result.get("schema_version") == "open_stock_ai.web_research.v1":
        candidates = [dict(item) for item in result.get("sources") or [] if isinstance(item, dict)]
    elif isinstance(result.get("items"), list) and result.get("items"):
        candidates = [dict(item) for item in result["items"] if isinstance(item, dict)]
    if not candidates:
        candidates = [dict(result)]

    records: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for candidate in candidates:
        source_url = _first_text(
            candidate.get("source_url"),
            candidate.get("final_url"),
            candidate.get("url"),
            result_provenance.get("source_url"),
        )
        source = _first_text(
            candidate.get("source"),
            candidate.get("source_id"),
            candidate.get("provider_id"),
            result_provenance.get("source"),
            result_provenance.get("source_id"),
            source_url,
            f"{tool_provider}:{tool}",
        )
        # Prefer the provider's stable source identity for semantic de-duplication.
        # Official endpoints commonly vary request-only query parameters (for
        # example TWSE ``date=latest`` versus a resolved date) while returning
        # the same published record.  Keep the exact URL in provenance, but do
        # not let those transport details create duplicate Evidence nodes.
        source_key = source or source_url
        # A result may contain many rows from the same publication. Preserve
        # materially different observation dates, but do not flood the graph
        # with duplicate rows from one receipt.
        publication = _first_text(
            candidate.get("published_at"),
            candidate.get("available_at"),
            candidate.get("report_date"),
            candidate.get("trade_date"),
            candidate.get("data_timestamp"),
            result_provenance.get("published_at"),
        )
        claim = _canonical_source_claim(
            candidate=candidate,
            fallback=_evidence_claim_text(observation),
        )
        dedupe_key = f"{source_key}|{publication}|{claim}"
        if dedupe_key in seen_sources:
            continue
        seen_sources.add(dedupe_key)
        identity = json.dumps(
            {
                "run_id": str(run_id or "unscoped"),
                "tool": tool,
                "source": source_key,
                "published_at": publication,
                "claim": claim,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence_id = f"EV-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"
        lowered_source = f"{source} {source_url or ''}".casefold()
        source_role = (
            "primary_official"
            if any(token in lowered_source for token in ("twse", "tpex", "taifex", "regulator", "official"))
            else "independent_web"
            if source_url
            else "host_validated_tool"
        )
        records.append(
            {
                "schema_version": "open_stock_ai.evidence.v1",
                "evidence_id": evidence_id,
                "claim": claim,
                "source_type": tool,
                "source": source,
                "source_url": source_url,
                "tool_provider": tool_provider,
                "worker_id": observation.get("worker_id"),
                "published_at": publication,
                "observed_at": _first_text(
                    candidate.get("observed_at"),
                    candidate.get("acquired_at"),
                    result_provenance.get("observed_at"),
                    observed_at,
                ),
                "freshness": _canonical_freshness(publication, observed_at),
                "confidence": 1.0,
                "source_role": source_role,
                "request_id": call_id,
                "supports": list(candidate.get("supports") or []),
                "contradicts": list(candidate.get("contradicts") or []),
            }
        )
    return records


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _canonical_source_claim(*, candidate: dict[str, Any], fallback: str) -> str:
    title = _first_text(candidate.get("title"), candidate.get("name"))
    snippet = _first_text(candidate.get("search_snippet"), candidate.get("summary"))
    if title and snippet:
        return f"{title}：{snippet}"[:500]
    if title:
        return title[:500]
    return fallback


def _canonical_freshness(published_at: str | None, observed_at: str) -> str:
    if not published_at:
        return "unknown"
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age_hours = max(0.0, (observed - published).total_seconds() / 3600)
    if age_hours <= 24:
        return "current"
    if age_hours <= 168:
        return "recent"
    return "stale"


def _incomplete_run_summary(
    *,
    successful_observations: int,
    completion_validation: dict[str, Any] | None,
) -> str:
    """Return a Host-owned terminal message without leaking rejected drafts."""
    remaining_gaps = list((completion_validation or {}).get("remaining_gaps") or [])
    if successful_observations:
        base = (
            "已完成部分研究，但完成條件尚未全部通過；"
            "目前不把未驗證的模型草稿當作答案。"
        )
    else:
        base = "目前缺少足夠且通過 Host 驗證的證據，未產生最終答案。"
    if remaining_gaps:
        compact_gaps = "、".join(" ".join(str(item).split()) for item in remaining_gaps[:3])
        return f"{base} 尚待補足：{compact_gaps}。已保留可驗證證據與 checkpoint。"
    return f"{base} 已保留可驗證證據與 checkpoint。"


def _display_value(value: Any) -> str:
    if value is None or value == "":
        return "未提供"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        # ``repr`` is Python's shortest round-trip representation.  A generic
        # ``:g`` format silently rounded Host evidence to six significant
        # digits, then the exact-number validator correctly rejected the model
        # for copying that rounded Host-authored value.
        return repr(value)
    return str(value)


def _workspace_evidence_details(workspace: dict[str, Any]) -> str:
    """Expose the exact validated workspace facts needed for grounded synthesis."""

    if not workspace:
        return ""
    portfolio = workspace.get("portfolio_status") or {}
    signal = workspace.get("signal_summary") or {}
    research = workspace.get("research_status") or {}
    risk = workspace.get("risk_summary") or {}
    blockers = [str(value) for value in workspace.get("blockers") or []]
    reason = " ".join(str(signal.get("reason") or "").split())
    details = (
        f"系統投資組合曝險 {_display_value(portfolio.get('total_position_size_pct'))}%，"
        f"標的曝險 {_display_value(portfolio.get('symbol_exposure') or {})}；"
        f"訊號 action={_display_value(signal.get('action'))}、"
        f"rule_score={_display_value(signal.get('rule_score'))}、"
        f"confidence_type={_display_value(signal.get('confidence_type'))}、"
        f"calibrated={_display_value(signal.get('confidence_calibrated'))}；"
        f"研究驗證 passed={_display_value(research.get('passed'))}、"
        f"advisory_ready={_display_value(research.get('advisory_ready'))}；"
        f"RiskEngine approved={_display_value(risk.get('approved'))}，"
        f"reason={_display_value(risk.get('reason'))}；"
        f"實際 blocker codes={','.join(blockers) if blockers else '無'}。"
    )
    if reason:
        details += f" 外部框架證據摘要原文：{reason[:900]}"
    return details


def _reasoning_step_summary(
    *,
    node_title: str,
    related_trace: list[dict[str, Any]],
    fallback: str,
) -> str:
    """Build one plan node's public result from its own Host-owned evidence."""
    completed = [item for item in related_trace if item.get("ok") is True]
    if not completed:
        return fallback
    statements: list[str] = []
    for observation in completed:
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        tool_name = str(
            observation.get("name")
            or observation.get("tool")
            or observation.get("call_id")
            or "Host 工具"
        )
        schema = str(result.get("schema_version") or "")
        symbol = _display_value(result.get("symbol"))
        if schema == "stock_ai.institutional_flow_evidence.v1":
            items = [item for item in result.get("items") or [] if isinstance(item, dict)]
            latest = items[0] if items else {}
            if items:
                statements.append(
                    f"{symbol} 法人證據已通過 Host 驗證：共 {len(items)} 筆，"
                    f"最新交易日 {_display_value(latest.get('trade_date'))}，"
                    f"外資淨額 {_display_value(latest.get('foreign_net'))}、"
                    f"投信淨額 {_display_value(latest.get('trust_net'))}、"
                    f"自營商淨額 {_display_value(latest.get('dealer_net'))}、"
                    f"三大法人合計 {_display_value(latest.get('total_institutional_net'))}，"
                    f"來源 {_display_value(latest.get('source'))}。"
                )
            else:
                statements.append(f"{symbol} 本次已驗證法人來源沒有可用資料。")
            continue
        if schema == "stock_ai.monthly_revenue_evidence.v1":
            items = [item for item in result.get("items") or [] if isinstance(item, dict)]
            latest = items[0] if items else {}
            if items:
                statements.append(
                    f"{symbol} 基本面月營收證據已通過 Host 驗證：共 {len(items)} 筆，"
                    f"最新期間 {_display_value(latest.get('period'))}，"
                    f"當月營收 {_display_value(latest.get('current_revenue'))} "
                    f"{_display_value(latest.get('unit'))}，"
                    f"月增率 {_display_value(latest.get('mom_change_percent'))}%、"
                    f"年增率 {_display_value(latest.get('yoy_change_percent'))}%、"
                    f"來源 {_display_value(latest.get('source'))}。"
                )
            else:
                statements.append(f"{symbol} 本次已驗證月營收來源沒有可用資料。")
            continue
        if schema == "open_stock_ai.agent_research_pack.v1":
            market = result.get("market_price") or {}
            technical = result.get("technical_features") or {}
            events = result.get("recent_events") or result.get("events") or []
            workspace = result.get("pipeline_workspace") or {}
            paper_position = result.get("paper_position")
            event_titles = [
                " ".join(str(item.get("title") or "").split())
                for item in events[:3]
                if isinstance(item, dict) and str(item.get("title") or "").strip()
            ]
            statements.append(
                f"{symbol} 行情與研究證據已通過 Host 驗證："
                f"價格 {_display_value(market.get('price'))}"
                f"（{_display_value(market.get('price_source'))}，"
                f"{_display_value(market.get('source_timestamp'))}），"
                f"SMA20 {_display_value(technical.get('sma_20'))}、"
                f"RSI14 {_display_value(technical.get('rsi_14'))}，"
                f"近期事件 {len(events)} 筆；"
                f"本系統 paper_position={'無持股' if paper_position is None else _display_value(paper_position)}。"
                f"{_workspace_evidence_details(workspace)}"
                + (f" 已讀取事件標題例：{'｜'.join(event_titles)}。" if event_titles else "")
            )
            continue
        if schema == "open_stock_ai.agent_workspace.v1":
            statements.append(
                f"{symbol} 策略與風險檢查已通過 Host 驗證："
                f"研究建議 {_display_value(result.get('recommendation_bucket'))}，"
                f"執行權限 {_display_value(result.get('execution_permission'))}，"
                f"{_workspace_evidence_details(result)}"
            )
            continue
        compact = _result_summary(observation)
        facts = "、".join(
            f"{key}={_display_value(value)}"
            for key, value in compact.items()
            if value not in (None, "", [], {})
        )
        statements.append(
            f"{tool_name} 已通過 Host 驗證"
            + (f"：{facts}。" if facts else "。")
        )
    if statements:
        return " ".join(statements)
    return f"{node_title}所需的 {len(completed)} 份 Host 證據已取得並通過驗證。"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_invocation_receipts(
    activity: list[dict[str, Any]],
    driver: str,
    model_id: str,
) -> list[dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    for event in activity:
        event_type = str(event.get("type") or "")
        call_id = str(event.get("model_call_id") or "")
        if not call_id or event_type not in {
            "model.turn.started",
            "model.turn.completed",
            "model.turn.failed",
        }:
            continue
        receipt = receipts.setdefault(
            call_id,
            {
                "call_id": call_id,
                "provider": str(event.get("driver") or driver),
                "model_id": str(event.get("model") or model_id),
                "status": "not_run",
                "started_at": str(event.get("timestamp") or _now()),
                "completed_at": str(event.get("timestamp") or _now()),
                "error_type": None,
                "error_message": None,
                "raw_output_preserved": False,
            },
        )
        if event_type == "model.turn.started":
            receipt["started_at"] = str(event.get("timestamp") or receipt["started_at"])
        elif event_type == "model.turn.completed":
            receipt["status"] = "succeeded"
            receipt["completed_at"] = str(event.get("timestamp") or _now())
        else:
            receipt["status"] = "failed"
            receipt["completed_at"] = str(event.get("timestamp") or _now())
            receipt["error_type"] = str(event.get("error_type") or "provider_error")
            receipt["error_message"] = str(event.get("error") or "Model invocation failed")
    return list(receipts.values())


def _checkpoint_reference(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Return the audit reference for a durable checkpoint, never its replay data.

    ``CheckpointStore`` is the canonical location for the full plan,
    transcript and trace. Re-emitting that payload as an activity event makes
    every checkpoint grow three additional durable copies (event stream,
    runtime projection and terminal run result). The event only needs a
    stable way to audit the save; recovery resolves the same checkpoint from
    the store.
    """

    return {
        key: checkpoint[key]
        for key in (
            "checkpoint_id",
            "session_id",
            "run_id",
            "plan_revision",
            "sequence",
            "status",
            "created_at",
            "snapshot_hash",
        )
        if checkpoint.get(key) is not None
    }


async def _driver_lifecycle(driver: AgentDriver, method_name: str, argument: Any) -> None:
    method = getattr(driver, method_name, None)
    if not callable(method):
        return
    result = method(argument)
    if inspect.isawaitable(result):
        await result


async def _tool_lifecycle(tools: AgentToolRegistry, method_name: str, argument: Any) -> None:
    method = getattr(tools, method_name, None)
    if method is None:
        return
    result = method(argument)
    if inspect.isawaitable(result):
        await result


async def _tool_prepare(tools: AgentToolRegistry, context: AgentRunContext) -> None:
    method = getattr(tools, "prepare", None)
    if not callable(method):
        return
    result = method(context)
    if inspect.isawaitable(result):
        await result


def _estimate_context_tokens(value: Any) -> int:
    """Use a stable, provider-neutral estimate for Host budget enforcement."""

    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        encoded = str(value)
    return max(1, (len(encoded) + 3) // 4)


def _compact_replay_result(observation: dict[str, Any]) -> dict[str, Any] | None:
    """Keep only small protocol receipts required by the next model turn."""

    result = observation.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("schema_version") == "open_stock_ai.ui_command_result.v1":
        acknowledgement = result.get("acknowledgement")
        return {
            "schema_version": result["schema_version"],
            "acknowledgement": _safe_arguments(acknowledgement)
            if isinstance(acknowledgement, dict)
            else None,
        }
    if result.get("schema_version") in {
        "stock_ai.institutional_flow_evidence.v1",
        "stock_ai.monthly_revenue_evidence.v1",
    }:
        return {
            "schema_version": result.get("schema_version"),
            "symbol": result.get("symbol"),
            "count": result.get("count"),
            "data_status": result.get("data_status"),
            "items": _safe_arguments(list(result.get("items") or [])[:12]),
        }
    return None


def _provider_transcript_v2(
    *,
    package: Any,
    trace: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose only scoped context plus a bounded Host-result handoff.

    Raw tool payloads remain in the durable trace for audit/replay.  A later
    model turn receives a BranchResult-style compression rather than every
    historical payload, preventing a long session from spending its budget on
    its own prior transcript.
    """

    evidence_ids = [
        str(item.get("call_id") or item.get("node_id"))
        for item in trace
        if item.get("ok") is True and (item.get("call_id") or item.get("node_id"))
    ]
    compressed = BranchResultCompressor().compress(
        conclusion=(
            f"Host completed {sum(1 for item in trace if item.get('ok') is True)} "
            "validated observations for the active branch."
        ),
        confidence=1.0,
        raw_outputs=[_result_summary(item) for item in trace[-20:]],
        evidence_ids=evidence_ids[-20:],
        contradictions=[
            str((item.get("error") or {}).get("message") or item.get("tool") or "tool_failed")
            for item in trace[-20:]
            if item.get("ok") is False
        ],
        branch_id=str((package.context.get("active_branch") or {}).get("branch_id") or ""),
    )
    compact: list[dict[str, Any]] = [
        {
            "role": "host",
            "type": "context_package.v2",
            "content": package.to_provider_payload(),
        },
        {
            "role": "host",
            "type": "branch_result.compressed",
            "content": compressed.to_context(),
        },
    ]
    history = next(
        (item for item in transcript if item.get("type") == "conversation_history"),
        None,
    )
    if history is not None:
        # Preserve the user's recent conversation semantics, but cap it before
        # it reaches the model rather than replaying the legacy 40-message tail.
        history_items = list(history.get("content") or [])[-8:]
        compact.append(
            {
                "role": "host",
                "type": "conversation_history",
                "content": _safe_arguments(history_items),
            }
        )
    def append_tool_results() -> None:
        if not trace:
            return
        def provider_result(item: dict[str, Any]) -> Any:
            tool = str(item.get("tool") or "unknown_tool")
            if not is_external_content_tool(tool):
                return _compact_replay_result(item)
            return label_tool_observation(
                _compact_replay_result(item),
                tool=tool,
                call_id=str(item.get("call_id") or "") or None,
                provenance=item.get("validation")
                if isinstance(item.get("validation"), dict)
                else None,
            )

        def provider_summary(item: dict[str, Any]) -> Any:
            tool = str(item.get("tool") or "unknown_tool")
            if not is_external_content_tool(tool):
                return _result_summary(item)
            return label_tool_observation(
                _result_summary(item),
                tool=tool,
                call_id=str(item.get("call_id") or "") or None,
                provenance=item.get("validation")
                if isinstance(item.get("validation"), dict)
                else None,
            )

        compact.append(
            {
                "role": "host",
                "type": "tool_results",
                "content": [
                    {
                        "call_id": item.get("call_id"),
                        "tool": item.get("tool"),
                        "ok": item.get("ok"),
                        "result_summary": provider_summary(item),
                        "result": provider_result(item),
                        "validation": item.get("validation"),
                    }
                    for item in trace[-12:]
                ],
            }
        )

    latest_is_policy_feedback = bool(transcript) and transcript[-1].get("type") == "policy_feedback"
    if latest_is_policy_feedback:
        append_tool_results()
    for item in transcript[-6:]:
        if item.get("type") not in {
            "control_message",
            "interaction_response",
            "information_clarification_auto_continue",
            "policy_feedback",
            "research_plan",
            "critic_evidence_budget_closed",
            "critic_join_completed",
        }:
            continue
        compact.append(
            {
                "role": str(item.get("role") or "host"),
                "type": str(item.get("type")),
                "content": _safe_arguments(item.get("content")),
            }
        )
    if not latest_is_policy_feedback:
        append_tool_results()
    return compact


def _tool_skills(metadata: dict[str, Any], call: dict[str, Any]) -> list[str]:
    skills = [str(value) for value in metadata.get("skills") or []]
    if call.get("name") == "skills.activate":
        selected = str((call.get("arguments") or {}).get("name") or "").strip()
        if selected and selected not in skills:
            skills.append(selected)
    return skills


def _critic_disclosed_tools(
    manifest: list[dict[str, Any]],
    *,
    successful_observations: int,
) -> list[dict[str, Any]]:
    """Apply the Host-owned local capability and evidence budget for a Critic."""

    scoped = [item for item in manifest if item.get("name") != "agent.run_subtasks"]
    if successful_observations >= 3:
        return []
    return scoped


def _completed_critic_observation(trace: list[dict[str, Any]]) -> bool:
    return any(
        item.get("ok") is True
        and item.get("tool") == "agent.run_subtasks"
        and isinstance(item.get("result"), dict)
        and _has_completed_critic_receipt(item["result"])
        for item in trace
    )


def _partition_redundant_critic_calls(
    calls: list[dict[str, Any]],
    *,
    trace: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not _completed_critic_observation(trace):
        return calls, []
    redundant = [item for item in calls if item.get("name") == "agent.run_subtasks"]
    if not redundant:
        return calls, []
    return (
        [item for item in calls if item.get("name") != "agent.run_subtasks"],
        redundant,
    )


def _safe_arguments(value: Any) -> Any:
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            lowered = str(key).casefold()
            if any(token in lowered for token in ("password", "secret", "token", "api_key", "authorization")):
                safe[key] = "[redacted]"
            else:
                safe[key] = _safe_arguments(item)
        return safe
    if isinstance(value, list):
        return [_safe_arguments(item) for item in value]
    return value


def transcript_as_json(transcript: tuple[dict[str, Any], ...]) -> str:
    return json.dumps(list(transcript), ensure_ascii=False, separators=(",", ":"))
