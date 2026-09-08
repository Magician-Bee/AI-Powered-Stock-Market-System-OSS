from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from threading import Lock
from typing import Any
from uuid import uuid4

from .broad_scanner import BroadScanner
from .contracts import (
    DecisionDataQualityReceipt,
    MarketIntelligenceSnapshot,
    ModelOverlay,
    UniverseSummary,
    WorkspaceContext,
    utc_now,
)
from .deep_analysis import select_deep_analysis_symbols
from .snapshot_store import SnapshotStore
from .trigger_engine import refresh_conditions


class MarketIntelligenceService:
    def __init__(
        self,
        *,
        store: SnapshotStore | None = None,
        scanner: BroadScanner | None = None,
    ) -> None:
        self.store = store or SnapshotStore()
        self.scanner = scanner or BroadScanner()
        self._scan_lock = Lock()

    def latest_snapshot(self) -> MarketIntelligenceSnapshot | None:
        return self.store.latest_snapshot()

    def snapshot(self, snapshot_id: str) -> MarketIntelligenceSnapshot | None:
        return self.store.snapshot(snapshot_id)

    def build_snapshot(self) -> MarketIntelligenceSnapshot:
        with self._scan_lock:
            previous = self.store.latest_snapshot()
            previous_ranks = (
                {
                    symbol: item.rank
                    for symbol, item in previous.candidate_details.items()
                }
                if previous
                else {}
            )
            scanned = self.scanner.scan(previous_ranks=previous_ranks)
            features = scanned["features"]
            valid = [
                item
                for item in features
                if (item.get("data_quality") or {}).get("status") == "ready"
            ]
            partial = [
                item
                for item in features
                if (item.get("data_quality") or {}).get("status") == "partial"
            ]
            insufficient = [
                item
                for item in features
                if (item.get("data_quality") or {}).get("status") == "insufficient"
            ]
            conflicts = [
                item
                for item in features
                if (item.get("data_quality") or {}).get("status") == "conflict"
            ]
            exchanges: dict[str, int] = {}
            for item in features:
                exchange = str(item.get("exchange") or "unknown")
                exchanges[exchange] = exchanges.get(exchange, 0) + 1
            data_dates = sorted(
                {
                    str(item.get("data_as_of"))
                    for item in features
                    if item.get("data_as_of")
                }
            )
            # A model result belongs to the exact market snapshot that
            # produced its evidence.  Never present a previous run as a
            # successful analysis of fresh prices/rankings.
            overlay = ModelOverlay()
            # A single-source universe is intentionally ``partial`` rather
            # than degraded: its primary data is still visible for research,
            # but the ranker must not create new actionable candidates until
            # it has independent same-day confirmation.  True missing or
            # conflicting primary inputs remain a degraded snapshot.
            status = "data_degraded" if not features or insufficient or conflicts else "partial"
            snapshot = MarketIntelligenceSnapshot(
                snapshot_id=f"MIS-{uuid4().hex}",
                generated_at=utc_now(),
                data_as_of=data_dates[-1] if data_dates else utc_now(),
                status=status,
                market_regime=scanned["market_regime"],
                universe=UniverseSummary(
                    resolved_count=len(features),
                    valid_data_count=len(valid),
                    partial_data_count=len(partial),
                    insufficient_data_count=len(insufficient),
                    exchanges=exchanges,
                    **scanned.get("universe_breakdown", {}),
                    classified_count=len(scanned["candidate_details"]),
                ),
                rankings=scanned["rankings"],
                portfolio_actions=scanned["portfolio_actions"],
                candidate_details=scanned["candidate_details"],
                data_quality={
                    "status": (
                        "ready"
                        if len(valid) == len(features)
                        else "partial"
                        if valid
                        else "insufficient"
                    ),
                    "valid_count": len(valid),
                    "partial_count": len(partial),
                    "insufficient_count": len(insufficient),
                    "conflict_count": len(conflicts),
                    "sources": ["TWSE_ALL_QUOTES", "TPEX_DAILY_QUOTES"],
                    "fallback": False,
                },
                model_overlay=overlay,
                risk_overlay={
                    "method": "deterministic_home_market_gate.v1",
                    "blocked_count": sum(
                        1
                        for item in scanned["candidate_details"].values()
                        if item.host_risk_status == "blocked"
                    ),
                    "host_verified": True,
                },
                next_refresh_conditions=refresh_conditions(),
                scan_statistics={
                    **scanned["statistics"],
                    "universe_breakdown": scanned.get("universe_breakdown", {}),
                    "deep_analysis_funnel": select_deep_analysis_symbols(
                        {"rankings": scanned["rankings"]},
                    ),
                },
            )
            certification_counts: dict[str, int] = {}
            for symbol, candidate in snapshot.candidate_details.items():
                candidate.data_quality_receipt = DecisionDataQualityReceipt.issue(
                    snapshot_id=snapshot.snapshot_id,
                    symbol=symbol,
                    decision_at=snapshot.generated_at,
                    snapshot_data_as_of=snapshot.data_as_of,
                    data_quality=candidate.data_quality,
                    evidence_ids=[item.evidence_id for item in candidate.evidence],
                )
                status_name = candidate.data_quality_receipt.certification_status
                certification_counts[status_name] = certification_counts.get(status_name, 0) + 1
            snapshot.data_quality = {
                **snapshot.data_quality,
                "decision_receipt_count": len(snapshot.candidate_details),
                "decision_receipt_certification_counts": certification_counts,
                "decision_receipt_schema": "stock_ai.decision_data_quality_receipt.v1",
            }
            return self.store.save_snapshot(snapshot)

    def run_scan(
        self,
        scan_id: str,
    ) -> MarketIntelligenceSnapshot:
        self.store.start_scan(scan_id)
        try:
            snapshot = self.build_snapshot()
        except Exception as exc:
            self.store.fail_scan(
                scan_id,
                {"type": type(exc).__name__, "message": str(exc)},
            )
            raise
        self.store.complete_scan(scan_id, snapshot.snapshot_id)
        return snapshot

    def create_scan(self, trigger: dict[str, Any] | None = None) -> dict[str, Any]:
        scan_id = f"MIS-SCAN-{uuid4().hex}"
        return self.store.create_scan(
            scan_id,
            trigger
            or {
                "event_type": "user_requested",
                "mode": "deterministic_whole_market",
                "llm_per_symbol": False,
            },
        )

    def apply_model_overlay(
        self,
        snapshot_id: str,
        *,
        succeeded: bool,
        provider: str | None = None,
        model_id: str | None = None,
        summaries: dict[str, dict[str, Any]] | None = None,
        receipt: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> MarketIntelligenceSnapshot:
        snapshot = self.store.snapshot(snapshot_id)
        if snapshot is None:
            raise KeyError(snapshot_id)
        if succeeded:
            if not receipt or receipt.get("status") != "succeeded":
                raise ValueError("a succeeded model overlay requires a succeeded invocation receipt")
            summaries = summaries or {}
            allowed = set(
                select_deep_analysis_symbols(
                    snapshot.model_dump(mode="json"),
                )
            )
            unknown = set(summaries) - allowed
            if unknown:
                raise ValueError(
                    "model overlay contains symbols outside the deterministic candidate funnel"
                )
            for symbol, summary in summaries.items():
                snapshot.candidate_details[symbol].model_overlay = deepcopy(summary)
                snapshot.candidate_details[symbol].model_status = "succeeded"
                snapshot.candidate_details[symbol].model_receipt = deepcopy(receipt)
            snapshot.model_overlay = ModelOverlay(
                status="succeeded",
                generated_at=utc_now(),
                provider=provider,
                model_id=model_id,
                analyzed_symbols=list(summaries),
                summaries=summaries,
            )
            snapshot.model_receipts.append(receipt)
            snapshot.status = "ready"
        else:
            for symbol in select_deep_analysis_symbols(snapshot.model_dump(mode="json")):
                if symbol in snapshot.candidate_details:
                    snapshot.candidate_details[symbol].model_status = "failed"
            previous = self.store.last_successful_model_overlay()
            if previous:
                snapshot.model_overlay = ModelOverlay.model_validate(
                    {
                        **previous,
                        "status": "failed",
                        "error": error
                        or {
                            "code": "model_failed",
                            "message": "AI 深度分析失敗；保留上一份成功 Overlay。",
                        },
                        "previous_success_generated_at": previous.get("generated_at"),
                    }
                )
            else:
                snapshot.model_overlay = ModelOverlay(
                    status="failed",
                    error=error
                    or {
                        "code": "model_failed",
                        "message": "AI 深度分析失敗；量化掃描結果仍可使用。",
                    },
                )
            snapshot.status = "model_failed"
        return self.store.save_snapshot(snapshot)

    def begin_model_overlay(
        self,
        snapshot_id: str,
        *,
        provider: str | None = None,
        model_id: str | None = None,
    ) -> MarketIntelligenceSnapshot:
        """Record an in-flight model analysis against this exact snapshot.

        A deterministic scan and its subsequent AI overlay are separate
        operations.  Recording the queued candidates avoids showing a
        misleading "0 analysed" state while a real Market Radar run is active.
        """
        snapshot = self.store.snapshot(snapshot_id)
        if snapshot is None:
            raise KeyError(snapshot_id)
        symbols = select_deep_analysis_symbols(snapshot.model_dump(mode="json"))
        for symbol in symbols:
            if symbol in snapshot.candidate_details:
                snapshot.candidate_details[symbol].model_status = "queued"
        snapshot.model_overlay = ModelOverlay(
            status="running",
            provider=provider,
            model_id=model_id,
            analyzed_symbols=[],
            summaries={},
        )
        return self.store.save_snapshot(snapshot)

    def context(self) -> WorkspaceContext:
        return self.store.context()

    def patch_context(self, updates: dict[str, Any]) -> WorkspaceContext:
        current = self.context().model_dump(mode="json")
        allowed = set(WorkspaceContext.model_fields) | {
            "selectedEntityId", "selectedSymbol", "selectedUniverse", "selectedCandidateId",
            "marketSnapshotId", "timeframe", "dateRange", "priceBasis", "indicators",
            "comparisonSymbols", "activePortfolio", "activeStrategy", "marketSession",
            "latestQuote", "dataFreshness", "agentSessionId", "agentRunId",
            "currentWorkspace", "activeWorkspaceTab", "activeDetailTab", "layoutState",
        }
        def merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
            merged = dict(base)
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key] = merge(merged[key], value)
                else:
                    merged[key] = value
            return merged

        next_payload = merge(
            current,
            {key: value for key, value in updates.items() if key in allowed},
        )
        next_payload["revision"] = current.get("revision", 0) + 1
        return self.store.save_context(WorkspaceContext.model_validate(next_payload))


@lru_cache(maxsize=1)
def get_market_intelligence_service() -> MarketIntelligenceService:
    return MarketIntelligenceService()
