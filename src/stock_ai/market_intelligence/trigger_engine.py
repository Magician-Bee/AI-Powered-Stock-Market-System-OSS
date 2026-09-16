from __future__ import annotations


DEFAULT_REFRESH_CONDITIONS = [
    "價格突破或跌破候選觸發／失效價位",
    "成交量或價值出現異常",
    "法人、融資融券、借券或當沖資料更新",
    "財報、月營收、法說會、重大訊息或新聞事件發布",
    "市場 Regime、資料品質、自選股或 Paper OMS 持倉改變",
]


def refresh_conditions() -> list[str]:
    return list(DEFAULT_REFRESH_CONDITIONS)


def should_refresh(event_type: str) -> bool:
    return str(event_type or "").strip().lower() in {
        "price_breakout",
        "price_breakdown",
        "volume_anomaly",
        "institutional_update",
        "margin_update",
        "financial_release",
        "monthly_revenue",
        "material_event",
        "market_regime_change",
        "watchlist_change",
        "portfolio_change",
        "data_quality_change",
    }
