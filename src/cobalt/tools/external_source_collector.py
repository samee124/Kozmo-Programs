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

import urllib.error
import urllib.parse
import urllib.request

from cobalt.core.companies_house import (
    companies_house_get_company,
    companies_house_search,
)
from cobalt.core.exceptions import SecEdgarError
from cobalt.core.gleif import gleif_search_by_name
from cobalt.core.name_matching import normalise_for_match
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
            a = set(normalise_for_match(target).split())
            b = set(normalise_for_match(title).split())
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
    target_tokens = set(normalise_for_match(target).split())
    if not target_tokens:
        return 0
    count = 0
    for r in results:
        title = r.get("title", "").lower().strip()
        title_tokens = set(normalise_for_match(title).split())
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
            a = set(normalise_for_match(target).split())
            b = set(normalise_for_match(name).split())
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
    target_tokens = set(normalise_for_match(target).split())
    if not target_tokens:
        return 0
    count = 0
    for r in results:
        name = (r.get("name") or "").lower()
        tokens = set(normalise_for_match(name).split())
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
    target_simple = normalise_for_match(target)
    name_simple = normalise_for_match(name)
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


def _backfill_website_from_search(ctx: _SearchContext, items: list[SourceEvidenceItem]) -> None:
    """If no website/domain at intake, infer the official site from web_search results.

    A domain that appears in 2+ OFFICIAL results is almost certainly the vendor's own site
    (root + at least one subpage), while noise results each come from a different domain.
    Social-media domains are excluded.
    """
    _SOCIAL_DOMAINS = {"instagram.com", "facebook.com", "twitter.com", "x.com",
                       "linkedin.com", "tiktok.com", "youtube.com", "yelp.com"}
    seen: dict[str, str] = {}  # base_domain → first full URL
    for item in items:
        if item.quality_signal not in ("OFFICIAL",):
            continue
        try:
            parsed = urllib.parse.urlparse(item.source_url)
            domain = parsed.netloc.removeprefix("www.")
            if not domain or domain in _SOCIAL_DOMAINS:
                continue
            if domain in seen:
                # Same domain appeared twice → strong signal this is the official site
                ctx.website = seen[domain]
                logger.debug("[backfill] ctx.website inferred as %r (appeared 2+ times in web_search)", ctx.website)
                return
            seen[domain] = item.source_url
        except Exception:
            continue


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
            a = set(normalise_for_match(target).split())
            b = set(normalise_for_match(label).split())
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
    target_tokens = set(normalise_for_match(target).split())
    if not target_tokens:
        return 0
    count = 0
    for m in matches:
        label = (m.get("label") or "").lower().strip()
        label_tokens = set(normalise_for_match(label).split())
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


