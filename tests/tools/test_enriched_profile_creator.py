"""Tests for enriched_profile_creator — Process 2 Tool 5."""

from __future__ import annotations

import pytest
import yaml

from cobalt.brain.loader import BrainData, KnownVendor
from cobalt.core.exceptions import EnrichedProfileWriteError
from cobalt.models.schemas.enrichment_schema import (
    BrainUpdateSuggestion,
    EnrichedProfileResult,
    EnrichmentReadinessResult,
    ExtractedAttributes,
    ExtractedField,
    KnownFacts,
    LifecycleSignal,
    RelationshipLifecycleResult,
    RelationshipMap,
)
from cobalt.tools.enriched_profile_creator import (
    _apply_inference_rules,
    _classify_gaps,
    _classify_profile_status,
    _compute_overall_confidence,
    _compute_pcs,
    _generate_flags,
    _generate_triage_tasks,
    _reconcile_conflicts,
    create_enriched_profile,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_NOW = "2026-01-01T00:00:00Z"


def _ef(value, confidence="HIGH", source="COMPANY_WEBSITE") -> ExtractedField:
    return ExtractedField(value=value, confidence=confidence, source=source)


def _make_readiness(
    confidence_floor: float = 0.80,
    depth_tier: str = "STANDARD",
    source_list: list[str] | None = None,
    flags: list[str] | None = None,
) -> EnrichmentReadinessResult:
    return EnrichmentReadinessResult(
        vendor_id="v-001",
        proceed=True,
        skip=False,
        skip_reason=None,
        depth_tier=depth_tier,
        source_list=source_list or ["web_search", "company_website"],
        query_count=5,
        known_facts=KnownFacts(confirmed=[], gaps=[], conflicts=[]),
        confidence_floor=confidence_floor,
        flags=flags or [],
    )


def _make_relationship(
    parent=None,
    lifecycle_signals=None,
    brain_suggestions=None,
    flags=None,
) -> RelationshipLifecycleResult:
    rm = RelationshipMap(
        vendor_id="v-001",
        parent_company=parent,
        subsidiaries=[],
        brands=[],
        former_names=[],
    )
    return RelationshipLifecycleResult(
        relationship_map=rm,
        lifecycle_signals=lifecycle_signals or [],
        brain_update_suggestions=brain_suggestions or [],
        flags=flags or [],
    )


def _make_extracted(
    fields: dict[str, ExtractedField] | None = None,
    conflicts: list[dict] | None = None,
    extraction_flags: list[str] | None = None,
) -> ExtractedAttributes:
    return ExtractedAttributes(
        vendor_id="v-001",
        fields=fields or {},
        conflicts=conflicts or [],
        extraction_flags=extraction_flags or [],
    )


def _core_fields_all_high() -> dict[str, ExtractedField]:
    """All 7 core fields at HIGH confidence — convenience builder."""
    return {
        "category":          _ef("IT_SOFTWARE"),
        "subcategory":       _ef("SAAS_PLATFORM"),
        "hq_country":        _ef("US"),
        "description":       _ef("Cloud platform for enterprises."),
        "company_status":    _ef("PRIVATE"),
        "vendor_type":       _ef("SAAS"),
        "company_size_band": _ef("MID_MARKET"),
    }


def _all_fields_enriched() -> dict[str, ExtractedField]:
    """All core + enrichment fields for a fully ENRICHED profile."""
    fields = _core_fields_all_high()
    # Other enrichment fields
    fields.update({
        "industry":           _ef("CROSS_INDUSTRY"),
        "primary_use_case":   _ef("Enterprise SaaS platform"),
        "hq_city":            _ef("San Francisco"),
        "founding_year":      _ef(2010),
        "employee_count_range": _ef("501-1000"),
        "revenue_range":      _ef("$50M-$100M"),
        "funding_stage":      _ef("PE_BACKED"),
        "website":            _ef("acme.com"),
        # composite list fields
        "_products_and_services": _ef([{"name": "CorePlatform", "type": "PRODUCT"}]),
        "_competitors":       _ef([{"name": "RivalCorp"}]),
        "_certifications":    _ef([{"name": "ISO 27001", "type": "SECURITY"}]),
        "_customer_segments": _ef([{"value": "Enterprise"}]),
        "_reputation_signals": _ef([{"signal_type": "POSITIVE", "description": "Award winner"}]),
    })
    return fields


def _entity(
    vendor_id: str = "v-001",
    canonical_name: str = "Acme Corp",
    parent_company: str | None = None,
) -> dict:
    return {
        "vendor_id":      vendor_id,
        "canonical_name": canonical_name,
        "aliases":        [],
        "parent_company": parent_company,
    }


# ---------------------------------------------------------------------------
# Test 1 — Non-conflict field passes through with source preserved
# ---------------------------------------------------------------------------

def test_reconcile_non_conflict_field_passes_through():
    """COMPANY_WEBSITE field at HIGH confidence passes through unchanged."""
    fields = {
        "hq_country": _ef("US", confidence="HIGH", source="COMPANY_WEBSITE"),
        "category":   _ef("IT_SOFTWARE", confidence="MEDIUM", source="LINKEDIN"),
    }
    extracted = _make_extracted(fields)
    readiness = _make_readiness()
    reconciled, unresolved = _reconcile_conflicts(extracted, readiness)

    assert reconciled["hq_country"].value == "US"
    assert reconciled["hq_country"].confidence == "HIGH"
    assert reconciled["hq_country"].source == "COMPANY_WEBSITE"
    assert reconciled["category"].source == "LINKEDIN"
    assert unresolved == []


# ---------------------------------------------------------------------------
# Test 2 — CONFLICT: REGISTRY wins over LINKEDIN, confidence downgraded to LOW
# ---------------------------------------------------------------------------

def test_reconcile_registry_beats_linkedin():
    """REGISTRY (priority 2) wins over LINKEDIN (priority 3); confidence → LOW."""
    fields = {
        "hq_country": _ef("UK", confidence="CONFLICT", source="REGISTRY"),
    }
    conflicts = [{
        "field": "hq_country",
        "source_a": {"source": "REGISTRY",  "value": "UK",  "retrieved_at": "2026-01-01"},
        "source_b": {"source": "LINKEDIN",  "value": "US",  "retrieved_at": "2026-01-01"},
    }]
    extracted = _make_extracted(fields, conflicts)
    readiness = _make_readiness()
    reconciled, unresolved = _reconcile_conflicts(extracted, readiness)

    assert reconciled["hq_country"].value == "UK"
    assert reconciled["hq_country"].confidence == "LOW"
    assert reconciled["hq_country"].source == "REGISTRY"
    assert unresolved == []


# ---------------------------------------------------------------------------
# Test 3 — Same priority: more recent retrieved_at wins
# ---------------------------------------------------------------------------

def test_reconcile_same_priority_recency_wins():
    """Two NEWS sources; the more recent retrieved_at is chosen."""
    fields = {
        "description": _ef("Old desc", confidence="CONFLICT", source="NEWS"),
    }
    conflicts = [{
        "field": "description",
        "source_a": {"source": "NEWS", "value": "Old desc",  "retrieved_at": "2025-12-01"},
        "source_b": {"source": "NEWS", "value": "New desc",  "retrieved_at": "2026-01-15"},
    }]
    extracted = _make_extracted(fields, conflicts)
    readiness = _make_readiness()
    reconciled, _ = _reconcile_conflicts(extracted, readiness)

    assert reconciled["description"].value == "New desc"
    assert reconciled["description"].confidence == "LOW"


# ---------------------------------------------------------------------------
# Test 4 — Two REGISTRY sources → UNRESOLVED_CONFLICT, field stays CONFLICT
# ---------------------------------------------------------------------------

def test_reconcile_registry_vs_registry_unresolved():
    """Two REGISTRY sources from different jurisdictions → UNRESOLVED_CONFLICT."""
    fields = {
        "hq_country": _ef("US", confidence="CONFLICT", source="REGISTRY"),
    }
    conflict_rec = {
        "field": "hq_country",
        "source_a": {"source": "REGISTRY", "value": "US", "jurisdiction": "US"},
        "source_b": {"source": "REGISTRY", "value": "UK", "jurisdiction": "UK"},
    }
    extracted = _make_extracted(fields, [conflict_rec])
    readiness = _make_readiness()
    reconciled, unresolved = _reconcile_conflicts(extracted, readiness)

    assert reconciled["hq_country"].confidence == "CONFLICT"
    assert len(unresolved) == 1
    assert unresolved[0]["field"] == "hq_country"


# ---------------------------------------------------------------------------
# Test 5 — Composite field passes through unchanged
# ---------------------------------------------------------------------------

def test_reconcile_composite_field_passes_through():
    """_competitors field with confidence=CONFLICT passes through as-is."""
    fields = {
        "_competitors": _ef([{"name": "RivalCorp"}], confidence="CONFLICT"),
    }
    extracted = _make_extracted(fields)
    readiness = _make_readiness()
    reconciled, unresolved = _reconcile_conflicts(extracted, readiness)

    assert reconciled["_competitors"].confidence == "CONFLICT"
    assert reconciled["_competitors"].value == [{"name": "RivalCorp"}]
    assert unresolved == []


# ---------------------------------------------------------------------------
# Test 6 — Inference: employee_count_range → company_size_band
# ---------------------------------------------------------------------------

def test_inference_employee_count_to_size_band():
    """501-1000 employees → company_size_band inferred as MID_MARKET."""
    fields = {
        "employee_count_range": _ef("501-1000", confidence="MEDIUM"),
        # company_size_band absent
    }
    result = _apply_inference_rules(fields)

    assert result["company_size_band"].value == "MID_MARKET"
    assert result["company_size_band"].confidence == "INFERRED"
    assert result["company_size_band"].source == "INFERRED"


# ---------------------------------------------------------------------------
# Test 7 — Inference: hq_city=Tokyo → hq_country=JP
# ---------------------------------------------------------------------------

def test_inference_unambiguous_city_to_country():
    """Tokyo unambiguously maps to JP."""
    fields = {
        "hq_city": _ef("Tokyo", confidence="HIGH"),
        # hq_country absent
    }
    result = _apply_inference_rules(fields)

    assert result["hq_country"].value == "JP"
    assert result["hq_country"].confidence == "INFERRED"


# ---------------------------------------------------------------------------
# Test 8 — No inference for ambiguous city (London)
# ---------------------------------------------------------------------------

def test_inference_ambiguous_city_no_inference():
    """London is ambiguous — hq_country remains absent."""
    fields = {
        "hq_city": _ef("London", confidence="HIGH"),
    }
    result = _apply_inference_rules(fields)

    assert "hq_country" not in result


# ---------------------------------------------------------------------------
# Test 9 — category missing → in gaps.blocking
# ---------------------------------------------------------------------------

def test_gap_classification_blocking_category():
    """Missing category → in gaps['blocking']."""
    fields = {
        "hq_country":     _ef("US"),
        "description":    _ef("A cloud platform."),
        "company_status": _ef("PRIVATE"),
        # category absent
    }
    gaps = _classify_gaps(fields)
    assert "category" in gaps["blocking"]
    assert "category" not in gaps["enrichment"]


# ---------------------------------------------------------------------------
# Test 10 — revenue_range missing → in gaps.enrichment
# ---------------------------------------------------------------------------

def test_gap_classification_enrichment_revenue():
    """Missing revenue_range → enrichment gap only, not blocking."""
    fields = _core_fields_all_high()  # all blocking fields present
    gaps = _classify_gaps(fields)

    assert "revenue_range" in gaps["enrichment"]
    assert "revenue_range" not in gaps["blocking"]


# ---------------------------------------------------------------------------
# Test 11 — All core blocking fields present → gaps.blocking empty
# ---------------------------------------------------------------------------

def test_gap_classification_no_blocking_gaps():
    """All 4 blocking fields present → gaps['blocking'] is empty."""
    fields = {
        "category":       _ef("IT_SOFTWARE"),
        "hq_country":     _ef("US"),
        "description":    _ef("A platform."),
        "company_status": _ef("PRIVATE"),
    }
    gaps = _classify_gaps(fields)
    assert gaps["blocking"] == []


# ---------------------------------------------------------------------------
# Test 12 — All 7 core fields HIGH → HIGH confidence + ENRICHED status
# ---------------------------------------------------------------------------

def test_overall_confidence_and_status_all_high():
    """All 7 core + all enrichment fields at HIGH → HIGH confidence, ENRICHED status."""
    fields = _all_fields_enriched()
    extracted   = _make_extracted(fields)
    readiness   = _make_readiness()
    relationship = _make_relationship()
    gaps         = _classify_gaps(fields)

    conf   = _compute_overall_confidence(fields, gaps, readiness, [])
    status = _classify_profile_status(fields, gaps, readiness, extracted, relationship)

    assert conf == "HIGH"
    assert status == "ENRICHED"


# ---------------------------------------------------------------------------
# Test 13 — One blocking field missing → PROVISIONAL
# ---------------------------------------------------------------------------

def test_status_blocking_gap_gives_provisional():
    """category missing → blocking gap → profile_status=PROVISIONAL."""
    fields = {
        "hq_country":        _ef("US"),
        "description":       _ef("A platform."),
        "company_status":    _ef("PRIVATE"),
        "subcategory":       _ef("SAAS"),
        "vendor_type":       _ef("SAAS"),
        "company_size_band": _ef("SMB"),
        # category absent → blocking gap
    }
    extracted    = _make_extracted(fields)
    readiness    = _make_readiness()
    relationship = _make_relationship()
    gaps         = _classify_gaps(fields)

    status = _classify_profile_status(fields, gaps, readiness, extracted, relationship)
    assert status == "PROVISIONAL"


# ---------------------------------------------------------------------------
# Test 14 — 6 fields HIGH, 1 LOW → MEDIUM confidence, PARTIALLY_ENRICHED
# ---------------------------------------------------------------------------

def test_overall_confidence_one_low_gives_medium():
    """One core field LOW → overall_confidence=MEDIUM, status=PARTIALLY_ENRICHED."""
    fields = _all_fields_enriched()
    fields["company_size_band"] = _ef("MID_MARKET", confidence="LOW")

    extracted    = _make_extracted(fields)
    readiness    = _make_readiness()
    relationship = _make_relationship()
    gaps         = _classify_gaps(fields)
    early_flags  = []

    conf   = _compute_overall_confidence(fields, gaps, readiness, early_flags)
    status = _classify_profile_status(fields, gaps, readiness, extracted, relationship)

    assert conf == "MEDIUM"
    assert status == "PARTIALLY_ENRICHED"


# ---------------------------------------------------------------------------
# Test 15 — WRONG_ENTITY_RISK → PROVISIONAL confidence and status
# ---------------------------------------------------------------------------

def test_wrong_entity_risk_gives_provisional():
    """WRONG_ENTITY_RISK in extraction flags → PROVISIONAL for both conf and status."""
    fields       = _all_fields_enriched()
    extracted    = _make_extracted(fields, extraction_flags=["WRONG_ENTITY_RISK"])
    readiness    = _make_readiness()
    relationship = _make_relationship()
    gaps         = _classify_gaps(fields)

    conf   = _compute_overall_confidence(fields, gaps, readiness, ["WRONG_ENTITY_RISK"])
    status = _classify_profile_status(fields, gaps, readiness, extracted, relationship)

    assert conf == "PROVISIONAL"
    assert status == "PROVISIONAL"


# ---------------------------------------------------------------------------
# Test 16 — confidence_floor < 0.60 → PROVISIONAL
# ---------------------------------------------------------------------------

def test_low_confidence_floor_gives_provisional():
    """readiness.confidence_floor=0.50 → overall_confidence=PROVISIONAL."""
    fields    = _all_fields_enriched()
    readiness = _make_readiness(confidence_floor=0.50)
    gaps      = _classify_gaps(fields)

    conf = _compute_overall_confidence(fields, gaps, readiness, [])
    assert conf == "PROVISIONAL"


# ---------------------------------------------------------------------------
# Test 17 — No core fields populated → FAILED_ENRICHMENT
# ---------------------------------------------------------------------------

def test_no_core_fields_gives_failed_enrichment():
    """Empty fields → no core fields → profile_status=FAILED_ENRICHMENT."""
    fields       = {}
    extracted    = _make_extracted(fields)
    readiness    = _make_readiness()
    relationship = _make_relationship()
    gaps         = _classify_gaps(fields)

    status = _classify_profile_status(fields, gaps, readiness, extracted, relationship)
    assert status == "FAILED_ENRICHMENT"


# ---------------------------------------------------------------------------
# Test 18 — description.confidence=CONFLICT → CONFLICTING_DESCRIPTION flag
# ---------------------------------------------------------------------------

def test_flag_conflicting_description():
    """description with confidence=CONFLICT → CONFLICTING_DESCRIPTION in flags."""
    fields = _core_fields_all_high()
    fields["description"] = _ef("Some desc", confidence="CONFLICT")
    extracted    = _make_extracted(fields)
    readiness    = _make_readiness()
    relationship = _make_relationship()
    gaps         = _classify_gaps(fields)

    flags = _generate_flags(fields, gaps, readiness, extracted, relationship, [], [])
    assert "CONFLICTING_DESCRIPTION" in flags


# ---------------------------------------------------------------------------
# Test 19 — brain_update_suggestions non-empty → BRAIN_UPDATE_PENDING flag
# ---------------------------------------------------------------------------

def test_flag_brain_update_pending():
    """Non-empty brain_update_suggestions → BRAIN_UPDATE_PENDING flag."""
    fields   = _core_fields_all_high()
    sug = BrainUpdateSuggestion(
        update_type="REBRAND_MAP", from_="OldCo", to="NewCo",
        confidence="HIGH", source_url="https://example.com",
        suggested_by_vendor_id="v-001",
    )
    extracted    = _make_extracted(fields)
    readiness    = _make_readiness()
    relationship = _make_relationship(brain_suggestions=[sug])
    gaps         = _classify_gaps(fields)

    flags = _generate_flags(fields, gaps, readiness, extracted, relationship, [], [sug])
    assert "BRAIN_UPDATE_PENDING" in flags


# ---------------------------------------------------------------------------
# Test 20 — All fields from one source → SINGLE_SOURCE_ONLY
# ---------------------------------------------------------------------------

def test_flag_single_source_only():
    """All populated fields share source=COMPANY_WEBSITE → SINGLE_SOURCE_ONLY."""
    fields = {
        "category":    _ef("IT_SOFTWARE", source="COMPANY_WEBSITE"),
        "hq_country":  _ef("US",          source="COMPANY_WEBSITE"),
        "description": _ef("A platform.", source="COMPANY_WEBSITE"),
    }
    extracted    = _make_extracted(fields)
    readiness    = _make_readiness()
    relationship = _make_relationship()
    gaps         = _classify_gaps(fields)

    flags = _generate_flags(fields, gaps, readiness, extracted, relationship, [], [])
    assert "SINGLE_SOURCE_ONLY" in flags


# ---------------------------------------------------------------------------
# Test 21 — WRONG_ENTITY_RISK → ENTITY_DISAMBIGUATION + WRONG_ENTITY_CONFIRMATION
# ---------------------------------------------------------------------------

def test_triage_wrong_entity_risk():
    """WRONG_ENTITY_RISK → both ENTITY_DISAMBIGUATION and WRONG_ENTITY_CONFIRMATION tasks."""
    result = create_enriched_profile(
        extracted=_make_extracted(
            _core_fields_all_high(),
            extraction_flags=["WRONG_ENTITY_RISK"],
        ),
        relationship_result=_make_relationship(),
        readiness=_make_readiness(),
        entity_data=_entity(),
        pcs_before=0.10,
        workspace_root=None,
        programme_id="p-001",
        vendor_id="v-001",
        now_iso=_NOW,
    )
    types = {t["triage_type"] for t in result.triage_tasks}
    assert "ENTITY_DISAMBIGUATION" in types
    assert "WRONG_ENTITY_CONFIRMATION" in types


# ---------------------------------------------------------------------------
# Test 22 — category missing → BLOCKING_GAP_RESOLUTION triage task
# ---------------------------------------------------------------------------

def test_triage_blocking_gap_resolution(tmp_path):
    """Missing category field → BLOCKING_GAP_RESOLUTION task referencing 'category'."""
    fields = {
        "hq_country":        _ef("US"),
        "description":       _ef("A platform."),
        "company_status":    _ef("PRIVATE"),
        "subcategory":       _ef("SAAS"),
        "vendor_type":       _ef("SAAS"),
        "company_size_band": _ef("SMB"),
    }
    result = create_enriched_profile(
        extracted=_make_extracted(fields),
        relationship_result=_make_relationship(),
        readiness=_make_readiness(),
        entity_data=_entity(),
        pcs_before=0.10,
        workspace_root=tmp_path,
        programme_id="p-001",
        vendor_id="v-001",
        now_iso=_NOW,
    )
    gap_tasks = [t for t in result.triage_tasks if t["triage_type"] == "BLOCKING_GAP_RESOLUTION"]
    assert len(gap_tasks) == 1
    assert "category" in gap_tasks[0]["question"]


# ---------------------------------------------------------------------------
# Test 23 — POSSIBLY_DEFUNCT → LIFECYCLE_CONFIRMATION triage task
# ---------------------------------------------------------------------------

def test_triage_lifecycle_confirmation(tmp_path):
    """POSSIBLY_DEFUNCT flag in relationship flags → LIFECYCLE_CONFIRMATION task."""
    sig = LifecycleSignal(
        signal_type="POSSIBLY_DEFUNCT", from_=None, to="Acme Corp",
        date=None, confidence="MEDIUM", source="multiple",
    )
    result = create_enriched_profile(
        extracted=_make_extracted(_core_fields_all_high()),
        relationship_result=_make_relationship(
            lifecycle_signals=[sig],
            flags=["POSSIBLY_DEFUNCT", "LIFECYCLE_EVENT_DETECTED"],
        ),
        readiness=_make_readiness(),
        entity_data=_entity(),
        pcs_before=0.10,
        workspace_root=tmp_path,
        programme_id="p-001",
        vendor_id="v-001",
        now_iso=_NOW,
    )
    types = {t["triage_type"] for t in result.triage_tasks}
    assert "LIFECYCLE_CONFIRMATION" in types


# ---------------------------------------------------------------------------
# Test 24 — UNRESOLVED_CONFLICT → one triage task with UNRESOLVED_CONFLICT type
# ---------------------------------------------------------------------------

def test_triage_unresolved_conflict(tmp_path):
    """UNRESOLVED_CONFLICT on hq_country → triage task with JSON evidence."""
    fields = _core_fields_all_high()
    fields["hq_country"] = _ef("US", confidence="CONFLICT")
    conflict_rec = {
        "field": "hq_country",
        "source_a": {"source": "REGISTRY", "value": "US", "jurisdiction": "US"},
        "source_b": {"source": "REGISTRY", "value": "UK", "jurisdiction": "UK"},
    }
    extracted = _make_extracted(fields, conflicts=[conflict_rec])
    result = create_enriched_profile(
        extracted=extracted,
        relationship_result=_make_relationship(),
        readiness=_make_readiness(),
        entity_data=_entity(),
        pcs_before=0.10,
        workspace_root=tmp_path,
        programme_id="p-001",
        vendor_id="v-001",
        now_iso=_NOW,
    )
    conflict_tasks = [t for t in result.triage_tasks if t["triage_type"] == "UNRESOLVED_CONFLICT"]
    assert len(conflict_tasks) == 1
    assert "hq_country" in conflict_tasks[0]["question"]
    # evidence should be a JSON string of the conflict record
    import json
    ev = json.loads(conflict_tasks[0]["evidence"])
    assert ev["field"] == "hq_country"


# ---------------------------------------------------------------------------
# Test 25 — Happy path: single vendor file written with valid YAML frontmatter
# ---------------------------------------------------------------------------

def test_workspace_write_happy_path(tmp_path):
    """Single vendor *.md file is written with enrichment data."""
    result = create_enriched_profile(
        extracted=_make_extracted(_all_fields_enriched()),
        relationship_result=_make_relationship(),
        readiness=_make_readiness(),
        entity_data=_entity(),
        pcs_before=0.20,
        workspace_root=tmp_path,
        programme_id="p-001",
        vendor_id="v-001",
        now_iso=_NOW,
    )

    assert result.profile_status in {"ENRICHED", "PARTIALLY_ENRICHED"}
    assert result.profile_path is not None

    # Single-file architecture: find the *.md in the vendor directory
    vendor_dir = tmp_path / "p-001" / "v-001"
    md_files = list(vendor_dir.glob("*.md"))
    assert md_files, f"No vendor *.md file found in {vendor_dir}"
    profile_file = md_files[0]

    raw = profile_file.read_text(encoding="utf-8")
    parts = raw.split("---\n", 2)
    assert len(parts) >= 3, "Expected YAML frontmatter delimited by ---"
    fm = yaml.safe_load(parts[1])
    assert fm["vendor_id"] == "v-001"
    assert fm["status"] in {"ENRICHED", "PARTIALLY_ENRICHED"}


# ---------------------------------------------------------------------------
# Test 26 — Enrichment appends change_log entry in vendor file
# ---------------------------------------------------------------------------

def test_enrichment_ledger_appended_to_change_log(tmp_path):
    """After create_enriched_profile, the vendor file's change_log has an enrichment entry."""
    result = create_enriched_profile(
        extracted=_make_extracted(_core_fields_all_high()),
        relationship_result=_make_relationship(),
        readiness=_make_readiness(),
        entity_data=_entity(),
        pcs_before=0.15,
        workspace_root=tmp_path,
        programme_id="p-001",
        vendor_id="v-001",
        now_iso=_NOW,
    )

    assert result.error is None

    # Find single vendor file
    vendor_dir = tmp_path / "p-001" / "v-001"
    md_files = list(vendor_dir.glob("*.md"))
    assert md_files, "No vendor file found"
    raw = md_files[0].read_text(encoding="utf-8")
    # change_log with pcs data should be present
    assert "pcs_before" in raw or "ENRICHMENT" in raw


# ---------------------------------------------------------------------------
# Test 27 — atomic_write failure → FAILED_ENRICHMENT result
# ---------------------------------------------------------------------------

def test_workspace_write_failure_returns_failed_enrichment(tmp_path, monkeypatch):
    """If _write_vendor_profile raises EnrichedProfileWriteError, result is FAILED_ENRICHMENT."""
    import cobalt.tools.enriched_profile_creator as epc

    def _raise(*args, **kwargs):
        raise EnrichedProfileWriteError("disk full")

    monkeypatch.setattr(epc, "_write_vendor_profile", _raise)

    result = create_enriched_profile(
        extracted=_make_extracted(_all_fields_enriched()),
        relationship_result=_make_relationship(),
        readiness=_make_readiness(),
        entity_data=_entity(),
        pcs_before=0.20,
        workspace_root=tmp_path,
        programme_id="p-001",
        vendor_id="v-001",
        now_iso=_NOW,
    )

    # Verify FAILED_ENRICHMENT result
    assert result.profile_status == "FAILED_ENRICHMENT"
    assert result.profile_path is None
    assert result.error is not None
    assert "disk full" in result.error
    assert result.pcs_after == result.pcs_before   # no PCS change on failure

    # Verify the FAILED_ENRICHMENT triage task
    fail_tasks = [t for t in result.triage_tasks if t["triage_type"] == "FAILED_ENRICHMENT"]
    assert len(fail_tasks) == 1


# ---------------------------------------------------------------------------
# Test 28 — PCS update: all 7 core HIGH + lifecycle signal + parent set
# ---------------------------------------------------------------------------

def test_pcs_update_full_weights(tmp_path):
    """pcs_before=0.20, all 7 core fields HIGH + lifecycle + parent → correct pcs_after."""
    from cobalt.tools.enriched_profile_creator import PCS_WEIGHTS

    sig = LifecycleSignal(
        signal_type="REBRANDED", from_="OldCo", to="Acme Corp",
        date=None, confidence="HIGH", source="brain",
    )
    parent = {
        "name": "BigParent Corp", "vendor_id": None,
        "relationship_type": "WHOLLY_OWNED", "confidence": "HIGH",
    }
    result = create_enriched_profile(
        extracted=_make_extracted(_all_fields_enriched()),
        relationship_result=_make_relationship(
            parent=parent,
            lifecycle_signals=[sig],
            flags=["LIFECYCLE_EVENT_DETECTED"],
        ),
        readiness=_make_readiness(),
        entity_data=_entity(),
        pcs_before=0.20,
        workspace_root=tmp_path,
        programme_id="p-001",
        vendor_id="v-001",
        now_iso=_NOW,
    )

    # Expected delta: 6 core fields + parent_resolved + lifecycle + certifications + reputation
    # category(0.10) + hq_country(0.06) + description(0.06) + company_status(0.05)
    # + vendor_type(0.05) + company_size_band(0.04) + parent_resolved(0.04)
    # + lifecycle_evaluated(0.03) + certifications(0.02) + reputation_evaluated(0.02)
    # = 0.47 → exactly at cap
    expected_delta = min(
        PCS_WEIGHTS["category"] + PCS_WEIGHTS["hq_country"] + PCS_WEIGHTS["description"]
        + PCS_WEIGHTS["company_status"] + PCS_WEIGHTS["vendor_type"]
        + PCS_WEIGHTS["company_size_band"] + PCS_WEIGHTS["parent_resolved"]
        + PCS_WEIGHTS["lifecycle_evaluated"] + PCS_WEIGHTS["certifications"]
        + PCS_WEIGHTS["reputation_evaluated"],
        0.47,
    )
    expected_pcs_after = min(1.0, 0.20 + expected_delta)

    assert result.pcs_after == pytest.approx(expected_pcs_after, abs=1e-6)
    assert result.pcs_after <= 1.0
    assert result.pcs_after > result.pcs_before
