"""Per-fill evidence stored atomically with the existing cash ledger entry.

Integrity checks bind a retained payload to one actual paper fill. They do not
certify the market provider, the deployment, or the realism of simulated fills.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "open_stock_ai.paper_fill_evidence.v1"
_IDENTITY_FIELDS = ("fill_id", "order_id", "account_id", "created_at", "symbol", "side")
_NUMBER_FIELDS = ("quantity", "reference_price", "fill_price", "gross_amount", "commission", "tax", "slippage_cost", "net_cash_delta")


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError(f"unsupported_paper_fill_evidence_value:{type(value).__name__}")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_value)


def _fill_facts(fill: Mapping[str, Any]) -> dict[str, Any]:
    facts = {key: fill[key] for key in _IDENTITY_FIELDS}
    if any(not isinstance(value, str) or not value for value in facts.values()):
        raise ValueError("paper_fill_evidence_identity_invalid")
    for key in _NUMBER_FIELDS:
        value = fill[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("paper_fill_evidence_number_invalid")
        facts[key] = float(value)
    return facts


def build_paper_fill_evidence(*, fill: Mapping[str, Any], market_context: dict[str, Any] | None,
                             execution_context: dict[str, Any] | None) -> dict[str, Any]:
    """Create a snapshot from actual fill facts and the Host-only keyword context."""
    if market_context is not None and not isinstance(market_context, dict):
        raise ValueError("paper_fill_market_context_invalid")
    if execution_context is not None and not isinstance(execution_context, dict):
        raise ValueError("paper_fill_execution_context_invalid")
    body = {
        "schema_version": SCHEMA_VERSION, "fill": _fill_facts(fill),
        "market_context": market_context or {}, "execution_context": execution_context or {},
    }
    encoded = _canonical(body)
    # Copy the entire JSON value before returning; later caller mutations must
    # not change the already committed observation or simulation parameters.
    return {**json.loads(encoded), "receipt_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}


def _verification(status: str, *, blockers: list[str] | None = None) -> dict[str, Any]:
    return {
        "integrity_status": status, "source_status": "unknown",
        "execution_model_status": "unknown", "deployment_status": "unknown",
        "receipt_sha256": None, "blockers": blockers or [],
    }


def inspect_paper_fill_evidence(metadata: Any, *, fill: Mapping[str, Any]) -> dict[str, Any]:
    """Verify identity, canonical hash and fill facts; leave provenance unclassified."""
    if not isinstance(metadata, dict):
        return _verification("invalid", blockers=["cash_ledger_metadata_invalid"])
    evidence = metadata.get("fill_evidence")
    if evidence is None:
        return _verification("missing", blockers=["paper_fill_evidence_missing"])
    if not isinstance(evidence, dict) or set(evidence) != {
        "schema_version", "fill", "market_context", "execution_context", "receipt_sha256",
    } or evidence.get("schema_version") != SCHEMA_VERSION:
        return _verification("invalid", blockers=["paper_fill_evidence_schema_invalid"])
    try:
        actual = _fill_facts(fill)
        if metadata.get("fill_id") != actual["fill_id"] or evidence["fill"] != actual:
            return _verification("invalid", blockers=["paper_fill_evidence_identity_or_facts_mismatch"])
        if not isinstance(evidence["market_context"], dict) or not isinstance(evidence["execution_context"], dict):
            return _verification("invalid", blockers=["paper_fill_evidence_context_invalid"])
        for key in ("gross_amount", "commission", "tax", "slippage_cost"):
            if metadata.get(key) != actual[key]:
                return _verification("invalid", blockers=["paper_fill_cash_metadata_mismatch"])
        body = {key: value for key, value in evidence.items() if key != "receipt_sha256"}
        expected_hash = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        if evidence["receipt_sha256"] != expected_hash:
            return _verification("invalid", blockers=["paper_fill_evidence_hash_mismatch"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return _verification("invalid", blockers=["paper_fill_evidence_payload_invalid"])
    context = evidence["execution_context"]
    return {
        **_verification("verified"), "receipt_sha256": expected_hash,
        "execution_model_status": "recorded" if isinstance(context.get("execution_model"), dict) and context["execution_model"] else "unknown",
        "deployment_status": "recorded_host_receipt" if isinstance(context.get("deployment_receipt"), dict) and context["deployment_receipt"] else "unknown",
    }


def project_paper_fill_evidence(fill: Mapping[str, Any], *,
                               ledger_entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Join only this fill's cash metadata, rejecting missing or ambiguous bindings."""
    candidates = []
    malformed = False
    for entry in ledger_entries:
        if (entry.get("account_id") != fill.get("account_id") or entry.get("order_id") != fill.get("order_id")
                or entry.get("entry_type") not in {"paper_buy", "paper_sell"}):
            continue
        try:
            metadata = json.loads(entry["metadata_json"])
        except (KeyError, TypeError, ValueError):
            malformed = True
            continue
        if not isinstance(metadata, dict):
            malformed = True
            continue
        evidence = metadata.get("fill_evidence")
        bound_fill = evidence.get("fill") if isinstance(evidence, dict) else None
        if (metadata.get("fill_id") == fill.get("fill_id")
                or isinstance(bound_fill, dict) and bound_fill.get("fill_id") == fill.get("fill_id")):
            candidates.append((metadata, entry))
    if malformed:
        verification = _verification("invalid", blockers=["cash_ledger_metadata_invalid"])
    elif len(candidates) > 1:
        verification = _verification("invalid", blockers=["paper_fill_evidence_ambiguous_cash_entries"])
    elif not candidates:
        verification = _verification("missing", blockers=["paper_fill_cash_metadata_missing"])
    else:
        metadata, entry = candidates[0]
        verification = inspect_paper_fill_evidence(metadata, fill=fill)
        if verification["integrity_status"] == "verified" and (
            entry.get("amount") != fill.get("net_cash_delta") or entry.get("created_at") != fill.get("created_at")
            or entry.get("entry_type") != ("paper_buy" if fill.get("side") == "buy" else "paper_sell")
        ):
            verification = _verification("invalid", blockers=["paper_fill_cash_entry_mismatch"])
    return {
        "fill_evidence": candidates[0][0]["fill_evidence"] if verification["integrity_status"] == "verified" else None,
        "fill_evidence_verification": verification,
    }
