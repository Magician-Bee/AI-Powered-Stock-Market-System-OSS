from __future__ import annotations

from datetime import datetime, timezone

from open_stock_ai.research.pit_dataset import (
    DATASET_MANIFEST_SCHEMA_VERSION,
    PointInTimeDatasetBuilder,
)
from open_stock_ai.types import MarketSnapshot, StockRequest
from stock_ai.data_platform.gateway import UnifiedResearchDataGateway

from .execution_quote import execution_eligibility, quote_envelope
from .mops_source import MOPSSource
from .news_source import NewsSource
from .tpex_source import TPExSource
from .twse_source import TWSESource
from .yahoo_source import YahooSource


class MarketDataHub:
    """Bridge OpenStockAI to the stock_ai data layer with one decision contract.

    The hub still collects auxiliary and fallback data for agent research, but it
    separately marks whether the snapshot is eligible to influence an executable
    decision. This lets an embedded Agent inspect everything without confusing a
    delayed/fallback value with a current tradable quote.
    """

    def load(self, request: StockRequest) -> MarketSnapshot:
        gateway = UnifiedResearchDataGateway()
        bundle = gateway.load(
            symbol=request.symbol,
            market=request.market,
        )
        raw = bundle["raw"]
        price = bundle["price"]
        price_source = bundle["price_source"]
        price_timestamp = bundle["price_timestamp"]
        price_is_fallback = bundle["price_is_fallback"]
        ohlcv = bundle["ohlcv"]
        news = bundle["news"]
        financials = bundle["financials"]
        chips = bundle["chips"]
        announcements = bundle["announcements"]
        raw["unified_data_bundle"] = {
            "schema_version": bundle["schema_version"],
            "symbol": bundle["symbol"],
            "market": bundle["market"],
        }
        pit_dataset = self._point_in_time_dataset(
            gateway=gateway,
            symbol=request.symbol,
        )
        raw["point_in_time_dataset_manifest"] = pit_dataset
        if pit_dataset.get("exact_replay_eligible") is True:
            raw["point_in_time_dataset"] = list(pit_dataset.get("replay_rows") or [])
            raw["point_in_time_dataset_features"] = list(pit_dataset.get("features") or [])

        source_envelopes = {
            "twse": TWSESource().normalize(
                request.symbol,
                request.market,
                {
                    "summary": raw.get("summary"),
                    "margin": raw.get("margin", []),
                    "revenue": raw.get("revenue", []),
                },
            ),
            "tpex": TPExSource().normalize(
                request.symbol,
                request.market,
                {"summary": raw.get("summary")} if request.market == "TW" else {},
            ),
            "yahoo": YahooSource().normalize(
                request.symbol,
                request.market,
                {
                    "price": price,
                    "ohlcv": ohlcv,
                    "news": news,
                },
            ),
            "mops": MOPSSource().normalize(request.symbol, request.market, announcements),
            "news": NewsSource().normalize(request.symbol, request.market, news),
        }
        raw["source_envelopes"] = source_envelopes

        source_names = [price_source] if price_source else []
        for envelope in source_envelopes.values():
            if envelope.get("loaded") and envelope.get("source_name"):
                source_names.append(str(envelope["source_name"]))
        source_names = list(dict.fromkeys(item for item in source_names if item))

        try:
            from stock_ai.source_policy import evaluate_source_policy

            source_policy = evaluate_source_policy(
                {
                    "payload_type": "price",
                    "latest_price": price,
                    "data_sources": source_names,
                }
            )
        except Exception as exc:
            source_policy = {
                "overall_allowed": False,
                "price_policy": {"allowed": False, "blockers": ["source_policy_unavailable"]},
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }

        blockers = list((source_policy.get("price_policy") or {}).get("blockers") or [])
        if price is None:
            blockers.append("price_unavailable")
        if raw.get("summary") is None:
            blockers.append("primary_market_summary_unavailable")
        if price_is_fallback:
            blockers.append("fallback_price_not_execution_eligible")
        summary_payload = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        source_contract = quote_envelope(
            provider_id=str(summary_payload.get("provider_id") or "unknown"),
            connector_id=str(summary_payload.get("connector_id") or "unknown"),
            quote_kind=str(summary_payload.get("quote_kind") or "unknown"),
            exchange_timestamp=price_timestamp,
            received_at=summary_payload.get("data_timestamp"),
            max_age_seconds=int(summary_payload.get("max_age_seconds") or 86400),
            authorized=summary_payload.get("authorized") is True,
            realtime=summary_payload.get("realtime") is True,
            delayed=summary_payload.get("delayed") is True,
            official_close=summary_payload.get("official_close") is True,
        )
        horizon_gate = execution_eligibility(source_contract, horizon=request.horizon)
        blockers.extend(horizon_gate["blockers"])
        decision_ready = not blockers and source_policy.get("overall_allowed") is True
        analysis_ready = bool(
            price is not None
            and raw.get("summary") is not None
            and horizon_gate.get("research_eligible") is True
        )
        analysis_mode = (
            "unavailable"
            if not analysis_ready
            else "official_close_advisory"
            if price_is_fallback or source_contract["official_close"] is True
            else "current_quote_advisory"
        )
        analysis_blockers: list[str] = []
        if price is None:
            analysis_blockers.append("price_unavailable")
        if raw.get("summary") is None:
            analysis_blockers.append("market_summary_unavailable")
        if horizon_gate.get("research_eligible") is not True:
            analysis_blockers.append("source_not_research_eligible")
        raw["data_contract"] = {
            "schema_version": "open_stock_ai.market_data_contract.v3",
            "method": "single_market_data_truth_gate",
            "symbol": request.symbol,
            "market": request.market,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "price_source": price_source,
            "exchange_timestamp": price_timestamp,
            "is_realtime": source_contract["realtime"],
            "is_fallback": price_is_fallback,
            "is_simulated": False,
            "source_names": source_names,
            "source_policy": source_policy,
            "decision_ready": decision_ready,
            "execution_eligible": decision_ready,
            "analysis_ready": analysis_ready,
            "analysis_mode": analysis_mode,
            "analysis_blockers": analysis_blockers,
            "source_envelope": source_contract,
            "horizon_gate": horizon_gate,
            "blockers": list(dict.fromkeys(blockers)),
            "agent_research_allowed": analysis_ready,
        }

        return MarketSnapshot(
            symbol=request.symbol,
            market=request.market,
            price=price,
            ohlcv=ohlcv,
            news=news,
            financials=financials,
            chips=chips,
            announcements=announcements,
            raw=raw,
        )

    @staticmethod
    def _point_in_time_dataset(
        *,
        gateway: UnifiedResearchDataGateway,
        symbol: str,
    ) -> dict:
        """Expose PIT availability as a research receipt, never an assumption.

        The live screen normally retrieves historical bars at the present time,
        so those bars are intentionally not promoted into an exact replay.  A
        dedicated historical source must provide the original availability
        times, all required feature domains, and PIT intelligence first.
        """

        try:
            resolution = gateway.platform.resolve_entity(
                symbol,
                identifier_type="display_symbol",
            )
            if resolution.get("status") != "resolved":
                return {
                    "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
                    "entity_id": None,
                    "feature_count": 0,
                    "replay_row_count": 0,
                    "exact_replay_eligible": False,
                    "blockers": ["pit_entity_resolution_unavailable"],
                }
            entity_id = str((resolution.get("entity") or {}).get("entity_id") or "")
            if not entity_id:
                raise ValueError("resolved entity has no entity_id")
            # A live single-stock request must stay responsive.  It has no
            # immutable historical intelligence stream to supply to an exact
            # replay, so scanning the entire warehouse here would be both
            # expensive and semantically misleading.  Historical replay jobs
            # materialize the warehouse-backed dataset explicitly through the
            # builder; the interactive path returns the same explicit blocker.
            dataset = PointInTimeDatasetBuilder().build_from_records(
                entity_id=entity_id,
                as_of=datetime.now(timezone.utc).isoformat(),
                records_by_domain={},
            )
            return dataset.to_dict()
        except Exception as exc:
            return {
                "schema_version": DATASET_MANIFEST_SCHEMA_VERSION,
                "entity_id": None,
                "feature_count": 0,
                "replay_row_count": 0,
                "exact_replay_eligible": False,
                "blockers": ["pit_dataset_build_failed"],
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
