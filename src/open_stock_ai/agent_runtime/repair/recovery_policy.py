from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..autonomy_contract import CAMPAIGN_MUTATIONS, campaign_execution_authorized, valid_campaign_mutation
from ..plan_graph import PlanGraph


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
    if call.get("name") == "autonomy.status":
        boundary, _ = _latest_campaign_mutation(trace)
        trace = trace[boundary + 1:]  # A pre-mutation account snapshot is stale.
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


def _latest_campaign_mutation(trace: list[dict[str, Any]]) -> tuple[int, str | None]:
    for index in range(len(trace) - 1, -1, -1):
        item = trace[index]
        result = item.get("result") or item.get("result_summary") or {}
        if (item.get("tool") in CAMPAIGN_MUTATIONS and item.get("ok") is True
                and (item.get("validation") or {}).get("passed") is True
                and valid_campaign_mutation(item["tool"], item.get("arguments") or {}, result)):
            return index, result["campaign_receipt_id"]
    return -1, None


def _prior_repeated_tool_failure(
    trace: list[dict[str, Any]],
    call: dict[str, Any],
    *, context: Any = None,
) -> dict[str, Any] | None:
    """Find a failed call whose source must not be retried in this Run.

    Exact arguments are blocked except one bounded retry of transient research
    or a proven pre-service scope rejection under corrected Host authority. Once a tool's own retry policy has
    exhausted a source/transport failure, cosmetic query rewrites must also
    move to an alternate source.  Argument-validation failures remain eligible
    for a genuine argument rewrite.
    """

    if (_autonomy_research_retry_available(trace, call) or _autonomy_activation_retry_available(trace, call, context)
            or _autonomy_proposal_retry_available(trace, call, context)):
        return None
    for item in reversed(trace):
        if item.get("ok") is not False or item.get("tool") != call.get("name"):
            continue
        if call.get("name") == "autonomy.research" and (call.get("arguments") or {}).get("cycle_id"):
            if (item.get("arguments") or {}).get("cycle_id") != call["arguments"]["cycle_id"]:
                continue  # A retained-cycle readback is not another failed scan.
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


def _autonomy_research_retry_available(trace: list[dict[str, Any]], call: dict[str, Any]) -> bool:
    if call.get("name") != "autonomy.research" or (call.get("arguments") or {}).get("cycle_id"):
        return False
    failures = [item for item in trace if item.get("tool") == "autonomy.research"
                and item.get("ok") is False and not (item.get("arguments") or {}).get("cycle_id")]
    return (len(failures) == 1 and (failures[0].get("error") or {}).get("retryable") is True
            and (failures[0].get("error") or {}).get("category") in {"timeout", "transport", "rate_limit", "api_500"})


def _activation_scope_rejected(failure: dict[str, Any]) -> bool:
    error = failure.get("error") or {}
    # This exact provider guard runs before service construction or mutation.
    # No other execution error (especially an uncertain mutation) can use it.
    return (failure.get("tool") == "autonomy.activate" and failure.get("ok") is False
            and failure.get("result") is None and error.get("category") == "execution_failure"
            and error.get("retryable") is False and error.get("exception_type") in {"PermissionError", "WorkerToolError"}
            and error.get("message") == "instrument_mandate_cannot_activate_whole_account_campaign: persistent activation and future model reviews require a whole-market mandate")


def _autonomy_activation_retry_available(trace: list[dict[str, Any]], call: dict[str, Any], context: Any = None) -> bool:
    if (context is None or not campaign_execution_authorized(context) or context.symbols
            or call.get("name") != "autonomy.activate"):
        return False
    attempts = [item for item in trace if item.get("tool") == "autonomy.activate"]
    return (len(attempts) == 1 and _activation_scope_rejected(attempts[0])
            and attempts[0].get("arguments") == call.get("arguments"))


def _proposal_evidence_rejected(failure: dict[str, Any]) -> bool:
    error = failure.get("error") or {}
    # This exact retained_records rejection precedes plan creation. An unknown
    # outcome, broker failure or arbitrary execution error is never retryable here.
    return (failure.get("tool") == "autonomy.propose_plan" and failure.get("ok") is False
            and failure.get("result") is None and error.get("category") == "execution_failure"
            and error.get("retryable") is False and error.get("exception_type") in {"ValueError", "WorkerToolError"}
            and error.get("message") == "retained_campaign_evidence_not_found")


def _autonomy_proposal_retry_available(trace: list[dict[str, Any]], call: dict[str, Any], context: Any = None) -> bool:
    if context is None or not campaign_execution_authorized(context) or call.get("name") != "autonomy.propose_plan":
        return False
    args = call.get("arguments") or {}
    if context.symbols and str(args.get("symbol") or "").upper() not in context.symbols:
        return False
    attempts = [item for item in trace if item.get("tool") == "autonomy.propose_plan" and item.get("arguments") == args]
    return len(attempts) == 1 and _proposal_evidence_rejected(attempts[0])


