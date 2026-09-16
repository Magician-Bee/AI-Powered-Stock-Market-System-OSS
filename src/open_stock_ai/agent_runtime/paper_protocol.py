from __future__ import annotations

import re
from typing import Any

from .completion_contract import objective_completion_contract
from .completion_policy import _verified_paper_order_summary
from .contracts import AgentRunContext
from .validators import _has_completed_critic_receipt, requested_market_evidence_requirements


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
