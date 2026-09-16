from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo
import re

from .contracts import EntityRecord, SecuritySourceSnapshot, normalize_timestamp
from .source_registry import source_endpoint


_NON_IDENTITY = re.compile(r"[\s\u3000\-_/.,，。()（）]+")
_VENUE_PRIORITY = {"TWSE": 40, "TPEx": 30, "TPEx-ESB": 20}


def _text(value: Any) -> str:
    return str(value or "").replace("\u3000", " ").strip()


def _date(value: Any) -> str | None:
    text = re.sub(r"\D", "", _text(value))
    if len(text) == 7:
        text = f"{int(text[:3]) + 1911:04d}{text[3:]}"
    if len(text) != 8:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _name_key(value: str) -> str:
    normalized = _NON_IDENTITY.sub("", value).casefold()
    for suffix in ("股份有限公司", "有限責任公司", "有限公司"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def _business_no(value: Any) -> str | None:
    digits = re.sub(r"\D", "", _text(value))
    # Some foreign issuers are published with the placeholder 00000000.
    # Treating a placeholder as corporate identity merges unrelated companies.
    if len(digits) != 8 or len(set(digits)) == 1:
        return None
    return digits


def _index_code(venue: str, name: str) -> str:
    return f"{venue}-INDEX-{sha256(name.encode('utf-8')).hexdigest()[:12].upper()}"


def _validated_catalogue_issue(
    issue: Any, *, venue: str, code: str, acquired_at: str,
) -> dict[str, Any]:
    """Recheck the Host's retained-byte projection without fetching any source."""
    from .catalogue_identity import CATALOGUES, _instant, _issue_row
    from .source_registry import get_source_registry
    from .warehouse import content_hash

    try:
        if not isinstance(issue, dict):
            raise ValueError("verified_catalogue_issuance_unavailable")
        receipt = issue["product_classification"]
        current = _instant(acquired_at)
        observed = _instant(receipt["acquired_at"])
        age = (current - observed).total_seconds()
        source_age = (current.astimezone(ZoneInfo("Asia/Taipei")).date()
                      - datetime.fromisoformat(receipt["source_updated_on"]).date()).days
        if age < 0 or age > get_source_registry().source("twse_isin").update_frequency_seconds or not 0 <= source_age <= 1:
            raise ValueError("catalogue_issuance_not_fresh")
        if (
            receipt["schema_version"] != "stock_ai.product_classification.v1"
            or receipt["status"] != "verified" or receipt["source_id"] != "twse_isin"
            or receipt["source_dataset"] not in CATALOGUES
            or receipt["source_url"] != source_endpoint(receipt["source_dataset"])
            or CATALOGUES[receipt["source_dataset"]][0] != venue
            or receipt["symbol"] != f"{code}.{CATALOGUES[receipt['source_dataset']][2]}"
            or receipt["venue"] != venue or issue["code"] != code
            or not str(receipt["raw_payload_id"]).startswith("RAW-")
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["raw_sha256"])
            or content_hash(receipt["source_row"]) != receipt["row_sha256"]
            or _issue_row(receipt, as_of=current) != issue
        ):
            raise ValueError("catalogue_issuance_binding_invalid")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("catalogue_issuance_binding_invalid") from exc
    except ValueError as exc:
        if str(exc).startswith("catalogue_issuance_") or str(exc) == "verified_catalogue_issuance_unavailable":
            raise
        raise ValueError("catalogue_issuance_binding_invalid") from exc
    return issue


def _company_snapshots(
    *,
    source_id: str,
    source_dataset: str,
    source_url: str,
    venue: str,
    listing_type: str,
    companies: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    company_code: str,
    quote_code: str,
    short_name: str,
    legal_name: str,
    quote_name: str,
    industry: str,
    listed_at: str,
    business_no: str,
    symbol_suffix: str,
) -> list[SecuritySourceSnapshot]:
    company_map = {
        _text(row.get(company_code)): row
        for row in companies
        if _text(row.get(company_code))
    }
    quote_map = {
        _text(row.get(quote_code)): row
        for row in quotes
        if _text(row.get(quote_code))
    }
    snapshots: list[SecuritySourceSnapshot] = []
    for code in sorted(set(company_map) | set(quote_map)):
        company = company_map.get(code, {})
        quote = quote_map.get(code, {})
        short = _text(
            quote.get(quote_name)
            or company.get(short_name)
            or company.get(legal_name)
            or code
        )
        legal = _text(company.get(legal_name) or short or code)
        is_etf = code.startswith("00") and listing_type != "emerging"
        snapshots.append(
            SecuritySourceSnapshot(
                source_id=source_id,
                source_dataset=source_dataset,
                source_url=source_url,
                venue=venue,
                listing_type="etf" if is_etf else listing_type,
                entity_type="etf" if is_etf else "stock",
                code=code,
                display_symbol=f"{code}{symbol_suffix}",
                short_name=short,
                legal_name=legal,
                unified_business_no=_business_no(company.get(business_no)),
                lifecycle_status="active" if quote else "pre_listing",
                listed_at=_date(company.get(listed_at)),
                industry=_text(company.get(industry)) or ("ETF" if is_etf else None),
                quote_present=bool(quote),
                raw_row={"company": company, "quote": quote},
            )
        )
    return snapshots


