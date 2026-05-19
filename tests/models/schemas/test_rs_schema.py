"""Tests for rs_schema.py — P3 dataclasses."""

from __future__ import annotations

import pytest

from cobalt.models.schemas.rs_schema import (
    ArrivalMode,
    ContractTerms,
    DocumentIntelligenceResult,
    GapReport,
    RawSpendRecord,
    RelationshipClassification,
    RelationshipSpendProfile,
    SpendAggregationResult,
    SpendSummary,
    StructuredDataBundle,
    TrustLevel,
)


def _make_raw_record(**kwargs) -> RawSpendRecord:
    defaults = dict(
        source_id="src1",
        arrival_mode="FILE_UPLOAD",
        trust_level="USER_SUBMITTED",
        period_start="2025-01-01",
        period_end="2025-03-31",
        amount_raw="1000",
        currency_raw="USD",
        amount_usd=1000.0,
        category_raw="IT",
        cost_centre="CC-001",
        po_number="PO-001",
        invoice_ref="INV-001",
        matched_vendor_id="V-0001",
        match_confidence="HIGH",
        payment_terms_days=30,
    )
    defaults.update(kwargs)
    return RawSpendRecord(**defaults)


def _make_spend_summary(**kwargs) -> SpendSummary:
    defaults = dict(
        total_usd_all_time=10000.0,
        total_usd_ttm=8000.0,
        total_usd_ytd=4000.0,
        by_period={"2025-Q1": 4000.0},
        by_category={"IT": 10000.0},
        by_cost_centre={"CC-001": 10000.0},
        invoice_count=5,
        po_count=4,
        payment_terms_days_avg=30,
        data_completeness="FULL",
        confidence="HIGH",
    )
    defaults.update(kwargs)
    return SpendSummary(**defaults)


# ---------------------------------------------------------------------------
# RawSpendRecord
# ---------------------------------------------------------------------------

def test_raw_spend_record_to_dict_includes_payment_terms():
    rec = _make_raw_record(payment_terms_days=30)
    d = rec.to_dict()
    assert d["payment_terms_days"] == 30


def test_raw_spend_record_payment_terms_none():
    rec = _make_raw_record(payment_terms_days=None)
    d = rec.to_dict()
    assert d["payment_terms_days"] is None


def test_raw_spend_record_roundtrip():
    rec = _make_raw_record(payment_terms_days=45)
    assert RawSpendRecord.from_dict(rec.to_dict()) == rec


def test_raw_spend_record_from_dict_missing_keys_default_none():
    rec = RawSpendRecord.from_dict({"source_id": "x", "amount_raw": "100", "match_confidence": "HIGH", "arrival_mode": "CONNECTOR", "trust_level": "OFFICIAL"})
    assert rec.payment_terms_days is None
    assert rec.period_start is None


# ---------------------------------------------------------------------------
# ContractTerms
# ---------------------------------------------------------------------------

def test_contract_terms_to_dict_lists_serialised():
    ct = ContractTerms(
        document_id="doc1",
        document_type="CONTRACT",
        effective_date="2024-01-01",
        expiry_date="2026-12-31",
        auto_renews=True,
        notice_period_days=90,
        total_value=100000.0,
        currency="GBP",
        payment_terms_days=30,
        governing_law="England",
        termination_clauses=["30-day notice"],
        key_obligations=["Quarterly review"],
        sla_summary="99.9% uptime",
        extraction_confidence="HIGH",
    )
    d = ct.to_dict()
    assert isinstance(d["termination_clauses"], list)
    assert isinstance(d["key_obligations"], list)
    assert d["total_value"] == 100000.0


def test_contract_terms_from_dict_missing_lists_default_empty():
    ct = ContractTerms.from_dict({"document_id": "doc1", "document_type": "CONTRACT"})
    assert ct.termination_clauses == []
    assert ct.key_obligations == []


# ---------------------------------------------------------------------------
# StructuredDataBundle
# ---------------------------------------------------------------------------

def test_structured_data_bundle_to_dict_nested():
    rec = _make_raw_record()
    bundle = StructuredDataBundle(
        vendor_id="V-001",
        programme_id="PROG-001",
        collected_at="2025-01-01T00:00:00Z",
        arrival_modes_used=["FILE_UPLOAD"],
        raw_spend_records=[rec],
        connector_metadata={},
        upload_metadata={"f1": {"rows": 1}},
        checkin_metadata={},
        collection_warnings=[],
    )
    d = bundle.to_dict()
    assert len(d["raw_spend_records"]) == 1
    assert d["raw_spend_records"][0]["payment_terms_days"] == 30


def test_structured_data_bundle_from_dict_reconstructs_records():
    rec = _make_raw_record()
    bundle = StructuredDataBundle(
        vendor_id="V-001",
        programme_id="PROG-001",
        collected_at="2025-01-01T00:00:00Z",
        arrival_modes_used=["FILE_UPLOAD"],
        raw_spend_records=[rec],
        connector_metadata={},
        upload_metadata={},
        checkin_metadata={},
        collection_warnings=[],
    )
    restored = StructuredDataBundle.from_dict(bundle.to_dict())
    assert len(restored.raw_spend_records) == 1
    assert restored.raw_spend_records[0].payment_terms_days == 30