def _prepare_recovery_retry_calls(calls: list[dict[str, Any]], trace: list[dict[str, Any]], *, context: Any = None) -> list[dict[str, Any]]:
    # The failed/incorrectly completed original node stays immutable. A Host
    # attempt identity creates one new executable node with the same arguments.
    prepared = [{**call, "_host_research_retry": 1} if _autonomy_research_retry_available(trace, call)
            else {**call, "_host_activation_scope_retry": 1} if _autonomy_activation_retry_available(trace, call, context)
            else {**call, "_host_proposal_evidence_retry": 1} if _autonomy_proposal_retry_available(trace, call, context)
            else call for call in calls]
    _, receipt_id = _latest_campaign_mutation(trace)
    # One new read node per verified mutation, stable across restart. Further
    # reads without another state change retain normal plan deduplication.
    return [{**call, "_host_status_after_mutation": receipt_id}
            if call.get("name") == "autonomy.status" and receipt_id else call for call in prepared]


def _retained_cycle_result(result: dict[str, Any], failure: dict[str, Any]) -> bool:
    cycle_id = result.get("cycle_id")
    requested = (failure.get("arguments") or {}).get("cycle_id")
    return (result.get("schema_version") == "open_stock_ai.autonomous_research_cycle.v1"
            and isinstance(cycle_id, str) and bool(cycle_id) and (not requested or requested == cycle_id))


def _recovered_failure_nodes(trace: list[dict[str, Any]]) -> set[str]:
    failures = {str(item.get("node_id") or ""): item for item in trace if item.get("ok") is False}
    recovered = set()
    for item in trace:
        if item.get("ok") is not True:
            continue
        for link in item.get("recovery_for") or []:
            if not isinstance(link, dict):
                continue
            node_id = str(link.get("failed_node_id") or "")
            failure = failures.get(node_id, {})
            if failure.get("tool") == "autonomy.research" and not (
                item.get("tool") == "autonomy.research"
                and _retained_cycle_result(item.get("result") or item.get("result_summary") or {}, failure)
            ):
                continue  # Legacy web evidence cannot substitute a retained campaign cycle.
            if failure.get("tool") == "autonomy.activate" and not (
                _activation_scope_rejected(failure) and item.get("tool") == "autonomy.activate"
                and item.get("arguments") == failure.get("arguments")
                and valid_campaign_mutation("autonomy.activate", failure.get("arguments") or {},
                                            item.get("result") or item.get("result_summary") or {})
            ):
                continue
            if failure.get("tool") == "autonomy.propose_plan" and not (
                _proposal_evidence_rejected(failure) and item.get("tool") == "autonomy.propose_plan"
                and item.get("arguments") == failure.get("arguments")
                and valid_campaign_mutation("autonomy.propose_plan", failure.get("arguments") or {},
                                            item.get("result") or item.get("result_summary") or {})
            ):
                continue
            recovered.add(node_id)
    return recovered


def _restore_campaign_recovery_disclosure(context: Any, transcript: list[dict[str, Any]], trace: list[dict[str, Any]] = ()) -> None:
    if context.state.get("explicit_autonomous_campaign_authorized") is not True:
        return
    context.state.pop("recovery_tool_surface", None)
    context.state.pop("retired_recovery_tools", None)
    restored_tools = {"autonomy.research"}
    if any(_autonomy_activation_retry_available(trace, {"name": "autonomy.activate", "arguments": item.get("arguments")}, context) for item in trace):
        restored_tools.add("autonomy.activate")
    if any(_autonomy_proposal_retry_available(trace, {"name": "autonomy.propose_plan", "arguments": item.get("arguments")}, context) for item in trace):
        restored_tools.add("autonomy.propose_plan")
    for item in list(transcript):
        if item.get("role") == "host" and item.get("type") == "retired_recovery_tools":
            content = item.get("content") or {}
            remaining = [name for name in content.get("tools", []) if name not in restored_tools]
            if remaining:
                item["content"] = {**content, "tools": remaining}
            else:
                transcript.remove(item)


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

    if failed_tool == "autonomy.research":
        available = {item.get("name") for item in manifest}
        return [name for name in ("autonomy.status", "autonomy.research", "autonomy.evidence", "system.capabilities") if name in available]
    if failed_tool == "autonomy.activate":
        return [item["name"] for item in manifest if item.get("name") in {"autonomy.status", "system.capabilities"}]
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

    recovered_nodes = _recovered_failure_nodes(trace)
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

    recovered_nodes = _recovered_failure_nodes(trace)
    return {
        str(item.get("tool") or "")
        for item in trace
        if item.get("ok") is False
        and isinstance(item.get("recovery"), dict)
        and str(item.get("node_id") or "") in recovered_nodes
        and str(item.get("tool") or "")
        and item.get("tool") not in {"autonomy.research", "autonomy.activate", "autonomy.propose_plan"}
    }


