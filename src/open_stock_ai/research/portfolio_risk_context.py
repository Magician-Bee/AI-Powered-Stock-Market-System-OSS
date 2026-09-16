from __future__ import annotations

"""Materialize one point-in-time portfolio risk context.

Portfolio sizing, optimization and constraint checks must consume the same
snapshot.  This builder is the boundary between PIT feature rows and those
engines: it rejects un-timestamped, future, mixed-manifest or unverified
inputs, then emits a deterministic context receipt.  It deliberately has no
order authority and does not fetch data or invent missing factors.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from statistics import fmean
from typing import Any, Mapping, Sequence


_SCHEMA = "open_stock_ai.pit_portfolio_risk_context.v1"
_SESSION_INPUT_SCHEMA = "open_stock_ai.portfolio_risk_materialization_input.v1"
_SIZING_FIELDS = (
    "annualized_volatility_pct",
    "expected_win_probability",
    "expected_win_pct",
    "expected_loss_pct",
    "max_loss_pct",
    "risk_budget_pct",
)
_METADATA_FIELDS = (
    "issuer_id",
    "industry",
    "currency",
    "broker_id",
    "account_alias",
)
_POLICY_FIELDS = (
    "account_equity",
    "max_participation_rate",
    "max_days_to_liquidate",
    "cvar_confidence",
    "max_cvar_pct",
    "max_stress_loss_pct",
)
_FORBIDDEN_POLICY_KEYS = {
    "secret",
    "token",
    "password",
    "api_key",
    "access_token",
    "private_key",
}


class PortfolioRiskContextError(ValueError):
    """Raised only for programmer-invalid arguments; data gaps are receipts."""


class PortfolioRiskContextStore:
    """Durable, immutable storage for PIT portfolio-risk receipts.

    The builder remains responsible for deciding whether a context is
    verified. This store makes both verified and withheld decisions
    restart-safe, and re-verifies the content hash whenever a receipt is
    loaded before it can reach a risk engine.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists portfolio_risk_contexts (
                    risk_context_receipt_sha256 text primary key,
                    schema_version text not null,
                    status text not null,
                    as_of text not null,
                    dataset_manifest_hash text not null,
                    payload_json text not null
                );
                create trigger if not exists portfolio_risk_contexts_immutable_update
                before update on portfolio_risk_contexts begin
                    select raise(abort, 'portfolio risk contexts are immutable');
                end;
                create trigger if not exists portfolio_risk_contexts_immutable_delete
                before delete on portfolio_risk_contexts begin
                    select raise(abort, 'portfolio risk contexts are immutable');
                end;
                """
            )

    def save(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a verified or withheld context exactly once."""

        normalized = self._validate(context)
        receipt = str(normalized["risk_context_receipt_sha256"])
        payload_json = _canonical_json(normalized)
        with self._connect() as connection:
            existing = connection.execute(
                "select payload_json from portfolio_risk_contexts where risk_context_receipt_sha256=?",
                (receipt,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != payload_json:
                    raise ValueError("portfolio_risk_context_receipt_is_bound_to_different_evidence")
                return normalized
            connection.execute(
                """insert into portfolio_risk_contexts(
                    risk_context_receipt_sha256, schema_version, status, as_of,
                    dataset_manifest_hash, payload_json
                ) values (?, ?, ?, ?, ?, ?)""",
                (
                    receipt,
                    normalized["schema_version"],
                    normalized["status"],
                    str(normalized.get("as_of") or ""),
                    str(normalized.get("dataset_manifest_hash") or ""),
                    payload_json,
                ),
            )
        return normalized

    put = save

    def by_receipt(self, receipt: str) -> dict[str, Any] | None:
        """Load one receipt and fail closed if the database row was altered."""

        key = str(receipt or "").lower().strip()
        if not _is_sha256(key):
            return None
        with self._connect() as connection:
            row = connection.execute(
                "select payload_json from portfolio_risk_contexts where risk_context_receipt_sha256=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row[0]))
        except json.JSONDecodeError as exc:
            raise ValueError("portfolio_risk_context_payload_invalid_json") from exc
        return self._validate(value)

    def contexts(self) -> list[dict[str, Any]]:
        """Return every stored receipt in deterministic replay order."""

        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from portfolio_risk_contexts order by as_of, risk_context_receipt_sha256"
            ).fetchall()
        return [self._validate(json.loads(str(row[0]))) for row in rows]

    @staticmethod
    def _validate(context: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(context, Mapping):
            raise ValueError("portfolio_risk_context_must_be_mapping")
        normalized = dict(context)
        status = str(normalized.get("status") or "")
        if status not in {"verified", "withheld"}:
            raise ValueError("portfolio_risk_context_status_invalid")
        if not verify_pit_portfolio_risk_context(normalized):
            raise ValueError("portfolio_risk_context_receipt_invalid")
        receipt = str(normalized.get("risk_context_receipt_sha256") or "").lower()
        normalized["risk_context_receipt_sha256"] = receipt
        return normalized

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection


@dataclass(frozen=True, slots=True)
class PointInTimePortfolioRiskContextBuilder:
    """Build risk inputs from already materialized, known-at-the-time rows."""

    minimum_return_observations: int = 20

    def materialize(
        self,
        *,
        as_of: str,
        dataset_manifest_hash: str,
        instruments: Mapping[str, Mapping[str, Any]],
        account_policy: Mapping[str, Any],
        receipt_store: PortfolioRiskContextStore | None = None,
        input_blockers: Sequence[str] = (),
    ) -> dict[str, Any]:
        # The session adapter reports its own missing/mismatched input coverage
        # here.  Bind those facts into the same receipt instead of silently
        # replacing a shared portfolio context with per-symbol guesses.
        blockers = sorted(
            {
                str(item).strip()
                for item in input_blockers
                if str(item).strip()
            }
        )
        try:
            as_of_dt = _parse_time(as_of)
        except (TypeError, ValueError):
            as_of_dt = None
            blockers.append("portfolio_risk_context_as_of_missing_or_invalid")

        manifest = str(dataset_manifest_hash or "").lower().strip()
        if not _is_sha256(manifest):
            blockers.append("portfolio_dataset_manifest_hash_missing")

        normalized: dict[str, dict[str, Any]] = {}
        if not isinstance(instruments, Mapping) or not instruments:
            blockers.append("portfolio_risk_context_instruments_missing")
        else:
            for raw_symbol, raw_item in instruments.items():
                symbol = str(raw_symbol or "").strip().upper()
                if not symbol or symbol in normalized:
                    blockers.append("portfolio_risk_context_symbols_missing_or_duplicate")
                    continue
                if not isinstance(raw_item, Mapping):
                    blockers.append(f"portfolio_risk_context_instrument_invalid:{symbol}")
                    continue
                item = dict(raw_item)
                item_symbol = str(item.get("symbol") or symbol).strip().upper()
                if item_symbol != symbol:
                    blockers.append(f"portfolio_risk_context_symbol_mismatch:{symbol}")
                normalized[symbol] = item

        policy = dict(account_policy) if isinstance(account_policy, Mapping) else {}
        policy_blockers = self._validate_policy(policy, normalized, as_of_dt)
        blockers.extend(policy_blockers)

        if not blockers and as_of_dt is not None:
            risk_parts: dict[str, Any] = {}
            observation_sets: dict[str, set[str]] = {}
            for symbol, item in normalized.items():
                item_blockers, materialized = self._materialize_instrument(
                    symbol=symbol,
                    item=item,
                    as_of=as_of_dt,
                    manifest=manifest,
                )
                blockers.extend(item_blockers)
                if materialized is not None:
                    risk_parts[symbol] = materialized
                    observation_sets[symbol] = set(materialized["return_timestamps"])

            if not blockers:
                required_factors = {str(key) for key in policy["factor_limits"]}
                for symbol in normalized:
                    missing_factors = sorted(required_factors - set(risk_parts[symbol]["factor_exposures"]))
                    blockers.extend(
                        f"portfolio_factor_exposure_missing:{symbol}:{factor}"
                        for factor in missing_factors
                    )
            if not blockers:
                expected_timestamps = next(iter(observation_sets.values()), set())
                if any(values != expected_timestamps for values in observation_sets.values()):
                    blockers.append("portfolio_return_observation_timestamps_not_aligned")
                elif len(expected_timestamps) < self.minimum_return_observations:
                    blockers.append("portfolio_historical_returns_insufficient")

        if blockers:
            return self._withheld(
                as_of=as_of,
                manifest=manifest,
                blockers=blockers,
                account_policy=policy,
                receipt_store=receipt_store,
            )

        symbols = sorted(normalized)
        return_maps = {
            symbol: risk_parts[symbol]["returns"] for symbol in symbols
        }
        returns = {
            symbol: [
                return_maps[symbol][timestamp]
                for timestamp in sorted(return_maps[symbol])
            ]
            for symbol in symbols
        }
        timestamps = sorted(next(iter(observation_sets.values())))
        covariance = _covariance_matrix(return_maps, symbols)
        factor_exposures = {
            symbol: risk_parts[symbol]["factor_exposures"] for symbol in symbols
        }
        position_metadata = {
            symbol: risk_parts[symbol]["position_risk_metadata"] for symbol in symbols
        }
        position_sizing_inputs = {
            symbol: risk_parts[symbol]["position_sizing_inputs"] for symbol in symbols
        }
        account_receipt_payload = self._account_receipt_payload(policy)
        account_receipt_sha = _sha(account_receipt_payload)
        if str(policy.get("approval_receipt_sha256") or "").lower() != account_receipt_sha:
            return self._withheld(
                as_of=as_of,
                manifest=manifest,
                blockers=["portfolio_account_policy_receipt_mismatch"],
                account_policy=policy,
                receipt_store=receipt_store,
            )

        context = {
            "schema_version": _SCHEMA,
            "status": "verified",
            "as_of": _canonical_time(as_of_dt),
            "dataset_manifest_hash": manifest,
            "symbols": symbols,
            "return_timestamps": timestamps,
            "historical_returns": returns,
            "covariance_matrix": covariance,
            "factor_exposures": factor_exposures,
            "factor_limits": dict(policy["factor_limits"]),
            "position_risk_metadata": position_metadata,
            "position_sizing_inputs": position_sizing_inputs,
            "concentration_limits": dict(policy["concentration_limits"]),
            "account_equity": float(policy["account_equity"]),
            "max_participation_rate": float(policy["max_participation_rate"]),
            "max_days_to_liquidate": float(policy["max_days_to_liquidate"]),
            "cvar_confidence": float(policy["cvar_confidence"]),
            "max_cvar_pct": float(policy["max_cvar_pct"]),
            "stress_scenarios": list(policy["stress_scenarios"]),
            "max_stress_loss_pct": float(policy["max_stress_loss_pct"]),
            "risk_context_status": "verified",
            "account_risk_status": "verified",
            "account_risk_receipt_sha256": account_receipt_sha,
            "source_revision_ids": sorted(
                {
                    revision
                    for symbol in symbols
                    for revision in risk_parts[symbol]["source_revision_ids"]
                }
            ),
            "execution_authority": "none",
            "execution_boundary": "paper_research_portfolio_risk_only_no_order_authority",
            "blockers": [],
        }
        context_payload = dict(context)
        context_payload.pop("risk_context_receipt_sha256", None)
        context["risk_context_receipt_sha256"] = _sha(context_payload)
        if receipt_store is not None:
            receipt_store.save(context)
        return context

    def issue_account_policy_receipt(self, policy: Mapping[str, Any]) -> str:
        """Return the exact hash required for a paper policy approval input."""

        return _sha(self._account_receipt_payload(dict(policy)))

    def _materialize_instrument(
        self,
        *,
        symbol: str,
        item: dict[str, Any],
        as_of: datetime,
        manifest: str,
    ) -> tuple[list[str], dict[str, Any] | None]:
        blockers: list[str] = []
        if item.get("dataset_manifest_hash") != manifest:
            blockers.append(f"portfolio_instrument_manifest_mismatch:{symbol}")
        if item.get("historical_pit_eligible") is not True:
            blockers.append(f"portfolio_instrument_pit_unverified:{symbol}")
        if item.get("production_contract_covered") is not True:
            blockers.append(f"portfolio_instrument_contract_unverified:{symbol}")
        metadata = dict(item.get("metadata") or {})
        for field in _METADATA_FIELDS:
            if not str(metadata.get(field) or "").strip():
                blockers.append(f"portfolio_position_metadata_missing:{symbol}:{field}")
        adv = _positive(metadata.get("adv_value"))
        adv_at = _safe_observation_time(metadata.get("adv_available_at"))
        if adv is None or adv_at is None or adv_at > as_of:
            blockers.append(f"portfolio_adv_not_pit_verified:{symbol}")
        adv_revision = str(metadata.get("adv_source_revision_id") or "").strip()
        if not adv_revision:
            blockers.append(f"portfolio_adv_source_revision_missing:{symbol}")

        raw_returns = item.get("returns")
        if not isinstance(raw_returns, list) or not raw_returns:
            blockers.append(f"portfolio_returns_missing:{symbol}")
            raw_returns = []
        returns: dict[str, float] = {}
        revisions: set[str] = {adv_revision} if adv_revision else set()
        for index, observation in enumerate(raw_returns):
            if not isinstance(observation, Mapping):
                blockers.append(f"portfolio_return_observation_invalid:{symbol}:{index}")
                continue
            timestamp = _safe_observation_time(observation.get("timestamp"))
            available_at = _safe_observation_time(observation.get("available_at"))
            value = _finite(observation.get("return"))
            revision = str(observation.get("source_revision_id") or "").strip()
            if timestamp is None or available_at is None or value is None or not revision:
                blockers.append(f"portfolio_return_observation_incomplete:{symbol}:{index}")
                continue
            if timestamp > as_of or available_at > as_of:
                blockers.append(f"portfolio_return_observation_future:{symbol}:{index}")
            key = _canonical_time(timestamp)
            if key in returns:
                blockers.append(f"portfolio_return_observation_duplicate:{symbol}:{key}")
            returns[key] = value
            revisions.add(revision)

        raw_factors = item.get("factor_exposures")
        factors: dict[str, float] = {}
        if not isinstance(raw_factors, Mapping) or not raw_factors:
            blockers.append(f"portfolio_factor_exposures_missing:{symbol}")
        else:
            for factor, raw_factor in raw_factors.items():
                if not isinstance(raw_factor, Mapping):
                    blockers.append(f"portfolio_factor_observation_invalid:{symbol}:{factor}")
                    continue
                value = _finite(raw_factor.get("value"))
                available_at = _safe_observation_time(raw_factor.get("available_at"))
                revision = str(raw_factor.get("source_revision_id") or "").strip()
                if value is None or available_at is None or not revision:
                    blockers.append(f"portfolio_factor_observation_incomplete:{symbol}:{factor}")
                    continue
                if available_at > as_of:
                    blockers.append(f"portfolio_factor_observation_future:{symbol}:{factor}")
                factors[str(factor)] = value
                revisions.add(revision)

        raw_sizing = item.get("sizing_inputs")
        sizing: dict[str, float] = {}
        if not isinstance(raw_sizing, Mapping):
            blockers.append(f"portfolio_sizing_inputs_missing:{symbol}")
        else:
            for field in _SIZING_FIELDS:
                raw_value = raw_sizing.get(field)
                if not isinstance(raw_value, Mapping):
                    blockers.append(f"portfolio_sizing_observation_invalid:{symbol}:{field}")
                    continue
                value = _finite(raw_value.get("value"))
                available_at = _safe_observation_time(raw_value.get("available_at"))
                revision = str(raw_value.get("source_revision_id") or "").strip()
                if value is None or available_at is None or not revision:
                    blockers.append(f"portfolio_sizing_observation_incomplete:{symbol}:{field}")
                    continue
                if available_at > as_of:
                    blockers.append(f"portfolio_sizing_observation_future:{symbol}:{field}")
                sizing[field] = value
                revisions.add(revision)

        if blockers:
            return sorted(set(blockers)), None
        return [], {
            "returns": {key: returns[key] for key in sorted(returns)},
            "return_timestamps": sorted(returns),
            "factor_exposures": factors,
            "position_risk_metadata": {
                **metadata,
                "adv_value": adv,
            },
            "position_sizing_inputs": sizing,
            "source_revision_ids": sorted(revisions),
        }

    def _validate_policy(
        self,
        policy: dict[str, Any],
        instruments: Mapping[str, Mapping[str, Any]],
        as_of: datetime | None,
    ) -> list[str]:
        blockers: list[str] = []
        if _contains_forbidden_key(policy):
            blockers.append("portfolio_account_policy_contains_secret_like_field")
        if policy.get("approval_status") != "approved":
            blockers.append("portfolio_account_policy_not_approved")
        approved_at = _safe_observation_time(policy.get("approved_at"))
        if as_of is None or approved_at is None or approved_at > as_of:
            blockers.append("portfolio_account_policy_approval_not_known_at_as_of")
        if not str(policy.get("policy_id") or "").strip() or not str(policy.get("policy_version") or "").strip():
            blockers.append("portfolio_account_policy_identity_missing")
        if not str(policy.get("approved_by") or "").strip():
            blockers.append("portfolio_account_policy_approver_missing")
        if not str(policy.get("account_alias") or "").strip():
            blockers.append("portfolio_account_policy_account_alias_missing")
        if policy.get("account_scope") != "paper":
            blockers.append("portfolio_account_policy_scope_not_paper")
        expected_alias = str(policy.get("account_alias") or "").strip()
        for symbol, item in instruments.items():
            metadata = item.get("metadata") if isinstance(item, Mapping) else None
            if isinstance(metadata, Mapping) and str(metadata.get("account_alias") or "").strip() != expected_alias:
                blockers.append(f"portfolio_account_alias_mismatch:{symbol}")

        for field in _POLICY_FIELDS:
            if _positive(policy.get(field)) is None:
                blockers.append(f"portfolio_policy_input_missing:{field}")
        participation = _positive(policy.get("max_participation_rate"))
        if participation is not None and participation > 1:
            blockers.append("portfolio_policy_participation_rate_invalid")
        confidence = _finite(policy.get("cvar_confidence"))
        if confidence is None or not 0.5 < confidence < 1:
            blockers.append("portfolio_policy_cvar_confidence_invalid")

        limits = policy.get("concentration_limits")
        if not isinstance(limits, Mapping):
            blockers.append("portfolio_concentration_limits_missing")
        else:
            for key in ("issuer", "industry", "factor", "currency", "broker", "account"):
                if _positive(limits.get(key)) is None:
                    blockers.append(f"portfolio_concentration_limit_missing:{key}")
        factor_limits = policy.get("factor_limits")
        if not isinstance(factor_limits, Mapping) or not factor_limits:
            blockers.append("portfolio_factor_limits_missing")
        elif any(_positive(value) is None for value in factor_limits.values()):
            blockers.append("portfolio_factor_limit_invalid")

        scenarios = policy.get("stress_scenarios")
        if not isinstance(scenarios, list) or not scenarios:
            blockers.append("portfolio_stress_scenarios_missing")
        else:
            symbols = set(instruments)
            for scenario in scenarios:
                if not isinstance(scenario, Mapping) or not str(scenario.get("scenario_id") or "").strip():
                    blockers.append("portfolio_stress_scenario_invalid")
                    continue
                shocks = scenario.get("shocks")
                if not isinstance(shocks, Mapping) or set(shocks) != symbols:
                    blockers.append(f"portfolio_stress_scenario_symbols_incomplete:{scenario['scenario_id']}")
                elif any(_finite(shocks.get(symbol)) is None for symbol in symbols):
                    blockers.append(f"portfolio_stress_scenario_values_invalid:{scenario['scenario_id']}")
        return sorted(set(blockers))

    @staticmethod
    def _account_receipt_payload(policy: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "open_stock_ai.paper_account_risk_policy.v1",
            "policy_id": str(policy.get("policy_id") or ""),
            "policy_version": str(policy.get("policy_version") or ""),
            "account_scope": str(policy.get("account_scope") or ""),
            "account_alias": str(policy.get("account_alias") or ""),
            "account_equity": policy.get("account_equity"),
            "concentration_limits": dict(policy.get("concentration_limits") or {}),
            "factor_limits": dict(policy.get("factor_limits") or {}),
            "max_participation_rate": policy.get("max_participation_rate"),
            "max_days_to_liquidate": policy.get("max_days_to_liquidate"),
            "cvar_confidence": policy.get("cvar_confidence"),
            "max_cvar_pct": policy.get("max_cvar_pct"),
            "stress_scenarios": list(policy.get("stress_scenarios") or []),
            "max_stress_loss_pct": policy.get("max_stress_loss_pct"),
            "approval_status": policy.get("approval_status"),
            "approved_by": str(policy.get("approved_by") or ""),
            "approved_at": str(policy.get("approved_at") or ""),
        }

    @staticmethod
    def _withheld(
        *,
        as_of: str,
        manifest: str,
        blockers: list[str],
        account_policy: Mapping[str, Any],
        receipt_store: PortfolioRiskContextStore | None = None,
    ) -> dict[str, Any]:
        account_payload = PointInTimePortfolioRiskContextBuilder._account_receipt_payload(account_policy)
        receipt = {
            "schema_version": _SCHEMA,
            "status": "withheld",
            "as_of": str(as_of or ""),
            "dataset_manifest_hash": manifest,
            "account_risk_receipt_sha256": _sha(account_payload),
            "risk_context_status": "withheld",
            "account_risk_status": "withheld",
            "blockers": sorted(set(blockers)),
            "execution_authority": "none",
            "execution_boundary": "paper_research_portfolio_risk_only_no_order_authority",
        }
        receipt["risk_context_receipt_sha256"] = _sha(receipt)
        if receipt_store is not None:
            receipt_store.save(receipt)
        return receipt


def materialize_session_portfolio_risk_context(
    decisions: Sequence[Mapping[str, Any]],
    *,
    receipt_store: PortfolioRiskContextStore | None = None,
    builder: PointInTimePortfolioRiskContextBuilder | None = None,
) -> dict[str, Any] | None:
    """Materialize one durable, shared PIT receipt for a batch session.

    Each decision must carry the identical, complete input in
    ``market_snapshot.raw.portfolio_risk_materialization``. The interactive
    path deliberately never fabricates historical PIT rows: missing or
    inconsistent input is persisted as a withheld receipt instead of allowing
    per-symbol estimates to reach portfolio construction.
    """

    normalized_decisions = [dict(item) for item in decisions if isinstance(item, Mapping)]
    if not normalized_decisions:
        return None
    symbols = sorted(
        {
            str((item.get("request") or {}).get("symbol") or "").strip().upper()
            for item in normalized_decisions
            if isinstance(item.get("request"), Mapping)
            and str((item.get("request") or {}).get("symbol") or "").strip()
        }
    )
    blockers: list[str] = []
    payloads: list[dict[str, Any]] = []
    for item in normalized_decisions:
        request = item.get("request") if isinstance(item.get("request"), Mapping) else {}
        symbol = str(request.get("symbol") or "").strip().upper() or "unknown"
        snapshot = item.get("market_snapshot") if isinstance(item.get("market_snapshot"), Mapping) else {}
        raw = snapshot.get("raw") if isinstance(snapshot.get("raw"), Mapping) else {}
        payload = raw.get("portfolio_risk_materialization")
        if not isinstance(payload, Mapping):
            blockers.append(f"portfolio_risk_materialization_missing:{symbol}")
        else:
            payloads.append(dict(payload))

    selected: dict[str, Any] = {}
    if payloads:
        if len({_canonical_json(item) for item in payloads}) != 1:
            blockers.append("portfolio_risk_materialization_mismatch_across_decisions")
        else:
            selected = payloads[0]
            if selected.get("schema_version") != _SESSION_INPUT_SCHEMA:
                blockers.append("portfolio_risk_materialization_schema_invalid")
            supplied_symbols = selected.get("instruments")
            if not isinstance(supplied_symbols, Mapping):
                blockers.append("portfolio_risk_materialization_instruments_missing")
            elif {str(key).strip().upper() for key in supplied_symbols} != set(symbols):
                blockers.append("portfolio_risk_materialization_symbol_coverage_mismatch")

    active_builder = builder or PointInTimePortfolioRiskContextBuilder()
    return active_builder.materialize(
        as_of=str(selected.get("as_of") or ""),
        dataset_manifest_hash=str(selected.get("dataset_manifest_hash") or ""),
        instruments=selected.get("instruments") if isinstance(selected.get("instruments"), Mapping) else {},
        account_policy=selected.get("account_policy") if isinstance(selected.get("account_policy"), Mapping) else {},
        receipt_store=receipt_store,
        input_blockers=blockers,
    )


def _covariance_matrix(returns: Mapping[str, Mapping[str, float]], symbols: list[str]) -> dict[str, dict[str, float]]:
    means = {symbol: fmean(returns[symbol].values()) for symbol in symbols}
    timestamps = sorted(next(iter(returns.values())))
    denominator = max(1, len(timestamps) - 1)
    return {
        left: {
            right: round(
                sum(
                    (returns[left][timestamp] - means[left])
                    * (returns[right][timestamp] - means[right])
                    for timestamp in timestamps
                )
                / denominator,
                12,
            )
            for right in symbols
        }
        for left in symbols
    }


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_observation_time(value: Any) -> datetime | None:
    try:
        return _parse_time(value)
    except (TypeError, ValueError):
        return None


def _canonical_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _FORBIDDEN_POLICY_KEYS:
                return True
            if _contains_forbidden_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(child) for child in value)
    return False


def _sha(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def verify_pit_portfolio_risk_context(context: Mapping[str, Any]) -> bool:
    """Verify the content hash of a context before a risk engine consumes it."""

    if context.get("schema_version") != _SCHEMA:
        return False
    supplied = str(context.get("risk_context_receipt_sha256") or "").lower()
    if not _is_sha256(supplied):
        return False
    payload = dict(context)
    payload.pop("risk_context_receipt_sha256", None)
    # PortfolioConstruction adds this non-PIT optimizer ceiling when it
    # forwards the context.  It is deliberately not part of the materialized
    # risk snapshot; the optimizer receipt records the effective ceiling.
    payload.pop("max_total_weight_pct", None)
    return supplied == _sha(payload)


__all__ = [
    "PointInTimePortfolioRiskContextBuilder",
    "PortfolioRiskContextError",
    "PortfolioRiskContextStore",
    "materialize_session_portfolio_risk_context",
    "verify_pit_portfolio_risk_context",
]
