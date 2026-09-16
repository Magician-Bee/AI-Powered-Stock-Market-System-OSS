from __future__ import annotations

from typing import Any


VALUATION_MODEL_POLICY_SCHEMA_VERSION = "stock_ai.valuation_model_policy.v1"
EVIDENCE_CATEGORIES = (
    "reported_fact",
    "company_guidance",
    "analyst_estimate",
    "model_assumption",
)


class ValuationModelPolicyError(ValueError):
    pass


def select_valuation_models(
    *,
    industry_profile: str,
    profitable: bool,
    positive_free_cash_flow: bool,
    pays_dividend: bool,
    asset_heavy: bool,
    high_growth: bool,
) -> dict[str, Any]:
    industry = str(industry_profile or "").strip().casefold()
    if not industry:
        raise ValuationModelPolicyError("industry profile is required")
    applicable: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []

    def include(model: str, reason: str, role: str = "supporting") -> None:
        applicable.append({"model": model, "role": role, "reason": reason})

    def exclude(model: str, reason: str) -> None:
        excluded.append({"model": model, "reason": reason})

    if any(word in industry for word in ("金融", "銀行", "保險", "financial", "bank")):
        include("price_to_book", "金融業資產負債表與資本結構是核心", "primary")
        include("residual_income", "以帳面權益與超額報酬連結股東價值")
        if pays_dividend:
            include("dividend_discount", "存在可觀察的股利政策")
        else:
            exclude("dividend_discount", "未支付股利")
        exclude("ev_to_ebitda", "金融業負債是營運原料，企業價值倍數不具可比性")
        exclude("standard_dcf", "金融業自由現金流定義與一般企業不同")
    elif high_growth and not positive_free_cash_flow:
        include("peer_revenue_multiple", "高成長但尚無正自由現金流", "primary")
        include("scenario_valuation", "成長與轉正時點不確定，需用範圍呈現")
        exclude("standard_dcf", "目前自由現金流為負，終值將主導結果")
        exclude("price_to_earnings", "目前獲利基礎不足" if not profitable else "高成長獲利尚未穩定")
    elif asset_heavy:
        include("ev_to_ebitda", "重資產企業需比較營運資產與折舊前獲利", "primary")
        include("price_to_book", "資產基礎具有經濟意義")
        if positive_free_cash_flow:
            include("standard_dcf", "已有正自由現金流，可作情境交叉檢查")
        else:
            exclude("standard_dcf", "自由現金流尚未轉正")
    elif profitable and positive_free_cash_flow:
        include("standard_dcf", "獲利與自由現金流皆為正，可建立現金流情境", "primary")
        include("peer_earnings_multiple", "可用同業獲利倍數交叉檢查")
        if pays_dividend:
            include("dividend_discount", "存在可觀察股利政策")
    else:
        include("scenario_valuation", "獲利或現金流不穩定，優先揭露情境範圍", "primary")
        include("peer_operating_multiple", "使用與產業相符的營運尺度交叉比較")
        exclude("standard_dcf", "缺少穩定正自由現金流")
    return {
        "industry_profile": industry_profile,
        "company_characteristics": {
            "profitable": bool(profitable),
            "positive_free_cash_flow": bool(positive_free_cash_flow),
            "pays_dividend": bool(pays_dividend),
            "asset_heavy": bool(asset_heavy),
            "high_growth": bool(high_growth),
        },
        "applicable_models": applicable,
        "excluded_models": excluded,
        "policy": "industry_and_company_characteristics_not_one_formula_for_all",
    }


def separate_valuation_evidence(
    items: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    buckets = {category: [] for category in EVIDENCE_CATEGORIES}
    for raw in items:
        item = dict(raw)
        category = str(item.get("category") or "")
        if category not in buckets:
            raise ValuationModelPolicyError(f"unsupported evidence category: {category}")
        if item.get("value") is None:
            raise ValuationModelPolicyError("evidence value is required")
        if category != "model_assumption" and not item.get("source"):
            raise ValuationModelPolicyError(
                f"{category} requires an explicit source"
            )
        if category in {"reported_fact", "company_guidance", "analyst_estimate"} and not item.get("as_of"):
            raise ValuationModelPolicyError(f"{category} requires as_of")
        item["category"] = category
        item["is_model_input"] = category == "model_assumption"
        buckets[category].append(item)
    return buckets


def build_valuation_model_policy(
    *,
    industry_profile: str,
    profitable: bool,
    positive_free_cash_flow: bool,
    pays_dividend: bool,
    asset_heavy: bool,
    high_growth: bool,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selection = select_valuation_models(
        industry_profile=industry_profile,
        profitable=profitable,
        positive_free_cash_flow=positive_free_cash_flow,
        pays_dividend=pays_dividend,
        asset_heavy=asset_heavy,
        high_growth=high_growth,
    )
    return {
        "schema_version": VALUATION_MODEL_POLICY_SCHEMA_VERSION,
        "selection": selection,
        "evidence_layers": separate_valuation_evidence(evidence or []),
        "evidence_contract": {
            "categories": list(EVIDENCE_CATEGORIES),
            "reported_fact": "official or audited historical observation",
            "company_guidance": "issuer-provided forward-looking statement",
            "analyst_estimate": "named analyst/provider forecast",
            "model_assumption": "user-entered or model scenario input",
            "never_merge_categories_into_one_number": True,
        },
    }
