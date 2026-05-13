"""Tool 2 (Process 2) — external_source_collector: gather raw evidence from external sources.

Network-facing via Brave Search API and Companies House API. No LLM calls. No workspace writes.
Returns SourceEvidenceBundle. All collector errors captured as flags.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cobalt.core.companies_house import (
    companies_house_get_company,
    companies_house_search,
)
from cobalt.core.exceptions import SecEdgarError
from cobalt.core.search import brave_search, classify_url_quality, fetch_url
from cobalt.core.sec_edgar import sec_get_company_submissions, sec_search_by_name
from cobalt.core.opencorporates import opencorporates_search
from cobalt.core.wikidata import wikidata_lookup_by_name
from cobalt.models.schemas.enrichment_schema import (
    EnrichmentReadinessResult,
    SourceEvidenceBundle,
    SourceEvidenceItem,
)

logger = logging.getLogger(__name__)

_LIFECYCLE_KEYWORDS: dict[str, str] = {
    "rebrand":        "REBRANDED",
    "renamed":        "REBRANDED",
    "acqui":          "ACQUIRED",
    "merger":         "MERGED",
    "merged":         "MERGED",
    "bankrupt":       "POSSIBLY_DEFUNCT",
    "defunct":        "POSSIBLY_DEFUNCT",
    "shut down":      "POSSIBLY_DEFUNCT",
    "spun off":       "SPUN_OFF",
    "spin-off":       "SPUN_OFF",
    "went public":    "WENT_PUBLIC",
    "ipo":            "WENT_PUBLIC",
    "taken private":  "WENT_PRIVATE",
    "parent changed": "PARENT_CHANGED",
}


# ---------------------------------------------------------------------------
# Private dataclass
# ---------------------------------------------------------------------------

@dataclass
class _SearchContext:
    vendor_id:      str
    canonical_name: str
    domain:         str | None
    website:        str | None
    hq_country:     str | None
    source_list:    list[str]
    depth_tier:     str


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_against_entity(content: str, url: str, ctx: _SearchContext) -> str:
    """Heuristic validation — no LLM."""
    name_lower = ctx.canonical_name.lower()
    content_lower = content.lower()
    url_lower = url.lower()

    if name_lower and name_lower in content_lower:
        return "CONFIRMED"
    if ctx.domain and ctx.domain.lower() in url_lower:
        return "LIKELY"
    return "UNCERTAIN"


def _detect_lifecycle_signal(content: str) -> str | None:
    content_lower = content.lower()
    for keyword, signal_type in _LIFECYCLE_KEYWORDS.items():
        if keyword in content_lower:
            return signal_type
    return None


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def _strip_suffixes(name: str) -> str:
    suffixes = (" limited", " ltd", " ltd.", " plc", " plc.",
                " llp", " llp.", " uk limited", " holdings")
    n = name
    for s in suffixes:
        if n.endswith(s):
            n = n[: -len(s)]
            break
    return n.strip()


def _pick_best_ch_match(results: list[dict], canonical_name: str) -> dict | None:
    if not results:
        return None
    target = canonical_name.lower().strip()

    def score(r: dict) -> float:
        title = r.get("title", "").lower().strip()
        if title == target:
            base = 1.0
        elif target in title or title in target:
            base = 0.7
        else:
            a = set(_strip_suffixes(target).split())
            b = set(_strip_suffixes(title).split())
            if a and b:
                base = len(a & b) / len(a | b)
            else:
                base = 0.0
        status_bonus = 0.1 if r.get("company_status") == "active" else 0.0
        return base + status_bonus

    scored = sorted(results, key=score, reverse=True)
    best = scored[0]
    if score(best) < 0.4:
        return None
    return best


def _count_close_matches(results: list[dict], canonical_name: str, threshold: float = 0.6) -> int:
    target = canonical_name.lower().strip()
    target_tokens = set(_strip_suffixes(target).split())
    if not target_tokens:
        return 0
    count = 0
    for r in results:
        title = r.get("title", "").lower().strip()
        title_tokens = set(_strip_suffixes(title).split())
        if not title_tokens:
            continue
        sim = len(target_tokens & title_tokens) / len(target_tokens | title_tokens)
        if sim >= threshold:
            count += 1
    return count


def _validate_registry_match(record: dict, ctx: _SearchContext) -> str:
    name = (record.get("company_name") or record.get("title") or "").lower()
    target = ctx.canonical_name.lower().strip()
    if name == target:
        return "CONFIRMED"
    if target in name or name in target:
        return "LIKELY"
    return "UNCERTAIN"


# ---------------------------------------------------------------------------
# OpenCorporates helpers
# ---------------------------------------------------------------------------

_CORPORATE_SUFFIXES: tuple[str, ...] = (
    # UK / English
    " limited", " ltd", " ltd.", " plc", " plc.", " llp", " llp.",
    " uk limited", " holdings", " group",
    " inc", " inc.", " corp", " corporation", " incorporated",
    " llc", " lp", " l.p.",
    # German
    " gmbh", " ag", " kg", " ohg", " gbr", " kgaa",
    # French / Iberian / Italian
    " sarl", " sas", " sci", " sl", " srl", " spa",
    # Benelux / Nordic
    " bv", " nv", " ab", " as", " asa", " oy",
    # APAC
    " pty", " pty ltd", " pty limited", " pte", " pte ltd",
    # Generic
    " co", " co.",
)


def _strip_corporate_suffixes(name: str) -> str:
    n = name.lower().strip()
    changed = True
    while changed:
        changed = False
        for s in _CORPORATE_SUFFIXES:
            if n.endswith(s):
                n = n[: -len(s)].strip()
                changed = True
                break
    return n.strip()


_COUNTRY_CODE_TO_NAMES: dict[str, list[str]] = {
    "US": ["united states", "usa"],
    "GB": ["united kingdom", "uk", "england", "scotland", "wales"],
    "DE": ["germany"],
    "FR": ["france"],
    "ES": ["spain"],
    "IT": ["italy"],
    "NL": ["netherlands"],
    "BE": ["belgium"],
    "CH": ["switzerland"],
    "SE": ["sweden"],
    "NO": ["norway"],
    "DK": ["denmark"],
    "FI": ["finland"],
    "IE": ["ireland"],
    "AU": ["australia"],
    "NZ": ["new zealand"],
    "CA": ["canada"],
    "JP": ["japan"],
    "IN": ["india"],
    "SG": ["singapore"],
}


def _country_code_matches(iso_code: str, country_name: str) -> bool:
    names = _COUNTRY_CODE_TO_NAMES.get(iso_code.upper(), [])
    return any(n in country_name for n in names)


def _pick_best_oc_match(
    results: list[dict], canonical_name: str, country_hint: str | None
) -> dict | None:
    if not results:
        return None
    target = canonical_name.lower().strip()

    def score(r: dict) -> float:
        name = (r.get("name") or "").lower().strip()
        if name == target:
            base = 1.0
        elif target in name or name in target:
            base = 0.7
        else:
            a = set(_strip_corporate_suffixes(target).split())
            b = set(_strip_corporate_suffixes(name).split())
            base = len(a & b) / len(a | b) if (a and b) else 0.0

        status = (r.get("current_status") or "").lower()
        status_bonus = 0.1 if (status == "active" or not r.get("inactive")) else 0.0

        country_bonus = 0.0
        if country_hint:
            jc = (r.get("jurisdiction_code") or "").lower()
            cn = (r.get("country") or "").lower()
            if jc.startswith(country_hint.lower()):
                country_bonus = 0.15
            elif cn and _country_code_matches(country_hint, cn):
                country_bonus = 0.1

        return base + status_bonus + country_bonus

    scored = sorted(results, key=score, reverse=True)
    best = scored[0]
    if score(best) < 0.5:
        return None
    return best


def _count_close_oc_matches(
    results: list[dict], canonical_name: str, threshold: float = 0.6
) -> int:
    target = canonical_name.lower().strip()
    target_tokens = set(_strip_corporate_suffixes(target).split())
    if not target_tokens:
        return 0
    count = 0
    for r in results:
        name = (r.get("name") or "").lower()
        tokens = set(_strip_corporate_suffixes(name).split())
        if not tokens:
            continue
        sim = len(target_tokens & tokens) / len(target_tokens | tokens)
        if sim >= threshold:
            count += 1
    return count


def _validate_oc_match(record: dict, ctx: _SearchContext) -> str:
    target = ctx.canonical_name.lower().strip()
    name = (record.get("name") or "").lower().strip()
    if name == target:
        return "CONFIRMED"
    target_simple = _strip_corporate_suffixes(target)
    name_simple = _strip_corporate_suffixes(name)
    if target_simple == name_simple:
        return "CONFIRMED"
    if target_simple in name_simple or name_simple in target_simple:
        return "LIKELY"
    return "UNCERTAIN"


def _try_opencorporates(
    ctx: _SearchContext, *, jurisdiction_hint: str | None = None
) -> tuple[SourceEvidenceItem | None, list[str]]:
    """Attempt an OpenCorporates lookup. Returns (item|None, flags)."""
    jurisdiction = jurisdiction_hint or (
        ctx.hq_country.lower() if ctx.hq_country else None
    )

    try:
        results = opencorporates_search(
            ctx.canonical_name,
            jurisdiction_code=jurisdiction,
            limit=5,
        )
    except Exception:
        logger.exception("OpenCorporates search error for %r", ctx.canonical_name)
        return None, ["REGISTRY_FETCH_ERROR"]

    if not results and jurisdiction:
        # Jurisdiction codes in OC are non-standard (e.g. "us_de" for Delaware).
        # Retry without the filter so OC can return its best match globally.
        try:
            results = opencorporates_search(
                ctx.canonical_name,
                jurisdiction_code=None,
                limit=5,
            )
        except Exception:
            logger.exception(
                "OpenCorporates fallback search error for %r", ctx.canonical_name
            )
            return None, ["REGISTRY_FETCH_ERROR"]

    if not results:
        return None, []

    best = _pick_best_oc_match(results, ctx.canonical_name, ctx.hq_country)
    if best is None:
        return None, []

    flags: list[str] = []
    if _count_close_oc_matches(results, ctx.canonical_name) > 1:
        flags.append("REGISTRY_MULTIPLE_MATCHES")

    item = SourceEvidenceItem(
        content=json.dumps(best, sort_keys=True),
        source_type="REGISTRY",
        source_url=best.get("opencorporates_url") or best.get("registry_url") or "",
        retrieved_at=_now_iso(),
        validation_status=_validate_oc_match(best, ctx),
        quality_signal="OFFICIAL",
        signal_type=None,
    )
    return item, flags


# ---------------------------------------------------------------------------
# Collectors — each returns (items, flags)
# ---------------------------------------------------------------------------

def _collect_web_search(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    query = f"{ctx.canonical_name} company"
    items: list[SourceEvidenceItem] = []
    flags: list[str] = []

    try:
        results = brave_search(query, result_type="web", count=10)
    except Exception as exc:
        logger.warning("[web_search] Brave search failed: %s", exc)
        flags.append("WEB_SEARCH_FETCH_ERROR")
        return items, flags

    for r in results:
        url = r.get("url", "")
        content = r.get("content", "")
        if not content:
            continue
        items.append(SourceEvidenceItem(
            content=content,
            source_type="WEB_SEARCH",
            source_url=url,
            retrieved_at=_now_iso(),
            validation_status=_validate_against_entity(content, url, ctx),
            quality_signal=classify_url_quality(url),
            signal_type=None,
        ))

    return items, flags


def _collect_company_website(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    items: list[SourceEvidenceItem] = []
    flags: list[str] = []

    target_url = ctx.website or (f"https://{ctx.domain}" if ctx.domain else None)
    if not target_url:
        return items, flags

    text, _ = fetch_url(target_url)
    if not text:
        logger.warning("[company_website] fetch failed for %r", target_url)
        flags.append("COMPANY_WEBSITE_FETCH_FAILED")
        return items, flags

    items.append(SourceEvidenceItem(
        content=text[:4000],
        source_type="COMPANY_WEBSITE",
        source_url=target_url,
        retrieved_at=_now_iso(),
        validation_status=_validate_against_entity(text, target_url, ctx),
        quality_signal="OFFICIAL",
        signal_type=None,
    ))

    return items, flags


def _collect_news(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    query = f"{ctx.canonical_name} news announcement"
    items: list[SourceEvidenceItem] = []
    flags: list[str] = []

    try:
        results = brave_search(query, result_type="news", count=10, freshness="py")
    except Exception as exc:
        logger.warning("[news] Brave search failed: %s", exc)
        flags.append("NEWS_SEARCH_FETCH_ERROR")
        return items, flags

    for r in results:
        url = r.get("url", "")
        content = r.get("content", "")
        if not content:
            continue
        items.append(SourceEvidenceItem(
            content=content,
            source_type="NEWS",
            source_url=url,
            retrieved_at=_now_iso(),
            validation_status=_validate_against_entity(content, url, ctx),
            quality_signal=classify_url_quality(url),
            signal_type=_detect_lifecycle_signal(content),
        ))

    return items, flags


def _collect_linkedin(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    return [], ["LINKEDIN_STUBBED"]


def _collect_registry(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    """Look up the vendor in business registries.

    V2.1 cascade:
      1. UK vendors → Companies House first (canonical UK source).
      2. UK vendors with no CH match → OpenCorporates as fallback.
      3. Non-UK vendors → OpenCorporates directly.
      4. No match anywhere → NO_REGISTRY_RECORD.
    """
    flags: list[str] = []
    items: list[SourceEvidenceItem] = []

    hq = (ctx.hq_country or "").upper()
    is_uk = hq in {"GB", "UK", "GBR"}

    if is_uk:
        ch_results: list[dict] = []
        try:
            ch_results = companies_house_search(ctx.canonical_name, limit=5)
        except Exception:
            logger.exception("Companies House search error for %r", ctx.canonical_name)
            flags.append("REGISTRY_FETCH_ERROR")

        if ch_results:
            best = _pick_best_ch_match(ch_results, ctx.canonical_name)
            if best is not None:
                if _count_close_matches(ch_results, ctx.canonical_name) > 1:
                    flags.append("REGISTRY_MULTIPLE_MATCHES")
                full = companies_house_get_company(best["company_number"])
                record: dict = full if full is not None else best
                items.append(SourceEvidenceItem(
                    content=json.dumps(record, sort_keys=True),
                    source_type="REGISTRY",
                    source_url=(
                        f"https://find-and-update.company-information.service.gov.uk"
                        f"/company/{best['company_number']}"
                    ),
                    retrieved_at=_now_iso(),
                    validation_status=_validate_registry_match(record, ctx),
                    quality_signal="OFFICIAL",
                    signal_type=None,
                ))
                return items, flags

        # CH had nothing (or no good match) — try OpenCorporates as fallback.
        # Covers branches and foreign-registered UK entities.
        oc_item, oc_flags = _try_opencorporates(ctx, jurisdiction_hint="gb")
        flags.extend(oc_flags)
        if oc_item is not None:
            items.append(oc_item)
            return items, flags

        return items, ["NO_REGISTRY_RECORD"] + flags

    # Non-UK: go directly to OpenCorporates.
    oc_item, oc_flags = _try_opencorporates(ctx, jurisdiction_hint=None)
    flags.extend(oc_flags)
    if oc_item is not None:
        items.append(oc_item)
        return items, flags

    return items, ["NO_REGISTRY_RECORD"] + flags


_NON_US_MARKETS: frozenset[str] = frozenset({
    "CN", "CHN", "RU", "RUS", "BR", "BRA",
    "IN", "IND", "JP", "JPN", "KR", "KOR",
})

_MIN_SEC_SCORE = 0.6


def _validate_sec_match(record: dict, ctx: _SearchContext) -> str:
    title = (record.get("title") or "").lower().strip()
    target = ctx.canonical_name.lower().strip()
    if title == target:
        return "CONFIRMED"
    if target in title or title in target:
        return "LIKELY"
    return "UNCERTAIN"


def _collect_financial(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    """Look up the vendor in SEC EDGAR for public financial signals.

    US-focused: non-US primary markets return NO_PUBLIC_FINANCIAL_DATA as a
    clean negative (not an error).  No auth required; User-Agent mandatory.
    """
    hq = (ctx.hq_country or "").upper()
    if hq in _NON_US_MARKETS:
        return [], ["NO_PUBLIC_FINANCIAL_DATA"]

    try:
        matches = sec_search_by_name(ctx.canonical_name, limit=5)
    except Exception:
        logger.exception("SEC EDGAR search error for %r", ctx.canonical_name)
        return [], ["FINANCIAL_FETCH_ERROR"]

    if not matches or matches[0]["score"] < _MIN_SEC_SCORE:
        return [], ["NO_PUBLIC_FINANCIAL_DATA"]

    best = matches[0]

    try:
        submissions = sec_get_company_submissions(best["cik"])
    except SecEdgarError:
        logger.warning("SEC EDGAR submissions fetch failed for CIK %r", best["cik"])
        return [], ["FINANCIAL_FETCH_ERROR"]

    record: dict = {
        "cik":    best["cik"],
        "ticker": best["ticker"],
        "title":  best["title"],
    }
    if submissions:
        for key in ("entityType", "sic", "sicDescription", "tickers", "exchanges",
                    "stateOfIncorporation", "fiscalYearEnd", "category"):
            if key in submissions:
                record[key] = submissions[key]

    item = SourceEvidenceItem(
        content=json.dumps(record, sort_keys=True),
        source_type="FINANCIAL",
        source_url=(
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={best['cik']}"
        ),
        retrieved_at=_now_iso(),
        validation_status=_validate_sec_match(record, ctx),
        quality_signal="OFFICIAL",
        signal_type=None,
    )
    return [item], []


def _pick_best_wikidata_match(
    matches: list[dict], canonical_name: str, country_hint: str | None = None
) -> dict | None:
    if not matches:
        return None
    target = canonical_name.lower().strip()
    country_hint_lower = (country_hint or "").lower()

    def score(m: dict) -> float:
        label = (m.get("label") or "").lower().strip()
        desc  = (m.get("description") or "").lower()
        if label == target:
            base = 1.0
        elif target in label or label in target:
            base = 0.7
        else:
            a = set(_strip_suffixes(target).split())
            b = set(_strip_suffixes(label).split())
            base = len(a & b) / len(a | b) if (a and b) else 0.0
        country_bonus = 0.1 if country_hint_lower and country_hint_lower in desc else 0.0
        company_bonus = 0.1 if any(
            kw in desc for kw in ("company", "corporation", "business", "enterprise",
                                  "software", "technology", "service", "consulting")
        ) else 0.0
        return base + country_bonus + company_bonus

    scored = sorted(matches, key=score, reverse=True)
    best = scored[0]
    if score(best) < 0.4:
        return None
    return best


def _count_close_wikidata_matches(
    matches: list[dict], canonical_name: str, threshold: float = 0.6
) -> int:
    target = canonical_name.lower().strip()
    target_tokens = set(_strip_suffixes(target).split())
    if not target_tokens:
        return 0
    count = 0
    for m in matches:
        label = (m.get("label") or "").lower().strip()
        label_tokens = set(_strip_suffixes(label).split())
        if not label_tokens:
            continue
        sim = len(target_tokens & label_tokens) / len(target_tokens | label_tokens)
        if sim >= threshold:
            count += 1
    return count


def _validate_wikidata_match(record: dict, ctx: _SearchContext) -> str:
    label = (record.get("label") or "").lower().strip()
    target = ctx.canonical_name.lower().strip()
    if label == target:
        return "CONFIRMED"
    if target in label or label in target:
        return "LIKELY"
    return "UNCERTAIN"


def _collect_wikidata(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    """Look up the vendor in Wikidata for structured reference facts.

    Two-step: search API (name → Q-IDs) then SPARQL (Q-IDs → facts).
    Returns MEDIUM-confidence DIRECTORY items. Not authoritative.
    """
    try:
        matches = wikidata_lookup_by_name(ctx.canonical_name, limit=5)
    except Exception:
        logger.exception("Wikidata lookup error for %r", ctx.canonical_name)
        return [], ["WIKIDATA_FETCH_ERROR"]

    if not matches:
        return [], ["NO_WIKIDATA_RECORD"]

    best = _pick_best_wikidata_match(matches, ctx.canonical_name, ctx.hq_country)
    if best is None:
        return [], ["NO_WIKIDATA_RECORD"]

    flags: list[str] = []
    if _count_close_wikidata_matches(matches, ctx.canonical_name) > 1:
        flags.append("WIKIDATA_MULTIPLE_MATCHES")

    qid = best.get("qid", "")
    item = SourceEvidenceItem(
        content=json.dumps(best, sort_keys=True),
        source_type="WIKIDATA",
        source_url=f"https://www.wikidata.org/wiki/{qid}" if qid else "",
        retrieved_at=_now_iso(),
        validation_status=_validate_wikidata_match(best, ctx),
        quality_signal="DIRECTORY",
        signal_type=None,
    )
    return [item], flags


_COLLECTORS: dict[str, Any] = {
    "web_search":      _collect_web_search,
    "company_website": _collect_company_website,
    "news":            _collect_news,
    "linkedin":        _collect_linkedin,
    "registry":        _collect_registry,
    "financial":       _collect_financial,
    "wikidata":        _collect_wikidata,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_sources(
    vendor_id: str,
    programme_id: str,
    readiness: EnrichmentReadinessResult,
    entity_data: dict[str, Any],
    workspace_root: Path | None = None,
) -> SourceEvidenceBundle:
    """Gather raw evidence from external sources per readiness.source_list.

    Network-facing. No LLM calls. No workspace writes. Never raises —
    all collector errors are captured as collection_flags.
    """
    ctx = _SearchContext(
        vendor_id=vendor_id,
        canonical_name=str(
            entity_data.get("vendor_name")
            or entity_data.get("canonical_name")
            or vendor_id
        ),
        domain=entity_data.get("domain"),
        website=entity_data.get("website"),
        hq_country=entity_data.get("hq_country"),
        source_list=readiness.source_list,
        depth_tier=readiness.depth_tier,
    )

    sources: dict[str, list[SourceEvidenceItem]] = {}
    all_flags: list[str] = []

    if not ctx.domain and not ctx.website:
        all_flags.append("MISSING_DOMAIN")

    for source_name in readiness.source_list:
        collector = _COLLECTORS.get(source_name)
        if collector is None:
            logger.warning("No collector for source %r — skipping", source_name)
            continue
        items, flags = collector(ctx)
        sources[source_name] = items
        all_flags.extend(flags)

    all_items = [item for item_list in sources.values() for item in item_list]
    disambiguation_notices: list[dict] = [
        {"source_url": item.source_url, "reason": "Content appears to reference a different entity"}
        for item in all_items
        if item.validation_status == "REJECTED"
    ]

    return SourceEvidenceBundle(
        vendor_id=vendor_id,
        depth_tier=readiness.depth_tier,
        sources=sources,
        disambiguation_notices=disambiguation_notices,
        collection_flags=all_flags,
    )