def normalize_security_payloads(
    payloads: dict[str, list[dict[str, Any]]],
    *,
    acquired_at: str,
    issue_bindings: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[SecuritySourceSnapshot]:
    """Normalize official TWSE/TPEx master payloads without choosing an identity."""

    snapshots: list[SecuritySourceSnapshot] = []
    if "twse_companies" in payloads or "twse_quotes" in payloads:
        snapshots.extend(
            _company_snapshots(
                source_id="twse_openapi",
                source_dataset="twse_companies_quotes",
                source_url=source_endpoint("twse_companies"),
                venue="TWSE",
                listing_type="listed",
                companies=payloads.get("twse_companies", []),
                quotes=payloads.get("twse_quotes", []),
                company_code="公司代號",
                quote_code="Code",
                short_name="公司簡稱",
                legal_name="公司名稱",
                quote_name="Name",
                industry="產業別",
                listed_at="上市日期",
                business_no="營利事業統一編號",
                symbol_suffix=".TW",
            )
        )
    if "tpex_companies" in payloads or "tpex_quotes" in payloads:
        snapshots.extend(
            _company_snapshots(
                source_id="tpex_openapi",
                source_dataset="tpex_otc_companies_quotes",
                source_url=source_endpoint("tpex_companies"),
                venue="TPEx",
                listing_type="otc",
                companies=payloads.get("tpex_companies", []),
                quotes=payloads.get("tpex_quotes", []),
                company_code="SecuritiesCompanyCode",
                quote_code="SecuritiesCompanyCode",
                short_name="CompanyAbbreviation",
                legal_name="CompanyName",
                quote_name="CompanyName",
                industry="SecuritiesIndustryCode",
                listed_at="DateOfListing",
                business_no="UnifiedBusinessNo.",
                symbol_suffix=".TWO",
            )
        )
    if "tpex_emerging_companies" in payloads or "tpex_emerging_quotes" in payloads:
        snapshots.extend(
            _company_snapshots(
                source_id="tpex_openapi",
                source_dataset="tpex_emerging_companies_quotes",
                source_url=source_endpoint("tpex_emerging_companies"),
                venue="TPEx-ESB",
                listing_type="emerging",
                companies=payloads.get("tpex_emerging_companies", []),
                quotes=payloads.get("tpex_emerging_quotes", []),
                company_code="SecuritiesCompanyCode",
                quote_code="SecuritiesCompanyCode",
                short_name="CompanyAbbreviation",
                legal_name="CompanyName",
                quote_name="CompanyName",
                industry="SecuritiesIndustryCode",
                listed_at="DateOfListing",
                business_no="UnifiedBusinessNo.",
                symbol_suffix=".TWO",
            )
        )

    for row in payloads.get("twse_etfs", []):
        code = _text(row.get("基金代號"))
        if not code:
            continue
        short = _text(row.get("基金簡稱") or code)
        snapshots.append(
            SecuritySourceSnapshot(
                source_id="twse_openapi",
                source_dataset="twse_etfs",
                source_url=source_endpoint("twse_etfs"),
                venue="TWSE",
                listing_type="etf",
                entity_type="etf",
                code=code,
                display_symbol=f"{code}.TW",
                short_name=short,
                legal_name=_text(row.get("基金中文名稱") or short),
                unified_business_no=_business_no(row.get("基金統一編號")),
                lifecycle_status="active",
                listed_at=_date(row.get("上市日期")),
                industry=_text(row.get("基金類型")) or "ETF",
                quote_present=True,
                raw_row=row,
                metadata={
                    "benchmark": _text(row.get("標的指數/追蹤指數名稱")) or None,
                    "inception_date": _date(row.get("成立日期")),
                },
            )
        )

    today = datetime.fromisoformat(normalize_timestamp(acquired_at, required=True)).astimezone(ZoneInfo("Asia/Taipei")).date()
    for source_dataset, source_id, venue, source_url, rows in (
        (
            "twse_warrants",
            "twse_openapi",
            "TWSE",
            source_endpoint("twse_warrants"),
            payloads.get("twse_warrants", []),
        ),
        (
            "tpex_warrants",
            "tpex_openapi",
            "TPEx",
            source_endpoint("tpex_warrants"),
            payloads.get("tpex_warrants", []),
        ),
    ):
        for row in rows:
            twse = source_dataset == "twse_warrants"
            code = _text(row.get("權證代號") if twse else row.get("Code"))
            if not code:
                continue
            expiry = _date(row.get("履約截止日") if twse else row.get("ExpiryDate"))
            exercise_start = _date(row.get("履約開始日")) if twse else None
            # TWSE t187ap37_L publishes the exercise period, not the listing
            # date. European-style warrants commonly have the same exercise
            # start and expiry, so using it as valid_from creates a zero-length
            # identity interval. Keep listing time unknown unless the source
            # actually provides it.
            listed = None if twse else _date(row.get("ListedDate"))
            expired = bool(expiry and datetime.fromisoformat(expiry).date() < today)
            short = _text(row.get("權證簡稱") if twse else row.get("Name")) or code
            issue = (issue_bindings or {}).get((venue, code))
            issue_failure = "verified_warrant_issuance_unavailable"
            if issue:
                if issue.get("product_type") != "warrant":
                    issue_failure = "warrant_catalogue_product_type_conflict"
                elif _name_key(short) != _name_key(str(issue.get("source_name") or "")):
                    issue_failure = "warrant_catalogue_name_conflict"
                elif listed and listed != issue.get("listed_on"):
                    issue_failure = "warrant_catalogue_listing_date_conflict"
                elif not expiry or expiry < str(issue.get("listed_on") or ""):
                    issue_failure = "warrant_expiry_missing_or_before_listing"
                else:
                    issue_failure = ""
                    listed = issue["listed_at"]
            pending = bool(listed and datetime.fromisoformat(listed).astimezone(ZoneInfo("Asia/Taipei")).date() > today)
            snapshots.append(
                SecuritySourceSnapshot(
                    source_id=source_id,
                    source_dataset=source_dataset,
                    source_url=source_url,
                    venue=venue,
                    listing_type="warrant",
                    entity_type="warrant",
                    code=code,
                    display_symbol=f"{code}{'.TW' if twse else '.TWO'}",
                    short_name=short,
                    legal_name=short,
                    lifecycle_status="pre_listing" if pending else "expired" if expired else "active",
                    listed_at=listed,
                    expires_at=expiry,
                    industry="Warrant",
                    quote_present=False,
                    raw_row=row,
                    metadata={
                        "issuance_identity": dict(issue) if issue and not issue_failure else None,
                        "issuance_identity_failure": issue_failure or None,
                        "warrant_type": _text(row.get("權證類型") if twse else row.get("Type")) or None,
                        "exercise_start_at": exercise_start,
                        "listing_time_available": listed is not None,
                        "underlying": _text(
                            row.get("標的證券/指數")
                            if twse
                            else row.get("UnderlyingStockCode")
                        )
                        or None,
                    },
                )
            )

    for row in payloads.get("twse_indices", []):
        name = _text(row.get("指數"))
        if not name:
            continue
        code = _index_code("TWSE", name)
        snapshots.append(
            SecuritySourceSnapshot(
                source_id="twse_openapi",
                source_dataset="twse_indices",
                source_url=source_endpoint("twse_indices"),
                venue="TWSE",
                listing_type="index",
                entity_type="index",
                code=code,
                display_symbol=f"INDEX:{code}",
                short_name=name,
                legal_name=name,
                lifecycle_status="active",
                quote_present=True,
                raw_row=row,
                metadata={"observation_date": _date(row.get("日期"))},
            )
        )
    for row in payloads.get("tpex_indices", []):
        code = _text(row.get("IndexCode"))
        name = _text(row.get("IndexName"))
        if not code or not name:
            continue
        snapshots.append(
            SecuritySourceSnapshot(
                source_id="tpex_openapi",
                source_dataset="tpex_indices",
                source_url=source_endpoint(
                    "tpex_index",
                    path=_text(row.get("Endpoint")),
                ),
                venue="TPEx",
                listing_type="index",
                entity_type="index",
                code=code,
                display_symbol=f"INDEX:TPEx:{code}",
                short_name=name,
                legal_name=name,
                lifecycle_status="active",
                quote_present=True,
                raw_row=row,
            )
        )

    for source_dataset, source_id, venue, source_url, rows in (
        (
            "twse_delisted",
            "twse_openapi",
            "TWSE",
            source_endpoint("twse_delisted"),
            payloads.get("twse_delisted", []),
        ),
        (
            "tpex_delisted",
            "tpex_official_web",
            "TPEx",
            source_endpoint("tpex_delisted"),
            payloads.get("tpex_delisted", []),
        ),
    ):
        for row in rows:
            code = _text(row.get("Code"))
            name = _text(row.get("Company") or code)
            ended = _date(row.get("DelistingDate"))
            if not code or not ended:
                continue
            snapshots.append(
                SecuritySourceSnapshot(
                    source_id=source_id,
                    source_dataset=source_dataset,
                    source_url=source_url,
                    venue=venue,
                    listing_type="delisted",
                    entity_type="stock",
                    code=code,
                    display_symbol=f"{code}{'.TW' if venue == 'TWSE' else '.TWO'}",
                    short_name=name,
                    legal_name=name,
                    lifecycle_status="delisted",
                    delisted_at=ended,
                    quote_present=False,
                    raw_row=row,
                    metadata={"reason": _text(row.get("Reason")) or None},
                )
            )
    # Company APIs provide legal names while the catalogue often has only a
    # short name. Carry the verified issue proof for identity adoption; names
    # alone must not create a replacement for a catalogue-owned security.
    for snapshot in snapshots:
        if snapshot.source_dataset not in {
            "twse_companies_quotes", "tpex_otc_companies_quotes",
            "tpex_emerging_companies_quotes", "twse_etfs",
        }:
            continue
        issue = (issue_bindings or {}).get((snapshot.venue, snapshot.code))
        if issue is None:
            continue
        try:
            snapshot.metadata["issuance_identity"] = _validated_catalogue_issue(
                issue, venue=snapshot.venue, code=snapshot.code, acquired_at=acquired_at,
            )
            if not issue["eligible_as_of"]:
                snapshot.lifecycle_status = "pre_listing"
                snapshot.quote_present = False
        except ValueError as exc:
            snapshot.metadata["catalogue_identity_failure"] = str(exc)
    return snapshots


def _identity_key(snapshot: SecuritySourceSnapshot) -> str:
    if snapshot.entity_type in {"warrant", "security"}:
        issue = snapshot.metadata.get("issuance_identity") or {}
        if not issue.get("isin"):
            raise ValueError("verified_warrant_issuance_required")
        return f"{snapshot.entity_type}:{snapshot.venue}:isin:{issue['isin']}"
    if snapshot.entity_type == "etf":
        # A business number identifies the fund issuer, not one exchange
        # security. Multiple ETF share classes from the same issuer must not
        # oscillate inside one entity and generate false immutable revisions.
        return f"etf:{snapshot.venue}:{snapshot.code}"
    if snapshot.unified_business_no:
        return f"business:{snapshot.unified_business_no}"
    if snapshot.entity_type == "stock":
        return f"stock:{snapshot.code}:{_name_key(snapshot.legal_name)}"
    if snapshot.entity_type == "etf":
        return f"etf:{snapshot.venue}:{snapshot.code}"
    return f"{snapshot.entity_type}:{snapshot.code}"


def _identity_aliases(snapshot: SecuritySourceSnapshot) -> set[str]:
    if snapshot.entity_type in {"warrant", "security"}:
        return {_identity_key(snapshot)}
    if snapshot.entity_type == "stock":
        identity = f"stock:{snapshot.code}:{_name_key(snapshot.legal_name)}"
    elif snapshot.entity_type == "etf":
        # TWSE publishes the same current fund through both company/quote and
        # ETF masters, often with different legal-name formatting. Venue plus
        # exchange code is the current official security identity; validity
        # intervals still preserve a later code reuse.
        identity = f"etf:{snapshot.venue}:{snapshot.code}"
    else:
        identity = f"{snapshot.entity_type}:{snapshot.code}"
    aliases = {identity}
    if snapshot.unified_business_no and snapshot.entity_type != "etf":
        aliases.add(f"business:{snapshot.unified_business_no}")
    return aliases


def _stable_entity_id(key: str) -> str:
    return f"ENT-{uuid5(NAMESPACE_URL, f'stock-ai:taiwan-security:{key}').hex}"


def _existing_entity_id(
    snapshots: list[SecuritySourceSnapshot],
    existing_by_business_no: dict[str, list[dict[str, Any]]],
    existing_by_code: dict[str, list[dict[str, Any]]],
    *,
    acquired_at: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    if all(item.entity_type == "warrant" for item in snapshots):
        matches: dict[str, dict[str, Any]] = {}
        unresolved_legacy = False
        for snapshot in snapshots:
            issue = snapshot.metadata["issuance_identity"]
            for item in existing_by_code.get(snapshot.code, []):
                if item.get("entity_type") != "warrant" or item.get("exchange") != snapshot.venue:
                    continue
                metadata = item.get("metadata") or {}
                known_isins = {
                    str(identifier.get("identifier_value"))
                    for identifier in item.get("identifiers") or []
                    if identifier.get("identifier_type") == "isin"
                }
                old_issue = metadata.get("issuance_identity") or {}
                if old_issue.get("isin"):
                    known_isins.add(str(old_issue["isin"]))
                if known_isins:
                    if len(known_isins) > 1:
                        unresolved_legacy = True
                    same_issue = known_isins == {issue["isin"]}
                else:
                    # Bridge a legacy code-only entity only when its name and
                    # recorded lifetime support the catalogue's original issue.
                    # An extension may change expiry, never the issuance ID.
                    try:
                        old_expiry = datetime.fromisoformat(str(metadata.get("expires_at") or item.get("delisted_at") or "").replace("Z", "+00:00")).date().isoformat()
                    except ValueError:
                        old_expiry = None
                    same_name = _name_key(str(item.get("canonical_name") or "")) == _name_key(snapshot.legal_name)
                    if old_expiry is None or (old_expiry >= issue["listed_on"] and not same_name):
                        unresolved_legacy = True
                    same_issue = (
                        same_name
                        and bool(old_expiry)
                        and old_expiry >= issue["listed_on"]
                    )
                if same_issue:
                    matches[str(item["entity_id"])] = item
        if unresolved_legacy:
            raise ValueError("warrant_legacy_issuance_unresolved")
        if len(matches) > 1:
            raise ValueError("warrant_legacy_issuance_ambiguous")
        if matches:
            return next(iter(matches.items()))
        return None, None
    catalogue_owners: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        for item in existing_by_code.get(snapshot.code, []):
            if (item.get("exchange") == snapshot.venue
                and (item.get("metadata") or {}).get("identity_origin") == "official_catalogue"):
                catalogue_owners[str(item["entity_id"])] = item
    if catalogue_owners:
        if len({(s.venue, s.code, s.entity_type) for s in snapshots}) != 1:
            raise ValueError("catalogue_issuance_group_conflict")
        issues = []
        for snapshot in snapshots:
            if acquired_at is None:
                raise ValueError("catalogue_issuance_time_context_missing")
            if not snapshot.metadata.get("issuance_identity"):
                raise ValueError(str(snapshot.metadata.get("catalogue_identity_failure") or "verified_catalogue_issuance_unavailable"))
            issue = _validated_catalogue_issue(
                snapshot.metadata.get("issuance_identity"), venue=snapshot.venue,
                code=snapshot.code, acquired_at=acquired_at,
            )
            expected_type = {"ordinary_stock": "stock", "etf": "etf", "warrant": "warrant"}.get(issue["product_type"], "security")
            if snapshot.entity_type != expected_type:
                raise ValueError("catalogue_issuance_product_type_conflict")
            if (snapshot.listed_at
                and datetime.fromisoformat(snapshot.listed_at).astimezone(ZoneInfo("Asia/Taipei")).date().isoformat() != issue["listed_on"]):
                raise ValueError("catalogue_issuance_listing_date_conflict")
            issues.append(issue)
        if len({issue["isin"] for issue in issues}) != 1:
            raise ValueError("catalogue_issuance_isin_conflict")
        matches = {}
        for entity_id, item in catalogue_owners.items():
            known_isins = {str(i["identifier_value"]) for i in item.get("identifiers") or [] if i.get("identifier_type") == "isin"}
            previous = (item.get("metadata") or {}).get("issuance_identity") or {}
            if previous.get("isin"):
                known_isins.add(str(previous["isin"]))
            if len(known_isins) != 1:
                raise ValueError("catalogue_issuance_owner_unresolved")
            if known_isins == {issues[0]["isin"]}:
                if item.get("entity_type") != snapshots[0].entity_type:
                    raise ValueError("catalogue_issuance_product_type_conflict")
                matches[entity_id] = item
        if len(matches) > 1:
            raise ValueError("catalogue_issuance_owner_ambiguous")
        if not matches:
            raise ValueError("catalogue_issuance_isin_conflict")
        return next(iter(matches.items()))
    business_numbers = {
        item.unified_business_no for item in snapshots if item.unified_business_no
    }
    codes = {item.code for item in snapshots}
    names = {_name_key(item.legal_name) for item in snapshots}
    entity_types = {item.entity_type for item in snapshots}
    venues = {item.venue for item in snapshots}
    matches_by_id: dict[str, dict[str, Any]] = {}
    if entity_types != {"etf"}:
        for business_no in business_numbers:
            for item in existing_by_business_no.get(business_no, []):
                if (item.get("metadata") or {}).get("identity_origin") == "official_catalogue":
                    continue
                matches_by_id[str(item["entity_id"])] = item
    for code in codes:
        for item in existing_by_code.get(code, []):
            if (item.get("metadata") or {}).get("identity_origin") == "official_catalogue":
                continue
            same_etf_security = (
                entity_types == {"etf"}
                and item.get("entity_type") == "etf"
                and item.get("exchange") in venues
            )
            if same_etf_security:
                incoming_symbols = {
                    snapshot.display_symbol.upper() for snapshot in snapshots
                }
                existing_symbol = str(
                    (item.get("metadata") or {}).get("display_symbol") or ""
                ).upper()
                existing_codes = {
                    str(identifier.get("identifier_value") or "")
                    for identifier in item.get("identifiers") or []
                    if identifier.get("identifier_type") == "exchange_code"
                    and not identifier.get("superseded_at")
                }
                # A legacy bad merge can own several simultaneous ETF codes.
                # Reuse it only for its canonical symbol; each other code gets
                # a new stable venue+code entity on the next synchronization.
                if len(existing_codes) > 1 and existing_symbol not in incoming_symbols:
                    same_etf_security = False
            if (
                same_etf_security
                or _name_key(str(item.get("canonical_name") or "")) in names
            ):
                matches_by_id[str(item["entity_id"])] = item
    matches = list(matches_by_id.values())
    if not matches:
        return None, None
    matches.sort(
        key=lambda item: (
            item.get("lifecycle_status") == "active",
            item.get("updated_at") or "",
        ),
        reverse=True,
    )
    return str(matches[0]["entity_id"]), matches[0]


def consolidate_security_snapshots(
    snapshots: Iterable[SecuritySourceSnapshot],
    *,
    acquired_at: str,
    existing_inventory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create canonical entities while retaining every source/venue observation."""

    inventory = existing_inventory or []
    existing_by_business_no: dict[str, list[dict[str, Any]]] = defaultdict(list)
    existing_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in inventory:
        for identifier in item.get("identifiers") or []:
            identifier_type = str(identifier.get("identifier_type") or "")
            identifier_value = str(identifier.get("identifier_value") or "")
            if identifier_type == "unified_business_no":
                existing_by_business_no[identifier_value].append(item)
            elif identifier_type == "exchange_code":
                existing_by_code[identifier_value].append(item)
    groups: dict[str, list[SecuritySourceSnapshot]] = {}
    aliases_to_key: dict[str, str] = {}
    unresolved: list[dict[str, Any]] = []

    def reject(snapshot: SecuritySourceSnapshot, reason: str) -> None:
        unresolved.append({"source_dataset": snapshot.source_dataset,
                           "venue": snapshot.venue, "code": snapshot.code,
                           "symbol": snapshot.display_symbol, "reason": reason})

    for snapshot in snapshots:
        if snapshot.entity_type == "warrant" and not snapshot.metadata.get("issuance_identity"):
            reject(snapshot, str(snapshot.metadata.get("issuance_identity_failure") or "verified_warrant_issuance_unavailable"))
            continue
        aliases = _identity_aliases(snapshot)
        matched_keys = {aliases_to_key[alias] for alias in aliases if alias in aliases_to_key}
        if not matched_keys:
            group_key = sorted(aliases)[0]
            groups[group_key] = []
        else:
            group_key = sorted(matched_keys)[0]
            for duplicate_key in matched_keys - {group_key}:
                groups[group_key].extend(groups.pop(duplicate_key))
                for alias, mapped_key in tuple(aliases_to_key.items()):
                    if mapped_key == duplicate_key:
                        aliases_to_key[alias] = group_key
        groups[group_key].append(snapshot)
        for alias in aliases:
            aliases_to_key[alias] = group_key

    entities: list[EntityRecord] = []
    identifiers: list[dict[str, Any]] = []
    revisions: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for identity_key, observations in groups.items():
        try:
            existing_id, existing = _existing_entity_id(
                observations, existing_by_business_no, existing_by_code, acquired_at=acquired_at,
            )
        except ValueError as exc:
            if (str(exc) not in {"warrant_legacy_issuance_ambiguous", "warrant_legacy_issuance_unresolved", "verified_catalogue_issuance_unavailable"}
                and not str(exc).startswith("catalogue_issuance_")):
                raise
            for observation in observations:
                reject(observation, str(exc))
            continue
        if existing and (existing.get("metadata") or {}).get("identity_origin") == "official_catalogue":
            # Preserve the exact official Taipei listing-day start. API date
            # strings use the older UTC-midnight normalization; their original
            # values remain available in raw_row alongside the complete proof.
            observations = [
                item.model_copy(update={"listed_at": item.metadata["issuance_identity"]["listed_at"]})
                if item.metadata.get("issuance_identity") else item
                for item in observations
            ]
        entity_id = existing_id or _stable_entity_id(identity_key)
        current = [
            item
            for item in observations
            if item.lifecycle_status in {"active", "pre_listing", "suspended"}
        ]
        current.sort(
            key=lambda item: (
                item.lifecycle_status == "active",
                _VENUE_PRIORITY.get(item.venue, 0),
                item.listed_at or "",
            ),
            reverse=True,
        )
        selected = current[0] if current else max(
            observations,
            key=lambda item: item.delisted_at or item.expires_at or item.listed_at or "",
        )
        preserve_existing_active = (
            not current
            and existing is not None
            and existing.get("lifecycle_status") == "active"
            and existing.get("exchange") not in {item.venue for item in observations}
        )
        lifecycle_status = (
            "active"
            if preserve_existing_active
            else selected.lifecycle_status
        )
        listed_dates = [item.listed_at for item in observations if item.listed_at]
        ended_dates = [
            item.delisted_at or item.expires_at
            for item in observations
            if item.delisted_at or item.expires_at
        ]
        venue_history = sorted(
            {
                (
                    item.venue,
                    item.listing_type,
                    item.listed_at or "",
                    item.delisted_at or item.expires_at or "",
                )
                for item in observations
            }
        )
        existing_metadata = dict(existing.get("metadata") or {}) if existing else {}
        for item in existing_metadata.get("venue_history") or []:
            if not isinstance(item, dict):
                continue
            venue_history.append(
                (
                    str(item.get("venue") or ""),
                    str(item.get("listing_type") or ""),
                    str(item.get("valid_from") or ""),
                    str(item.get("valid_to") or ""),
                )
            )
        venue_history = sorted(set(venue_history))
        exchange = str(existing.get("exchange")) if preserve_existing_active else selected.venue
        canonical_name = (
            str(existing.get("canonical_name"))
            if preserve_existing_active
            else selected.legal_name
        )
        display_symbol = (
            str(existing_metadata.get("display_symbol") or "")
            if preserve_existing_active
            else selected.display_symbol
        )
        entities.append(
            EntityRecord(
                entity_id=entity_id,
                entity_type=selected.entity_type,
                canonical_name=canonical_name,
                market="taiwan",
                exchange=exchange,
                currency="TWD",
                sector=selected.industry if selected.entity_type in {"etf", "index", "warrant"} else None,
                industry=selected.industry,
                lifecycle_status=lifecycle_status,
                listed_at=min(listed_dates) if listed_dates else existing.get("listed_at") if existing else None,
                delisted_at=(
                    max(ended_dates)
                    if ended_dates and lifecycle_status in {"delisted", "expired"}
                    else None
                ),
                metadata={
                    **existing_metadata,
                    **({"issuance_identity": selected.metadata["issuance_identity"],
                        "product_classifications": {
                            **(existing_metadata.get("product_classifications") or {}),
                            f"{selected.venue}:{selected.display_symbol}": selected.metadata["issuance_identity"].get("product_classification"),
                        }}
                       if selected.metadata.get("issuance_identity") else {}),
                    **({"catalogue_only": False}
                       if existing_metadata.get("identity_origin") == "official_catalogue"
                       and selected.source_id != "twse_isin" else {}),
                    "short_name": selected.short_name,
                    "source_code": selected.code,
                    "display_symbol": display_symbol or selected.display_symbol,
                    "listing_type": (
                        existing_metadata.get("listing_type")
                        if preserve_existing_active
                        else selected.listing_type
                    ),
                    "quote_present": selected.quote_present,
                    "unified_business_no": (
                        selected.unified_business_no
                        or existing_metadata.get("unified_business_no")
                    ),
                    "expires_at": selected.expires_at,
                    "venue_history": [
                        {
                            "venue": venue,
                            "listing_type": listing_type,
                            "valid_from": valid_from or None,
                            "valid_to": valid_to or None,
                        }
                        for venue, listing_type, valid_from, valid_to in venue_history
                    ],
                    "source_datasets": sorted(
                        set(existing_metadata.get("source_datasets") or [])
                        | {item.source_dataset for item in observations}
                    ),
                },
            )
        )
        for item in observations:
            valid_to = item.delisted_at or item.expires_at
            if item.entity_type == "warrant" and item.expires_at:
                # A date-only expiry includes that Taiwan trading day. Keep
                # the reported expiry in the source revision, and close the
                # exchange alias at the following local midnight.
                end_day = datetime.fromisoformat(item.expires_at).date() + timedelta(days=1)
                valid_to = datetime.combine(end_day, datetime.min.time(), ZoneInfo("Asia/Taipei")).astimezone(timezone.utc).isoformat()
            identifier_metadata = {
                "venue": item.venue,
                "listing_type": item.listing_type,
                **({"issuance_identity": {
                    key: item.metadata["issuance_identity"].get(key)
                    for key in ("isin", "listed_on", "source_dataset", "raw_payload_id", "raw_sha256", "row_sha256")
                }}
                   if item.metadata.get("issuance_identity") else {}),
            }
            for identifier_type, identifier_value in (
                ("exchange_code", item.code),
                ("display_symbol", item.display_symbol),
                ("unified_business_no", item.unified_business_no),
                ("isin", (item.metadata.get("issuance_identity") or {}).get("isin")),
            ):
                if not identifier_value:
                    continue
                if identifier_type == "unified_business_no" and item.entity_type == "etf":
                    # This number belongs to the issuer and may legitimately
                    # be shared by several ETF securities. Keep it as entity
                    # metadata until issuer entities are modeled separately.
                    continue
                identifiers.append(
                    {
                        "entity_id": entity_id,
                        "source_id": "twse_isin" if identifier_type == "isin" else item.source_id,
                        "identifier_type": identifier_type,
                        "identifier_value": identifier_value,
                        "valid_from": item.listed_at,
                        "valid_to": None if identifier_type == "isin" else valid_to,
                        "confidence": (
                            1.0
                            if identifier_type == "unified_business_no"
                            else 0.99
                            if identifier_type == "display_symbol"
                            else 0.95
                        ),
                        "is_primary": (
                            identifier_type == "display_symbol"
                            and item is selected
                            and not valid_to
                        ),
                        "metadata": identifier_metadata,
                    }
                )
            revisions.append(
                {
                    "entity_id": entity_id,
                    "source_id": item.source_id,
                    "observation_key": f"lifecycle:{item.source_dataset}:{item.venue}",
                    "effective_at": item.listed_at or item.delisted_at or item.expires_at or acquired_at,
                    "expires_at": item.expires_at,
                    "payload": {
                        **item.model_dump(mode="json", exclude={"raw_row"}),
                        "entity_id": entity_id,
                        "raw_row": item.raw_row,
                    },
                    "source_dataset": item.source_dataset,
                    "quality_flags": (
                        ["publication_time_unavailable"]
                        if item.source_dataset not in {"twse_indices", "tpex_indices"}
                        else ["publication_time_unavailable", "index_master_derived_from_official_series"]
                    ),
                }
            )
            if item.listed_at:
                event_type = {
                    "emerging": "entered_emerging",
                    "otc": "listed_otc",
                    "listed": "listed_twse",
                    "etf": "listed_etf",
                    "warrant": "listed_warrant",
                    "index": "index_published",
                }.get(item.listing_type, "listed")
                events.append(
                    {
                        "entity_id": entity_id,
                        "event_type": event_type,
                        "venue": item.venue,
                        "listing_type": item.listing_type,
                        "effective_at": item.listed_at,
                        "source_id": item.source_id,
                        "source_dataset": item.source_dataset,
                        "metadata": item.metadata,
                    }
                )
            elif item.entity_type == "index":
                events.append(
                    {
                        "entity_id": entity_id,
                        "event_type": "index_observed",
                        "venue": item.venue,
                        "listing_type": item.listing_type,
                        "effective_at": acquired_at,
                        "source_id": item.source_id,
                        "source_dataset": item.source_dataset,
                        "metadata": item.metadata,
                    }
                )
            if item.delisted_at:
                events.append(
                    {
                        "entity_id": entity_id,
                        "event_type": "delisted",
                        "venue": item.venue,
                        "listing_type": item.listing_type,
                        "effective_at": item.delisted_at,
                        "source_id": item.source_id,
                        "source_dataset": item.source_dataset,
                        "metadata": item.metadata,
                    }
                )
            if item.expires_at:
                events.append(
                    {
                        "entity_id": entity_id,
                        "event_type": "expires",
                        "venue": item.venue,
                        "listing_type": item.listing_type,
                        "effective_at": item.expires_at,
                        "source_id": item.source_id,
                        "source_dataset": item.source_dataset,
                        "metadata": item.metadata,
                    }
                )
    return {
        "entities": entities,
        "identifiers": identifiers,
        "revisions": revisions,
        "events": events,
        "unresolved": unresolved,
    }
