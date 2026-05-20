"""Tests for evidence_validator — Tool 1 P4."""

from __future__ import annotations

import pytest

from cobalt.models.schemas.an_schema import (
    HistoricalEvidenceState,
    ValidatedEvidenceAssembly,
)
from cobalt.models.schemas.rs_schema import (
    ContractTerms,
    DocumentIntelligenceResult,
    RawSpendRecord,
    StructuredDataBundle,
)
from cobalt.tools.evidence_validator import (
    EXPECTED_FIELDS,
    TRUST_WEIGHTS,
    _compute_quality_score,
    _freshness_threshold_for,
    validate_evidence,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VENDOR_ID = "v-test-001"
PROG_ID = "prog-test-001"


def _empty_vendor_file() -> dict:
    return {}


def _vendor_file(**kwargs) -> dict:
    return kwargs


def _make_doc_intelligence(
    doc_id: str = "contract_001.pdf",
    effective_date: str | None = "2024-01-01",
    expiry_date: str | None = "2025-01-01",
    auto_renews: bool | None = True,
    notice_period_days: int | None = 30,
    total_value: float | None = 100_000.0,
    sla_summary: str | None = "99.9% uptime",
) -> DocumentIntelligenceResult:
    contract = ContractTerms(
        document_id=doc_id,
        document_type="CONTRACT",
        effective_date=effective_date,
        expiry_date=expiry_date,
        auto_renews=auto_renews,
        notice_period_days=notice_period_days,
        total_value=total_value,
        currency="USD",
        payment_terms_days=30,
        governing_law="England",
        sla_summary=sla_summary,
    )
    return DocumentIntelligenceResult(
        vendor_id=VENDOR_ID,
        documents_processed=1,
        documents_skipped=0,
        extracted_contracts=[contract],
        extraction_warnings=[],
    )


def _make_structured_bundle(
    amount_usd: float = 50_000.0,
    trust_level: str = "SYSTEM_EXPORT",
) -> StructuredDataBundle:
    record = RawSpendRecord(
        source_id="erp_001",
        arrival_mode="CONNECTOR",
        trust_level=trust_level,
        period_start="2024-01-01",
        period_end="2024-12-31",
        amount_raw="50000",
        currency_raw="USD",
        amount_usd=amount_usd,
        category_raw="Software",
        cost_centre=None,
        po_number="PO-001",
        invoice_ref="INV-001",
        matched_vendor_id=VENDOR_ID,
        match_confidence="HIGH",
    )
    return StructuredDataBundle(
        vendor_id=VENDOR_ID,
        programme_id=PROG_ID,
        collected_at="2024-06-01T00:00:00+00:00",
        arrival_modes_used=["CONNECTOR"],
        raw_spend_records=[record],
        connector_metadata={},
        upload_metadata={},
        checkin_metadata={},
        collection_warnings=[],
    )


def _make_signal_bundle(*signals: dict) -> dict:
    return {"signals": list(signals)}


# ---------------------------------------------------------------------------
# _compute_quality_score
# ---------------------------------------------------------------------------

class TestComputeQualityScore:
    def test_official_current_no_conflict(self):
        score = _compute_quality_score("OFFICIAL", "CURRENT", False)
        assert score == pytest.approx(1.0 * 1.0 * 1.0)

    def test_user_submitted_current_no_conflict(self):
        score = _compute_quality_score("USER_SUBMITTED", "CURRENT", False)
        assert score == pytest.approx(0.65)

    def test_official_stale_no_conflict(self):
        score = _compute_quality_score("OFFICIAL", "STALE", False)
        assert score == pytest.approx(1.0 * 0.5 * 1.0)

    def test_official_current_conflict(self):
        score = _compute_quality_score("OFFICIAL", "CURRENT", True)
        assert score == pytest.approx(1.0 * 1.0 * 0.7)

    def test_missing_freshness_gives_zero(self):
        score = _compute_quality_score("OFFICIAL", "MISSING", False)
        assert score == pytest.approx(0.0)

    def test_unknown_trust_gives_zero(self):
        score = _compute_quality_score("UNKNOWN_TRUST", "CURRENT", False)
        assert score == pytest.approx(0.0)

    def test_clamped_at_one(self):
        score = _compute_quality_score("OFFICIAL", "CURRENT", False)
        assert score <= 1.0

    def test_clamped_at_zero(self):
        score = _compute_quality_score("USER_SUBMITTED", "MISSING", True)
        assert score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _freshness_threshold_for
# ---------------------------------------------------------------------------

class TestFreshnessThreshold:
    def test_signal_extraction_type(self):
        assert _freshness_threshold_for("SIGNAL", "") == 30

    def test_check_in_source(self):
        assert _freshness_threshold_for("COMPUTED", "checkin_2024.json") == 30

    def test_invoice_source(self):
        assert _freshness_threshold_for("COMPUTED", "INVOICE_Q1.xlsx") == 90

    def test_qbr_source(self):
        assert _freshness_threshold_for("COMPUTED", "QBR_2024.pdf") == 90

    def test_sla_source(self):
        assert _freshness_threshold_for("AUTO_EXTRACTED", "SLA_Exhibit_A.pdf") == 45

    def test_compliance_cert_source(self):
        assert _freshness_threshold_for("AUTO_EXTRACTED", "compliance_cert.pdf") == 180

    def test_msa_source(self):
        assert _freshness_threshold_for("AUTO_EXTRACTED", "MSA_2024.pdf") == 365

    def test_sow_source(self):
        assert _freshness_threshold_for("AUTO_EXTRACTED", "SOW_Annex.pdf") == 365

    def test_contract_extraction_type(self):
        assert _freshness_threshold_for("AUTO_EXTRACTED", "contract_main.pdf") == 365

    def test_spend_source(self):
        assert _freshness_threshold_for("COMPUTED", "SPEND_history.csv") == 90

    def test_default_fallback(self):
        assert _freshness_threshold_for("COMPUTED", "mystery_file.csv") == 90


# ---------------------------------------------------------------------------
# validate_evidence — source None guards
# ---------------------------------------------------------------------------

class TestValidateEvidenceNullSources:
    def test_doc_intelligence_none_no_crash(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        assert isinstance(result, ValidatedEvidenceAssembly)

    def test_structured_bundle_none_no_crash(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=_make_doc_intelligence(),
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        assert isinstance(result, ValidatedEvidenceAssembly)

    def test_signal_bundle_none_no_crash(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=_make_structured_bundle(),
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        assert isinstance(result, ValidatedEvidenceAssembly)

    def test_historical_state_none_all_facts_current(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=_make_doc_intelligence(),
            structured_bundle=_make_structured_bundle(),
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        non_missing = [f for f in result.facts if f.freshness_status != "MISSING"]
        assert all(f.freshness_status == "CURRENT" for f in non_missing)

    def test_all_sources_none_empty_vendor_file(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        # All 11 EXPECTED_FIELDS must appear as MISSING
        missing_fields = {f.field_name for f in result.facts if f.freshness_status == "MISSING"}
        assert EXPECTED_FIELDS == missing_fields
        assert result.missing_count == len(EXPECTED_FIELDS)
        assert result.completeness_pct == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# validate_evidence — completeness
# ---------------------------------------------------------------------------

class TestValidateEvidenceCompleteness:
    def test_completeness_zero_when_all_missing(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        assert result.completeness_pct == pytest.approx(0.0)

    def test_completeness_one_when_all_present_and_current(self):
        # Fill every EXPECTED_FIELD via vendor_file + doc_intelligence + structured_bundle
        vf = _vendor_file(
            relationship_type="STRATEGIC",
            dependency_tier="HIGH",
            primary_owner="alice@example.com",
            renewal_date="2025-06-01",
            auto_renew=True,
        )
        doc = _make_doc_intelligence(
            effective_date="2024-01-01",
            expiry_date="2025-01-01",
            auto_renews=True,
            notice_period_days=30,
            total_value=100_000.0,
            sla_summary="99.9% uptime",
        )
        bundle = _make_structured_bundle(amount_usd=50_000.0)
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=bundle,
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        assert result.completeness_pct == pytest.approx(1.0)

    def test_missing_count_matches_missing_facts(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        explicit_missing = sum(1 for f in result.facts if f.freshness_status == "MISSING")
        assert result.missing_count == explicit_missing


# ---------------------------------------------------------------------------
# validate_evidence — quality scores from sources
# ---------------------------------------------------------------------------

class TestValidateEvidenceQualityScores:
    def _get_fact(self, result, field_name, source_file=None):
        facts = [f for f in result.facts if f.field_name == field_name]
        if source_file:
            facts = [f for f in facts if f.source_file == source_file]
        assert facts, f"No fact found for field={field_name}"
        return facts[0]

    def test_official_contract_fact_quality_score_one(self):
        doc = _make_doc_intelligence(effective_date="2024-01-01")
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        fact = self._get_fact(result, "contract_term_start")
        assert fact.trust_level == "OFFICIAL"
        assert fact.freshness_status == "CURRENT"
        assert fact.conflict_flag is False
        assert fact.quality_score == pytest.approx(1.0)

    def test_system_export_vendor_file_quality(self):
        vf = _vendor_file(relationship_type="STRATEGIC")
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        fact = self._get_fact(result, "relationship_type", "vendor_file")
        assert fact.trust_level == "SYSTEM_EXPORT"
        assert fact.quality_score == pytest.approx(0.85)

    def test_user_submitted_spend_record_quality(self):
        bundle = _make_structured_bundle(amount_usd=20_000.0, trust_level="USER_SUBMITTED")
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=bundle,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        fact = self._get_fact(result, "spend_total_ttm_usd")
        assert fact.trust_level == "USER_SUBMITTED"
        assert fact.quality_score == pytest.approx(0.65)

    def test_missing_fact_quality_score_zero(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        missing_facts = [f for f in result.facts if f.freshness_status == "MISSING"]
        assert all(f.quality_score == pytest.approx(0.0) for f in missing_facts)


# ---------------------------------------------------------------------------
# validate_evidence — conflict detection
# ---------------------------------------------------------------------------

class TestValidateEvidenceConflicts:
    def test_conflict_detected_when_two_sources_differ(self):
        # auto_renew from doc_intelligence (OFFICIAL) and vendor_file (SYSTEM_EXPORT) differ
        doc = _make_doc_intelligence(auto_renews=True)
        vf = _vendor_file(auto_renew=False)  # different value
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        auto_renew_facts = [f for f in result.facts if f.field_name == "auto_renew"]
        assert len(auto_renew_facts) >= 2
        conflicted = [f for f in auto_renew_facts if f.conflict_flag]
        assert len(conflicted) == len(auto_renew_facts), "All auto_renew facts should be flagged"
        assert result.conflict_count >= 1

    def test_conflict_values_contains_both_values(self):
        doc = _make_doc_intelligence(auto_renews=True)
        vf = _vendor_file(auto_renew=False)
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        auto_renew_facts = [f for f in result.facts if f.field_name == "auto_renew" and f.conflict_flag]
        assert auto_renew_facts
        cv_strings = [str(v) for v in auto_renew_facts[0].conflict_values]
        assert "True" in cv_strings
        assert "False" in cv_strings

    def test_conflict_reduces_quality_score(self):
        doc = _make_doc_intelligence(auto_renews=True)
        vf = _vendor_file(auto_renew=False)
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        official_fact = next(
            f for f in result.facts
            if f.field_name == "auto_renew" and f.trust_level == "OFFICIAL"
        )
        # OFFICIAL + CURRENT + conflict = 1.0 * 1.0 * 0.7 = 0.70
        assert official_fact.quality_score == pytest.approx(0.70)

    def test_no_conflict_when_same_source(self):
        # Only one source provides auto_renew — no conflict possible
        doc = _make_doc_intelligence(auto_renews=True)
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        auto_renew_facts = [f for f in result.facts if f.field_name == "auto_renew"]
        assert all(not f.conflict_flag for f in auto_renew_facts)

    def test_no_conflict_when_values_agree(self):
        doc = _make_doc_intelligence(auto_renews=True)
        vf = _vendor_file(auto_renew=True)  # same value
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        auto_renew_facts = [f for f in result.facts if f.field_name == "auto_renew"]
        assert all(not f.conflict_flag for f in auto_renew_facts)


# ---------------------------------------------------------------------------
# validate_evidence — staleness via historical_state
# ---------------------------------------------------------------------------

class TestValidateEvidenceStaleness:
    def test_stale_fact_gets_low_confidence(self):
        # Make a historical state where contract_term_start was validated 400 days ago
        # (threshold for CONTRACT is 365 days → this is stale)
        old_date = "2023-01-01T00:00:00+00:00"  # well over 365 days ago
        historical = HistoricalEvidenceState(
            vendor_id=VENDOR_ID,
            prior_assembly_at=old_date,
            fact_snapshot={
                "contract_term_start": {
                    "value": "2022-01-01",
                    "quality_score": 1.0,
                    "validated_at": old_date,
                }
            },
        )
        doc = _make_doc_intelligence(effective_date="2022-01-01")
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=historical,
        )
        start_fact = next(f for f in result.facts if f.field_name == "contract_term_start")
        assert start_fact.freshness_status == "STALE"
        assert start_fact.confidence == "LOW"
        # OFFICIAL + STALE + no conflict = 1.0 * 0.5 * 1.0 = 0.50
        assert start_fact.quality_score == pytest.approx(0.50)

    def test_stale_fact_in_stale_count(self):
        old_date = "2023-01-01T00:00:00+00:00"
        historical = HistoricalEvidenceState(
            vendor_id=VENDOR_ID,
            prior_assembly_at=old_date,
            fact_snapshot={
                "contract_term_start": {"value": "x", "quality_score": 1.0, "validated_at": old_date}
            },
        )
        doc = _make_doc_intelligence(effective_date="x")
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=historical,
        )
        assert result.stale_count >= 1

    def test_field_not_in_snapshot_is_current(self):
        # historical_state exists but does not mention contract_term_end
        historical = HistoricalEvidenceState(
            vendor_id=VENDOR_ID,
            prior_assembly_at="2025-01-01T00:00:00+00:00",
            fact_snapshot={},  # empty snapshot
        )
        doc = _make_doc_intelligence(expiry_date="2026-01-01")
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=historical,
        )
        end_fact = next(f for f in result.facts if f.field_name == "contract_term_end")
        assert end_fact.freshness_status == "CURRENT"


# ---------------------------------------------------------------------------
# validate_evidence — MISSING placeholders
# ---------------------------------------------------------------------------

class TestValidateEvidenceMissingPlaceholders:
    def test_missing_field_has_zero_value_and_empty_display(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        for f in result.facts:
            if f.freshness_status == "MISSING":
                assert f.value is None
                assert f.display_value == ""
                assert f.quality_score == pytest.approx(0.0)
                assert f.confidence == "LOW"

    def test_expected_fields_covered_by_expected_fields_constant(self):
        assert len(EXPECTED_FIELDS) == 11

    def test_all_eleven_fields_present_when_all_missing(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        missing_field_names = {f.field_name for f in result.facts if f.freshness_status == "MISSING"}
        assert missing_field_names == EXPECTED_FIELDS


# ---------------------------------------------------------------------------
# validate_evidence — signal bundle
# ---------------------------------------------------------------------------

class TestValidateEvidenceSignals:
    def test_signal_facts_extracted(self):
        signals = _make_signal_bundle(
            {"field": "primary_owner", "value": "bob@example.com", "source": "email_signal"},
        )
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=signals,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        signal_facts = [f for f in result.facts if f.extraction_type == "SIGNAL"]
        assert len(signal_facts) == 1
        assert signal_facts[0].field_name == "primary_owner"
        assert signal_facts[0].value == "bob@example.com"
        assert signal_facts[0].trust_level == "USER_SUBMITTED"

    def test_signal_with_none_value_skipped(self):
        signals = _make_signal_bundle(
            {"field": "primary_owner", "value": None},
        )
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=signals,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        signal_facts = [f for f in result.facts if f.extraction_type == "SIGNAL"]
        assert len(signal_facts) == 0

    def test_signal_quality_score(self):
        signals = _make_signal_bundle(
            {"field": "primary_owner", "value": "carol@example.com", "source": "checkin"},
        )
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=signals,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        fact = next(f for f in result.facts if f.extraction_type == "SIGNAL")
        # USER_SUBMITTED + CURRENT + no conflict = 0.65
        assert fact.quality_score == pytest.approx(0.65)


# ---------------------------------------------------------------------------
# validate_evidence — structured bundle spend aggregation
# ---------------------------------------------------------------------------

class TestValidateEvidenceStructuredBundle:
    def test_spend_total_computed(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=_make_structured_bundle(amount_usd=75_000.0),
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        spend_fact = next(f for f in result.facts if f.field_name == "spend_total_ttm_usd")
        assert spend_fact.value == pytest.approx(75_000.0)

    def test_invoice_count_and_po_count_included(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=_make_structured_bundle(),
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        field_names = {f.field_name for f in result.facts}
        assert "invoice_count" in field_names
        assert "po_count" in field_names

    def test_empty_records_returns_zero_spend(self):
        bundle = StructuredDataBundle(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            collected_at="2024-06-01T00:00:00+00:00",
            arrival_modes_used=[],
            raw_spend_records=[],
            connector_metadata={},
            upload_metadata={},
            checkin_metadata={},
            collection_warnings=[],
        )
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=bundle,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        spend_facts = [f for f in result.facts if f.field_name == "spend_total_ttm_usd"]
        # Empty records → no spend facts (helper returns [] early)
        assert all(f.freshness_status == "MISSING" or f.value == 0.0 for f in spend_facts)


# ---------------------------------------------------------------------------
# validate_evidence — vendor_file facts
# ---------------------------------------------------------------------------

class TestValidateEvidenceVendorFile:
    def test_vendor_file_relationship_type(self):
        vf = _vendor_file(relationship_type="STRATEGIC", primary_owner="alice@example.com")
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        rt_fact = next(f for f in result.facts if f.field_name == "relationship_type" and f.source_file == "vendor_file")
        assert rt_fact.value == "STRATEGIC"
        assert rt_fact.trust_level == "SYSTEM_EXPORT"

    def test_vendor_file_none_values_skipped(self):
        vf = _vendor_file(relationship_type=None)
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        rt_facts = [f for f in result.facts if f.field_name == "relationship_type" and f.source_file == "vendor_file"]
        assert len(rt_facts) == 0


# ---------------------------------------------------------------------------
# validate_evidence — assembly metadata
# ---------------------------------------------------------------------------

class TestValidateEvidenceAssemblyMetadata:
    def test_vendor_id_and_programme_id_propagated(self):
        result = validate_evidence(
            vendor_id="v-abc",
            programme_id="p-xyz",
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        assert result.vendor_id == "v-abc"
        assert result.programme_id == "p-xyz"

    def test_validated_at_is_set(self):
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=None,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=_empty_vendor_file(),
            historical_state=None,
        )
        assert result.validated_at
        assert "T" in result.validated_at  # ISO format


# ---------------------------------------------------------------------------
# Round-trip serialisation
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_assembly_round_trip(self):
        vf = _vendor_file(
            relationship_type="STRATEGIC",
            dependency_tier="HIGH",
            primary_owner="alice@example.com",
            renewal_date="2025-06-01",
            auto_renew=True,
        )
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=_make_doc_intelligence(),
            structured_bundle=_make_structured_bundle(),
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        as_dict = result.to_dict()
        restored = ValidatedEvidenceAssembly.from_dict(as_dict)

        assert restored.vendor_id == result.vendor_id
        assert restored.programme_id == result.programme_id
        assert restored.completeness_pct == pytest.approx(result.completeness_pct)
        assert restored.conflict_count == result.conflict_count
        assert restored.stale_count == result.stale_count
        assert restored.missing_count == result.missing_count
        assert len(restored.facts) == len(result.facts)

    def test_fact_round_trip_preserves_conflict_values(self):
        doc = _make_doc_intelligence(auto_renews=True)
        vf = _vendor_file(auto_renew=False)
        result = validate_evidence(
            vendor_id=VENDOR_ID,
            programme_id=PROG_ID,
            doc_intelligence=doc,
            structured_bundle=None,
            signal_bundle=None,
            vendor_file=vf,
            historical_state=None,
        )
        as_dict = result.to_dict()
        restored = ValidatedEvidenceAssembly.from_dict(as_dict)
        conflicted_original = [f for f in result.facts if f.conflict_flag]
        conflicted_restored = [f for f in restored.facts if f.conflict_flag]
        assert len(conflicted_original) == len(conflicted_restored)
        for orig, rest in zip(conflicted_original, conflicted_restored):
            assert orig.field_name == rest.field_name
            assert orig.quality_score == pytest.approx(rest.quality_score)
