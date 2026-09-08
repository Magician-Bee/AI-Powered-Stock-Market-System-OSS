from __future__ import annotations


def entity_id_for_symbol(
    symbol: str,
    *,
    market: str,
    exchange: str,
    source_code: str | None = None,
) -> str:
    """Resolve a runtime symbol through Entity Registry with a neutral provisional fallback."""

    from .service import get_market_data_platform, stable_entity_id

    normalized_symbol = str(symbol or "").strip().upper()
    platform = get_market_data_platform()
    resolution = platform.resolve_entity(
        normalized_symbol,
        identifier_type="display_symbol",
    )
    if resolution["status"] == "resolved":
        return str(resolution["entity"]["entity_id"])
    if resolution["status"] == "ambiguous":
        raise LookupError(
            f"Display symbol {normalized_symbol!r} maps to multiple current entities"
        )
    code = str(source_code or normalized_symbol.split(".", 1)[0]).strip().upper()
    return stable_entity_id(
        market=market,
        exchange=exchange,
        source_code=code,
    )