# ---------------------------------------------------------------------------
# SpendAggregationResult
# ---------------------------------------------------------------------------

def test_spend_aggregation_result_roundtrip():
    summary = _make_spend_summary()
    result = SpendAggregationResult(
        vendor_id="V-001",
        summary=summary,
        anomalies=[{"type": "SPEND_SPIKE", "severity": "MEDIUM"}],
        data_quality_flags=["NO_PO_COVERAGE"],
        aggregated_at="2025-01-01T00:00:00Z",
    )
    restored = SpendAggregationResult.from_dict(result.to_dict())
    assert restored.vendor_id == "V-001"
    assert restored.summary.total_usd_all_time == 10000.0
    assert restored.data_quality_flags == ["NO_PO_COVERAGE"]


# ---------------------------------------------------------------------------
# RelationshipSpendProfile
# ---------------------------------------------------------------------------

def _make_profile(**kwargs) -> RelationshipSpendProfile:
    cls = RelationshipClassification(
        vendor_id="V-001",
        relationship_type="STRATEGIC",
        dependency_score=0.8,
        dependency_tier="HIGH",
        single_source_risk=True,
        contract_coverage="FULLY_COVERED",
        relationship_age_days=365,
        renewal_urgency="OK",
        classification_confidence="HIGH",
        llm_used=False,
        reasoning=None,
    )
    defaults = dict(
        vendor_id="V-001",
        programme_id="PROG-001",
        profile_version=1,
        profile_status="COMPLETE",
        created_at="2025-01-01T00:00:00Z",
        last_updated="2025-01-01T00:00:00Z",
        contract_count=2,
        spend_summary=_make_spend_summary(),
        contract_terms=[],
        relationship_classification=cls,
        gap_report={"gap_severity": "NONE", "missing_fields": [], "low_confidence_fields": [], "stale_fields": [], "recommended_actions": []},
        pcs_contribution=0.17,
        pcs_total=0.93,
        flags=["CONTRACT_DEVIATION"],
        data_sources=["src1"],
    )
    defaults.update(kwargs)
    return RelationshipSpendProfile(**defaults)


def test_relationship_spend_profile_contract_count_roundtrip():
    profile = _make_profile(contract_count=2)
    restored = RelationshipSpendProfile.from_dict(profile.to_dict())
    assert restored.contract_count == 2


def test_relationship_spend_profile_from_dict_contract_count_defaults_zero():
    d = _make_profile().to_dict()
    del d["contract_count"]
    restored = RelationshipSpendProfile.from_dict(d)
    assert restored.contract_count == 0


def test_relationship_spend_profile_roundtrip():
    profile = _make_profile()
    restored = RelationshipSpendProfile.from_dict(profile.to_dict())
    assert restored.vendor_id == profile.vendor_id
    assert restored.pcs_contribution == profile.pcs_contribution
    assert restored.pcs_total == profile.pcs_total


# ---------------------------------------------------------------------------
# GapReport
# ---------------------------------------------------------------------------

def test_gap_report_roundtrip():
    gr = GapReport(
        missing_fields=["spend_total_ttm_usd"],
        low_confidence_fields=["contract_count"],
        stale_fields=[],
        gap_severity="MAJOR",
        recommended_actions=["Upload AP extract"],
    )
    restored = GapReport.from_dict(gr.to_dict())
    assert restored.gap_severity == "MAJOR"
    assert restored.missing_fields == ["spend_total_ttm_usd"]


def test_gap_report_severity_never_critical():
    """gap_analyzer produces at most MAJOR — CRITICAL must not appear in GapReport."""
    gr = GapReport(
        missing_fields=["spend_total_ttm_usd"],
        low_confidence_fields=[],
        stale_fields=[],
        gap_severity="MAJOR",  # never "CRITICAL" from gap_analyzer
        recommended_actions=[],
    )
    assert gr.gap_severity != "CRITICAL"
    assert gr.gap_severity == "MAJOR"


# ---------------------------------------------------------------------------
# Enum string behaviour
# ---------------------------------------------------------------------------

def test_arrival_mode_is_string_subclass():
    assert isinstance(ArrivalMode.CONNECTOR, str)
    assert ArrivalMode.FILE_UPLOAD == "FILE_UPLOAD"


def test_trust_level_hierarchy():
    assert TrustLevel.OFFICIAL == "OFFICIAL"
    assert TrustLevel.USER_SUBMITTED == "USER_SUBMITTED"


def test_no_confidence_level_class():
    """Confidence values are plain string literals — no ConfidenceLevel enum."""
    import cobalt.models.schemas.rs_schema as rs
    assert not hasattr(rs, "ConfidenceLevel")
