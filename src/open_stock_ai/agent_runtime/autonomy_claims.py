"""Bound explicit campaign outcome claims to the relevant Host receipts.

Activation and a waiting order are valid outcomes. They do not imply a fill,
positive expectancy qualification, or a guarantee of future profitability.
This narrow check does not attempt unrestricted natural-language entailment.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable


_FILL = re.compile(r"(?:已(?:經)?|成功|實際|完成)(?:完成|成功|實際)?(?:紙上|模擬|實盤|真實)?(?:成交|買進|賣出|建倉|平倉)"
                   r"|(?:紙上|模擬|實盤)?(?:成交|買進|賣出|建倉|平倉)(?:了|成功|完成)"
                   r"|\b(?:order|trade|position)\s+(?:was|has been|is)\s+(?:filled|executed|opened|closed)"
                   r"|\b(?:order|trade)\s+(?:filled|executed)\b"
                   r"|\b(?:filled|executed)\s+(?:(?:an?|the|our)\s+)?(?:order|trade)\b", re.I)
_EV_TERM = r"(?:正(?:的)?期望值|正期望資格|正\s*EV|positive[ -]*(?:EV|expectancy|expected value))"
_EV = re.compile(r"(?:已|確認|確定|證實|證明|通過|達到|具有|具備|擁有|符合|取得|獲得|\bhas\b|\bhave\b|\bproven\b|\bconfirmed\b|\bverified\b).{0,20}" + _EV_TERM
                 + r"|" + _EV_TERM + r".{0,12}(?:成立|合格|已證實|已確認|qualified|proven|verified|confirmed)", re.I)
_PROFIT_PROMISE = re.compile(r"(?:能夠|可以|能|會|保證|確保).{0,8}(?:持續|穩定|一直)賺錢"
                             r"|\b(?:guaranteed|will always|will consistently)\s+(?:profits?|profitability|make money)\b", re.I)
_NEGATED = re.compile(r"(?:尚未|還未|未能|不能|無法|不得|不可|不應|並非|不是|不足|未|沒有|避免|不代表|不保證|不)"
                     r".{0,10}$|\b(?:not|never|no|cannot|can't|insufficient|unproven)\b.{0,24}$", re.I)
_EV_CONDITION = re.compile(
    r"^\s*(?:[-*]\s*)?(?:(?:重新|再)?評估|進場|執行|啟動|前置|必要)條件\s*[：:]"
    r"|^\s*(?:[-*]\s*)?(?:reassessment|re-entry|entry|prerequisite) conditions?\s*:"
    r"|(?:^|[，,：:]\s*)(?:如果|假如|倘若|若|等到|一旦)"
    r"|\b(?:if|once|provided that)\b", re.I)
_EV_FUTURE_REQUIREMENT = re.compile(
    r"^\s*(?:[-*]\s*)?(?:希望|目標(?:是|為)?|預計|仍需(?:要)?|需要|尚待|待|未來(?:需(?:要)?|須|將)|將來(?:需(?:要)?|須|將))"
    r"|^\s*(?:[-*]\s*)?(?:we\s+)?(?:aim to|need to|will need to)\b", re.I)
_EV_ASSERTION_RESET = re.compile(
    r"但是|但|然而|不過|實際上|事實上|其實|目前|現在|已|屆時|才會|就會|那麼"
    r"|\.(?=\s|$)|\b(?:but|however|currently|in fact|already|then)\b", re.I)


def _future_ev_requirement(clause_before: str, sentence_before: str, matched: str) -> bool:
    # Only EV qualification inherits an explicit condition within this sentence.
    # A comma/conjunction must not discard "重新評估條件：...且策略通過正EV".
    # Achieved/current assertions and condition consequents end that scope.
    context = sentence_before + clause_before
    conditions = list(_EV_CONDITION.finditer(context))
    if conditions and not _EV_ASSERTION_RESET.search(context[conditions[-1].end():] + matched):
        return True
    requirement = _EV_FUTURE_REQUIREMENT.search(clause_before)
    return bool(requirement and not _EV_ASSERTION_RESET.search(clause_before[requirement.end():] + matched))


def _claim_clauses(text: str):
    for sentence in re.split(r"[。！？!?\n；;]", text):
        start = 0
        for delimiter in re.finditer(r"[，,]|(?:但是|但|而且|且)|\b(?:but|and)\b", sentence, re.I):
            yield sentence[start:delimiter.start()].strip(), sentence[:start]
            start = delimiter.end()
        yield sentence[start:].strip(), sentence[:start]


def _positive(pattern: re.Pattern, clause: str, *, sentence_before: str = "") -> bool:
    for match in pattern.finditer(clause):
        before = clause[max(0, match.start()-28):match.start()]
        if _NEGATED.search(before) or re.search(r"(?:尚未|未能|無法|並非|不是|不足|不代表|尚無|並未|不成立|不合格)|\b(?:not|never|cannot|can't|unproven)\b", match.group(), re.I):
            continue
        # A request, condition or plan is not a claim of an achieved result.
        if re.search(r"(?:若|如果|假如|希望|目標|預計|需要|仍需|才能|待).{0,10}$|\b(?:if|aim to|need to)\s*$", before, re.I):
            continue
        if pattern is _EV and _future_ev_requirement(clause[:match.start()], sentence_before, match.group()):
            continue
        return True
    return False


def _dicts(value: Any, depth: int = 0):
    if depth > 20:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _dicts(item, depth+1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _dicts(item, depth+1)


def _paper_fill_symbols(rows: list[dict[str, Any]]) -> set[str]:
    from .autonomy_contract import valid_campaign_mutation

    accounts = {row["result"]["account_id"] for row in rows
                if row.get("tool", row.get("name")) == "autonomy.activate" and isinstance(row.get("result"), dict)
                and valid_campaign_mutation("autonomy.activate", {"cycle_id": row["result"].get("cycle_id")}, row["result"])}
    if not accounts:
        accounts = {row["result"]["account_id"] for row in rows
                    if row.get("tool", row.get("name")) == "autonomy.status" and isinstance(row.get("result"), dict)
                    and row["result"].get("mode") == "paper" and row["result"].get("account_id")}
    symbols = set()
    for row in rows:
        if row.get("tool", row.get("name")) not in {
            "autonomy.status", "autonomy.activate", "autonomy.manage", "autonomy.close_plan", "paper.submit_order",
        }:
            continue
        for item in _dicts(row.get("result")):
            if item.get("schema_version") != "open_stock_ai.paper_broker_result.v1":
                continue
            order = item.get("order") or {}
            if not isinstance(order, dict):
                continue
            fill = order.get("fill") or {}
            try:
                valid = (isinstance(fill, dict) and fill.get("fill_id") and order.get("account_id") in accounts
                         and fill.get("account_id") == order["account_id"]
                         and fill.get("order_id") == order.get("order_id")
                         and fill.get("symbol") == order.get("symbol")
                         and float(order.get("filled_quantity", 0)) >= float(fill.get("quantity", 0)) > 0
                         and math.isfinite(float(fill["quantity"])) and math.isfinite(float(fill["fill_price"]))
                         and float(fill["fill_price"]) > 0)
            except (TypeError, ValueError, KeyError, OverflowError):
                valid = False
            if valid:
                symbols.add(str(order["symbol"]).upper())
    return symbols


def _qualifications(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from open_stock_ai.execution.trading_plan import content_hash
    from open_stock_ai.research.candle_qualification import verify_qualification_receipt

    qualified = []
    for row in rows:
        tool = row.get("tool", row.get("name"))
        payload = row.get("result")
        if tool == "autonomy.evidence":
            if not (isinstance(payload, dict) and payload.get("kind") == "qualification"
                    and re.fullmatch(r"AE-[0-9a-f]{64}", str(payload.get("evidence_id") or ""))
                    and payload.get("is_partial_view") is not True
                    and isinstance(payload.get("payload_view"), dict)):
                continue
            try:
                if content_hash(payload["payload_view"]) != payload.get("retained_payload_sha256"):
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            payload = payload["payload_view"]
        elif tool != "autonomy.research":
            continue
        for item in _dicts(payload):
            if item.get("schema_version") != "open_stock_ai.candle_qualification.v1":
                continue
            symbol, strategy, version = item.get("symbol"), item.get("candidate_id"), item.get("strategy_version_hash")
            source = item.get("data_evidence")
            if (symbol and strategy and version and isinstance(source, dict) and source.get("symbol") == symbol
                    and verify_qualification_receipt(item, strategy_id=strategy, strategy_version_hash=version)):
                qualified.append(item)
    return qualified


def campaign_summary_claims_check(*, objective: str, task_kind: str | None, final_summary: str | None,
                                  observations: Iterable[dict[str, Any]]) -> dict[str, Any]:
    from .completion_contract import objective_completion_contract

    applies = objective_completion_contract(objective, task_kind).get("autonomous_pipeline_requested") is True
    result = {"name": "autonomous_campaign_summary_claims_are_grounded", "passed": True, "applies": applies,
              "blockers": [], "scope": "activation_and_waiting_are_not_fill_or_positive_EV_receipts"}
    if not applies or not final_summary:
        return result
    rows = [row for row in observations if isinstance(row, dict) and row.get("ok") is True
            and isinstance(row.get("validation"), dict) and row["validation"].get("passed") is True]
    text = str(final_summary)
    clauses = _claim_clauses(text)
    fill_symbols = _paper_fill_symbols(rows)
    paper_label = bool(re.search(r"紙上|模擬|\bpaper\b|\bsimulat", text, re.I))
    qualifications = None
    for clause, sentence_before in clauses:
        if _positive(_FILL, clause):
            mentioned = {s.upper() for s in re.findall(r"(?<![A-Z0-9])\d{4,6}\.(?:TWO|TW)(?![A-Z0-9])", clause, re.I)}
            bare = set(re.findall(r"(?<!\d)(\d{4,6})\s*(?=紙上|模擬|股票)", clause))
            if (not fill_symbols or mentioned and not mentioned.issubset(fill_symbols)
                    or bare and not bare.issubset({s.split('.')[0] for s in fill_symbols})):
                result["blockers"].append("campaign_fill_claim_requires_matching_broker_fill")
            if not paper_label or re.search(r"實盤|真實(?:帳戶|成交)|\blive\b|\breal\s+(?:fill|trade|order)", clause, re.I):
                result["blockers"].append("campaign_fill_claim_must_identify_paper_execution")
        if _positive(_EV, clause, sentence_before=sentence_before):
            qualifications = _qualifications(rows) if qualifications is None else qualifications
            if not any(str(q["symbol"]).casefold() in clause.casefold() and str(q["candidate_id"]).casefold() in clause.casefold()
                       for q in qualifications):
                result["blockers"].append("positive_ev_claim_requires_verified_symbol_and_strategy_qualification")
        if _positive(_PROFIT_PROMISE, clause):
            result["blockers"].append("observed_results_cannot_guarantee_continuing_profit")
    result["blockers"] = list(dict.fromkeys(result["blockers"]))
    result["passed"] = not result["blockers"]
    return result
