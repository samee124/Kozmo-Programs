"""Tool 3 (Process 2) — attribute_extractor: convert raw evidence into structured attributes.

Uses ONE LLM call via llm_call(). No workspace writes. No network calls.

List fields (products_and_services, competitors, certifications, customer_segments,
reputation_signals) are stored in ExtractedAttributes.fields under underscore-prefixed keys:
  "_products_and_services", "_competitors", "_certifications",
  "_customer_segments", "_reputation_signals".
Each is an ExtractedField(value=list, confidence=..., source="MULTIPLE").
Tool 5 reads these composite fields.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from cobalt.core.llm_call import llm_call
from cobalt.models.schemas.enrichment_schema import (
    CONFIDENCE_LEVELS,
    ExtractedAttributes,
    ExtractedField,
    KnownFacts,
    SourceEvidenceBundle,
    SourceEvidenceItem,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FILTER_SOURCE_TYPES = frozenset({"COMPANY_WEBSITE", "LINKEDIN"})
_PROMPT_BUDGET = 30_000

_MARKETING_PATTERNS = re.compile(
    r"\bworld'?s leading\b"
    r"|\bindustry[-\s]first\b"
    r"|\bbest[-\s]in[-\s]class\b"
    r"|\b#1 (?:provider|platform|solution)\b"
    r"|\bmarket[-\s]leader\b"
    r"|\bleading provider\b"
    r"|\btop-rated\b"
    r"|\bcutting[-\s]edge\b"
    r"|\binnovative\b"
    r"|\brevolutionary\b"
    r"|\btransformative\b"
    r"|\bnext[-\s]generation\b"
    r"|\bgame[-\s]changing\b"
    r"|\bseamless\b"
    r"|\bunparalleled\b"
    r"|\btrusted by (?:thousands|millions) of (?:companies|customers|enterprises)\b"
    r"|\b(?:over|more than) \d+(?:,\d+)* (?:customers|enterprises|companies) (?:worldwide|globally)\b",
    re.IGNORECASE,
)

_EMPLOYEE_TO_SIZE_BAND: dict[str, str] = {
    "1-10":       "STARTUP",
    "11-50":      "STARTUP",
    "51-200":     "SMB",
    "201-500":    "SMB",
    "501-1000":   "MID_MARKET",
    "1001-5000":  "MID_MARKET",
    "5001-10000": "ENTERPRISE",
    "10000+":     "ENTERPRISE",
}

_REVENUE_TO_SIZE_BAND: dict[str, str] = {
    "<$1M":        "STARTUP",
    "$1M-$10M":    "SMB",
    "$10M-$50M":   "SMB",
    "$50M-$100M":  "MID_MARKET",
    "$100M-$500M": "MID_MARKET",
    "$500M-$1B":   "ENTERPRISE",
    "$1B-$10B":    "ENTERPRISE",
    "$10B+":       "ENTERPRISE",
}

_SYSTEM_PROMPT = (
    "You are the Analysis Agent for the Cobalt vendor intelligence platform.\n"
    "You extract structured facts from collected evidence. You do not infer beyond "
    "what evidence supports. You return ONLY JSON matching the requested schema. "
    "No preamble, no commentary, no markdown fences.\n"
    "For every field, choose ONE source as the primary evidence and record it.\n"
    "If two sources disagree on a field, record both values as a conflict and set "
    "confidence=CONFLICT. Do not resolve disagreements yourself.\n"
    "If evidence is insufficient for a field, set value=null and confidence=MISSING. "
    "Do not invent values.\n"
    "Marketing language has been stripped from the evidence. Treat "
    "[MARKETING_CLAIM_REMOVED] markers as redacted regions — they contained "
    "unverifiable promotional text.\n"
    "Note: WIKIDATA source items contain structured JSON facts (inception date, "
    "employee count, industries, headquarters). Treat WIKIDATA values as MEDIUM "
    "confidence unless corroborated by another source. WIKIDATA quality_signal is "
    "DIRECTORY — it is a reference database, not an authoritative official filing."
)

_OUTPUT_SCHEMA = """{
  "fields": {
    "website":              {"value": "<str|null>", "confidence": "HIGH|MEDIUM|LOW|MISSING", "source": "<source_type>"},
    "description":          {"value": "<str|null>", "confidence": "...", "source": "..."},
    "hq_city":              {"value": "<str|null>", "confidence": "...", "source": "..."},
    "hq_country":           {"value": "<ISO-3166-1-alpha-2|null>", "confidence": "...", "source": "..."},
    "hq_address":           {"value": "<full street address|null>", "confidence": "...", "source": "..."},
    "registered_address":   {"value": "<registered office address|null>", "confidence": "...", "source": "..."},
    "founding_year":        {"value": "<int|null>", "confidence": "...", "source": "..."},
    "incorporation_date":   {"value": "<YYYY-MM-DD|null>", "confidence": "...", "source": "..."},
    "company_status":       {"value": "<PUBLIC|PRIVATE|SUBSIDIARY|NON_PROFIT|UNKNOWN|null>", "confidence": "...", "source": "..."},
    "legal_name":           {"value": "<official registered legal name|null>", "confidence": "...", "source": "..."},
    "lei":                  {"value": "<20-char LEI code|null>", "confidence": "...", "source": "..."},
    "registration_number":  {"value": "<company reg number|null>", "confidence": "...", "source": "..."},
    "jurisdiction":         {"value": "<e.g. US-DE, GB, AU|null>", "confidence": "...", "source": "..."},
    "linkedin_url":         {"value": "<LinkedIn company page URL|null>", "confidence": "...", "source": "..."},
    "employee_count_range": {"value": "<1-10|11-50|51-200|201-500|501-1000|1001-5000|5001-10000|10000+|null>", "confidence": "...", "source": "..."},
    "revenue_range":        {"value": "<<$1M|$1M-$10M|$10M-$50M|$50M-$100M|$100M-$500M|$500M-$1B|$1B-$10B|$10B+|null>", "confidence": "...", "source": "..."},
    "revenue":              {"value": "<reported revenue string e.g. '$41.5B'|null>", "confidence": "...", "source": "..."},
    "company_size_band":    {"value": "<STARTUP|SMB|MID_MARKET|ENTERPRISE|null>", "confidence": "...", "source": "..."},
    "funding_stage":        {"value": "<BOOTSTRAPPED|SEED|SERIES_A|SERIES_B|SERIES_C_PLUS|PE_BACKED|PUBLIC|UNKNOWN|null>", "confidence": "...", "source": "..."},
    "ticker":               {"value": "<str|null>", "confidence": "...", "source": "..."},
    "exchange":             {"value": "<NYSE|NASDAQ|LSE|TSX|ASX|OTHER|null>", "confidence": "...", "source": "..."},
    "category":             {"value": "<IT_SOFTWARE|PROFESSIONAL_SERVICES|FACILITIES|LOGISTICS|MARKETING|FINANCE|HR|OTHER|null>", "confidence": "...", "source": "..."},
    "subcategory":          {"value": "<str|null>", "confidence": "...", "source": "..."},
    "industry":             {"value": "<CROSS_INDUSTRY|FINANCIAL_SERVICES|HEALTHCARE|PUBLIC_SECTOR|RETAIL|OTHER|null>", "confidence": "...", "source": "..."},
    "vendor_type":          {"value": "<SAAS|SERVICES|HARDWARE|CONSULTING|MARKETPLACE|STAFFING|INFRASTRUCTURE|OTHER|null>", "confidence": "...", "source": "..."},
    "primary_use_case":     {"value": "<str|null>", "confidence": "...", "source": "..."},
    "additional_categories":{"value": "<[list]|null>", "confidence": "...", "source": "..."}
  },
  "products_and_services": [{"name": "<str>", "type": "<PRODUCT|SERVICE|PLATFORM|MODULE|ADD_ON>", "description": "<str>", "source": "<str>"}],
  "competitors":            [{"name": "<str>", "source": "<str>"}],
  "certifications":         [{"name": "<str>", "type": "<SECURITY|QUALITY|DIVERSITY|PARTNER|REGULATORY>", "verification": "<SELF_REPORTED|THIRD_PARTY_VERIFIED>", "source": "<str>"}],
  "customer_segments":      [{"value": "<str>", "confidence_tag": "<SELF_REPORTED|THIRD_PARTY_CONFIRMED|CLAIMED>", "source": "<str>"}],
  "reputation_signals":     [{"signal_type": "<LEGAL|FINANCIAL_RISK|DATA_SECURITY|ETHICS|ESG|POSITIVE>", "description": "<str>", "source_url": "<str>", "publication_date": "<str|null>"}],
  "key_people":             [{"name": "<str>", "role": "<CEO|CFO|CTO|COO|FOUNDER|CHAIR|OTHER>", "source": "<str>"}],
  "conflicts":              [{"field": "<str>", "source_a": {}, "source_b": {}}]
}
Return ONLY the JSON. No other text."""

_LIST_KEY_MAP = [
    ("products_and_services", "_products_and_services"),
    ("competitors",           "_competitors"),
    ("certifications",        "_certifications"),
    ("customer_segments",     "_customer_segments"),
    ("reputation_signals",    "_reputation_signals"),
    ("key_people",            "_key_people"),
]

_OFFICIAL_SOURCES = frozenset({"company_website", "registry", "financial",
                                "COMPANY_WEBSITE", "REGISTRY", "FINANCIAL"})
_SOCIAL_DIR_SOURCES = frozenset({"social", "directory", "linkedin",
                                  "SOCIAL", "DIRECTORY", "LINKEDIN"})


# ---------------------------------------------------------------------------
# Private dataclass
# ---------------------------------------------------------------------------

@dataclass
class _FilteredBundle:
    vendor_id:              str
    depth_tier:             str
    sources:                dict[str, list[SourceEvidenceItem]]
    disambiguation_notices: list[dict]
    collection_flags:       list[str]
    extraction_flags:       list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Skill 0 — Marketing language filter
# ---------------------------------------------------------------------------

def _filter_sentences(text: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text)
    kept = []
    for part in parts:
        lower = part.lower()
        if "redefin" in lower and any(w in lower for w in ("enterprise", "industry", "how")):
            continue
        if "our mission is to transform" in lower:
            continue
        kept.append(part)
    return " ".join(kept)


def _filter_content(content: str) -> tuple[str, bool]:
    original_len = len(content)
    after_sentence = _filter_sentences(content)
    after_phrase = _MARKETING_PATTERNS.sub("[MARKETING_CLAIM_REMOVED]", after_sentence)
    non_marker_len = len(after_phrase.replace("[MARKETING_CLAIM_REMOVED]", ""))
    removed = original_len - non_marker_len
    is_heavy = original_len > 0 and (removed / original_len) > 0.30
    return after_phrase, is_heavy


def _copy_item(item: SourceEvidenceItem, content: str | None = None) -> SourceEvidenceItem:
    return SourceEvidenceItem(
        content=content if content is not None else item.content,
        source_type=item.source_type,
        source_url=item.source_url,
        retrieved_at=item.retrieved_at,
        validation_status=item.validation_status,
        quality_signal=item.quality_signal,
        signal_type=item.signal_type,
    )


def _filter_marketing_language(bundle: SourceEvidenceBundle) -> _FilteredBundle:
    """Return a filtered copy of the bundle. Original untouched."""
    new_sources: dict[str, list[SourceEvidenceItem]] = {}
    extraction_flags: list[str] = []
    heavy_found = False

    for source_name, items in bundle.sources.items():
        new_items: list[SourceEvidenceItem] = []
        for item in items:
            if item.source_type in _FILTER_SOURCE_TYPES and item.quality_signal == "OFFICIAL":
                filtered_content, is_heavy = _filter_content(item.content)
                if is_heavy:
                    heavy_found = True
                new_items.append(_copy_item(item, filtered_content))
            else:
                new_items.append(_copy_item(item))
        new_sources[source_name] = new_items

    if heavy_found:
        extraction_flags.append("MARKETING_HEAVY_SOURCE")

    return _FilteredBundle(
        vendor_id=bundle.vendor_id,
        depth_tier=bundle.depth_tier,
        sources=new_sources,
        disambiguation_notices=list(bundle.disambiguation_notices),
        collection_flags=list(bundle.collection_flags),
        extraction_flags=extraction_flags,
    )


# ---------------------------------------------------------------------------
# Skill 1-10 — Analysis Agent extraction (one LLM call)
# ---------------------------------------------------------------------------

def _build_evidence_list(filtered: _FilteredBundle) -> list[dict]:
    items = []
    for source_items in filtered.sources.values():
        for item in source_items:
            items.append({
                "source_type": item.source_type,
                "source_url":  item.source_url,
                "content":     item.content[:4000],
                "validation_status": item.validation_status,
                "quality_signal":    item.quality_signal,
            })
    return items


def _trim_to_budget(evidence_items: list[dict]) -> list[dict]:
    def _drop_priority(item: dict) -> int:
        if item.get("quality_signal") == "NEWS":
            return 3
        if item.get("validation_status") == "UNCERTAIN":
            return 2
        return 1

    total = sum(len(json.dumps(ev)) for ev in evidence_items)
    if total <= _PROMPT_BUDGET:
        return evidence_items

    sorted_items = sorted(evidence_items, key=_drop_priority, reverse=True)
    kept: list[dict] = []
    running = 0
    for item in sorted_items:
        size = len(json.dumps(item))
        if running + size <= _PROMPT_BUDGET:
            kept.append(item)
            running += size
    return kept


def _build_user_prompt(
    filtered: _FilteredBundle,
    vendor_id: str,
    known_facts: KnownFacts,
) -> str:
    evidence_items = _trim_to_budget(_build_evidence_list(filtered))
    parts = [
        f"VENDOR_ID: {vendor_id}",
        "",
        "KNOWN FACTS (already confirmed, do not re-extract, just echo if present in evidence with same value):",
        json.dumps(known_facts.confirmed),
        "",
        "PRIORITY GAPS (extract these first if evidence supports them):",
        json.dumps(known_facts.gaps),
        "",
        "EVIDENCE BUNDLE:",
        json.dumps(evidence_items, indent=2),
        "",
        "OUTPUT SCHEMA:",
        _OUTPUT_SCHEMA,
    ]
    return "\n".join(parts)


def _analysis_agent_extract(
    filtered: _FilteredBundle,
    vendor_id: str,
    known_facts: KnownFacts,
) -> dict:
    """ONE call to llm_call(). Returns parsed JSON dict. Raises on failure."""
    prompt = _build_user_prompt(filtered, vendor_id, known_facts)
    result = llm_call(prompt=prompt, system=_SYSTEM_PROMPT, expect_json=True)
    if not isinstance(result, dict):
        from cobalt.core.exceptions import LLMCallFailure
        raise LLMCallFailure(f"Expected dict from LLM, got {type(result).__name__}")
    return result


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def _list_confidence(items: list[dict]) -> str:
    if not items:
        return "MISSING"
    if any(item.get("source", "").lower() in {s.lower() for s in _OFFICIAL_SOURCES} for item in items):
        return "HIGH"
    if all(item.get("source", "").lower() in {s.lower() for s in _SOCIAL_DIR_SOURCES} for item in items):
        return "MEDIUM"
    return "LOW"


def _post_process(
    raw_extraction: dict,
    filtered: _FilteredBundle,
    known_facts: KnownFacts,
) -> tuple[dict[str, ExtractedField], list[str], list[dict]]:
    flags: list[str] = []
    conflicts: list[dict] = list(raw_extraction.get("conflicts") or [])
    conflict_fields = {
        c.get("field") for c in conflicts
        if isinstance(c, dict) and "field" in c
    }

    # Build ExtractedField objects
    fields: dict[str, ExtractedField] = {}
    raw_fields: dict = raw_extraction.get("fields") or {}

    for fname, fdata in raw_fields.items():
        if not isinstance(fdata, dict):
            continue
        value = fdata.get("value")
        confidence = str(fdata.get("confidence", "MISSING"))
        source = str(fdata.get("source", ""))

        # Fix: if LLM reported a conflict for this field but forgot confidence=CONFLICT
        if fname in conflict_fields and confidence != "CONFLICT":
            confidence = "CONFLICT"

        # Coerce invalid confidence levels
        if confidence not in CONFIDENCE_LEVELS:
            flags.append(f"LLM_INVALID_CONFIDENCE_{fname}")
            confidence = "LOW"

        fields[fname] = ExtractedField(value=value, confidence=confidence, source=source)

    # -----------------------------------------------------------------------
    # Inference: company_size_band from employee_count_range / revenue_range
    # -----------------------------------------------------------------------
    emp_field  = fields.get("employee_count_range")
    size_field = fields.get("company_size_band")
    rev_field  = fields.get("revenue_range")

    emp_value  = emp_field.value  if emp_field  else None
    size_value = size_field.value if size_field else None
    rev_value  = rev_field.value  if rev_field  else None

    emp_band = _EMPLOYEE_TO_SIZE_BAND.get(emp_value) if emp_value else None
    rev_band = _REVENUE_TO_SIZE_BAND.get(rev_value)  if rev_value else None

    if emp_band and rev_band and emp_band != rev_band:
        flags.append("SIZE_SIGNALS_CONFLICT")
        conflicts.append({
            "field":    "company_size_band",
            "source_a": {"value": emp_band, "source": "employee_count_range"},
            "source_b": {"value": rev_band, "source": "revenue_range"},
        })
    elif emp_band and not size_value:
        fields["company_size_band"] = ExtractedField(
            value=emp_band, confidence="INFERRED", source="INFERRED"
        )

    # -----------------------------------------------------------------------
    # Category conflict
    # -----------------------------------------------------------------------
    cat_field = fields.get("category")
    if "category" in known_facts.confirmed and cat_field and cat_field.value is not None:
        flags.append("CATEGORY_CONFLICT")

    # -----------------------------------------------------------------------
    # MISSING_CATEGORY flag
    # -----------------------------------------------------------------------
    cat_field = fields.get("category")
    if cat_field is None or cat_field.value is None or cat_field.confidence == "MISSING":
        flags.append("MISSING_CATEGORY")

    # -----------------------------------------------------------------------
    # NO_DESCRIPTION_EXTRACTABLE
    # -----------------------------------------------------------------------
    desc_field = fields.get("description")
    if desc_field is None or not desc_field.value:
        flags.append("NO_DESCRIPTION_EXTRACTABLE")

    # -----------------------------------------------------------------------
    # Composite list fields (underscore-prefixed)
    # -----------------------------------------------------------------------
    for raw_key, field_key in _LIST_KEY_MAP:
        items_list = raw_extraction.get(raw_key) or []
        if isinstance(items_list, list):
            fields[field_key] = ExtractedField(
                value=items_list,
                confidence=_list_confidence(items_list),
                source="MULTIPLE",
            )

    return fields, flags, conflicts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_attributes(
    bundle: SourceEvidenceBundle,
    known_facts: KnownFacts,
) -> ExtractedAttributes:
    """Filter marketing language, extract structured attributes via one LLM call.

    Returns ExtractedAttributes. Never raises — failures recorded as extraction_flags.
    Conflicts recorded but not resolved (Tool 5 resolves them).
    """
    flags: list[str] = []

    # Skill 0: marketing filter FIRST
    filtered = _filter_marketing_language(bundle)
    flags.extend(filtered.extraction_flags)

    # Guard: propagate inherited collection failures
    if "FAILED_COLLECTION" in bundle.collection_flags:
        flags.extend(["FAILED_COLLECTION", "NO_EVIDENCE_TO_EXTRACT"])
        return ExtractedAttributes(
            vendor_id=bundle.vendor_id, fields={}, conflicts=[], extraction_flags=flags
        )

    # Guard: empty bundle
    all_items = [item for items in filtered.sources.values() for item in items]
    if not all_items:
        flags.append("NO_EVIDENCE_TO_EXTRACT")
        return ExtractedAttributes(
            vendor_id=bundle.vendor_id, fields={}, conflicts=[], extraction_flags=flags
        )

    # Skills 1-10: one LLM call
    try:
        raw_extraction = _analysis_agent_extract(filtered, bundle.vendor_id, known_facts)
    except Exception as exc:
        logger.exception("Analysis Agent extraction failed: %s", exc)
        flags.append("EXTRACTION_FAILED")
        return ExtractedAttributes(
            vendor_id=bundle.vendor_id, fields={}, conflicts=[], extraction_flags=flags
        )

    if not isinstance(raw_extraction, dict) or "fields" not in raw_extraction:
        flags.append("EXTRACTION_FAILED")
        return ExtractedAttributes(
            vendor_id=bundle.vendor_id, fields={}, conflicts=[], extraction_flags=flags
        )

    extracted_fields, post_flags, post_conflicts = _post_process(
        raw_extraction, filtered, known_facts
    )
    flags.extend(post_flags)

    return ExtractedAttributes(
        vendor_id=bundle.vendor_id,
        fields=extracted_fields,
        conflicts=post_conflicts,
        extraction_flags=flags,
    )
