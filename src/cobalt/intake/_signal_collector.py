"""Signal collector — pure computation, no LLM calls, no external I/O."""

from decimal import Decimal
from typing import Optional

from cobalt.brain.loader import BrainData, KnownVendor
from cobalt.intake._cleaner import structural_clean
from cobalt.intake._normalizer import detect_country_hint, normalize
from cobalt.models.schemas.signal_profile_schema import (
    ApSignal,
    BatchContext,
    BrainHit,
    DedupResult,
    EntityType,
    ErpSignal,
    ScriptType,
    SignalProfile,
)


# ---------------------------------------------------------------------------
# Script detection
# ---------------------------------------------------------------------------

def detect_script(text: str) -> ScriptType:
    """Detect the dominant Unicode script of a string."""
    counts: dict[ScriptType, int] = {
        ScriptType.CJK: 0,
        ScriptType.ARABIC: 0,
        ScriptType.CYRILLIC: 0,
        ScriptType.DEVANAGARI: 0,
    }
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF or 0xAC00 <= cp <= 0xD7AF:
            counts[ScriptType.CJK] += 1
        elif 0x0600 <= cp <= 0x06FF:
            counts[ScriptType.ARABIC] += 1
        elif 0x0400 <= cp <= 0x04FF:
            counts[ScriptType.CYRILLIC] += 1
        elif 0x0900 <= cp <= 0x097F:
            counts[ScriptType.DEVANAGARI] += 1

    best_type = max(counts, key=lambda k: counts[k])
    if counts[best_type] >= 2:
        return best_type
    return ScriptType.LATIN


# ---------------------------------------------------------------------------
# Brain lookup — rebrand → alias → known_vendor (most specific first)
# ---------------------------------------------------------------------------

def brain_lookup(key: str, brain_data: BrainData) -> BrainHit:
    """Look up a comparison key in the Brain, most-specific match first."""
    # 1. Rebrand map — vendor has changed its legal name
    if key in brain_data.rebrand_map:
        target = brain_data.rebrand_map[key]
        return BrainHit(
            matched=True,
            confidence=0.95,
            canonical=target,
            match_type="REBRAND",
            rebrand_match=True,
            rebrand_target=target,
            alias_match=False,
            alias_target=None,
        )

    # 2. Alias map — short name or common variant
    if key in brain_data.alias_map:
        target = brain_data.alias_map[key]
        return BrainHit(
            matched=True,
            confidence=0.97,
            canonical=target,
            match_type="ALIAS",
            rebrand_match=False,
            rebrand_target=None,
            alias_match=True,
            alias_target=target,
        )

    # 3. Known vendors — direct entry
    if key in brain_data.known_vendors:
        vendor = brain_data.known_vendors[key]
        return BrainHit(
            matched=True,
            confidence=0.99,
            canonical=vendor.canonical_name,
            match_type="KNOWN_VENDOR",
            rebrand_match=False,
            rebrand_target=None,
            alias_match=False,
            alias_target=None,
        )

    # No match
    return BrainHit(
        matched=False,
        confidence=0.0,
        canonical=None,
        match_type=None,
        rebrand_match=False,
        rebrand_target=None,
        alias_match=False,
        alias_target=None,
    )


# ---------------------------------------------------------------------------
# Similarity — Jaro-Winkler (inline, no external deps)
# ---------------------------------------------------------------------------

