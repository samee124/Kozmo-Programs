"""Tool 5 (Process 2) — enriched_profile_creator.

Reconciles all upstream outputs into one authoritative VendorProfile,
writes vendor_profile.md atomically, appends an enrichment ledger entry,
syncs the DB row, recomputes PCS, and generates triage tasks.

No LLM calls — purely deterministic reconciliation.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from cobalt.core.atomic_write import append_md
from cobalt.core.exceptions import EnrichedProfileWriteError
from cobalt.models.schemas.enrichment_schema import (
    BrainUpdateSuggestion,
    EnrichedProfileResult,
    EnrichmentReadinessResult,
    ExtractedAttributes,
    ExtractedField,
    LifecycleSignal,
    RelationshipLifecycleResult,
    VendorProfile,
)
from cobalt.workspace.builder import append_enrichment_ledger_entry, write_vendor_profile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_PRIORITY: dict[str, int] = {
    "COMPANY_WEBSITE": 1,
    "REGISTRY":        2,
    "LINKEDIN":        3,
    "FINANCIAL":       4,
    "NEWS":            5,
    "WEB_SEARCH":      6,
    "INFERRED":        7,
}

CORE_FIELDS: list[str] = [
    "category", "subcategory", "hq_country", "description",
    "company_status", "vendor_type", "company_size_band",
]

BLOCKING_GAP_FIELDS: frozenset[str] = frozenset({
    "category", "hq_country", "description", "company_status",
})

ENRICHMENT_GAP_FIELDS: frozenset[str] = frozenset({
    "competitors", "certifications", "customer_segments",
    "revenue_range", "funding_stage", "products_and_services",
    "subcategory", "industry", "vendor_type", "company_size_band",
    "founding_year", "hq_city", "employee_count_range",
    "primary_use_case",
    "_competitors", "_certifications", "_customer_segments",
    "_reputation_signals", "_products_and_services",
})

PCS_WEIGHTS: dict[str, float] = {
    "category":             0.10,
    "hq_country":           0.06,
    "description":          0.06,
    "company_status":       0.05,
    "vendor_type":          0.05,
    "company_size_band":    0.04,
    "parent_resolved":      0.04,
    "lifecycle_evaluated":  0.03,
    "certifications":       0.02,
    "reputation_evaluated": 0.02,
}

# Unambiguous city→ISO-3166-1-alpha-2 mapping (keep small — no ambiguous cities)
_CITY_TO_COUNTRY: dict[str, str] = {
    "tokyo":        "JP",
    "osaka":        "JP",
    "singapore":    "SG",
    "reykjavik":    "IS",
    "dubai":        "AE",
    "abu dhabi":    "AE",
    "helsinki":     "FI",
    "stockholm":    "SE",
    "oslo":         "NO",
    "amsterdam":    "NL",
    "zurich":       "CH",
    "vienna":       "AT",
    "brussels":     "BE",
    "lisbon":       "PT",
    "nairobi":      "KE",
    "lagos":        "NG",
    "riyadh":       "SA",
    "doha":         "QA",
    "muscat":       "OM",
    "bangkok":      "TH",
    "kuala lumpur": "MY",
    "jakarta":      "ID",
    "manila":       "PH",
    "taipei":       "TW",
    "seoul":        "KR",
    "beijing":      "CN",
    "shanghai":     "CN",
    "hong kong":    "HK",
}

_EMPLOYEE_TO_SIZE_BAND: dict[str, str] = {
    "1-10":     "STARTUP",
    "11-50":    "STARTUP",
    "51-200":   "SMB",
    "201-500":  "SMB",
    "501-1000": "MID_MARKET",
    "1001-5000": "MID_MARKET",
    "5001-10000": "ENTERPRISE",
    "10000+":   "ENTERPRISE",
}

# Non-underscore list keys have underscore equivalents in ExtractedAttributes.
# If either form is non-null the conceptual gap is satisfied.
_LIST_FIELD_ALIASES: dict[str, str] = {
    "competitors":          "_competitors",
    "certifications":       "_certifications",
    "customer_segments":    "_customer_segments",
    "products_and_services": "_products_and_services",
    "key_people":           "_key_people",
}

_IDENTITY_FIELDS      = ["website", "description", "hq_city", "hq_country",
                          "founding_year", "company_status", "legal_name", "lei",
                          "registration_number", "jurisdiction", "incorporation_date",
                          "hq_address", "registered_address", "linkedin_url"]
_CLASSIFICATION_FIELDS = ["category", "subcategory", "industry", "vendor_type",
                           "primary_use_case", "additional_categories"]
_SIZE_FIELDS           = ["employee_count_range", "company_size_band", "revenue_range",
                           "funding_stage", "ticker", "exchange", "revenue"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _field_dict(ef: ExtractedField | None) -> dict:
    if ef is None:
        return {"value": None, "confidence": "MISSING", "source": ""}
    return {"value": ef.value, "confidence": ef.confidence, "source": ef.source}


def _get_list(key: str, fields: dict[str, ExtractedField]) -> list:
    ef = fields.get(key)
    if ef is None or ef.value is None:
        return []
    return ef.value if isinstance(ef.value, list) else []


def _get_profile_field(profile: VendorProfile, fname: str) -> dict | None:
    for section in (profile.identity, profile.classification, profile.size):
        if fname in section:
            return section[fname]
    return None


# ---------------------------------------------------------------------------
# Skill 1 — Source priority reconciliation
# ---------------------------------------------------------------------------

def _reconcile_conflicts(
    extracted: ExtractedAttributes,
    readiness: EnrichmentReadinessResult,
) -> tuple[dict[str, ExtractedField], list[dict]]:
    reconciled: dict[str, ExtractedField] = {}
    unresolved: list[dict] = []

    conflict_lookup: dict[str, dict] = {
        c["field"]: c
        for c in extracted.conflicts
        if isinstance(c, dict) and "field" in c
    }

    for fname, ef in extracted.fields.items():
        # Composite fields pass through unchanged
        if fname.startswith("_"):
            reconciled[fname] = ef
            continue

        # Non-conflict fields pass through unchanged
        if ef.confidence != "CONFLICT":
            reconciled[fname] = ef
            continue

        conflict = conflict_lookup.get(fname)
        if conflict is None:
            reconciled[fname] = ef
            continue

        src_a = conflict.get("source_a") or {}
        src_b = conflict.get("source_b") or {}
        st_a = str(src_a.get("source", "")).upper()
        st_b = str(src_b.get("source", "")).upper()
        prio_a = SOURCE_PRIORITY.get(st_a, 99)
        prio_b = SOURCE_PRIORITY.get(st_b, 99)

        # Both REGISTRY → cannot auto-resolve; different jurisdictions
        if st_a == "REGISTRY" and st_b == "REGISTRY":
            reconciled[fname] = ef  # stays CONFLICT
            unresolved.append(conflict)
            continue

        # Determine winner by priority (lower = higher priority)
        if prio_a < prio_b:
            winner = src_a
        elif prio_b < prio_a:
            winner = src_b
        else:
            # Same priority: more recent retrieved_at wins
            ra_a = str(src_a.get("retrieved_at", ""))
            ra_b = str(src_b.get("retrieved_at", ""))
            winner = src_a if ra_a >= ra_b else src_b

        reconciled[fname] = ExtractedField(
            value=winner.get("value"),
            confidence="LOW",          # downgraded because disagreement existed
            source=str(winner.get("source", ef.source)),
        )

    return reconciled, unresolved


# ---------------------------------------------------------------------------
# Skill 2 — Inference rules
# ---------------------------------------------------------------------------

def _apply_inference_rules(
    fields: dict[str, ExtractedField],
) -> dict[str, ExtractedField]:
    result = dict(fields)

    # Rule 1: company_size_band from employee_count_range
    size_ef = result.get("company_size_band")
    emp_ef  = result.get("employee_count_range")
    if (size_ef is None or size_ef.value is None) and emp_ef and emp_ef.value:
        band = _EMPLOYEE_TO_SIZE_BAND.get(str(emp_ef.value))
        if band:
            result["company_size_band"] = ExtractedField(
                value=band, confidence="INFERRED", source="INFERRED"
            )

    # Rule 2: hq_country from hq_city (unambiguous cities only)
    country_ef = result.get("hq_country")
    city_ef    = result.get("hq_city")
    if (country_ef is None or country_ef.value is None) and city_ef and city_ef.value:
        cc = _CITY_TO_COUNTRY.get(str(city_ef.value).lower().strip())
        if cc:
            result["hq_country"] = ExtractedField(
                value=cc, confidence="INFERRED", source="INFERRED"
            )

    return result


# ---------------------------------------------------------------------------
# Skill 5 — Gap classification
# ---------------------------------------------------------------------------

def _classify_gaps(
    fields: dict[str, ExtractedField],
) -> dict[str, list[str]]:
    def _is_gap(ef: ExtractedField | None) -> bool:
        if ef is None:
            return True
        if ef.value is None:
            return True
        if isinstance(ef.value, str) and ef.value == "":
            return True
        if ef.confidence == "MISSING":
            return True
        return False

    blocking: list[str] = []
    enrichment: list[str] = []

    for fname in BLOCKING_GAP_FIELDS:
        if _is_gap(fields.get(fname)):
            blocking.append(fname)

    reported: set[str] = set()
    for fname in sorted(ENRICHMENT_GAP_FIELDS):
        if fname in BLOCKING_GAP_FIELDS or fname in reported:
            continue
        alias = _LIST_FIELD_ALIASES.get(fname)
        if alias:
            # Gap only if BOTH the regular and underscore forms are missing/null
            gap = _is_gap(fields.get(fname)) and _is_gap(fields.get(alias))
            # Don't double-report the alias
            reported.add(alias)
        else:
            gap = _is_gap(fields.get(fname))
        if gap:
            enrichment.append(fname)

    return {"blocking": sorted(blocking), "enrichment": sorted(enrichment)}


# ---------------------------------------------------------------------------
# Skill 6 — Flag generation
# ---------------------------------------------------------------------------

def _generate_flags(
    reconciled_fields: dict[str, ExtractedField],
    gaps: dict[str, list[str]],
    readiness: EnrichmentReadinessResult,
    extracted: ExtractedAttributes,
    relationship_result: RelationshipLifecycleResult,
    unresolved_conflicts: list[dict],
    brain_update_suggestions: list[BrainUpdateSuggestion],
) -> list[str]:
    flags: list[str] = []
    seen: set[str] = set()

    def _add(f: str) -> None:
        if f not in seen:
            flags.append(f)
            seen.add(f)

    # Carry forward upstream flags (extraction first, then relationship)
    for f in extracted.extraction_flags:
        _add(f)
    for f in relationship_result.flags:
        _add(f)

    # NO_DIGITAL_PRESENCE: no website, description, hq_country AND no linkedin attempted
    def _null(fname: str) -> bool:
        ef = reconciled_fields.get(fname)
        return ef is None or ef.value is None

    if (
        _null("website") and _null("description") and _null("hq_country")
        and "linkedin" not in [s.lower() for s in readiness.source_list]
    ):
        _add("NO_DIGITAL_PRESENCE")

    # CONFLICTING_DESCRIPTION
    desc_ef = reconciled_fields.get("description")
    if desc_ef and desc_ef.confidence == "CONFLICT":
        _add("CONFLICTING_DESCRIPTION")

    # SINGLE_SOURCE_ONLY: all populated non-INFERRED fields share one source_type
    real_sources = {
        ef.source.upper()
        for ef in reconciled_fields.values()
        if ef.value is not None and ef.source and ef.source.upper() not in ("INFERRED", "")
    }
    if len(real_sources) == 1:
        _add("SINGLE_SOURCE_ONLY")

    # PARTIAL_PROFILE
    if gaps["enrichment"]:
        _add("PARTIAL_PROFILE")

    # MISSING_CATEGORY
    if "category" in gaps["blocking"]:
        _add("MISSING_CATEGORY")

    # MISSING_HQ
    if "hq_country" in gaps["blocking"]:
        _add("MISSING_HQ")

    # BRAIN_UPDATE_PENDING
    if brain_update_suggestions:
        _add("BRAIN_UPDATE_PENDING")

    return flags


# ---------------------------------------------------------------------------
# Skill 4 — Overall confidence
# ---------------------------------------------------------------------------

def _compute_overall_confidence(
    reconciled_fields: dict[str, ExtractedField],
    gaps: dict[str, list[str]],
    readiness: EnrichmentReadinessResult,
    flags_in: list[str],
) -> str:
    # Identity floor / PROVISIONAL tier
    if readiness.confidence_floor < 0.60 or readiness.depth_tier == "PROVISIONAL":
        return "PROVISIONAL"

    # WRONG_ENTITY_RISK forces PROVISIONAL
    if "WRONG_ENTITY_RISK" in flags_in:
        return "PROVISIONAL"

    high_count = medium_count = low_count = missing_count = 0

    for fname in CORE_FIELDS:
        ef = reconciled_fields.get(fname)
        if ef is None or ef.value is None or ef.confidence == "MISSING":
            missing_count += 1
        elif ef.confidence == "HIGH":
            high_count += 1
        elif ef.confidence == "MEDIUM":
            medium_count += 1
        elif ef.confidence in {"LOW", "INFERRED", "CONFLICT"}:
            low_count += 1

    if missing_count == 0 and low_count == 0:
        return "HIGH"
    if missing_count <= 1 and low_count <= 2:
        return "MEDIUM"
    return "LOW"


# ---------------------------------------------------------------------------
# Skill 7 — Profile status
# ---------------------------------------------------------------------------

def _classify_profile_status(
    reconciled_fields: dict[str, ExtractedField],
    gaps: dict[str, list[str]],
    readiness: EnrichmentReadinessResult,
    extracted: ExtractedAttributes,
    relationship_result: RelationshipLifecycleResult,
) -> str:
    flags_combined = set(extracted.extraction_flags) | set(relationship_result.flags)

    # FAILED_ENRICHMENT: no core fields populated at all
    populated_core = [
        f for f in CORE_FIELDS
        if reconciled_fields.get(f) is not None and reconciled_fields[f].value is not None
    ]
    if not populated_core:
        return "FAILED_ENRICHMENT"

    # PROVISIONAL
    if (
        readiness.depth_tier == "PROVISIONAL"
        or "WRONG_ENTITY_RISK" in flags_combined
        or "LOW_IDENTITY_CONFIDENCE" in flags_combined
        or "EXTRACTION_FAILED" in flags_combined
        or "NO_DIGITAL_PRESENCE" in flags_combined
    ):
        return "PROVISIONAL"

    if gaps["blocking"]:
        return "PROVISIONAL"

    # PARTIALLY_ENRICHED: any core LOW/INFERRED or any enrichment gap
    has_low_core = any(
        reconciled_fields[f].confidence in {"LOW", "INFERRED"}
        for f in CORE_FIELDS
        if reconciled_fields.get(f) is not None and reconciled_fields[f].value is not None
    )
    if has_low_core or gaps["enrichment"]:
        return "PARTIALLY_ENRICHED"

    return "ENRICHED"


# ---------------------------------------------------------------------------
# Skill 10 — PCS update
# ---------------------------------------------------------------------------

def _compute_pcs(
    pcs_before: float,
    profile: VendorProfile,
    relationship_result: RelationshipLifecycleResult,
) -> float:
    delta = 0.0

    # Weighted regular fields
    for fname in ["category", "hq_country", "description", "company_status",
                  "vendor_type", "company_size_band"]:
        fd = _get_profile_field(profile, fname) or {}
        value = fd.get("value")
        confidence = fd.get("confidence", "MISSING")
        if value is None:
            continue
        if confidence in {"HIGH", "MEDIUM"}:
            delta += PCS_WEIGHTS[fname]
        elif confidence in {"LOW", "INFERRED"}:
            delta += PCS_WEIGHTS[fname] / 2

    # parent_resolved: parent_company not null
    if relationship_result.relationship_map.parent_company is not None:
        delta += PCS_WEIGHTS["parent_resolved"]

    # lifecycle_evaluated: any lifecycle signal
    if relationship_result.lifecycle_signals:
        delta += PCS_WEIGHTS["lifecycle_evaluated"]

    # certifications: list non-empty
    if profile.certifications:
        delta += PCS_WEIGHTS["certifications"]

    # reputation_evaluated: list non-empty
    if profile.reputation_signals:
        delta += PCS_WEIGHTS["reputation_evaluated"]

    # Cap delta at Process 2 maximum
    delta = min(delta, 0.47)
    return min(1.0, pcs_before + delta)


# ---------------------------------------------------------------------------
# Build VendorProfile object
# ---------------------------------------------------------------------------

def _build_vendor_profile(
    vendor_id: str,
    entity_data: dict,
    reconciled_fields: dict[str, ExtractedField],
    gaps: dict[str, list[str]],
    flags: list[str],
    profile_status: str,
    overall_confidence: str,
    relationship_result: RelationshipLifecycleResult,
    extracted: ExtractedAttributes,
    readiness: EnrichmentReadinessResult,
    now: str,
) -> VendorProfile:
    canonical_name = str(entity_data.get("canonical_name") or vendor_id)

    identity       = {f: _field_dict(reconciled_fields.get(f)) for f in _IDENTITY_FIELDS}
    classification = {f: _field_dict(reconciled_fields.get(f)) for f in _CLASSIFICATION_FIELDS}
    size           = {f: _field_dict(reconciled_fields.get(f)) for f in _SIZE_FIELDS}

    rm = relationship_result.relationship_map
    organisation = {
        "parent_company": rm.parent_company,
        "subsidiaries":   rm.subsidiaries,
        "brands":         rm.brands,
        "former_names":   rm.former_names,
    }

    enrichment_metadata = {
        "depth_tier":   readiness.depth_tier,
        "sources_used": readiness.source_list,
        "pcs_before":   0.0,   # updated after PCS computed
        "pcs_after":    0.0,
    }

    return VendorProfile(
        vendor_id=vendor_id,
        canonical_name=canonical_name,
        profile_status=profile_status,
        overall_confidence=overall_confidence,
        enriched_at=now,
        identity=identity,
        classification=classification,
        size=size,
        organisation=organisation,
        products_and_services=_get_list("_products_and_services", reconciled_fields),
        competitors=_get_list("_competitors", reconciled_fields),
        certifications=_get_list("_certifications", reconciled_fields),
        customer_segments=_get_list("_customer_segments", reconciled_fields),
        reputation_signals=_get_list("_reputation_signals", reconciled_fields),
        key_people=_get_list("_key_people", reconciled_fields),
        lifecycle_signals=relationship_result.lifecycle_signals,
        gaps=gaps,
        flags=flags,
        enrichment_metadata=enrichment_metadata,
    )


# ---------------------------------------------------------------------------
# Skill 8 — Triage task generation
# ---------------------------------------------------------------------------

def _generate_triage_tasks(
    profile: VendorProfile,
    unresolved_conflicts: list[dict],
    extracted: ExtractedAttributes,
    relationship_result: RelationshipLifecycleResult,
    readiness: EnrichmentReadinessResult,
) -> list[dict]:
    tasks: list[dict] = []
    seen_types: set[str] = set()

    def _add_task(t: dict) -> None:
        tt = t.get("triage_type", "")
        # Deduplicate by triage_type (except UNRESOLVED_CONFLICT which is per-field)
        if tt != "UNRESOLVED_CONFLICT" and tt in seen_types:
            return
        tasks.append(t)
        seen_types.add(tt)

    pf = profile.flags

    # ENTITY_DISAMBIGUATION
    if "DISAMBIGUATION_REQUIRED" in pf or "WRONG_ENTITY_RISK" in pf:
        _add_task({
            "vendor_id":          profile.vendor_id,
            "canonical_name":     profile.canonical_name,
            "triage_type":        "ENTITY_DISAMBIGUATION",
            "question":           "Confirm the correct vendor entity for this record",
            "evidence":           (
                "Multiple entities found or source mismatch detected during collection"
            ),
            "downstream_impact":  "Cannot trust enriched profile until entity confirmed",
            "suggested_action":   "Review evidence sources and confirm correct vendor",
            "created_at":         profile.enriched_at,
        })

    # BLOCKING_GAP_RESOLUTION
    if profile.gaps["blocking"]:
        _add_task({
            "vendor_id":         profile.vendor_id,
            "canonical_name":    profile.canonical_name,
            "triage_type":       "BLOCKING_GAP_RESOLUTION",
            "question":          f"Resolve missing core fields: {', '.join(profile.gaps['blocking'])}",
            "evidence":          "Core profile fields could not be extracted from available evidence",
            "downstream_impact": "Vendor cannot be classified, geo-assigned, or compliance-checked",
            "suggested_action":  "Provide source documents or manual input for missing fields",
            "created_at":        profile.enriched_at,
        })

    # LIFECYCLE_CONFIRMATION
    if "POSSIBLY_DEFUNCT" in pf or "ACQUISITION_UNRESOLVED" in pf:
        _add_task({
            "vendor_id":         profile.vendor_id,
            "canonical_name":    profile.canonical_name,
            "triage_type":       "LIFECYCLE_CONFIRMATION",
            "question":          "Confirm vendor lifecycle status",
            "evidence":          (
                "Vendor may be defunct or acquisition is unconfirmed — "
                "lifecycle signals require human verification"
            ),
            "downstream_impact": "Cannot determine vendor viability or correct parent entity",
            "suggested_action":  (
                "Verify operational status through direct contact or official registry"
            ),
            "created_at":        profile.enriched_at,
        })

    # UNRESOLVED_CONFLICT — one task per conflict
    for conflict in unresolved_conflicts:
        field = conflict.get("field", "unknown")
        tasks.append({
            "vendor_id":         profile.vendor_id,
            "canonical_name":    profile.canonical_name,
            "triage_type":       "UNRESOLVED_CONFLICT",
            "question":          f"Resolve conflict on field '{field}'",
            "evidence":          json.dumps(conflict),
            "downstream_impact": f"Field '{field}' cannot be trusted until resolved",
            "suggested_action":  "Check source registries and confirm authoritative value",
            "created_at":        profile.enriched_at,
        })

    # WRONG_ENTITY_CONFIRMATION (separate from ENTITY_DISAMBIGUATION)
    if "WRONG_ENTITY_RISK" in pf:
        _add_task({
            "vendor_id":         profile.vendor_id,
            "canonical_name":    profile.canonical_name,
            "triage_type":       "WRONG_ENTITY_CONFIRMATION",
            "question":          "Confirm this is the correct entity or reject and reprocess",
            "evidence":          "WRONG_ENTITY_RISK flag raised during evidence collection",
            "downstream_impact": "Profile may describe the wrong company",
            "suggested_action":  "Compare evidence sources with invoice/contract details",
            "created_at":        profile.enriched_at,
        })

    return tasks


# ---------------------------------------------------------------------------
# Workspace write wrappers
# ---------------------------------------------------------------------------

def _write_vendor_profile(
    profile: VendorProfile,
    programme_id: str,
    vendor_id: str,
    workspace_root: Path,
) -> Path:
    try:
        return write_vendor_profile(profile, programme_id, vendor_id, workspace_root)
    except Exception as exc:
        raise EnrichedProfileWriteError(
            f"Failed to write vendor_profile.md for {vendor_id}: {exc}"
        ) from exc


def _append_enrichment_ledger(
    programme_id: str,
    vendor_id: str,
    workspace_root: Path,
    profile: VendorProfile,
    pcs_before: float,
    pcs_after: float,
    now: str,
) -> None:
    append_enrichment_ledger_entry(
        programme_id=programme_id,
        vendor_id=vendor_id,
        workspace_root=workspace_root,
        profile_status=profile.profile_status,
        overall_confidence=profile.overall_confidence,
        depth_tier=profile.enrichment_metadata.get("depth_tier", "STANDARD"),
        sources_used=profile.enrichment_metadata.get("sources_used", []),
        flags=profile.flags,
        pcs_before=pcs_before,
        pcs_after=pcs_after,
        now=now,
    )


def _record_failed_enrichment(
    programme_id: str | None,
    vendor_id: str,
    workspace_root: Path,
    error_msg: str,
    now: str,
) -> None:
    """Append a FAILED_ENRICHMENT record to the vendor's change_log. Swallows all errors."""
    if not programme_id:
        return
    try:
        from cobalt.core.atomic_write import atomic_write
        from cobalt.core.file_system import _find_vendor_file, read_md

        file_path = _find_vendor_file(programme_id, vendor_id, workspace_root)
        if file_path is None:
            logger.warning("Could not find vendor file for FAILED_ENRICHMENT record: %s", vendor_id)
            return

        existing_data = read_md(file_path) or {}
        change_log = list(existing_data.get("change_log") or [])
        change_log.append({
            "event":          "ENRICHMENT_FAILED",
            "enriched_at":    now,
            "profile_status": "FAILED_ENRICHMENT",
            "error":          error_msg,
        })
        existing_data["change_log"] = change_log
        atomic_write(file_path, existing_data, vendor_id=vendor_id, programme_id=programme_id)
    except Exception:
        logger.warning("Could not record FAILED_ENRICHMENT ledger entry for %s", vendor_id)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_enriched_profile(
    extracted: ExtractedAttributes,
    relationship_result: RelationshipLifecycleResult,
    readiness: EnrichmentReadinessResult,
    entity_data: dict,
    pcs_before: float,
    workspace_root: Path | None = None,
    programme_id: str | None = None,
    vendor_id: str | None = None,
    now_iso: str | None = None,
) -> EnrichedProfileResult:
    """Reconcile upstream outputs, build VendorProfile, write workspace, sync DB.

    Returns EnrichedProfileResult even on failure — never raises for expected
    failures (write errors).  Only truly unexpected logic errors propagate.
    """
    now = now_iso or _now_utc_iso()
    workspace = workspace_root or Path(os.getenv("WORKSPACE_ROOT", "./workspace"))
    v_id = vendor_id or extracted.vendor_id

    try:
        # Step 1 — Source priority reconciliation
        reconciled_fields, unresolved_conflicts = _reconcile_conflicts(extracted, readiness)

        # Step 2 — Inference rules
        reconciled_fields = _apply_inference_rules(reconciled_fields)

        # Step 3 — Gap classification
        gaps = _classify_gaps(reconciled_fields)

        # Step 4 — Early flags for overall confidence computation
        early_flags = list(extracted.extraction_flags) + list(relationship_result.flags)

        # Step 5 — Overall confidence
        overall_confidence = _compute_overall_confidence(
            reconciled_fields, gaps, readiness, early_flags
        )

        # Step 6 — Profile status
        profile_status = _classify_profile_status(
            reconciled_fields, gaps, readiness, extracted, relationship_result
        )

        # Step 7 — Full flag generation
        flags = _generate_flags(
            reconciled_fields, gaps, readiness, extracted, relationship_result,
            unresolved_conflicts, relationship_result.brain_update_suggestions,
        )

        # Step 8 — Build VendorProfile
        profile = _build_vendor_profile(
            v_id, entity_data, reconciled_fields, gaps, flags,
            profile_status, overall_confidence, relationship_result,
            extracted, readiness, now,
        )

        # Step 9 — PCS update
        pcs_after = _compute_pcs(pcs_before, profile, relationship_result)
        profile.enrichment_metadata["pcs_before"] = round(pcs_before, 4)
        profile.enrichment_metadata["pcs_after"]  = round(pcs_after, 4)

        # Step 10 — Triage tasks
        triage_tasks = _generate_triage_tasks(
            profile, unresolved_conflicts, extracted, relationship_result, readiness
        )

        # Step 11 — Write vendor_profile.md (atomic). ENRICHMENT_COMPLETED entry is
        # appended to change_log inside write_vendor_profile — no separate ledger write needed.
        profile_path = _write_vendor_profile(profile, programme_id or "", v_id, workspace)

        return EnrichedProfileResult(
            vendor_id=v_id,
            profile_status=profile_status,
            overall_confidence=overall_confidence,
            profile_path=str(profile_path),
            pcs_before=pcs_before,
            pcs_after=pcs_after,
            flags=flags,
            triage_tasks=triage_tasks,
            brain_update_suggestions=relationship_result.brain_update_suggestions,
            enriched_at=now,
            error=None,
        )

    except EnrichedProfileWriteError as exc:
        _record_failed_enrichment(programme_id, v_id, workspace, str(exc), now)
        return EnrichedProfileResult(
            vendor_id=v_id,
            profile_status="FAILED_ENRICHMENT",
            overall_confidence="PROVISIONAL",
            profile_path=None,
            pcs_before=pcs_before,
            pcs_after=pcs_before,
            flags=["FAILED_ENRICHMENT"],
            triage_tasks=[{
                "vendor_id":          v_id,
                "triage_type":        "FAILED_ENRICHMENT",
                "question":           "Enrichment write failed — review and rerun",
                "evidence":           str(exc),
                "downstream_impact":  "Vendor cannot advance until resolved",
                "suggested_action":   "Inspect workspace and rerun enrichment",
                "created_at":         now,
            }],
            brain_update_suggestions=relationship_result.brain_update_suggestions,
            enriched_at=now,
            error=str(exc),
        )

    except Exception as exc:  # noqa: BLE001 — outer guard: workspace commit must never crash orchestrator
        logger.error(
            "Unexpected error in create_enriched_profile for %s: %s", v_id, exc, exc_info=True
        )
        return EnrichedProfileResult(
            vendor_id=v_id,
            profile_status="FAILED_ENRICHMENT",
            overall_confidence="PROVISIONAL",
            profile_path=None,
            pcs_before=pcs_before,
            pcs_after=pcs_before,
            flags=["FAILED_ENRICHMENT", "UNEXPECTED_ERROR"],
            triage_tasks=[{
                "vendor_id":         v_id,
                "triage_type":       "FAILED_ENRICHMENT",
                "question":          "Unexpected error during enrichment",
                "evidence":          str(exc),
                "downstream_impact": "Vendor cannot advance until resolved",
                "suggested_action":  "Review logs and contact engineering",
                "created_at":        now,
            }],
            brain_update_suggestions=getattr(relationship_result, "brain_update_suggestions", []),
            enriched_at=now,
            error=str(exc),
        )