def _gleif_normalize(s: str) -> str:
    """Strip punctuation/suffixes for fuzzy GLEIF name matching."""
    import re as _re
    return _re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _collect_gleif(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    """Look up the vendor in GLEIF LEI registry for authoritative entity data.

    Provides LEI, legal name, jurisdiction, and parent/subsidiary links for
    ~2.5M regulated entities globally. No API key required.
    """
    try:
        matches = gleif_search_by_name(ctx.canonical_name, limit=5)
    except Exception:
        logger.exception("GLEIF search error for %r", ctx.canonical_name)
        return [], ["GLEIF_FETCH_ERROR"]

    if not matches:
        return [], ["NO_GLEIF_RECORD"]

    target_norm = _gleif_normalize(ctx.canonical_name)
    best = None
    for m in matches:
        if _gleif_normalize(m.get("legal_name") or "") == target_norm:
            best = m
            break
    if best is None:
        # Fuzzy: accept if normalized target is a substring of the legal name or vice versa
        candidate = matches[0]
        cand_norm = _gleif_normalize(candidate.get("legal_name") or "")
        if target_norm in cand_norm or cand_norm in target_norm:
            best = candidate
        else:
            return [], ["NO_GLEIF_RECORD"]

    lei = best.get("lei", "")
    item = SourceEvidenceItem(
        content=json.dumps(best, sort_keys=True),
        source_type="GLEIF",
        source_url=f"https://www.gleif.org/lei/{lei}" if lei else "",
        retrieved_at=_now_iso(),
        validation_status="CONFIRMED" if _gleif_normalize(best.get("legal_name") or "") == target_norm else "LIKELY",
        quality_signal="OFFICIAL",
        signal_type=None,
    )
    return [item], []


def _collect_opensanctions(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    """Screen the vendor against OpenSanctions watchlist/sanctions database.

    Uses the free public search endpoint. Optional OPENSANCTIONS_API_KEY for
    higher rate limits. Returns a SANCTIONS source item if results found.
    """
    api_key = os.environ.get("OPENSANCTIONS_API_KEY", "")
    url = "https://api.opensanctions.org/search/default"
    params = urllib.parse.urlencode({"q": ctx.canonical_name, "schema": "Company", "limit": 5})
    full_url = f"{url}?{params}"

    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"

    req = urllib.request.Request(full_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("OpenSanctions unavailable for %r: %s", ctx.canonical_name, exc)
        return [], ["OPENSANCTIONS_FETCH_ERROR"]

    results = data.get("results") or []
    if not results:
        return [], ["NO_SANCTIONS_RECORD"]

    item = SourceEvidenceItem(
        content=json.dumps(data, sort_keys=True),
        source_type="SANCTIONS",
        source_url=full_url,
        retrieved_at=_now_iso(),
        validation_status="UNCERTAIN",
        quality_signal="OFFICIAL",
        signal_type="SANCTIONS_HIT" if results else None,
    )
    return [item], []


def _collect_abn_lookup(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    """Look up an Australian vendor in the ABN (Australian Business Number) register.

    Only called for AU vendors. Requires ABN_LOOKUP_GUID env var.
    Free public API from the Australian Business Register.
    """
    hq = (ctx.hq_country or "").upper()
    if hq not in {"AU", "AUS", "AUSTRALIA"}:
        return [], []

    guid = os.environ.get("ABN_LOOKUP_GUID", "")
    if not guid:
        return [], ["ABN_LOOKUP_NO_GUID"]

    name_enc = urllib.parse.quote(ctx.canonical_name)
    url = (
        f"https://api.abr.business.gov.au/ABRxmlSearch/AbrXmlSearch.asmx/SearchByNameSimpleProtocol"
        f"?name={name_enc}&postcode=&legalName=Y&tradingName=Y&NSW=Y&SA=Y&ACT=Y&VIC=Y&WA=Y"
        f"&NT=Y&QLD=Y&TAS=Y&authenticationGuid={guid}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:
        logger.exception("ABN Lookup error for %r", ctx.canonical_name)
        return [], ["ABN_LOOKUP_FETCH_ERROR"]

    item = SourceEvidenceItem(
        content=raw[:4000],
        source_type="REGISTRY",
        source_url=url,
        retrieved_at=_now_iso(),
        validation_status=_validate_against_entity(raw, url, ctx),
        quality_signal="OFFICIAL",
        signal_type=None,
    )
    return [item], []


def _collect_sirene(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    """Look up a French vendor in the SIRENE official business register.

    Only called for FR vendors. Requires SIRENE_API_TOKEN env var (INSEE token).
    """
    hq = (ctx.hq_country or "").upper()
    if hq not in {"FR", "FRA", "FRANCE"}:
        return [], []

    token = os.environ.get("SIRENE_API_TOKEN", "")
    if not token:
        return [], ["SIRENE_NO_TOKEN"]

    name_enc = urllib.parse.quote(ctx.canonical_name)
    url = f"https://api.insee.fr/api-sirene/3.11/siren?q=denominationUniteLegale:{name_enc}&nombre=5"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        logger.exception("SIRENE search error for %r", ctx.canonical_name)
        return [], ["SIRENE_FETCH_ERROR"]

    units = data.get("unitesLegales") or []
    if not units:
        return [], ["NO_SIRENE_RECORD"]

    item = SourceEvidenceItem(
        content=json.dumps(units[0], sort_keys=True),
        source_type="REGISTRY",
        source_url=url,
        retrieved_at=_now_iso(),
        validation_status=_validate_against_entity(json.dumps(units[0]), url, ctx),
        quality_signal="OFFICIAL",
        signal_type=None,
    )
    return [item], []


def _collect_wikipedia(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    """Fetch a Wikipedia page summary for the vendor.

    Extends Wikidata with natural-language description from Wikipedia REST API.
    Returns MEDIUM-confidence DIRECTORY quality items.
    """
    title_enc = urllib.parse.quote(ctx.canonical_name.replace(" ", "_"))
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title_enc}"
    req = urllib.request.Request(url, headers={"User-Agent": "Cobalt VendorIntelligence/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.debug("No Wikipedia article for %r", ctx.canonical_name)
        else:
            logger.warning("Wikipedia HTTP %s for %r", exc.code, ctx.canonical_name)
        return [], ["WIKIPEDIA_FETCH_ERROR"]
    except Exception as exc:
        logger.warning("Wikipedia unavailable for %r: %s", ctx.canonical_name, exc)
        return [], ["WIKIPEDIA_FETCH_ERROR"]

    if data.get("type") == "https://mediawiki.org/wiki/HyperSwitch/errors/not_found":
        return [], ["NO_WIKIPEDIA_RECORD"]

    extract = data.get("extract") or ""
    if not extract:
        return [], ["NO_WIKIPEDIA_RECORD"]

    item = SourceEvidenceItem(
        content=extract[:4000],
        source_type="WIKIDATA",
        source_url=data.get("content_urls", {}).get("desktop", {}).get("page", url),
        retrieved_at=_now_iso(),
        validation_status=_validate_against_entity(extract, url, ctx),
        quality_signal="DIRECTORY",
        signal_type=_detect_lifecycle_signal(extract),
    )
    return [item], []


def _collect_search_discovery(ctx: _SearchContext) -> tuple[list[SourceEvidenceItem], list[str]]:
    """DuckDuckGo Instant Answer API for low-confidence discovery enrichment.

    Free, no API key. Returns abstract text if available. LOW confidence.
    """
    query_enc = urllib.parse.quote(ctx.canonical_name)
    url = f"https://api.duckduckgo.com/?q={query_enc}&format=json&no_redirect=1&no_html=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Cobalt VendorIntelligence/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        logger.exception("DuckDuckGo search error for %r", ctx.canonical_name)
        return [], ["SEARCH_DISCOVERY_FETCH_ERROR"]

    abstract = data.get("Abstract") or data.get("AbstractText") or ""
    if not abstract:
        return [], ["NO_SEARCH_DISCOVERY_RESULT"]

    item = SourceEvidenceItem(
        content=abstract[:2000],
        source_type="WEB_SEARCH",
        source_url=data.get("AbstractURL") or url,
        retrieved_at=_now_iso(),
        validation_status=_validate_against_entity(abstract, url, ctx),
        quality_signal="DIRECTORY",
        signal_type=_detect_lifecycle_signal(abstract),
    )
    return [item], []


_RS_BLOCKED_FIELDS: frozenset[str] = frozenset({
    "contract_value", "renewal_date", "sla_terms", "auto_renewal", "notice_period",
})

_CONTRACT_DE_FIELDS: frozenset[str] = frozenset({
    "counterparty_legal_name", "counterparty_registration_number",
    "counterparty_jurisdiction", "counterparty_registered_address",
    "counterparty_governing_law", "contract_type",
})


def _extract_contract_entity_fields(file_path: str, ctx: _SearchContext) -> str | None:
    """Read contract file bytes, extract text, call LLM for DE entity fields.

    Returns a formatted content string on success, None on any failure.
    Only extracts DE-relevant entity fields — never RS fields.
    """
    from cobalt.core.exceptions import ContractExtractionError
    try:
        path = Path(file_path)
        text: str | None = None

        if path.suffix.lower() == ".pdf":
            try:
                from pdfminer.high_level import extract_text as pdfminer_extract
                text = pdfminer_extract(file_path)
            except ImportError:
                pass
            if not text:
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(file_path)
                    text = "\n".join(
                        page.extract_text() or "" for page in reader.pages
                    )
                except ImportError:
                    pass
        elif path.suffix.lower() == ".docx":
            try:
                import docx
                doc = docx.Document(file_path)
                text = "\n".join(para.text for para in doc.paragraphs)
            except ImportError:
                pass

        if not text or len(text.strip()) < 100:
            raise ContractExtractionError(
                f"Text extraction yielded < 100 chars for {file_path}"
            )

        prompt = (
            "Extract ONLY these entity identification facts from the contract below.\n"
            "Return null for any field not explicitly stated. Do not infer. Do not guess.\n"
            "Return JSON only. No preamble.\n\n"
            "Fields:\n"
            "- counterparty_legal_name\n"
            "- counterparty_registration_number\n"
            "- counterparty_jurisdiction\n"
            "- counterparty_registered_address\n"
            "- counterparty_governing_law\n"
            "- contract_type (MSA/SOW/DPA/LICENCE/FRAMEWORK/AMENDMENT/UNKNOWN)\n\n"
            f"Contract text (first 3000 characters):\n{text[:3000]}"
        )

        from cobalt.core.llm_call import llm_call
        raw = llm_call(prompt=prompt, system="You are a contract analysis assistant. Return valid JSON only.", expect_json=True)
        if not isinstance(raw, dict):
            raise ContractExtractionError("LLM returned non-dict response")

        lines = ["CONTRACT ENTITY FACTS:"]
        for field_name in _CONTRACT_DE_FIELDS:
            val = raw.get(field_name)
            if val:
                lines.append(f"{field_name}: {val}")
        return "\n".join(lines)

    except Exception as exc:
        logger.warning("[contract] extraction failed for %r: %s", file_path, exc)
        return None


def _collect_contract_evidence(
    ctx: _SearchContext,
    contract_evidence: list[dict],
    now_iso: str,
) -> list[SourceEvidenceItem]:
    """Build SourceEvidenceItems from pre-detected contract evidence.

    Scenario A (rs_extracted): format existing DE fields as content, no LLM call.
    Scenario B (uploaded_file + needs_extraction): call LLM for entity fields.
    RS fields (contract_value, renewal_date, etc.) are never included in content.
    """
    items: list[SourceEvidenceItem] = []
    for entry in contract_evidence:
        source = entry.get("source")
        content: str | None = None

        if source == "rs_extracted":
            lines = ["CONTRACT ENTITY FACTS:"]
            for field_name in _CONTRACT_DE_FIELDS:
                val = entry.get(field_name)
                if val:
                    lines.append(f"{field_name}: {val}")
            if len(lines) > 1:
                content = "\n".join(lines)

        elif source == "uploaded_file" and entry.get("needs_extraction"):
            content = _extract_contract_entity_fields(entry.get("file_path", ""), ctx)

        if not content:
            continue

        items.append(SourceEvidenceItem(
            content=content,
            source_type="CONTRACT",
            source_url=entry.get("file_path") or f"workspace://contract/{ctx.vendor_id}",
            retrieved_at=now_iso,
            validation_status="CONFIRMED",
            quality_signal="OFFICIAL",
            signal_type=None,
        ))
    return items


_COLLECTORS: dict[str, Any] = {
    "web_search":        _collect_web_search,
    "company_website":   _collect_company_website,
    "news":              _collect_news,
    "linkedin":          _collect_linkedin,
    "registry":          _collect_registry,
    "financial":         _collect_financial,
    "wikidata":          _collect_wikidata,
    "gleif":             _collect_gleif,
    "opensanctions":     _collect_opensanctions,
    "abn_lookup":        _collect_abn_lookup,
    "sirene":            _collect_sirene,
    "wikipedia":         _collect_wikipedia,
    "search_discovery":  _collect_search_discovery,
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
        if source_name == "web_search" and not ctx.website and not ctx.domain:
            _backfill_website_from_search(ctx, items)

    # Contract evidence — collect separately after the main source loop
    if getattr(readiness, "contract_evidence", None):
        contract_items = _collect_contract_evidence(ctx, readiness.contract_evidence, _now_iso())
        if contract_items:
            sources["contract"] = contract_items

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