def _jaro_winkler(s1: str, s2: str) -> float:
    """Return Jaro-Winkler similarity in [0.0, 1.0]."""
    if s1 == s2:
        return 1.0
    len1, len2 = len(s1), len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    match_dist = max(len1, len2) // 2 - 1
    if match_dist < 0:
        match_dist = 0

    s1_matched = [False] * len1
    s2_matched = [False] * len2
    matches = 0

    for i in range(len1):
        lo = max(0, i - match_dist)
        hi = min(i + match_dist + 1, len2)
        for j in range(lo, hi):
            if s2_matched[j] or s1[i] != s2[j]:
                continue
            s1_matched[i] = True
            s2_matched[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i in range(len1):
        if not s1_matched[i]:
            continue
        while k < len2 and not s2_matched[k]:
            k += 1
        if k < len2 and s1[i] != s2[k]:
            transpositions += 1
        if k < len2:
            k += 1

    jaro = (matches / len1 + matches / len2 + (matches - transpositions / 2) / matches) / 3

    # Winkler prefix bonus (up to 4 chars)
    prefix = 0
    for i in range(min(4, len1, len2)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break

    return jaro + prefix * 0.1 * (1 - jaro)


# ---------------------------------------------------------------------------
# Deduplication check
# ---------------------------------------------------------------------------

def dedup_check(key: str, all_keys: list[str]) -> DedupResult:
    """Compare key against all_keys; return the best match result."""
    best_sim = 0.0
    best_key: Optional[str] = None

    for other in all_keys:
        if other == key:
            continue
        # Quick length pre-check: if lengths differ by >50%, skip expensive Jaro-Winkler
        len_diff_ratio = abs(len(key) - len(other)) / max(len(key), len(other))
        if len_diff_ratio > 0.5:
            continue
        sim = _jaro_winkler(key, other)
        if sim > best_sim:
            best_sim = sim
            best_key = other
            # Early exit if we found a strong match
            if best_sim >= 0.95:
                break

    if best_key is None or best_sim < 0.80:
        return DedupResult(status="UNIQUE", match_key=None, match_name=None, similarity=0.0)

    if best_sim >= 0.90:
        status = "AUTO_MERGE"
    else:
        status = "CANDIDATE"

    return DedupResult(
        status=status,
        match_key=best_key,
        match_name=best_key,
        similarity=best_sim,
    )


# ---------------------------------------------------------------------------
# Full signal collection
# ---------------------------------------------------------------------------

def collect_signals(raw: str, ctx: BatchContext) -> SignalProfile:
    """Collect all deterministic signals for a raw vendor name.

    No external calls. No LLM. Safe defaults for any missing ctx fields.
    """
    # 1. Structural clean
    cleaned = structural_clean(raw)

    # 2. Script detection
    script_type = detect_script(cleaned)

    # 3. Country hint
    country_hint = detect_country_hint(cleaned)

    # 4. Normalize
    normalized, comparison_key = normalize(cleaned, country_hint)

    # 5. Brain lookup
    brain_data = BrainData(
        known_vendors=ctx.known_vendors,
        rebrand_map=ctx.rebrand_map,
        alias_map=ctx.alias_map,
    )
    brain_hit = brain_lookup(comparison_key, brain_data)

    # Backfill country_hint and category_hint from Brain record when the raw
    # vendor name didn't carry that information (the common case for plain lists).
    if brain_hit.matched and brain_hit.match_type == "KNOWN_VENDOR":
        _kv = brain_data.known_vendors.get(comparison_key)
        if _kv is not None:
            if country_hint is None and _kv.country_code:
                country_hint = _kv.country_code

    # 6. Dedup
    dedup_result = dedup_check(comparison_key, ctx.all_keys)

    # 7. Entity type — import here to avoid circular at module level
    from cobalt.tools.candidate_screening import entity_type_detect
    entity_type = entity_type_detect(cleaned)

    # 8. ERP signal
    erp_signal = ctx.erp_hits.get(
        comparison_key,
        ErpSignal(exists=False, spend=None, category=None, vendor_ids=[]),
    )

    # 9. AP signal
    ap_signal = ctx.ap_counts.get(
        comparison_key,
        ApSignal(invoice_count=0, flags=[], single_approver=False, approver_id=None),
    )

    # 10. Linked doc IDs
    doc_registry = getattr(ctx, "doc_registry", {})
    linked_doc_ids = doc_registry.get(comparison_key, [])

    # 11. Spend, category, and extra_fields hints
    candidate_hints = getattr(ctx, "candidate_hints", {})
    hints = candidate_hints.get(comparison_key, {})
    spend_hint: Optional[Decimal] = hints.get("spend_hint")
    category_hint: Optional[str] = hints.get("category_hint")
    extra_fields: dict = hints.get("extra_fields") or {}

    # Backfill category_hint from Brain record (after candidate_hints are resolved,
    # so explicit per-candidate hints always win over the Brain default).
    if brain_hit.matched and brain_hit.match_type == "KNOWN_VENDOR" and category_hint is None:
        _kv = brain_data.known_vendors.get(comparison_key)
        if _kv is not None and _kv.category:
            category_hint = _kv.category

    return SignalProfile(
        raw=raw,
        cleaned=cleaned,
        script_type=script_type,
        country_hint=country_hint,
        normalized=normalized,
        comparison_key=comparison_key,
        brain_hit=brain_hit,
        erp_signal=erp_signal,
        ap_signal=ap_signal,
        linked_doc_ids=linked_doc_ids,
        spend_hint=spend_hint,
        category_hint=category_hint,
        entity_type=entity_type,
        dedup_result=dedup_result,
        extra_fields=extra_fields,
    )