def _recovery_tool_surface(
    trace: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
    *, context: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the bounded L4/L5 tool surface for unresolved local failures.

    This remains data-driven: alternatives come from the current capability
    manifest and each failed tool's declared category, rather than a stock- or
    provider-specific fallback table.  Mutating, paper-execution and approval
    tools are excluded, except one authorized retry of a proven pre-mutation
    scope or retained-evidence rejection.
    """

    unresolved = _unresolved_recovery_failures(trace)
    if not unresolved:
        return [], []
    allowed_names: set[str] = set()
    requirements: list[dict[str, Any]] = []
    activation_retry = False
    proposal_retry = False
    for failure in unresolved:
        failed_tool = str(failure.get("tool") or "")
        if not failed_tool:
            continue
        alternatives = _alternative_tools_for_failure(failed_tool, manifest)
        if _autonomy_activation_retry_available(trace, {"name": failed_tool, "arguments": failure.get("arguments")}, context):
            alternatives.append("autonomy.activate")
            activation_retry = True
        if _autonomy_proposal_retry_available(trace, {"name": failed_tool, "arguments": failure.get("arguments")}, context):
            alternatives.append("autonomy.propose_plan")
            proposal_retry = True
        allowed_names.update(alternatives)
        requirements.append(
            {
                "failed_node_id": str(failure.get("node_id") or ""),
                "failed_tool": failed_tool,
                "alternatives": alternatives,
                **({"requires_retained_cycle": True, "maximum_transient_scan_retries": 1}
                   if failed_tool == "autonomy.research" else {}),
                **({"requires_activation_receipt": True, "maximum_scope_guard_retries": 1,
                    "scope_guard_retry_available": activation_retry} if failed_tool == "autonomy.activate" else {}),
                **({"requires_plan_receipt": True, "maximum_evidence_resolution_retries": 1,
                    "evidence_retry_available": proposal_retry} if failed_tool == "autonomy.propose_plan" else {}),
            }
        )
    if not allowed_names:
        return [], requirements
    surface = [
        item
        for item in manifest
        if str(item.get("name") or "") in allowed_names
        and ((activation_retry and item.get("name") == "autonomy.activate")
             or (proposal_retry and item.get("name") == "autonomy.propose_plan")
             or (not bool(item.get("mutating")) and not bool(item.get("requires_paper_execution"))))
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
            if signature in attempted and not _autonomy_research_retry_available(trace, {"name": tool_name, "arguments": arguments}):
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
    previous = failure.get("arguments")
    previous = dict(previous) if isinstance(previous, dict) else {}
    if metadata.get("name") == "autonomy.research":
        return {key: previous[key] for key in ("deep_limit", "cycle_id") if key in previous and key in properties}
    symbol = _recovery_symbol(previous, symbols, objective)
    query = _recovery_query(previous, objective)
    required = schema.get("required") or []
    if not isinstance(required, list):
        return None
    alternatives = schema.get("oneOf")
    if alternatives is not None:
        if not isinstance(alternatives, list):
            return None
        required_sets = []
        for option in alternatives:
            option_required = option.get("required") if isinstance(option, dict) else None
            if not isinstance(option_required, list):
                continue
            required_sets.append(list(dict.fromkeys([*required, *option_required])))
        def fillable(fields):
            return all(
                isinstance(field, str) and (
                    previous.get(field) not in (None, "", [], {})
                    or field == "symbol" and bool(symbol)
                    or field == "symbols" and bool(symbol)
                    or field == "query" and bool(query)
                )
                for field in fields
            )
        required = next((fields for fields in required_sets if fillable(fields)), None)
        if required is None:
            return None
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
    context: Any = None,
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
    already_recovered = _recovered_failure_nodes(trace)
    recovered: list[dict[str, str]] = []
    current_name = str(call.get("name") or "")
    current_id = str(call.get("id") or "")
    for item in trace:
        if item.get("ok") is not False:
            continue
        failed_tool = str(item.get("tool") or "")
        activation_recovery = (failed_tool == current_name == "autonomy.activate"
                               and _autonomy_activation_retry_available(trace, call, context)
                               and valid_campaign_mutation(current_name, call.get("arguments") or {}, result or {}))
        proposal_recovery = (failed_tool == current_name == "autonomy.propose_plan"
                             and item.get("arguments") == call.get("arguments")
                             and _proposal_evidence_rejected(item)
                             and _autonomy_proposal_retry_available(trace, call, context)
                             and valid_campaign_mutation(current_name, call.get("arguments") or {}, result or {}))
        cycle_recovery = (failed_tool == current_name == "autonomy.research"
                          and _retained_cycle_result(result or {}, item))
        if not failed_tool or (failed_tool == current_name and not (cycle_recovery or activation_recovery or proposal_recovery)):
            continue
        if not (activation_recovery or proposal_recovery) and current_name not in _alternative_tools_for_failure(failed_tool, manifest):
            continue
        if not (cycle_recovery or activation_recovery or proposal_recovery) and (failed_tool in {"autonomy.research", "autonomy.activate", "autonomy.propose_plan"} or not _recovery_scope_is_relevant(
            failed=item,
            call=call,
            result=result or {},
            objective=objective,
            symbols=symbols,
        )):
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
    recovered_nodes = _recovered_failure_nodes(trace)
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
            if failed_node_id in failed_node_ids and failed_node_id in recovered_nodes and evidence_ids:
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

    recovered = _recovered_failure_nodes(trace)
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


def _hash_payload(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
