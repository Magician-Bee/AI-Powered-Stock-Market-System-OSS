from __future__ import annotations

from typing import Any


SCHEMA_VERSION = "stock_ai.source_policy.v1"

PRICE_FIELDS = {
    "price",
    "last_price",
    "latest_price",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "vwap",
}

DECISION_REQUIRED_FIELDS = {
    "technical_reason": "技術面理由",
    "chip_reason": "籌碼面理由",
    "fundamental_reason": "基本面理由",
    "news_reason": "新聞/事件面理由",
    "risk": "風險因素",
    "entry_price": "進場價",
    "stop_loss": "停損價",
    "take_profit": "停利價",
    "invalid_condition": "失效條件",
    "confidence": "信心分數",
    "confidence_type": "信心類型",
    "confidence_calibrated": "是否校準",
    "data_timestamp": "資料時間戳記",
}


SOURCE_PATTERNS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "broker_or_authorized_realtime", ("broker", "券商", "富邦", "neo api", "shioaji", "spark", "authorized realtime", "授權即時")),
    (1, "twse_realtime_quote", ("twse mis", "mis.twse", "realtime quote", "即時報價", "即時行情")),
    (2, "official_exchange_or_disclosure", ("twse openapi", "tpex openapi", "mops", "tdcc", "taifex", "t86", "官方", "證交所", "櫃買", "公開資訊")),
    (2, "official_financial_or_chip_data", ("monthly revenue", "月營收", "三大法人", "融資融券", "集保", "財報")),
    (3, "auxiliary_news_or_vendor", ("yahoo", "google news", "finmind", "moneydj", "鉅亨", "工商", "經濟日報", "新聞", "news")),
    (3, "local_preview_or_research", ("daily_report", "watchlist", "notification", "local_preview", "strategy", "research", "backtest")),
)


def classify_source(source: str) -> dict[str, Any]:
    raw = str(source or "").strip()
    lowered = raw.lower()
    for tier, source_type, patterns in SOURCE_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return {"source": raw, "tier": tier, "source_type": source_type}
    return {"source": raw, "tier": None, "source_type": "unknown"}


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _extract_sources(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("data_sources", "sources", "price_sources", "news_sources"):
        raw = payload.get(key)
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(str(item) for item in raw if item)
    data_source = payload.get("data_source")
    if data_source:
        values.append(str(data_source))
    return list(dict.fromkeys(item.strip() for item in values if item and item.strip()))


def _has_price_payload(payload: dict[str, Any]) -> bool:
    if payload.get("payload_type") in {"price", "quote", "ohlcv", "decision"}:
        return True
    return any(field in payload and _non_empty(payload.get(field)) for field in PRICE_FIELDS)


def evaluate_source_policy(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {})
    sources = _extract_sources(payload)
    classified = [classify_source(source) for source in sources]
    tier_counts = {
        "tier_1": sum(1 for item in classified if item["tier"] == 1),
        "tier_2": sum(1 for item in classified if item["tier"] == 2),
        "tier_3": sum(1 for item in classified if item["tier"] == 3),
        "unknown": sum(1 for item in classified if item["tier"] is None),
    }

    has_price_payload = _has_price_payload(payload)
    has_primary_price_source = any(item["tier"] in {1, 2} for item in classified)
    auxiliary_only = bool(classified) and all(item["tier"] == 3 for item in classified)
    price_blockers: list[str] = []
    if has_price_payload and not has_primary_price_source:
        price_blockers.append("price_requires_tier_1_or_tier_2_source")
    if has_price_payload and auxiliary_only:
        price_blockers.append("auxiliary_source_cannot_be_sole_price_source")

    missing_decision_fields = [
        label for field, label in DECISION_REQUIRED_FIELDS.items() if not _non_empty(payload.get(field))
    ]
    source_count = len(sources)
    if source_count < 2 and payload.get("payload_type") == "decision":
        missing_decision_fields.append("至少兩個資料來源")
    decision_blockers = [f"missing:{label}" for label in missing_decision_fields]

    conflict_detected = bool(payload.get("conflicting_sources") or payload.get("source_conflict"))
    conflict_note = str(payload.get("conflict_note") or "").strip()
    conflict_blockers = []
    if conflict_detected and not conflict_note:
        conflict_blockers.append("source_conflict_requires_conflict_note")

    price_allowed = not price_blockers
    decision_allowed = not decision_blockers if payload.get("payload_type") == "decision" else True
    conflict_allowed = not conflict_blockers
    overall_allowed = price_allowed and decision_allowed and conflict_allowed

    return {
        "schema_version": SCHEMA_VERSION,
        "method": "local_source_tier_and_decision_evidence_gate",
        "rules": [
            {
                "code": "price_requires_primary_source",
                "rule": "價格、K 線、成交量資料必須至少有第 1 層或第 2 層來源。",
            },
            {
                "code": "auxiliary_cannot_be_sole_price_source",
                "rule": "Yahoo、Google、新聞或輔助資料不可作為唯一行情來源。",
            },
            {
                "code": "decision_requires_multi_factor_evidence",
                "rule": "交易建議必須包含技術、籌碼、基本面、新聞/事件、風險、進出場、停損停利、失效條件、信心與時間戳記。",
            },
            {
                "code": "source_conflict_must_be_marked",
                "rule": "資料來源衝突時必須標記資料衝突，不能假裝確定。",
            },
        ],
        "sources": classified,
        "source_summary": {
            "source_count": source_count,
            **tier_counts,
        },
        "price_policy": {
            "has_price_payload": has_price_payload,
            "has_primary_price_source": has_primary_price_source,
            "auxiliary_only": auxiliary_only,
            "allowed": price_allowed,
            "blockers": price_blockers,
        },
        "decision_policy": {
            "evaluated": payload.get("payload_type") == "decision",
            "allowed": decision_allowed,
            "missing_fields": missing_decision_fields,
            "blockers": decision_blockers,
        },
        "conflict_policy": {
            "conflict_detected": conflict_detected,
            "conflict_note_present": bool(conflict_note),
            "allowed": conflict_allowed,
            "blockers": conflict_blockers,
        },
        "overall_allowed": overall_allowed,
    }


def source_policy_status() -> dict[str, Any]:
    sample_price = evaluate_source_policy(
        {
            "payload_type": "price",
            "last_price": 100,
            "data_sources": ["Yahoo Finance"],
        }
    )
    sample_decision = evaluate_source_policy(
        {
            "payload_type": "decision",
            "technical_reason": "站上均線",
            "chip_reason": "法人買超",
            "fundamental_reason": "月營收成長",
            "news_reason": "無重大利空",
            "risk": "波動偏高",
            "entry_price": 100,
            "stop_loss": 95,
            "take_profit": 110,
            "invalid_condition": "跌破支撐",
            "confidence": 0.7,
            "confidence_type": "rule_score",
            "confidence_calibrated": False,
            "data_timestamp": "2026-07-10T09:05:00+08:00",
            "data_sources": ["TWSE MIS", "MOPS"],
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "method": "source_policy_status",
        "sample_auxiliary_only_price": sample_price,
        "sample_complete_decision": sample_decision,
        "guardrails_enforced": {
            "auxiliary_source_cannot_be_sole_price_source": not sample_price["price_policy"]["allowed"],
            "complete_decision_evidence_allowed": sample_decision["overall_allowed"],
            "source_conflict_requires_note": not evaluate_source_policy(
                {"payload_type": "decision", "conflicting_sources": True, "data_sources": ["TWSE MIS", "Yahoo Finance"]}
            )["conflict_policy"]["allowed"],
        },
    }
