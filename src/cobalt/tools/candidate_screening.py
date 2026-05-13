"""Tool 2 — candidate_screening: structural clean, normalise, classify, disposition."""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from cobalt.intake._cleaner import structural_clean
from cobalt.intake._normalizer import normalize, detect_country_hint
from cobalt.models.schemas.signal_profile_schema import EntityType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Entity-type detection
# ---------------------------------------------------------------------------

_PERSON_TITLE_RE = re.compile(r"^(Mr|Mrs|Ms|Dr|Prof|Rev)\.?\s+\w+", re.IGNORECASE)

_CORPORATE_INDICATORS = {
    # Soft descriptors
    "consulting", "consultancy", "associates", "partners",
    "group", "solutions", "services", "ventures", "advisors",
    # Legal entity designators — hard evidence of a company
    "corp", "inc", "ltd", "limited", "llc", "llp", "plc",
    "gmbh", "ag", "kg", "sarl", "sas", "pty", "pte", "bv", "nv",
    "ltda", "fze", "fzc", "co",
}

_INTERNAL_INDICATORS = [
    "department", "dept", "division", "business unit",
    "team", "directorate", "committee", "board",
    "internal", "head office", "corporate",
]

# Pre-compiled whole-word pattern for internal indicators.
_INTERNAL_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(i) for i in _INTERNAL_INDICATORS) + r")\b",
    re.IGNORECASE,
)


def entity_type_detect(cleaned: str) -> EntityType:
    """Classify a cleaned name as COMPANY, PERSON, INTERNAL, or AMBIGUOUS."""
    lower = cleaned.lower()

    has_internal = bool(_INTERNAL_RE.search(lower))
    if has_internal:
        return EntityType.INTERNAL

    is_person_pattern = bool(_PERSON_TITLE_RE.match(cleaned))
    has_corporate = any(ind in lower for ind in _CORPORATE_INDICATORS)

    if is_person_pattern and not has_corporate:
        return EntityType.PERSON
    if is_person_pattern and has_corporate:
        return EntityType.AMBIGUOUS

    return EntityType.COMPANY


# ---------------------------------------------------------------------------
# ScreenedCandidate
# ---------------------------------------------------------------------------

@dataclass
class ScreenedCandidate:
    raw: str
    cleaned: str
    normalized: str
    comparison_key: str
    country_hint: Optional[str]
    entity_type: EntityType
    disposition: str          # PASS / REJECTED / RECLASSIFIED
    rejection_reason: Optional[str]
    source_refs: list[str] = field(default_factory=list)
    linked_doc_ids: list[str] = field(default_factory=list)
    spend_hint: Optional[Decimal] = None
    category_hint: Optional[str] = None
    extra_fields: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# screen() and screen_all()
# ---------------------------------------------------------------------------

def screen(
    raw: str,
    source_refs: list[str] | None = None,
    linked_doc_ids: list[str] | None = None,
    spend_hint: Decimal | None = None,
    category_hint: str | None = None,
    extra_fields: dict | None = None,
) -> ScreenedCandidate:
    """Screen a single raw vendor string through clean → normalise → classify."""
    cleaned = structural_clean(raw)
    country_hint = detect_country_hint(cleaned)
    normalized, comparison_key = normalize(cleaned, country_hint)
    entity_type = entity_type_detect(cleaned)

    if entity_type == EntityType.PERSON:
        disposition = "REJECTED"
        rejection_reason = "IS_PERSON"
    elif entity_type == EntityType.INTERNAL:
        disposition = "REJECTED"
        rejection_reason = "IS_INTERNAL"
    else:
        disposition = "PASS"
        rejection_reason = None

    return ScreenedCandidate(
        raw=raw,
        cleaned=cleaned,
        normalized=normalized,
        comparison_key=comparison_key,
        country_hint=country_hint,
        entity_type=entity_type,
        disposition=disposition,
        rejection_reason=rejection_reason,
        source_refs=list(source_refs) if source_refs else [],
        linked_doc_ids=list(linked_doc_ids) if linked_doc_ids else [],
        spend_hint=spend_hint,
        category_hint=category_hint,
        extra_fields=dict(extra_fields) if extra_fields else {},
    )


def screen_all(raw_candidates: list[dict]) -> list[ScreenedCandidate]:
    """Screen a list of candidate dicts, never raising on individual failures.

    Each dict is expected to have at minimum a "raw" key.  Any exception for a
    single entry is caught and the entry is returned as REJECTED / SCREENING_ERROR.
    """
    logger.info("Screening %d candidates", len(raw_candidates))
    results: list[ScreenedCandidate] = []
    for entry in raw_candidates:
        try:
            raw = entry.get("raw", "")
            result = screen(
                raw=raw,
                source_refs=entry.get("source_refs"),
                linked_doc_ids=entry.get("linked_doc_ids"),
                spend_hint=entry.get("spend_hint"),
                category_hint=entry.get("category_hint"),
                extra_fields=entry.get("extra_fields"),
            )
        except Exception:
            raw = entry.get("raw", "") if isinstance(entry, dict) else ""
            result = ScreenedCandidate(
                raw=raw,
                cleaned=raw,
                normalized="",
                comparison_key="",
                country_hint=None,
                entity_type=EntityType.AMBIGUOUS,
                disposition="REJECTED",
                rejection_reason="SCREENING_ERROR",
            )
        results.append(result)

    passed   = sum(1 for r in results if r.disposition == "PASS")
    persons  = sum(1 for r in results if r.rejection_reason == "IS_PERSON")
    internal = sum(1 for r in results if r.rejection_reason == "IS_INTERNAL")
    errors   = sum(1 for r in results if r.rejection_reason == "SCREENING_ERROR")
    logger.info(
        "Screening done — %d passed  %d rejected  (person=%d  internal=%d  error=%d)",
        passed, len(results) - passed, persons, internal, errors,
    )
    return results
