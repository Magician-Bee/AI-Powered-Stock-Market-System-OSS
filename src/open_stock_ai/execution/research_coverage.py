"""Persistent per-security research coverage for an autonomous campaign.

The market screen is broad while deep research is deliberately bounded.  This
ledger keeps those two facts separate: every security seen in the official
research universe gets a row, and only retained, attributable evidence can
move an individual data domain out of ``unavailable`` or ``never_researched``.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import re
import zlib
from typing import Any, Iterable
from zoneinfo import ZoneInfo


COVERAGE_SCHEMA = "open_stock_ai.security_research_coverage.v1"
COVERAGE_SUMMARY_SCHEMA = "open_stock_ai.security_research_coverage_summary.v1"
COVERAGE_DOMAINS = (
    "identity",
    "daily_price",
    "intraday_price",
    "order_book",
    "price_history",
    "financials",
    "revenue",
    "ownership_flows",
    "news_events",
    "industry",
    "cross_market",
)
DEEP_RESEARCH_STATES = frozenset({"never_researched", "current", "stale", "failed", "not_attributed"})
DOMAIN_AVAILABILITY_STATES = frozenset({"ready", "partial", "unavailable", "conflict"})
DOMAIN_FRESHNESS_STATES = frozenset({"current", "stale", "unknown"})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _pack_domains(value: dict[str, dict[str, Any]]) -> bytes:
    return zlib.compress(_json(value).encode("utf-8"), level=6)


def _unpack_domains(value: bytes) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(zlib.decompress(bytes(value)).decode("utf-8"))
    except (TypeError, ValueError, zlib.error, json.JSONDecodeError) as exc:
        raise ValueError("research_coverage_domains_corrupted") from exc
    if set(payload) != set(COVERAGE_DOMAINS):
        raise ValueError("research_coverage_domain_contract_incomplete")
    return payload


def _needs_update_mask(domains: dict[str, dict[str, Any]]) -> int:
    return sum(
        1 << index
        for index, domain in enumerate(COVERAGE_DOMAINS)
        if domains[domain].get("needs_update") is True
    )


def _utc(value: str | datetime | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(raw), time(), tzinfo=timezone.utc)
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _domain(
    *,
    availability: str,
    freshness: str,
    source: str,
    reason: str | None,
    evidence_at: datetime | None,
    expires_at: datetime | None,
    evidence_ref: str | None = None,
) -> dict[str, Any]:
    if availability not in DOMAIN_AVAILABILITY_STATES:
        raise ValueError("research_coverage_domain_availability_invalid")
    if freshness not in DOMAIN_FRESHNESS_STATES:
        raise ValueError("research_coverage_domain_freshness_invalid")
    needs_update = availability != "ready" or freshness != "current"
    next_update_at = (
        evidence_at if availability in {"unavailable", "conflict"}
        else expires_at if expires_at is not None
        else evidence_at
    )
    return {
        "availability": availability,
        "freshness": freshness,
        "needs_update": needs_update,
        "source": source,
        "reason": reason,
        "evidence_at": _iso(evidence_at),
        "expires_at": _iso(expires_at),
        "next_update_at": _iso(next_update_at),
        "evidence_ref": evidence_ref,
    }


def _missing(reason: str, *, observed_at: datetime) -> dict[str, Any]:
    return _domain(
        availability="unavailable",
        freshness="unknown",
        source="not_connected",
        reason=reason,
        evidence_at=observed_at,
        expires_at=None,
    )


class SecurityResearchCoverageLedger:
    """Current, account-scoped coverage state keyed by immutable entity identity."""

    def __init__(self, store, *, account_id: str, calendar) -> None:
        self.store = store
        self.account_id = str(account_id)
        self.calendar = calendar
        with self.store._connect() as conn:
            conn.executescript(
                """
                create table if not exists autonomous_security_research_coverage (
                    account_id text not null,
                    coverage_key text not null,
                    entity_id text,
                    symbol text not null,
                    venue text not null,
                    name text not null,
                    lifecycle_status text not null,
                    product_type text not null,
                    new_entry_eligible integer not null,
                    exclusion_reasons_json text not null,
                    in_latest_universe integer not null,
                    universe_observed_at text not null,
                    universe_expires_at text not null,
                    deep_research_status text not null,
                    last_deep_research_at text,
                    last_deep_research_cycle_id text,
                    last_deep_research_evidence_id text,
                    last_deep_research_error text,
                    data_domains_blob blob not null,
                    needs_update_mask integer not null,
                    snapshot_sha256 text not null,
                    updated_at text not null,
                    primary key(account_id, coverage_key)
                );
                create index if not exists idx_autonomous_security_coverage_symbol
                    on autonomous_security_research_coverage(account_id, symbol, coverage_key);
                create index if not exists idx_autonomous_security_coverage_deep
                    on autonomous_security_research_coverage(account_id, in_latest_universe, deep_research_status, symbol);
                create table if not exists autonomous_security_research_coverage_snapshots (
                    snapshot_id text primary key,
                    account_id text not null,
                    observed_at text not null,
                    source_cycle_id text,
                    entity_count integer not null,
                    summary_json text not null,
                    snapshot_sha256 text not null
                );
                create index if not exists idx_autonomous_security_coverage_snapshots_account
                    on autonomous_security_research_coverage_snapshots(account_id, observed_at);
                """
            )

    def _completed_day(self, observed_at: datetime) -> date:
        local = observed_at.astimezone(ZoneInfo("Asia/Taipei"))
        day = local.date() if local.time().replace(tzinfo=None) >= time(14, 30) else local.date() - timedelta(days=1)
        while not self.calendar.day_status(day)["trading_day"]:
            day -= timedelta(days=1)
        return day

    def _next_market_expiry(self, evidence_at: datetime) -> datetime:
        local_day = evidence_at.astimezone(ZoneInfo("Asia/Taipei")).date()
        next_day = self.calendar.next_trading_day(local_day)
        return datetime.combine(next_day, time(15, 30), tzinfo=ZoneInfo("Asia/Taipei")).astimezone(timezone.utc)

    def _next_universe_expiry(self, observed_at: datetime) -> datetime:
        local = observed_at.astimezone(ZoneInfo("Asia/Taipei"))
        day = local.date()
        if self.calendar.day_status(day)["trading_day"] and local.time().replace(tzinfo=None) < time(15, 30):
            due_day = day
        else:
            due_day = self.calendar.next_trading_day(day)
        return datetime.combine(due_day, time(15, 30), tzinfo=ZoneInfo("Asia/Taipei")).astimezone(timezone.utc)

    @staticmethod
    def _factor_domain(payload: dict[str, Any], *, fallback_reason: str, observed_at: datetime,
                       ttl: timedelta) -> dict[str, Any]:
        if not payload or payload.get("value") is None:
            return _missing(fallback_reason, observed_at=observed_at)
        available_at = _utc(payload.get("available_at"))
        expires_at = available_at + ttl if available_at is not None else None
        freshness = "current" if expires_at is not None and expires_at > observed_at else "stale" if expires_at else "unknown"
        raw_status = str(payload.get("status") or "unavailable")
        availability = raw_status if raw_status in DOMAIN_AVAILABILITY_STATES else "unavailable"
        return _domain(
            availability=availability,
            freshness=freshness,
            source=str(payload.get("source") or "unknown"),
            reason=str(payload.get("reason") or "") or None,
            evidence_at=available_at,
            expires_at=expires_at,
            evidence_ref=str(payload.get("revision_id") or "") or None,
        )

    def _validated_history(self, evidence_id: str, encoded: str, *, observed_at: datetime) -> dict[str, Any]:
        try:
            payload = json.loads(encoded)
        except (TypeError, json.JSONDecodeError):
            return {
                "evidence_id": str(evidence_id), "verified": False, "current": False,
                "last_bar": None, "expires_at": None, "reason": "retained_price_history_json_invalid",
            }
        expected_ids = {
            "AE-" + _sha({"kind": "price_history", "payload": payload}),
            "AE-" + _sha({"account_id": self.account_id, "kind": "price_history", "payload": payload}),
        }
        if evidence_id not in expected_ids:
            return {
                "evidence_id": str(evidence_id), "verified": False, "current": False,
                "last_bar": None, "expires_at": None, "reason": "retained_price_history_hash_mismatch",
            }
        source = payload.get("data_evidence") or {}
        bars = payload.get("rows") or []
        last_bar = _utc((bars[-1] or {}).get("timestamp")) if bars and isinstance(bars[-1], dict) else None
        stamps = [_utc(item.get("timestamp")) for item in bars if isinstance(item, dict)]
        hash_matches = (
            (not source.get("data_sha256") or source.get("data_sha256") == _sha(bars))
            and (not source.get("normalized_data_sha256") or source.get("normalized_data_sha256") == _sha(bars))
        )
        verified = (
            source.get("source_provenance_verified") is True
            and source.get("instrument_identity_verified") is True
            and isinstance(bars, list)
            and len(bars) >= 62
            and len(stamps) == len(bars)
            and all(stamp is not None for stamp in stamps)
            and all(right > left for left, right in zip(stamps, stamps[1:]))
            and hash_matches
            and last_bar is not None
            and last_bar <= observed_at
        )
        expires_at = self._next_market_expiry(last_bar) if verified else None
        current = bool(
            verified
            and last_bar.astimezone(ZoneInfo("Asia/Taipei")).date() == self._completed_day(observed_at)
        )
        return {
            "evidence_id": str(evidence_id),
            "symbol": str(source.get("symbol") or "").strip().upper(),
            "last_bar": last_bar,
            "expires_at": expires_at,
            "verified": verified,
            "current": current,
            "reason": None if verified else "retained_price_history_not_verified",
        }

    def _research_by_entity(self, conn, *, observed_at: datetime) -> dict[str, dict[str, Any]]:
        histories = {
            str(evidence_id): self._validated_history(str(evidence_id), encoded, observed_at=observed_at)
            for evidence_id, encoded in conn.execute(
                """select evidence_id,payload_json from autonomous_evidence
                   where account_id=? and kind='price_history'""",
                (self.account_id,),
            )
        }
        latest: dict[str, dict[str, Any]] = {}
        cycles = conn.execute(
            """select cycle_id,created_at,payload_json from autonomous_research_cycles
               where account_id=? order by created_at desc,rowid desc""",
            (self.account_id,),
        ).fetchall()
        for cycle_id, created_at, encoded in cycles:
            try:
                payload = json.loads(encoded)
            except (TypeError, json.JSONDecodeError):
                continue
            if "AC-" + _sha({key: value for key, value in payload.items() if key != "cycle_id"}) != str(cycle_id):
                continue
            for result in payload.get("results") or []:
                feature = result.get("feature") or {}
                admission = result.get("product_admission") or {}
                feature_entity = str(feature.get("entity_id") or "")
                admission_entity = str(admission.get("entity_id") or "")
                if feature_entity and admission_entity and feature_entity != admission_entity:
                    continue
                entity_id = admission_entity or feature_entity
                history_id = str(result.get("history_id") or "")
                symbol = str(result.get("symbol") or "").strip().upper()
                if not entity_id or not history_id:
                    continue
                history = histories.get(history_id)
                if history is None or history.get("symbol") != symbol:
                    state = latest.setdefault(entity_id, {})
                    if "latest_status" not in state:
                        state.update(
                            latest_status="failed", observed_at=str(created_at), cycle_id=str(cycle_id),
                            error="cycle_history_identity_or_evidence_missing",
                        )
                    continue
                state = latest.setdefault(entity_id, {})
                if "latest_status" not in state:
                    state.update(latest_status="success", observed_at=str(created_at),
                                 cycle_id=str(cycle_id), error=None)
                state.setdefault("history", history)
            for error in payload.get("errors") or []:
                entity_id = str(error.get("entity_id") or "")
                if not entity_id:
                    continue
                state = latest.setdefault(entity_id, {})
                if "latest_status" not in state:
                    state.update(
                        latest_status="failed", observed_at=str(created_at), cycle_id=str(cycle_id),
                        error=str(error.get("error") or "history_research_failed"),
                    )
        return latest

    @staticmethod
    def _feature_identities(feature: dict[str, Any]) -> list[dict[str, Any]]:
        quality = feature.get("data_quality") or {}
        binding = (quality.get("source_observation") or {}).get("primary_quote_binding") or {}
        candidates = binding.get("candidates") if isinstance(binding.get("candidates"), list) else []
        if candidates:
            return [
                {
                    "entity_id": str(candidate.get("entity_id") or "") or None,
                    "venue": str(candidate.get("exchange") or feature.get("exchange") or "unknown"),
                    "lifecycle_status": str(candidate.get("trading_status") or feature.get("lifecycle_status") or "unknown"),
                    "identity_conflict": len(candidates) > 1,
                    "binding_reason": str(binding.get("reason") or "research_listing_identity_unverified"),
                    "candidate_count": len(candidates),
                }
                for candidate in candidates
            ]
        return [{
            "entity_id": str(feature.get("entity_id") or "") or None,
            "venue": str(feature.get("exchange") or "unknown"),
            "lifecycle_status": str(feature.get("lifecycle_status") or "unknown"),
            "identity_conflict": bool(binding),
            "binding_reason": str(binding.get("reason") or "") or None,
            "candidate_count": int(binding.get("candidate_count") or 0),
        }]

    def _domains(self, feature: dict[str, Any], identity: dict[str, Any], *, observed_at: datetime,
                 history: dict[str, Any] | None, attributable: bool) -> dict[str, dict[str, Any]]:
        quality = feature.get("data_quality") or {}
        factors = feature.get("scanner_factor_inputs") or {}
        identity_expiry = self._next_universe_expiry(observed_at)
        entity_id = identity.get("entity_id")
        identity_status = "conflict" if identity.get("identity_conflict") else "ready" if entity_id else "unavailable"
        domains = {
            "identity": _domain(
                availability=identity_status,
                freshness="current" if entity_id else "unknown",
                source="OFFICIAL_SECURITY_MASTER",
                reason=identity.get("binding_reason") if identity_status != "ready" else None,
                evidence_at=observed_at,
                expires_at=identity_expiry,
                evidence_ref=entity_id,
            ),
            "intraday_price": _missing("intraday_security_coverage_not_connected", observed_at=observed_at),
            "order_book": _missing("order_book_security_coverage_not_connected", observed_at=observed_at),
            "cross_market": _missing("cross_market_security_coverage_not_connected", observed_at=observed_at),
        }
        data_at = _utc(feature.get("data_as_of"))
        close = feature.get("close")
        has_price = isinstance(close, (int, float)) and not isinstance(close, bool) and math.isfinite(float(close)) and float(close) > 0
        price_expiry = self._next_market_expiry(data_at) if data_at is not None else None
        price_freshness = "current" if price_expiry and price_expiry > observed_at else "stale" if price_expiry else "unknown"
        quality_status = str(quality.get("status") or "unavailable")
        price_availability = quality_status if quality_status in DOMAIN_AVAILABILITY_STATES and has_price else "unavailable"
        source_error = (quality.get("source_observation") or {}).get("primary_source_error") or {}
        price_reason = (
            f"{source_error.get('type')}: {source_error.get('message')}" if source_error
            else identity.get("binding_reason") if not has_price and identity.get("binding_reason")
            else "latest_daily_price_missing" if not has_price
            else ";".join(str(item) for item in quality.get("quality_flags") or []) or None
        )
        domains["daily_price"] = _domain(
            availability=price_availability,
            freshness=price_freshness,
            source=str(feature.get("source") or quality.get("source") or "unknown"),
            reason=price_reason,
            evidence_at=data_at,
            expires_at=price_expiry,
            evidence_ref=next((str(item.get("evidence_id")) for item in feature.get("evidence") or []
                               if isinstance(item, dict) and item.get("evidence_id")), None),
        )
        if history is not None:
            domains["price_history"] = _domain(
                availability="ready" if history.get("verified") else "unavailable",
                freshness="current" if history.get("current") else "stale" if history.get("verified") else "unknown",
                source="retained_campaign_evidence", reason=history.get("reason"),
                evidence_at=history.get("last_bar"), expires_at=history.get("expires_at"),
                evidence_ref=history.get("evidence_id"),
            )
        elif not attributable:
            domains["price_history"] = _domain(
                availability="conflict", freshness="unknown", source="retained_campaign_evidence",
                reason="symbol_history_not_attributable_to_entity", evidence_at=None,
                expires_at=None, evidence_ref=None,
            )
        else:
            domains["price_history"] = _missing("deep_price_history_never_researched", observed_at=observed_at)
        fundamental = factors.get("fundamental") or {}
        valuation = factors.get("valuation") or {}
        financial_factor = valuation if valuation.get("value") is not None else (
            fundamental if str(fundamental.get("domain") or "") == "financials" else {})
        domains["financials"] = self._factor_domain(
            financial_factor, fallback_reason="financial_statement_coverage_missing",
            observed_at=observed_at, ttl=timedelta(days=120),
        )
        revenue_factor = fundamental if str(fundamental.get("domain") or "revenue") == "revenue" else {}
        domains["revenue"] = self._factor_domain(
            revenue_factor, fallback_reason="monthly_revenue_coverage_missing",
            observed_at=observed_at, ttl=timedelta(days=45),
        )
        domains["ownership_flows"] = self._factor_domain(
            factors.get("chip") or {}, fallback_reason="ownership_flow_coverage_missing",
            observed_at=observed_at, ttl=timedelta(days=4),
        )
        domains["news_events"] = self._factor_domain(
            factors.get("event") or {}, fallback_reason="news_event_coverage_missing",
            observed_at=observed_at, ttl=timedelta(days=7),
        )
        industry = str(feature.get("industry") or "").strip()
        domains["industry"] = _domain(
            availability="ready" if industry else "unavailable",
            freshness="current" if industry else "unknown",
            source="OFFICIAL_SECURITY_MASTER" if industry else "not_connected",
            reason=None if industry else "industry_classification_missing",
            evidence_at=observed_at,
            expires_at=observed_at + timedelta(days=30) if industry else None,
            evidence_ref=identity.get("entity_id") if industry else None,
        )
        if identity.get("identity_conflict"):
            # Symbol-level current data cannot be copied onto every issuance
            # when the source cannot bind the quote/factor row to one entity.
            # Preserve its observed source/time while making attribution
            # conflict explicit for each candidate identity.
            reason = str(identity.get("binding_reason") or "ambiguous_research_listing_identity")
            for domain_name in (
                "daily_price", "financials", "revenue", "ownership_flows", "news_events", "industry",
            ):
                state = domains[domain_name]
                if state["availability"] == "unavailable":
                    continue
                domains[domain_name] = _domain(
                    availability="conflict",
                    freshness=state["freshness"],
                    source=state["source"],
                    reason=reason,
                    evidence_at=_utc(state["evidence_at"]),
                    expires_at=_utc(state["expires_at"]),
                    evidence_ref=state["evidence_ref"],
                )
        if set(domains) != set(COVERAGE_DOMAINS):
            raise ValueError("research_coverage_domain_contract_incomplete")
        return {name: domains[name] for name in COVERAGE_DOMAINS}

    def sync(
        self,
        features: Iterable[dict[str, Any]],
        *,
        observed_at: datetime,
        product_admissions: dict[str, dict[str, Any]],
        source_cycle_id: str | None,
    ) -> dict[str, Any]:
        observed_at = (_utc(observed_at) or datetime.now(timezone.utc))
        observed_iso = observed_at.isoformat()
        universe_expires = self._next_universe_expiry(observed_at).isoformat()
        features = list(features)
        if not features:
            raise ValueError("research_coverage_official_universe_empty")
        with self.store._connect() as conn:
            research_by_entity = self._research_by_entity(conn, observed_at=observed_at)
            deep = {
                str(row[0]).upper(): {
                    "last_selected_at": row[1], "last_success_at": row[2], "last_error": row[3],
                }
                for row in conn.execute(
                    """select symbol,last_selected_at,last_success_at,last_error
                       from autonomous_deep_research_coverage where account_id=?""",
                    (self.account_id,),
                )
            }
            prior = {
                str(row[0]): {
                    "last_deep_research_at": row[1], "last_deep_research_cycle_id": row[2],
                    "last_deep_research_evidence_id": row[3], "last_deep_research_error": row[4],
                }
                for row in conn.execute(
                    """select coverage_key,last_deep_research_at,last_deep_research_cycle_id,
                              last_deep_research_evidence_id,last_deep_research_error
                       from autonomous_security_research_coverage where account_id=?""",
                    (self.account_id,),
                )
            }

            records: list[tuple[Any, ...]] = []
            deep_counts: Counter[str] = Counter()
            lifecycle_counts: Counter[str] = Counter()
            domain_counts = {
                domain: Counter({"ready_current": 0, "partial": 0, "stale": 0,
                                 "unavailable": 0, "conflict": 0, "needs_update": 0})
                for domain in COVERAGE_DOMAINS
            }
            new_entry_eligible_count = 0
            all_domains_current = True
            seen: dict[str, dict[str, Any]] = {}
            for feature in features:
                symbol = str(feature.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                identities = self._feature_identities(feature)
                admission = product_admissions.get(symbol) or {}
                classification = feature.get("product_classification") or {}
                for identity in identities:
                    entity_id = identity.get("entity_id")
                    coverage_key = entity_id or "unresolved:" + _sha({
                        "symbol": symbol, "venue": identity.get("venue"),
                        "binding_reason": identity.get("binding_reason"),
                    })[:32]
                    prior_claim = seen.get(coverage_key)
                    # Scanner ranking fields such as trade_value do not alter
                    # this ledger row. Coalesce repeats only when every field
                    # that the coverage projection consumes is identical.
                    coverage_fields = (
                            "symbol", "entity_id", "exchange", "lifecycle_status", "name", "industry",
                            "close", "data_as_of", "source", "evidence", "data_quality",
                            "scanner_factor_inputs", "product_classification",
                    )
                    if prior_claim is not None:
                        if all(prior_claim.get(key) == feature.get(key) for key in coverage_fields):
                            continue
                        raise ValueError(f"research_coverage_duplicate_entity_identity:{coverage_key}")
                    seen[coverage_key] = feature
                    attributable = len(identities) == 1 and bool(entity_id)
                    research = research_by_entity.get(str(entity_id)) if entity_id else None
                    history = (research or {}).get("history")
                    legacy = deep.get(symbol) or {}
                    old = prior.get(coverage_key) or {}
                    if research and research.get("latest_status") == "failed":
                        deep_status = "failed"
                    elif history and history.get("verified") and history.get("current"):
                        deep_status = "current"
                    elif history and history.get("verified"):
                        deep_status = "stale"
                    elif history:
                        deep_status = "failed"
                    elif legacy:
                        deep_status = "not_attributed"
                    else:
                        deep_status = "never_researched"
                    last_deep_at = str((research or {}).get("observed_at") or "") or old.get("last_deep_research_at")
                    last_cycle = str((research or {}).get("cycle_id") or "") or old.get("last_deep_research_cycle_id")
                    last_evidence = (history.get("evidence_id") if history
                                     else old.get("last_deep_research_evidence_id"))
                    if research:
                        last_error = str(
                            research.get("error")
                            or (history or {}).get("reason")
                            or ""
                        ) or None
                    elif legacy:
                        last_error = ("symbol_history_not_attributable_to_ambiguous_entity" if not attributable
                                      else "legacy_symbol_research_not_attributable_to_entity")
                    else:
                        last_error = old.get("last_deep_research_error")
                    domains = self._domains(feature, identity, observed_at=observed_at,
                                            history=history, attributable=attributable)
                    exclusion_reasons = [str(item) for item in admission.get("reasons") or []]
                    snapshot = {
                        "schema_version": COVERAGE_SCHEMA,
                        "account_id": self.account_id,
                        "coverage_key": coverage_key,
                        "entity_id": entity_id,
                        "symbol": symbol,
                        "venue": str(identity.get("venue") or "unknown"),
                        "name": str(feature.get("name") or symbol),
                        "lifecycle_status": str(identity.get("lifecycle_status") or "unknown"),
                        "product_type": str(classification.get("product_type") or "unknown"),
                        "new_entry_eligible": admission.get("allowed") is True and attributable,
                        "exclusion_reasons": exclusion_reasons,
                        "in_latest_universe": True,
                        "universe_observed_at": observed_iso,
                        "universe_expires_at": universe_expires,
                        "deep_research_status": deep_status,
                        "last_deep_research_at": last_deep_at,
                        "last_deep_research_cycle_id": last_cycle,
                        "last_deep_research_evidence_id": last_evidence,
                        "last_deep_research_error": last_error,
                        "data_domains": domains,
                    }
                    snapshot_hash = _sha(snapshot)
                    deep_counts[deep_status] += 1
                    lifecycle_counts[snapshot["lifecycle_status"]] += 1
                    new_entry_eligible_count += int(snapshot["new_entry_eligible"])
                    for domain, state in domains.items():
                        domain_counts[domain]["ready_current"] += int(
                            state["availability"] == "ready" and state["freshness"] == "current"
                        )
                        domain_counts[domain]["partial"] += int(state["availability"] == "partial")
                        domain_counts[domain]["stale"] += int(state["freshness"] == "stale")
                        domain_counts[domain]["unavailable"] += int(state["availability"] == "unavailable")
                        domain_counts[domain]["conflict"] += int(state["availability"] == "conflict")
                        domain_counts[domain]["needs_update"] += int(state["needs_update"])
                        all_domains_current = all_domains_current and not state["needs_update"]
                    records.append((
                        self.account_id, coverage_key, entity_id, symbol, snapshot["venue"], snapshot["name"],
                        snapshot["lifecycle_status"], snapshot["product_type"], int(snapshot["new_entry_eligible"]),
                        _json(exclusion_reasons), 1, observed_iso, universe_expires, deep_status, last_deep_at,
                        last_cycle, last_evidence, last_error, _pack_domains(domains),
                        _needs_update_mask(domains), snapshot_hash, observed_iso,
                    ))

            if not records:
                raise ValueError("research_coverage_official_universe_has_no_valid_identity_rows")
            conn.execute("begin immediate")
            latest = conn.execute(
                """select snapshot_id,observed_at,summary_json
                   from autonomous_security_research_coverage_snapshots
                   where account_id=? order by observed_at desc,rowid desc limit 1""",
                (self.account_id,),
            ).fetchone()
            if latest is not None and (_utc(latest[1]) or observed_at) > observed_at:
                # A slower, older scan must not replace rows produced by a
                # newer concurrent scan. The research cycle remains retained,
                # while the current ledger continues to describe the newest
                # official-universe observation.
                conn.rollback()
                return {**json.loads(latest[2]), "snapshot_id": str(latest[0])}
            conn.execute(
                """update autonomous_security_research_coverage
                   set in_latest_universe=0,updated_at=?
                   where account_id=? and in_latest_universe=1""",
                (observed_iso, self.account_id),
            )
            conn.executemany(
                """insert into autonomous_security_research_coverage(
                       account_id,coverage_key,entity_id,symbol,venue,name,lifecycle_status,product_type,
                       new_entry_eligible,exclusion_reasons_json,in_latest_universe,universe_observed_at,
                       universe_expires_at,deep_research_status,last_deep_research_at,
                       last_deep_research_cycle_id,last_deep_research_evidence_id,last_deep_research_error,
                       data_domains_blob,needs_update_mask,snapshot_sha256,updated_at
                   ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   on conflict(account_id,coverage_key) do update set
                       entity_id=excluded.entity_id,symbol=excluded.symbol,venue=excluded.venue,name=excluded.name,
                       lifecycle_status=excluded.lifecycle_status,product_type=excluded.product_type,
                       new_entry_eligible=excluded.new_entry_eligible,
                       exclusion_reasons_json=excluded.exclusion_reasons_json,in_latest_universe=1,
                       universe_observed_at=excluded.universe_observed_at,
                       universe_expires_at=excluded.universe_expires_at,
                       deep_research_status=excluded.deep_research_status,
                       last_deep_research_at=excluded.last_deep_research_at,
                       last_deep_research_cycle_id=excluded.last_deep_research_cycle_id,
                       last_deep_research_evidence_id=excluded.last_deep_research_evidence_id,
                       last_deep_research_error=excluded.last_deep_research_error,
                       data_domains_blob=excluded.data_domains_blob,
                       needs_update_mask=excluded.needs_update_mask,
                       snapshot_sha256=excluded.snapshot_sha256,updated_at=excluded.updated_at""",
                records,
            )
            summary = self._summarize(
                security_count=len(records),
                new_entry_eligible_count=new_entry_eligible_count,
                deep_counts=deep_counts,
                lifecycle_counts=lifecycle_counts,
                domain_counts=domain_counts,
                all_domains_current=all_domains_current,
                observed_at=observed_at,
                source_cycle_id=source_cycle_id,
            )
            snapshot_id = "ACV-" + _sha(summary)
            conn.execute(
                """insert or ignore into autonomous_security_research_coverage_snapshots(
                       snapshot_id,account_id,observed_at,source_cycle_id,entity_count,summary_json,snapshot_sha256
                   ) values (?,?,?,?,?,?,?)""",
                (snapshot_id, self.account_id, observed_iso, source_cycle_id, len(records), _json(summary), _sha(summary)),
            )
            conn.commit()
        return {**summary, "snapshot_id": snapshot_id}

    def _summarize(
        self,
        *,
        security_count: int,
        new_entry_eligible_count: int,
        deep_counts: Counter[str],
        lifecycle_counts: Counter[str],
        domain_counts: dict[str, Counter[str]],
        all_domains_current: bool,
        observed_at: datetime,
        source_cycle_id: str | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": COVERAGE_SUMMARY_SCHEMA,
            "account_id": self.account_id,
            "observed_at": observed_at.isoformat(),
            "universe_expires_at": self._next_universe_expiry(observed_at).isoformat(),
            "source_cycle_id": source_cycle_id,
            "scope": "latest_official_research_universe_expanded_by_entity_identity",
            "security_count": security_count,
            "new_entry_eligible_count": new_entry_eligible_count,
            "deep_research_status_counts": dict(sorted(deep_counts.items())),
            "lifecycle_status_counts": dict(sorted(lifecycle_counts.items())),
            "data_domain_counts": {domain: dict(counts) for domain, counts in domain_counts.items()},
            "complete_deep_coverage": bool(security_count) and deep_counts.get("current", 0) == security_count,
            "all_domains_current": bool(security_count) and all_domains_current,
        }

    def latest_summary(self) -> dict[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute(
                """select snapshot_id,summary_json from autonomous_security_research_coverage_snapshots
                   where account_id=? order by observed_at desc,rowid desc limit 1""",
                (self.account_id,),
            ).fetchone()
        if not row:
            return {
                "schema_version": COVERAGE_SUMMARY_SCHEMA,
                "account_id": self.account_id,
                "status": "not_initialized",
                "security_count": 0,
                "complete_deep_coverage": False,
                "all_domains_current": False,
            }
        return {**json.loads(row[1]), "snapshot_id": str(row[0]), "status": "available"}

    def query(
        self,
        *,
        symbols: list[str] | None = None,
        deep_status: str | None = None,
        domain: str | None = None,
        needs_update: bool | None = None,
        new_entry_eligible: bool | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("research_coverage_limit_between_1_and_200")
        if deep_status is not None and deep_status not in DEEP_RESEARCH_STATES:
            raise ValueError("research_coverage_deep_status_invalid")
        if domain is not None and domain not in COVERAGE_DOMAINS:
            raise ValueError("research_coverage_domain_invalid")
        if needs_update is not None and domain is None:
            raise ValueError("research_coverage_needs_update_requires_domain")
        normalized_symbols = [str(symbol).strip().upper() for symbol in symbols or []]
        if (len(normalized_symbols) > 20 or len(set(normalized_symbols)) != len(normalized_symbols)
                or any(not re.fullmatch(r"[0-9]{4}[A-Z0-9]{0,2}\.TW(?:O)?", symbol)
                       for symbol in normalized_symbols)):
            raise ValueError("research_coverage_symbols_invalid")
        where = ["account_id=?", "in_latest_universe=1"]
        params: list[Any] = [self.account_id]
        if normalized_symbols:
            where.append("symbol in (" + ",".join("?" for _ in normalized_symbols) + ")")
            params.extend(normalized_symbols)
        if deep_status is not None:
            where.append("deep_research_status=?")
            params.append(deep_status)
        if new_entry_eligible is not None:
            where.append("new_entry_eligible=?")
            params.append(int(new_entry_eligible))
        if domain is not None and needs_update is not None:
            bit = 1 << COVERAGE_DOMAINS.index(domain)
            where.append(f"(needs_update_mask & {bit}) {'!=' if needs_update else '='} 0")
        if after:
            where.append("(symbol || char(31) || coverage_key)>?")
            params.append(str(after))
        with self.store._connect() as conn:
            rows = conn.execute(
                """select coverage_key,entity_id,symbol,venue,name,lifecycle_status,product_type,
                          new_entry_eligible,exclusion_reasons_json,universe_observed_at,universe_expires_at,
                          deep_research_status,last_deep_research_at,last_deep_research_cycle_id,
                          last_deep_research_evidence_id,last_deep_research_error,data_domains_blob,snapshot_sha256
                   from autonomous_security_research_coverage where """ + " and ".join(where)
                + " order by symbol,coverage_key limit ?",
                (*params, limit + 1),
            ).fetchall()
        items = []
        for row in rows:
            domains = _unpack_domains(row[16])
            item = {
                "schema_version": COVERAGE_SCHEMA,
                "coverage_key": row[0], "entity_id": row[1], "symbol": row[2], "venue": row[3],
                "name": row[4], "lifecycle_status": row[5], "product_type": row[6],
                "new_entry_eligible": bool(row[7]), "exclusion_reasons": json.loads(row[8]),
                "universe_observed_at": row[9], "universe_expires_at": row[10],
                "deep_research_status": row[11], "last_deep_research_at": row[12],
                "last_deep_research_cycle_id": row[13], "last_deep_research_evidence_id": row[14],
                "last_deep_research_error": row[15],
                "data_domains": {domain: domains[domain]} if domain else domains,
                "snapshot_sha256": row[17],
            }
            items.append(item)
            if len(items) > limit:
                break
        has_more = len(items) > limit
        items = items[:limit]
        next_after = f"{items[-1]['symbol']}\x1f{items[-1]['coverage_key']}" if has_more and items else None
        return {
            "schema_version": "open_stock_ai.security_research_coverage_query.v1",
            "summary": self.latest_summary(),
            "filters": {"symbols": normalized_symbols, "deep_status": deep_status, "domain": domain,
                        "needs_update": needs_update, "new_entry_eligible": new_entry_eligible},
            "items": items,
            "item_count": len(items),
            "next_after": next_after,
        }


__all__ = [
    "COVERAGE_DOMAINS",
    "COVERAGE_SCHEMA",
    "COVERAGE_SUMMARY_SCHEMA",
    "DEEP_RESEARCH_STATES",
    "SecurityResearchCoverageLedger",
]
