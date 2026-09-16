from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from open_stock_ai.config.settings import load_settings

from stock_ai.config import get_settings

from .availability import get_data_availability_registry
from .contracts import EntityRecord
from .news_history_coverage import NewsHistoryCoverageStore
from .service import MarketDataPlatform, get_market_data_platform, stable_entity_id


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        return {key: _dump(item) for key, item in value.items()}
    return value


@lru_cache(maxsize=1)
def _default_news_coverage_store() -> NewsHistoryCoverageStore:
    path = Path(load_settings().sqlite_path).expanduser()
    if not path.is_absolute():
        path = get_settings().project_root / path
    return NewsHistoryCoverageStore(path)


def get_news_history_coverage_store() -> NewsHistoryCoverageStore:
    """Expose the runtime's immutable news coverage evidence store read-only."""

    return _default_news_coverage_store()


class UnifiedResearchDataGateway:
    """The only connector-facing ingress used by research pipelines.

    Connector calls are deliberately contained here. Consumers receive normalized
    bundle fields and warehouse revisions instead of invoking source modules.
    """

    def __init__(self, platform: MarketDataPlatform | None = None) -> None:
        self.platform = platform or get_market_data_platform()

    def load(self, *, symbol: str, market: str) -> dict[str, Any]:
        raw: dict[str, Any] = {"errors": {}}
        raw["data_availability_audit"] = self.platform.source_registry_service.availability_audit
        price: float | None = None
        price_source: str | None = None
        price_timestamp: str | None = None
        price_is_fallback = False
        ohlcv: list[dict[str, Any]] = []
        news: list[dict[str, Any]] = []
        financials: dict[str, Any] = {}
        chips: dict[str, Any] = {}
        announcements: list[dict[str, Any]] = []

        try:
            from stock_ai.services import get_market_detail_summary

            # This gateway is the research ingress, rather than an order-price
            # resolver.  The detail service returns a realtime quote when one
            # exists and otherwise an explicitly labelled official close.  Keep
            # the latter as a fallback so downstream contracts can allow
            # advisory analysis without ever treating it as executable.
            summary = get_market_detail_summary(symbol)
            raw["summary"] = _dump(summary)
            raw["summary_mode"] = (
                "official_close_research_fallback"
                if summary is not None and getattr(summary, "official_close", False)
                else "realtime"
                if summary is not None
                else "unavailable"
            )
            if summary:
                price = float(summary.latest_price.close)
                price_source = str(summary.data_source or "primary_market_summary")
                price_timestamp = str(summary.data_timestamp or summary.latest_price.date or "") or None
                price_is_fallback = bool(getattr(summary, "official_close", False))
                announcements = _dump(getattr(summary, "events", []))
        except Exception as exc:
            raw["summary"] = None
            raw["summary_mode"] = "unavailable"
            raw["errors"]["summary"] = {"type": type(exc).__name__, "message": str(exc)}

        try:
            from stock_ai.services import get_price_history

            history = get_price_history(symbol)
            ohlcv = _dump(history)
            if price is None and history:
                latest = history[-1]
                price = float(latest.close)
                price_source = "latest_available_history_close"
                price_timestamp = str(getattr(latest, "date", "") or "") or None
                price_is_fallback = True
            self._persist_price_history(
                symbol=symbol,
                market=market,
                source_label=price_source,
                records=ohlcv,
                is_fallback=price_is_fallback,
            )
        except Exception as exc:
            raw["errors"]["history"] = {"type": type(exc).__name__, "message": str(exc)}

        try:
            from stock_ai.mvp_features import get_news_center
            from stock_ai.data_platform.news_event_lake import ingest_news_events

            news_payload = get_news_center(symbol=symbol, limit=8)
            raw["news_center"] = _dump(news_payload)
            news = _dump(news_payload.get("items", []))
            resolution = self.platform.resolve_entity(symbol, identifier_type="display_symbol")
            if resolution.get("status") == "resolved":
                raw["news_event_lake"] = ingest_news_events(
                    self.platform,
                    entity_id=str((resolution.get("entity") or {})["entity_id"]),
                    items=news,
                    coverage_store=_default_news_coverage_store(),
                )
            else:
                raw["news_event_lake"] = {
                    "schema_version": "stock_ai.news_event_lake.v1",
                    "accepted_count": 0,
                    "historical_pit_eligible_count": 0,
                    "rejected": [{"reason": "news_entity_resolution_unavailable", "title": symbol}],
                }
        except Exception as exc:
            raw["errors"]["news"] = {"type": type(exc).__name__, "message": str(exc)}

        try:
            from stock_ai.phase1_data import list_margin_trading, list_monthly_revenues

            margin_items = list_margin_trading(symbol=symbol, limit=1)
            revenue_items = list_monthly_revenues(symbol=symbol, limit=1)
            raw["margin"] = _dump(margin_items)
            raw["revenue"] = _dump(revenue_items)
            if margin_items:
                chips["margin"] = _dump(margin_items[0])
            if revenue_items:
                financials["revenue"] = _dump(revenue_items[0])
        except Exception as exc:
            raw["errors"]["phase1"] = {"type": type(exc).__name__, "message": str(exc)}

        return {
            "schema_version": "stock_ai.research_data_bundle.v1",
            "symbol": symbol,
            "market": market,
            "price": price,
            "price_source": price_source,
            "price_timestamp": price_timestamp,
            "price_is_fallback": price_is_fallback,
            "ohlcv": ohlcv,
            "news": news,
            "financials": financials,
            "chips": chips,
            "announcements": announcements,
            "raw": raw,
        }

    def _persist_price_history(
        self,
        *,
        symbol: str,
        market: str,
        source_label: str | None,
        records: list[dict[str, Any]],
        is_fallback: bool,
    ) -> None:
        if not records:
            return
        source_id = self._price_source_id(symbol=symbol, source_label=source_label)
        resolution = self.platform.resolve_entity(
            symbol,
            identifier_type="display_symbol",
        )
        if resolution["status"] == "resolved":
            entity_id = str(resolution["entity"]["entity_id"])
        else:
            exchange = "TPEx" if symbol.upper().endswith(".TWO") else "TWSE"
            canonical_market = "taiwan" if market.casefold() in {"tw", "taiwan"} else market
            entity_id = stable_entity_id(
                market=canonical_market,
                exchange=exchange,
                source_code=symbol.split(".", 1)[0],
            )
            self.platform.warehouse.upsert_entity(
                EntityRecord(
                    entity_id=entity_id,
                    entity_type="stock",
                    canonical_name=symbol,
                    market=canonical_market,
                    exchange=exchange,
                    currency="TWD" if canonical_market == "taiwan" else None,
                    lifecycle_status="unknown",
                    metadata={"display_symbol": symbol, "provisional": True},
                ),
                identifiers=(
                    {
                        "source_id": source_id,
                        "identifier_type": "display_symbol",
                        "identifier_value": symbol,
                        "confidence": 0.75,
                        "is_primary": True,
                        "metadata": {"provisional": True},
                    },
                ),
            )
        availability_contract = get_data_availability_registry().resolve(
            source_id=source_id,
            dataset="prices_daily",
        )
        self.platform.ingest_records(
            source_id=source_id,
            dataset="prices_daily",
            records=records,
            entity_id_for=lambda _row: entity_id,
            observation_key_for=lambda row: str(row["date"]),
            observed_at_for=lambda row: str(row["date"]),
            published_at_for=None,
            available_at_for=lambda row: availability_contract.receipt(
                observed_at=str(row["date"]),
                published_at=None,
                acquired_at=None,
            )["available_at"],
            effective_at_for=lambda row: str(row["date"]),
            time_basis="trade_date",
            trade_date_for=lambda row: str(row["date"]),
            is_fallback=is_fallback or source_id == "yahoo_finance",
            transformation_id="stock_ai.price_history_normalizer.v1",
        )

    @staticmethod
    def _price_source_id(*, symbol: str, source_label: str | None) -> str:
        label = str(source_label or "").casefold()
        if "yahoo" in label or "fallback" in label:
            return "yahoo_finance"
        if "tpex" in label or symbol.upper().endswith(".TWO"):
            return "tpex_openapi"
        return "twse_openapi"
