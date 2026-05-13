"""Tool 4 (Process 2) — relationship_and_lifecycle_mapper.

Brain-heavy, no LLM calls. Maps parent/subsidiary/brand relationships and
detects lifecycle events (rebrand, acquisition, merger, spin-off, defunct, etc.).
Generates BrainUpdateSuggestions for discoveries not yet in Brain.
Never writes to Brain or workspace.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

from cobalt.brain.loader import BrainData, load_brain
from cobalt.core.gleif import gleif_get_direct_parent, gleif_search_by_name
from cobalt.models.schemas.enrichment_schema import (
    BrainUpdateSuggestion,
    ExtractedAttributes,
    LifecycleSignal,
    RelationshipLifecycleResult,
    RelationshipMap,
    SourceEvidenceBundle,
    SourceEvidenceItem,
)

# ---------------------------------------------------------------------------
# Module-level compiled patterns
# ---------------------------------------------------------------------------

_REBRAND_PATTERNS = [
    re.compile(r"formerly known as ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"previously named ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"rebranded from ([A-Z][\w\s&.,'-]{2,80}) to", re.IGNORECASE),
    re.compile(r"name change from ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"([A-Z][\w\s&.,'-]{2,80}) is now ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
]

_ACQUISITION_PATTERNS = [
    re.compile(r"(?:was )?acquired by ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"acquisition (?:by|completed by) ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"now (?:a )?subsidiary of ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"to be acquired by ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"in talks (?:to be acquired by|with) ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"(?:rumoured|rumored)[^.]{0,60}([A-Z][\w\s&.,'-]{2,60})", re.IGNORECASE),
]

_MADE_ACQUISITION_PATTERNS = [
    re.compile(r"\b(?<!be )(?<!was )(?<!been )acquired ([A-Z][\w\s&.,'-]{2,60})", re.IGNORECASE),
]

_MERGER_PATTERNS = [
    re.compile(r"merged with ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"merger of ([A-Z][\w\s&.,'-]{2,80}) and ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"formed (?:from|by) the merger of ([A-Z][\w\s&.,'-]{2,80}) and ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
]

_SPINOFF_PATTERNS = [
    re.compile(r"spun off from ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"spinoff from ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
    re.compile(r"spun out (?:of|from) ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE),
]

_PARENT_PATTERNS = [
    (re.compile(r"(?:a )?wholly[- ]owned subsidiary of ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE), "WHOLLY_OWNED"),
    (re.compile(r"part of (?:the )?([A-Z][\w\s&.,'-]{2,80}) (?:Group|family|portfolio)", re.IGNORECASE), "PORTFOLIO_COMPANY"),
    (re.compile(r"subsidiary of ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE), "MAJORITY_OWNED"),
    (re.compile(r"owned by ([A-Z][\w\s&.,'-]{2,80})", re.IGNORECASE), "MAJORITY_OWNED"),
]

_SUBSIDIARY_PATTERNS = [
    re.compile(r"(?:our )?subsidiaries (?:include|are):? ([^.]{5,200})", re.IGNORECASE),
]

_BRAND_SCAN_PATTERNS = [
    re.compile(r"our brands? (?:include|includes|are):? ([^.]{5,200})", re.IGNORECASE),
    re.compile(r"brands? (?:include|includes):? ([^.]{5,200})", re.IGNORECASE),
]

_IPO_PATTERNS = [
    re.compile(r"went public (?:on|in) ([A-Z\d\s-]+)", re.IGNORECASE),
    re.compile(r"\bIPO (?:on|in) (\d{4})", re.IGNORECASE),
    re.compile(r"completed its initial public offering", re.IGNORECASE),
]

_PRIVATE_PATTERNS = [
    re.compile(r"taken private by", re.IGNORECASE),
    re.compile(r"went private", re.IGNORECASE),
    re.compile(r"acquired by ([A-Z][\w\s&.,'-]{2,80}) in a private", re.IGNORECASE),
]

_TRAILING_NOISE = re.compile(
    r"\s+(?:in|for|last|earlier|this|that|from|today|recently|during|after|before|by|with)\b.*$",
    re.IGNORECASE,
)

_DEFUNCT_KEYWORDS = frozenset({
    "dissolved", "struck off", "ceased operations",
    "no longer in business", "closed down",
})
_ACTIVE_KEYWORDS = frozenset({
    "launched", "announced new", "new contract", "partnership agreement",
    "new product", "new hire", "expanded into", "won contract",
})

_SOURCE_PRIORITY = ["company_website", "registry", "news", "linkedin", "web_search"]
_CONFIDENCE_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFERRED": 0, "MISSING": -1, "CONFLICT": -1}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _normalise_key(name: str) -> str:
    """Lowercase, strip non-word chars, collapse whitespace."""
    if not name:
        return ""
    cleaned = re.sub(r"[^\w\s]", "", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _trim_name(raw: str) -> str:
    """Strip trailing noise words and punctuation from a captured entity name."""
    trimmed = _TRAILING_NOISE.sub("", raw)
    return trimmed.strip().rstrip(".,;:")


def _source_confidence(source_type: str) -> str:
    if source_type.upper() in ("COMPANY_WEBSITE", "REGISTRY"):
        return "HIGH"
    if source_type.upper() == "NEWS":
        return "MEDIUM"
    return "LOW"


def _all_items(bundle: SourceEvidenceBundle) -> list[SourceEvidenceItem]:
    return [item for items in bundle.sources.values() for item in items]


def _items_by_priority(bundle: SourceEvidenceBundle) -> list[SourceEvidenceItem]:
    out: list[SourceEvidenceItem] = []
    for src in _SOURCE_PRIORITY:
        out.extend(bundle.sources.get(src, []))
    seen = {id(i) for i in out}
    for items in bundle.sources.values():
        for item in items:
            if id(item) not in seen:
                out.append(item)
                seen.add(id(item))
    return out


def _make_lifecycle(
    signal_type: str,
    from_: str | None,
    to: str | None,
    date: str | None,
    confidence: str,
    source: str,
    brain_update_required: bool = False,
) -> LifecycleSignal:
    return LifecycleSignal(
        signal_type=signal_type,
        from_=from_,
        to=to,
        date=date,
        confidence=confidence,
        source=source,
        brain_update_required=brain_update_required,
    )


def _make_suggestion(
    update_type: str,
    from_: str,
    to: str,
    confidence: str,
    source_url: str,
    vendor_id: str,
) -> BrainUpdateSuggestion:
    return BrainUpdateSuggestion(
        update_type=update_type,
        from_=from_,
        to=to,
        confidence=confidence,
        source_url=source_url,
        suggested_by_vendor_id=vendor_id,
        review_required=True,
    )


def _dedup_suggestions(suggestions: list[BrainUpdateSuggestion]) -> list[BrainUpdateSuggestion]:
    seen: dict[tuple, BrainUpdateSuggestion] = {}
    for s in suggestions:
        key = (s.update_type, s.from_, s.to)
        existing = seen.get(key)
        if existing is None or (_CONFIDENCE_ORDER.get(s.confidence, -1) >
                                 _CONFIDENCE_ORDER.get(existing.confidence, -1)):
            seen[key] = s
    return list(seen.values())


# ---------------------------------------------------------------------------
# Private context dataclass
# ---------------------------------------------------------------------------

@dataclass
class MappingContext:
    vendor_id:      str
    canonical_name: str
    canonical_key:  str
    aliases:        list[str]
    bundle:         SourceEvidenceBundle
    extracted:      ExtractedAttributes
    brain:          BrainData
    hq_country:     str | None = None


# ---------------------------------------------------------------------------
# Skill 5 — Rebrand detection
# ---------------------------------------------------------------------------

def _detect_rebrand(
    ctx: MappingContext,
    suggestions: list[BrainUpdateSuggestion],
    lifecycle: list[LifecycleSignal],
    flags: list[str],
) -> dict | None:
    brain = ctx.brain

    # Case (b): canonical_key IS a key in rebrand_map → entity.md has stale name
    if ctx.canonical_key in brain.rebrand_map:
        new_name = brain.rebrand_map[ctx.canonical_key]
        flags.append("CANONICAL_NAME_STALE_PER_BRAIN")
        lifecycle.append(_make_lifecycle(
            "REBRANDED", ctx.canonical_name, new_name, None, "HIGH", "brain"
        ))
        return {"from": ctx.canonical_name, "to": new_name, "date": None, "confidence": "HIGH"}

    # Case (a): canonical_key IS a value in rebrand_map → vendor is the new name
    for old_key, new_name in brain.rebrand_map.items():
        if _normalise_key(new_name) == ctx.canonical_key:
            lifecycle.append(_make_lifecycle(
                "REBRANDED", old_key, ctx.canonical_name, None, "HIGH", "brain"
            ))
            return {"from": old_key, "to": ctx.canonical_name, "date": None, "confidence": "HIGH"}

    # Evidence scan
    for item in _items_by_priority(ctx.bundle):
        for pat in _REBRAND_PATTERNS:
            m = pat.search(item.content)
            if not m:
                continue
            if m.lastindex and m.lastindex >= 2:
                from_raw = _trim_name(m.group(1))
                to_raw = _trim_name(m.group(2))
                if _normalise_key(to_raw) != ctx.canonical_key and not any(
                    _normalise_key(a) == _normalise_key(to_raw) for a in ctx.aliases
                ):
                    continue
            else:
                from_raw = _trim_name(m.group(1))
                to_raw = ctx.canonical_name

            confidence = _source_confidence(item.source_type)
            lifecycle.append(_make_lifecycle(
                "REBRANDED", from_raw, to_raw, None, confidence, item.source_type.lower(),
                brain_update_required=True,
            ))
            suggestions.append(_make_suggestion(
                "REBRAND_MAP", from_raw, ctx.canonical_name, confidence,
                item.source_url, ctx.vendor_id,
            ))
            return {"from": from_raw, "to": ctx.canonical_name, "date": None, "confidence": confidence}

    return None


# ---------------------------------------------------------------------------
# Skill 6 — Acquisition detection
# ---------------------------------------------------------------------------

def _determine_acq_status(content: str) -> str:
    lower = content.lower()
    if any(w in lower for w in ("rumour", "rumor", "in talks", "reportedly")):
        return "RUMOURED"
    if any(w in lower for w in ("to be acquired", "pending", "announced", "agreement to acquire")):
        return "ANNOUNCED"
    return "COMPLETED"


def _detect_acquisition(
    ctx: MappingContext,
    suggestions: list[BrainUpdateSuggestion],
    lifecycle: list[LifecycleSignal],
    acquirer_subsidiaries: list[dict],
) -> dict | None:
    brain = ctx.brain

    # Brain check
    if ctx.canonical_key in brain.acquisition_map:
        acq = brain.acquisition_map[ctx.canonical_key]
        acquired_by = acq["acquired_by"]
        date = acq.get("date")
        status = acq.get("status", "COMPLETED")
        lifecycle.append(_make_lifecycle(
            "ACQUIRED", None, acquired_by, date, "HIGH", "brain"
        ))
        return {
            "acquired_by": acquired_by,
            "acquired_by_key": acq.get("acquired_by_key", _normalise_key(acquired_by)),
            "date": date,
            "status": status,
            "confidence": "HIGH",
            "source": "brain",
        }

    # Evidence scan — detect this vendor being acquired
    for item in _items_by_priority(ctx.bundle):
        for pat in _ACQUISITION_PATTERNS:
            m = pat.search(item.content)
            if not m:
                continue
            acquirer_raw = _trim_name(m.group(1))
            if not acquirer_raw or len(acquirer_raw) < 3:
                continue
            status = _determine_acq_status(item.content)
            confidence = _source_confidence(item.source_type)
            if item.source_type.upper() == "NEWS" and item.signal_type == "ACQUIRED":
                confidence = "MEDIUM"
            lifecycle.append(_make_lifecycle(
                "ACQUIRED", None, acquirer_raw, None, confidence,
                item.source_type.lower(),
            ))
            suggestions.append(_make_suggestion(
                "ACQUISITION_MAP", ctx.canonical_name, acquirer_raw, confidence,
                item.source_url, ctx.vendor_id,
            ))
            return {
                "acquired_by": acquirer_raw,
                "acquired_by_key": _normalise_key(acquirer_raw),
                "date": None,
                "status": status,
                "confidence": confidence,
                "source": item.source_type.lower(),
            }

    # Detect reverse: this vendor acquired another → record as subsidiary candidate
    for item in _items_by_priority(ctx.bundle):
        for pat in _MADE_ACQUISITION_PATTERNS:
            m = pat.search(item.content)
            if not m:
                continue
            acquiree_raw = _trim_name(m.group(1))
            if acquiree_raw and len(acquiree_raw) >= 3:
                acquirer_subsidiaries.append({
                    "name": acquiree_raw,
                    "confidence": _source_confidence(item.source_type),
                    "source": item.source_type.lower(),
                })

    return None


# ---------------------------------------------------------------------------
# Skill 7 — Merger detection
# ---------------------------------------------------------------------------

def _detect_merger(
    ctx: MappingContext,
    lifecycle: list[LifecycleSignal],
    suggestions: list[BrainUpdateSuggestion],
) -> dict | None:
    for item in _items_by_priority(ctx.bundle):
        for pat in _MERGER_PATTERNS:
            m = pat.search(item.content)
            if not m:
                continue
            pred_a = _trim_name(m.group(1))
            pred_b = _trim_name(m.group(2)) if m.lastindex and m.lastindex >= 2 else None
            confidence = _source_confidence(item.source_type)
            lifecycle.append(_make_lifecycle(
                "MERGED", pred_a, ctx.canonical_name, None, confidence,
                item.source_type.lower(),
            ))
            for pred in filter(None, [pred_a, pred_b]):
                suggestions.append(_make_suggestion(
                    "ALIAS_DICTIONARY", pred, ctx.canonical_name, confidence,
                    item.source_url, ctx.vendor_id,
                ))
            return {"predecessor_a": pred_a, "predecessor_b": pred_b, "confidence": confidence}

    return None


# ---------------------------------------------------------------------------
# Skill 9 — Spin-off detection
# ---------------------------------------------------------------------------

def _detect_spinoff(
    ctx: MappingContext,
    lifecycle: list[LifecycleSignal],
) -> dict | None:
    for item in _items_by_priority(ctx.bundle):
        for pat in _SPINOFF_PATTERNS:
            m = pat.search(item.content)
            if not m:
                continue
            former_parent = _trim_name(m.group(1))
            confidence = _source_confidence(item.source_type)
            lifecycle.append(_make_lifecycle(
                "SPUN_OFF", former_parent, ctx.canonical_name, None, confidence,
                item.source_type.lower(),
            ))
            return {"former_parent": former_parent, "confidence": confidence}

    return None


# ---------------------------------------------------------------------------
# GLEIF helpers — used by Skill 1
# ---------------------------------------------------------------------------

def _strip_lei_suffixes(name: str) -> str:
    """Strip global corporate suffixes for GLEIF name similarity matching."""
    suffixes = (
        " incorporated", " inc.", " inc",
        " corporation", " corp.", " corp",
        " limited", " ltd.", " ltd",
        " llc", " l.l.c.",
        " plc", " plc.",
        " sa", " s.a.", " s.a",
        " gmbh", " ag", " kg",
        " bv", " b.v.",
        " nv", " n.v.",
        " co.", " co", " company",
        " group", " holdings",
    )
    n = name.lower().strip()
    changed = True
    while changed:
        changed = False
        for s in suffixes:
            if n.endswith(s):
                n = n[: -len(s)].strip()
                changed = True
    return n


def _pick_best_gleif_match(
    matches: list[dict],
    canonical_name: str,
    country_hint: str | None,
) -> dict | None:
    if not matches:
        return None
    target = canonical_name.lower().strip()

    def _score(m: dict) -> float:
        name_lc = m["legal_name"].lower().strip()
        if name_lc == target:
            base = 1.0
        elif target in name_lc or name_lc in target:
            base = 0.7
        else:
            a = set(_strip_lei_suffixes(target).split())
            b = set(_strip_lei_suffixes(name_lc).split())
            base = len(a & b) / len(a | b) if (a and b) else 0.0
        status_bonus = 0.1 if m.get("status") == "ACTIVE" else 0.0
        country_bonus = (
            0.1
            if country_hint and m.get("country")
            and country_hint.upper() == m["country"].upper()
            else 0.0
        )
        return base + status_bonus + country_bonus

    scored = sorted(matches, key=_score, reverse=True)
    best = scored[0]
    if _score(best) < 0.5:
        return None
    return best


def _identify_parent_via_gleif(ctx: MappingContext) -> dict | None:
    """Look up the vendor in GLEIF and resolve its direct parent.

    Returns a parent dict (same shape as _identify_parent output) or None
    if no LEI match, no parent link, or fetch failure.
    """
    try:
        matches = gleif_search_by_name(ctx.canonical_name, limit=3)
    except Exception:
        logger.exception("GLEIF search error for %r", ctx.canonical_name)
        return None

    if not matches:
        return None

    best = _pick_best_gleif_match(matches, ctx.canonical_name, ctx.hq_country)
    if best is None:
        return None

    if not best["has_direct_parent_link"]:
        return None

    try:
        parent = gleif_get_direct_parent(best["lei"])
    except Exception:
        logger.exception("GLEIF parent fetch error for LEI %r", best["lei"])
        return None

    if parent is None:
        return None

    parent_key = _normalise_key(parent["legal_name"])
    parent_vendor_id = parent_key if parent_key in ctx.brain.known_vendors else None

    return {
        "name":              parent["legal_name"],
        "vendor_id":         parent_vendor_id,
        "relationship_type": "WHOLLY_OWNED",
        "confidence":        "HIGH",
        "source":            "gleif",
        "lei":               parent["lei"],
    }


# ---------------------------------------------------------------------------
# Skill 1 — Parent company identification
# ---------------------------------------------------------------------------

def _identify_parent(ctx: MappingContext, acquired: dict | None) -> dict | None:
    brain = ctx.brain

    # Step 1: Acquisition-derived parent (highest priority — Brain-confirmed)
    if acquired and acquired.get("confidence") == "HIGH":
        acquirer_key = acquired.get("acquired_by_key", _normalise_key(acquired["acquired_by"]))
        parent_vid = acquirer_key if acquirer_key in brain.known_vendors else None
        return {
            "name":              acquired["acquired_by"],
            "vendor_id":         parent_vid,
            "relationship_type": "WHOLLY_OWNED",
            "confidence":        "HIGH",
            "source":            acquired.get("source", "evidence"),
        }

    # Step 2: GLEIF LEI registry (authoritative legal-entity parent relationships)
    gleif_parent = _identify_parent_via_gleif(ctx)
    if gleif_parent is not None:
        return gleif_parent

    # Step 3: Website pattern scan (existing heuristic logic)
    for item in _items_by_priority(ctx.bundle):
        for pat, rel_type in _PARENT_PATTERNS:
            m = pat.search(item.content)
            if not m:
                continue
            parent_name = _trim_name(m.group(1))
            if not parent_name:
                continue
            parent_key = _normalise_key(parent_name)
            parent_vid = parent_key if parent_key in brain.known_vendors else None
            return {
                "name":              parent_name,
                "vendor_id":         parent_vid,
                "relationship_type": rel_type,
                "confidence":        _source_confidence(item.source_type),
                "source":            item.source_type.lower(),
            }

    return None


# ---------------------------------------------------------------------------
# Skill 2 — Subsidiary mapping
# ---------------------------------------------------------------------------

def _map_subsidiaries(ctx: MappingContext, extra: list[dict]) -> list[dict]:
    results: list[dict] = list(extra)
    seen_names = {_normalise_key(s["name"]) for s in results}

    for item in _items_by_priority(ctx.bundle):
        for pat in _SUBSIDIARY_PATTERNS:
            m = pat.search(item.content)
            if not m:
                continue
            raw_list = m.group(1)
            parts = re.split(r",\s*|\s+and\s+|\s*;\s*", raw_list)
            for part in parts:
                name = part.strip().rstrip(".,;:")
                if len(name) < 2:
                    continue
                nk = _normalise_key(name)
                if nk not in seen_names:
                    results.append({
                        "name":       name,
                        "confidence": _source_confidence(item.source_type),
                        "source":     item.source_type.lower(),
                    })
                    seen_names.add(nk)

    return results


# ---------------------------------------------------------------------------
# Skill 3 — Brand-to-company resolution
# ---------------------------------------------------------------------------

def _resolve_brands(
    ctx: MappingContext,
    suggestions: list[BrainUpdateSuggestion],
    flags: list[str],
) -> list[dict]:
    brain = ctx.brain
    results: list[dict] = []
    seen: set[str] = set()

    # Case (A): canonical_key IS a brand in brand_map
    if ctx.canonical_key in brain.brand_map:
        legal_entity = brain.brand_map[ctx.canonical_key]
        flags.append("BRAND_AS_LEGAL_ENTITY_RISK")
        results.append({
            "brand_name":  ctx.canonical_name,
            "maps_to":     legal_entity,
            "confidence":  "HIGH",
            "source":      "brain",
        })
        seen.add(ctx.canonical_key)

    # Case (B): this vendor owns brands — scan brand_map for values matching canonical_key
    for brand_key, legal_key in brain.brand_map.items():
        if _normalise_key(legal_key) == ctx.canonical_key and brand_key not in seen:
            results.append({
                "brand_name":  brand_key,
                "maps_to":     ctx.canonical_name,
                "confidence":  "HIGH",
                "source":      "brain",
            })
            seen.add(brand_key)

    # Scan COMPANY_WEBSITE content for "our brands include X, Y"
    for item in ctx.bundle.sources.get("company_website", []):
        for pat in _BRAND_SCAN_PATTERNS:
            m = pat.search(item.content)
            if not m:
                continue
            raw_list = m.group(1)
            parts = re.split(r",\s*|\s+and\s+|\s*;\s*", raw_list)
            for part in parts:
                brand = part.strip().rstrip(".,;:")
                if len(brand) < 2:
                    continue
                bk = _normalise_key(brand)
                if bk in seen:
                    continue
                seen.add(bk)
                results.append({
                    "brand_name":  brand,
                    "maps_to":     ctx.canonical_name,
                    "confidence":  "MEDIUM",
                    "source":      "company_website",
                })
                # If not in brand_map → suggest Brain update
                if bk not in brain.brand_map:
                    suggestions.append(_make_suggestion(
                        "BRAND_MAP", brand, ctx.canonical_name, "MEDIUM",
                        item.source_url, ctx.vendor_id,
                    ))

    return results


# ---------------------------------------------------------------------------
# Skill 4 — Former names
# ---------------------------------------------------------------------------

def _extract_former_names(
    ctx: MappingContext,
    rebrand: dict | None,
    acquired: dict | None,
    merged: dict | None,
    suggestions: list[BrainUpdateSuggestion],
) -> list[dict]:
    brain = ctx.brain
    results: list[dict] = []
    seen: set[str] = set()

    def _add(name: str, transition_type: str, date: str | None) -> None:
        nk = _normalise_key(name)
        if not nk or nk in seen:
            return
        seen.add(nk)
        results.append({
            "name":            name,
            "transition_type": transition_type,
            "transitioned_from": date,
        })
        # Suggest ALIAS_DICTIONARY if not already there
        if nk not in {_normalise_key(k) for k in brain.alias_map}:
            suggestions.append(_make_suggestion(
                "ALIAS_DICTIONARY", name, ctx.canonical_name, "MEDIUM",
                "", ctx.vendor_id,
            ))

    # From rebrand
    if rebrand:
        _add(rebrand["from"], "REBRAND", rebrand.get("date"))

    # From merger
    if merged:
        _add(merged["predecessor_a"], "POST_MERGER", None)
        if merged.get("predecessor_b"):
            _add(merged["predecessor_b"], "POST_MERGER", None)

    # From Brain alias_map
    for alias, canonical in brain.alias_map.items():
        if _normalise_key(canonical) == ctx.canonical_key:
            _add(alias, "REBRAND", None)

    return results


# ---------------------------------------------------------------------------
# Skill 8 — Defunct detection
# ---------------------------------------------------------------------------

def _detect_defunct(ctx: MappingContext, lifecycle: list[LifecycleSignal]) -> bool:
    bundle = ctx.bundle
    score = 0
    top_source = "multiple"

    # Company website exists but has empty content
    site_items = bundle.sources.get("company_website")
    if site_items is not None:
        for item in site_items:
            if not item.content.strip():
                score += 2
                break

    # News source exists but has 0 items
    news_items = bundle.sources.get("news")
    if news_items is not None and len(news_items) == 0:
        score += 2

    # Web search exists but returned 0 results
    web_items = bundle.sources.get("web_search")
    if web_items is not None and len(web_items) == 0:
        score += 1

    # Evidence content contains defunct keywords (+3, once)
    for items in bundle.sources.values():
        if score >= 3:
            break
        for item in items:
            if any(kw in item.content.lower() for kw in _DEFUNCT_KEYWORDS):
                score += 3
                top_source = item.source_url or item.source_type.lower()
                break

    # Active signal offset (-3, once)
    for items in bundle.sources.values():
        for item in items:
            if any(kw in item.content.lower() for kw in _ACTIVE_KEYWORDS):
                score -= 3
                break
        else:
            continue
        break

    if score >= 4:
        lifecycle.append(_make_lifecycle(
            "POSSIBLY_DEFUNCT", None, ctx.canonical_name, None,
            "MEDIUM", top_source,
        ))
        return True
    return False


# ---------------------------------------------------------------------------
# Skill 10 — Public/private transitions
# ---------------------------------------------------------------------------

def _detect_public_private_transitions(
    ctx: MappingContext,
    lifecycle: list[LifecycleSignal],
) -> None:
    for item in _items_by_priority(ctx.bundle):
        for pat in _IPO_PATTERNS:
            if pat.search(item.content):
                lifecycle.append(_make_lifecycle(
                    "WENT_PUBLIC", None, ctx.canonical_name, None,
                    _source_confidence(item.source_type), item.source_type.lower(),
                ))
                return

    for item in _items_by_priority(ctx.bundle):
        for pat in _PRIVATE_PATTERNS:
            if pat.search(item.content):
                lifecycle.append(_make_lifecycle(
                    "WENT_PRIVATE", None, ctx.canonical_name, None,
                    _source_confidence(item.source_type), item.source_type.lower(),
                ))
                return


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def map_relationships_and_lifecycle(
    bundle: SourceEvidenceBundle,
    extracted: ExtractedAttributes,
    entity_data: dict,
    brain: BrainData | None = None,
) -> RelationshipLifecycleResult:
    """Map relationships and lifecycle events. No LLM, no workspace writes."""
    if brain is None:
        brain = load_brain()

    canonical_name = str(entity_data.get("canonical_name") or "")
    vendor_id = str(entity_data.get("vendor_id") or bundle.vendor_id)
    canonical_key = _normalise_key(canonical_name)
    aliases = list(entity_data.get("aliases") or [])
    hq_country = entity_data.get("hq_country")

    ctx = MappingContext(
        vendor_id=vendor_id,
        canonical_name=canonical_name,
        canonical_key=canonical_key,
        aliases=aliases,
        bundle=bundle,
        extracted=extracted,
        brain=brain,
        hq_country=hq_country,
    )

    flags: list[str] = []
    suggestions: list[BrainUpdateSuggestion] = []
    lifecycle: list[LifecycleSignal] = []
    acquirer_subsidiaries: list[dict] = []

    # Skills 5, 6, 7, 9 — event detection
    rebrand = _detect_rebrand(ctx, suggestions, lifecycle, flags)
    acquired = _detect_acquisition(ctx, suggestions, lifecycle, acquirer_subsidiaries)
    merged = _detect_merger(ctx, lifecycle, suggestions)
    _detect_spinoff(ctx, lifecycle)

    # Skill 1 — parent (depends on acquisition result)
    parent = _identify_parent(ctx, acquired)

    # Skill 2 — subsidiaries
    subsidiaries = _map_subsidiaries(ctx, acquirer_subsidiaries)

    # Skill 3 — brands
    brands = _resolve_brands(ctx, suggestions, flags)

    # Skill 4 — former names
    former_names = _extract_former_names(ctx, rebrand, acquired, merged, suggestions)

    # Skill 8 — defunct detection
    _detect_defunct(ctx, lifecycle)

    # Skill 10 — public/private transitions
    _detect_public_private_transitions(ctx, lifecycle)

    # Defunct + Acquired collision: ACQUIRED takes precedence
    has_acquired = any(s.signal_type == "ACQUIRED" for s in lifecycle)
    if has_acquired:
        lifecycle = [s for s in lifecycle if s.signal_type != "POSSIBLY_DEFUNCT"]

    # Skill 11 — top-level flags
    if any(s.signal_type == "POSSIBLY_DEFUNCT" for s in lifecycle):
        flags.append("POSSIBLY_DEFUNCT")
    if any(s.signal_type == "ACQUIRED" and s.confidence != "HIGH" for s in lifecycle):
        flags.append("ACQUISITION_UNRESOLVED")
    if lifecycle:
        flags.append("LIFECYCLE_EVENT_DETECTED")
    if suggestions:
        flags.append("BRAIN_UPDATE_PENDING")
    recorded_parent = entity_data.get("parent_company")
    if parent and recorded_parent and parent.get("name") != recorded_parent:
        flags.append("PARENT_CHANGED")

    # Dedup suggestions
    suggestions = _dedup_suggestions(suggestions)

    relationship_map = RelationshipMap(
        vendor_id=vendor_id,
        parent_company=parent,
        subsidiaries=subsidiaries,
        brands=brands,
        former_names=former_names,
    )

    return RelationshipLifecycleResult(
        relationship_map=relationship_map,
        lifecycle_signals=lifecycle,
        brain_update_suggestions=suggestions,
        flags=flags,
    )
